"""Tests for baton.worker."""

import json
import shlex
import uuid
from collections.abc import Mapping
from datetime import timedelta
from importlib import resources
from pathlib import Path

import pytest

from baton.config import BatonConfig
from baton.models import WorkerRecord
from baton.prompts import WORKER_PREAMBLE
from baton.tmux import PaneInfo
from baton.worker import WorkerLauncher


class FakeTmux:
    """A stand-in for `TmuxAdapter` that records calls and runs no command.

    `respawn_pane` calls are recorded as dicts of their arguments.
    `pane_info` calls are recorded as the target queried, and every call
    returns the `PaneInfo` given to the constructor.
    """

    def __init__(self, pane_info: PaneInfo) -> None:
        """Store the `PaneInfo` every `pane_info` call should return.

        Args:
            pane_info: The value `pane_info` returns on every call.
        """
        self._pane_info = pane_info
        self.respawn_calls: list[dict[str, object]] = []
        self.pane_info_calls: list[str] = []

    def respawn_pane(
        self, target: str, start_dir: Path, env: Mapping[str, str], command: str
    ) -> None:
        """Record a `respawn_pane` call instead of running `tmux`.

        Args:
            target: The pane target that was respawned.
            start_dir: The start directory that was passed.
            env: The environment mapping that was passed.
            command: The command string that was passed.
        """
        self.respawn_calls.append(
            {
                "target": target,
                "start_dir": start_dir,
                "env": dict(env),
                "command": command,
            }
        )

    def pane_info(self, target: str) -> PaneInfo:
        """Record the queried target and return the scripted `PaneInfo`.

        Args:
            target: The pane target that was queried.

        Returns:
            The `PaneInfo` given to the constructor.
        """
        self.pane_info_calls.append(target)
        return self._pane_info


def _build_config(
    tmp_path: Path,
    *,
    state_dir: Path | None = None,
    port: int = 8910,
    claude_bin: Path = Path("/opt/claude/bin/claude"),
) -> BatonConfig:
    """Build a `BatonConfig` for tests, without touching a real binary.

    Args:
        tmp_path: Pytest's per-test temporary directory, used as the
            default parent for `state_dir`.
        state_dir: The state directory to use. Defaults to
            `tmp_path / "state"`.
        port: The port to configure. Defaults to `8910`.
        claude_bin: The claude binary path to configure.

    Returns:
        A `BatonConfig` usable without a real `claude` or `tmux` binary.
    """
    return BatonConfig(
        host="127.0.0.1",
        port=port,
        state_dir=state_dir if state_dir is not None else tmp_path / "state",
        claude_bin=claude_bin,
        tmux_bin=Path("/usr/bin/tmux"),
        grace_period=20,
        termination_timeout=5,
        poll_interval=2,
    )


def _packaged_skill_text() -> str:
    """Read the skill file baton ships, the way baton reads it at runtime.

    Returns:
        The packaged SKILL.md text.
    """
    return (
        resources.files("baton")
        .joinpath("skill", "SKILL.md")
        .read_text(encoding="utf-8")
    )


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


@pytest.fixture
def config(tmp_path: Path) -> BatonConfig:
    """Build a `BatonConfig` pointed at a fresh state directory.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        A `BatonConfig` usable without touching any real binary or session.
    """
    return _build_config(tmp_path)


@pytest.fixture
def project_path(tmp_path: Path) -> Path:
    """Build a project directory distinct from the state directory.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        A project directory path, used as `launch`'s start directory.
    """
    path = tmp_path / "project"
    path.mkdir()
    return path


def test_launch_writes_the_prompt_text_to_prompt_md(
    config: BatonConfig, project_path: Path
) -> None:
    """Launch writes the given prompt text to prompt.md under the worker dir."""
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_path, "baton-1:worker", "do the thing")

    assert record.prompt_path.read_text(encoding="utf-8") == "do the thing"


def test_launch_script_matches_the_pinned_template_line_by_line(
    config: BatonConfig, project_path: Path
) -> None:
    """launch.sh matches the pinned template exactly, line by line."""
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_path, "baton-1:worker", "do the thing")

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
    config: BatonConfig, project_path: Path
) -> None:
    """launch.sh is written with executable mode 0o755."""
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_path, "baton-1:worker", "do the thing")

    launch_script = config.state_dir / "workers" / record.worker_id / "launch.sh"
    assert launch_script.stat().st_mode & 0o777 == 0o755


def test_launch_script_quotes_every_path_containing_a_space(
    tmp_path: Path, project_path: Path
) -> None:
    """Paths with a space are shell-quoted wherever launch.sh names them."""
    state_dir = tmp_path / "state dir"
    claude_bin = tmp_path / "claude code" / "claude"
    config = _build_config(tmp_path, state_dir=state_dir, claude_bin=claude_bin)
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_path, "baton-1:worker", "do the thing")

    worker_dir = state_dir / "workers" / record.worker_id
    script_text = (worker_dir / "launch.sh").read_text(encoding="utf-8")
    assert shlex.quote(str(claude_bin)) in script_text
    assert shlex.quote(str(state_dir / "mcp.json")) in script_text
    assert shlex.quote(str(worker_dir / "prompt.md")) in script_text


def test_launch_script_uses_the_shell_quoted_worker_preamble(
    config: BatonConfig, project_path: Path
) -> None:
    """--append-system-prompt carries the shell-quoted WORKER_PREAMBLE."""
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_path, "baton-1:worker", "do the thing")

    worker_dir = config.state_dir / "workers" / record.worker_id
    script_text = (worker_dir / "launch.sh").read_text(encoding="utf-8")
    assert shlex.quote(WORKER_PREAMBLE) in script_text
    assert "You are a baton worker." in script_text


def test_launch_respawns_the_pane_with_env_and_quoted_command(
    config: BatonConfig, project_path: Path
) -> None:
    """Launch respawns the target pane with the launch script env and command."""
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_path, "baton-1:worker", "do the thing")

    launch_script = config.state_dir / "workers" / record.worker_id / "launch.sh"
    assert tmux.respawn_calls == [
        {
            "target": "baton-1:worker",
            "start_dir": project_path,
            "env": {"BATON_WORKER_ID": record.worker_id},
            "command": shlex.quote(str(launch_script)),
        }
    ]


def test_launch_returns_a_worker_record_with_the_expected_shape(
    config: BatonConfig, project_path: Path
) -> None:
    """Launch returns a WorkerRecord with a UUID id, prompt path, UTC time, pid."""
    tmux = FakeTmux(PaneInfo(dead=False, pid=4242))
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_path, "baton-1:worker", "do the thing")

    assert isinstance(record, WorkerRecord)
    assert uuid.UUID(record.worker_id).version == 4
    assert record.prompt_path == (
        config.state_dir / "workers" / record.worker_id / "prompt.md"
    )
    assert record.launched_at.tzinfo is not None
    assert record.launched_at.utcoffset() == timedelta(0)
    assert record.pane_pid == 4242


def test_launch_records_pane_pid_none_when_pane_has_no_pid(
    config: BatonConfig, project_path: Path
) -> None:
    """Launch stores pane_pid=None when pane_info reports no pid."""
    tmux = FakeTmux(PaneInfo(dead=True, pid=None))
    launcher = WorkerLauncher(config, tmux)

    record = launcher.launch(project_path, "baton-1:worker", "do the thing")

    assert record.pane_pid is None


def test_launch_twice_mints_different_worker_ids_and_directories(
    config: BatonConfig, project_path: Path
) -> None:
    """Two launches produce distinct worker ids and distinct worker directories."""
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    first = launcher.launch(project_path, "baton-1:worker", "task one")
    second = launcher.launch(project_path, "baton-1:worker", "task two")

    assert first.worker_id != second.worker_id
    assert first.prompt_path.parent != second.prompt_path.parent


def test_launch_writes_mcp_config_when_absent(
    config: BatonConfig, project_path: Path
) -> None:
    """Launch writes mcp.json when it does not already exist."""
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    launcher.launch(project_path, "baton-1:worker", "do the thing")

    mcp_config_path = config.state_dir / "mcp.json"
    assert json.loads(mcp_config_path.read_text(encoding="utf-8")) == {
        "mcpServers": {"baton": {"type": "sse", "url": "http://127.0.0.1:8910/sse"}}
    }


def test_launch_leaves_an_existing_mcp_config_untouched(
    config: BatonConfig, project_path: Path
) -> None:
    """A pre-existing mcp.json is left exactly as it is."""
    config.state_dir.mkdir(parents=True)
    mcp_config_path = config.state_dir / "mcp.json"
    sentinel = "not what write_mcp_config would produce"
    mcp_config_path.write_text(sentinel, encoding="utf-8")
    tmux = FakeTmux(PaneInfo(dead=False, pid=111))
    launcher = WorkerLauncher(config, tmux)

    launcher.launch(project_path, "baton-1:worker", "do the thing")

    assert mcp_config_path.read_text(encoding="utf-8") == sentinel


def test_write_mcp_config_returns_the_mcp_json_path(config: BatonConfig) -> None:
    """write_mcp_config returns <state_dir>/mcp.json."""
    launcher = WorkerLauncher(config, FakeTmux(PaneInfo(dead=False, pid=None)))

    result = launcher.write_mcp_config()

    assert result == config.state_dir / "mcp.json"


def test_write_mcp_config_writes_the_pinned_json_structure(
    config: BatonConfig,
) -> None:
    """write_mcp_config writes the pinned mcpServers structure, indented."""
    launcher = WorkerLauncher(config, FakeTmux(PaneInfo(dead=False, pid=None)))

    result = launcher.write_mcp_config()

    expected = {
        "mcpServers": {"baton": {"type": "sse", "url": "http://127.0.0.1:8910/sse"}}
    }
    assert result.read_text(encoding="utf-8") == json.dumps(expected, indent=2) + "\n"


def test_write_mcp_config_builds_the_url_from_the_configured_port(
    tmp_path: Path,
) -> None:
    """The mcp.json URL uses the config's own port, proving it is not hardcoded."""
    config = _build_config(tmp_path, port=9191)
    launcher = WorkerLauncher(config, FakeTmux(PaneInfo(dead=False, pid=None)))

    result = launcher.write_mcp_config()

    parsed = json.loads(result.read_text(encoding="utf-8"))
    assert parsed["mcpServers"]["baton"]["url"] == "http://127.0.0.1:9191/sse"


def test_install_skill_writes_the_packaged_skill_text(
    config: BatonConfig, project_path: Path
) -> None:
    """install_skill copies the packaged SKILL.md text into the project."""
    launcher = WorkerLauncher(config, FakeTmux(PaneInfo(dead=False, pid=None)))
    expected_text = _packaged_skill_text()

    result = launcher.install_skill(project_path)

    assert result == (project_path / ".claude" / "skills" / "baton-worker" / "SKILL.md")
    assert result.read_text(encoding="utf-8") == expected_text


def test_install_skill_overwrites_an_existing_file(
    config: BatonConfig, project_path: Path
) -> None:
    """install_skill overwrites whatever was previously written there."""
    launcher = WorkerLauncher(config, FakeTmux(PaneInfo(dead=False, pid=None)))
    skill_path = project_path / ".claude" / "skills" / "baton-worker" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text("stale content", encoding="utf-8")
    expected_text = _packaged_skill_text()

    result = launcher.install_skill(project_path)

    assert result.read_text(encoding="utf-8") == expected_text
