"""Tests for baton.worker."""

import json
import shlex
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from baton.config import BatonConfig
from baton.models import WorkerRecord
from baton.prompts import WORKER_PREAMBLE
from baton.tmux import PaneInfo
from baton.worker import WorkerLauncher, write_mcp_config
from tests.doubles import FakeTmux


def _expected_launch_script(
    *, claude_bin: Path, worker_id: str, mcp_config_path: Path, prompt_path: Path
) -> str:
    """Build the launch.sh text the pinned template should produce.

    Args:
        claude_bin: The claude binary path baton would invoke.
        worker_id: The worker id used as the Claude Code session id.
        mcp_config_path: The shared mcp.json path passed to `--mcp-config`.
        prompt_path: The worker's prompt.md path read into the positional
            argument.

    Returns:
        The exact text `WorkerLauncher.launch` should write to launch.sh.
    """
    return (
        "#!/usr/bin/env bash\n"
        f"exec {shlex.quote(str(claude_bin))} \\\n"
        f"  --session-id {shlex.quote(worker_id)} \\\n"
        f"  --mcp-config {shlex.quote(str(mcp_config_path))} \\\n"
        f"  --append-system-prompt {shlex.quote(WORKER_PREAMBLE)} \\\n"
        "  -- \\\n"
        f'  "$(cat {shlex.quote(str(prompt_path))})"\n'
    )


def test_launch_writes_the_prompt_text_to_prompt_md(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """Launch writes the given prompt text to prompt.md under the worker dir."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=111)])
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_dir, "baton-1:worker", "do the thing")

    assert record.prompt_path.read_text(encoding="utf-8") == "do the thing"


def test_launch_script_matches_the_pinned_template_line_by_line(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """launch.sh matches the pinned template exactly, line by line."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=111)])
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_dir, "baton-1:worker", "do the thing")

    worker_dir = config.state_dir / "workers" / record.worker_id
    expected = _expected_launch_script(
        claude_bin=config.claude_bin,
        worker_id=record.worker_id,
        mcp_config_path=config.state_dir / "mcp.json",
        prompt_path=worker_dir / "prompt.md",
    )
    actual_lines = (worker_dir / "launch.sh").read_text(encoding="utf-8").splitlines()

    assert actual_lines == expected.splitlines()
    assert actual_lines[0] == "#!/usr/bin/env bash"


def test_launch_script_is_written_with_mode_0o755(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """launch.sh is written with executable mode 0o755."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=111)])
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_dir, "baton-1:worker", "do the thing")

    launch_script = config.state_dir / "workers" / record.worker_id / "launch.sh"
    assert launch_script.stat().st_mode & 0o777 == 0o755


def test_launch_script_quotes_every_path_containing_a_space(
    tmp_path: Path, project_dir: Path, config: BatonConfig, make_tmux: type[FakeTmux]
) -> None:
    """Paths with a space are shell-quoted wherever launch.sh names them."""
    state_dir = tmp_path / "state dir"
    claude_bin = tmp_path / "claude code" / "claude"
    config = replace(config, state_dir=state_dir, claude_bin=claude_bin)
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=111)])
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_dir, "baton-1:worker", "do the thing")

    worker_dir = state_dir / "workers" / record.worker_id
    script_text = (worker_dir / "launch.sh").read_text(encoding="utf-8")
    assert shlex.quote(str(claude_bin)) in script_text
    assert shlex.quote(str(state_dir / "mcp.json")) in script_text
    assert shlex.quote(str(worker_dir / "prompt.md")) in script_text


def test_launch_script_uses_the_shell_quoted_worker_preamble(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """--append-system-prompt carries the shell-quoted WORKER_PREAMBLE."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=111)])
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_dir, "baton-1:worker", "do the thing")

    worker_dir = config.state_dir / "workers" / record.worker_id
    script_text = (worker_dir / "launch.sh").read_text(encoding="utf-8")
    assert shlex.quote(WORKER_PREAMBLE) in script_text
    assert "You are a baton worker." in script_text


def test_launch_respawns_the_pane_with_env_and_quoted_command(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """Launch respawns the target pane with the launch script env and command."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=111)])
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_dir, "baton-1:worker", "do the thing")

    launch_script = config.state_dir / "workers" / record.worker_id / "launch.sh"
    assert tmux.respawn_calls == [
        {
            "target": "baton-1:worker",
            "start_dir": project_dir,
            "env": {"BATON_WORKER_ID": record.worker_id},
            "command": shlex.quote(str(launch_script)),
        }
    ]


def test_launch_returns_a_worker_record_with_the_expected_shape(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """Launch returns a WorkerRecord with a UUID id, prompt path, UTC time, pid."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=4242)])
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_dir, "baton-1:worker", "do the thing")

    assert isinstance(record, WorkerRecord)
    assert uuid.UUID(record.worker_id).version == 4
    assert record.prompt_path == (
        config.state_dir / "workers" / record.worker_id / "prompt.md"
    )
    assert record.launched_at.tzinfo is not None
    assert record.launched_at.utcoffset() == timedelta(0)
    assert record.pane_pid == 4242


def test_launch_records_pane_pid_none_when_pane_has_no_pid(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """Launch stores pane_pid=None when pane_info reports no pid."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=True, pid=None)])
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_dir, "baton-1:worker", "do the thing")

    assert record.pane_pid is None


def test_launch_twice_mints_different_worker_ids_and_directories(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """Two launches produce distinct worker ids and distinct worker directories."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=111)])
    launcher = WorkerLauncher(config, tmux)

    first = launcher.launch(project_dir, "baton-1:worker", "task one")
    second = launcher.launch(project_dir, "baton-1:worker", "task two")

    assert first.worker_id != second.worker_id
    assert first.prompt_path.parent != second.prompt_path.parent


def test_launch_does_not_write_the_mcp_config(
    config: BatonConfig, project_dir: Path, make_tmux: type[FakeTmux]
) -> None:
    """Launch names mcp.json in the launch script, leaving writing it to its writer."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=111)])
    launcher = WorkerLauncher(config, tmux)

    launcher.launch(project_dir, "baton-1:worker", "do the thing")

    assert not (config.state_dir / "mcp.json").exists()


def test_write_mcp_config_returns_the_mcp_json_path(config: BatonConfig) -> None:
    """write_mcp_config returns <state_dir>/mcp.json."""
    result = write_mcp_config(config)

    assert result == config.state_dir / "mcp.json"


def test_write_mcp_config_writes_the_pinned_json_structure(
    config: BatonConfig,
) -> None:
    """write_mcp_config writes the pinned mcpServers structure, indented."""
    result = write_mcp_config(config)

    expected = {
        "mcpServers": {"baton": {"type": "sse", "url": "http://127.0.0.1:8910/sse"}}
    }
    assert result.read_text(encoding="utf-8") == json.dumps(expected, indent=2) + "\n"


def test_write_mcp_config_builds_the_url_from_the_configured_port(
    config: BatonConfig,
) -> None:
    """The mcp.json URL uses the config's own port, proving it is not hardcoded."""
    config = replace(config, port=9191)

    result = write_mcp_config(config)

    parsed = json.loads(result.read_text(encoding="utf-8"))
    assert parsed["mcpServers"]["baton"]["url"] == "http://127.0.0.1:9191/sse"


def test_install_skill_writes_the_packaged_skill_text(
    config: BatonConfig, project_dir: Path, skill_text: str, make_tmux: type[FakeTmux]
) -> None:
    """install_skill copies the packaged SKILL.md text into the project."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=None)])
    launcher = WorkerLauncher(config, tmux)

    result = launcher.install_skill(project_dir)

    assert result == (project_dir / ".claude" / "skills" / "baton-worker" / "SKILL.md")
    assert result.read_text(encoding="utf-8") == skill_text


def test_install_skill_overwrites_an_existing_file(
    config: BatonConfig, project_dir: Path, skill_text: str, make_tmux: type[FakeTmux]
) -> None:
    """install_skill overwrites whatever was previously written there."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=None)])
    launcher = WorkerLauncher(config, tmux)
    skill_path = project_dir / ".claude" / "skills" / "baton-worker" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("stale content", encoding="utf-8")

    result = launcher.install_skill(project_dir)

    assert result.read_text(encoding="utf-8") == skill_text
