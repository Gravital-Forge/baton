"""Launches Claude Code workers into a tmux pane and writes their files."""

import json
import shlex
import uuid
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from baton.config import BatonConfig
from baton.models import WorkerRecord
from baton.prompts import WORKER_PREAMBLE
from baton.state import project_state_dir
from baton.tmux import TmuxAdapter

_LAUNCH_SCRIPT_TEMPLATE = (
    "#!/usr/bin/env bash\n"
    "exec {claude} \\\n"
    "  --session-id {worker_id} \\\n"
    "  --model {model} \\\n"
    "  --mcp-config {mcp_config} \\\n"
    "  --append-system-prompt {preamble} \\\n"
    "  -- \\\n"
    '  "$(cat {prompt_path})"\n'
)


def _mcp_config_path(config: BatonConfig) -> Path:
    """Return the MCP config path inside a configuration's state directory.

    Args:
        config: The configuration naming the state directory.

    Returns:
        The `mcp.json` path every worker's launch script points at.
    """
    return config.state_dir / "mcp.json"


def write_mcp_config(config: BatonConfig) -> Path:
    """Write the shared MCP config every worker's launch script points at.

    The daemon writes it at startup, so an operator can point a Claude Code
    session at baton before any project exists, and `Supervisor.initialize`
    writes it again at every project start, so an address changed between
    runs still reaches a worker.

    Args:
        config: The configuration naming the address to publish and the
            directory to publish it in.

    Returns:
        The path the config was written to.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    mcp_config_path = _mcp_config_path(config)
    url = f"http://{config.host}:{config.port}/sse"
    client_config = {"mcpServers": {"baton": {"type": "sse", "url": url}}}
    mcp_config_path.write_text(
        json.dumps(client_config, indent=2) + "\n", encoding="utf-8"
    )
    return mcp_config_path


class WorkerLauncher:
    """Launches Claude Code workers and writes the files a launch needs."""

    def __init__(self, config: BatonConfig, tmux: TmuxAdapter) -> None:
        """Bind the launcher to its config and tmux adapter.

        Args:
            config: The runtime configuration to launch workers with.
            tmux: The adapter used to respawn and inspect the worker pane.
        """
        self._config = config
        self._tmux = tmux

    def launch(
        self,
        project_id: str,
        project_path: Path,
        pane_target: str,
        prompt: str,
        model: str,
    ) -> WorkerRecord:
        """Launch a worker into the given pane and return its record.

        Args:
            project_id: The id of the project the worker belongs to. It
                names the state directory the worker's files are written
                under, and reaches the worker as ``BATON_PROJECT``.
            project_path: The directory the worker's pane starts in.
            pane_target: The tmux pane to respawn, e.g. `"<session>:worker"`.
            prompt: The task prompt to give the worker.
            model: The model name passed to the worker's `--model` flag.

        Returns:
            The `WorkerRecord` describing the launched worker.
        """
        worker_id = str(uuid.uuid4())
        worker_dir = (
            project_state_dir(self._config.state_dir, project_id)
            / "workers"
            / worker_id
        )
        worker_dir.mkdir(parents=True, exist_ok=True)

        prompt_path = worker_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        mcp_config_path = _mcp_config_path(self._config)

        launch_path = worker_dir / "launch.sh"
        launch_path.write_text(
            _LAUNCH_SCRIPT_TEMPLATE.format(
                claude=shlex.quote(str(self._config.claude_bin)),
                worker_id=shlex.quote(worker_id),
                model=shlex.quote(model),
                mcp_config=shlex.quote(str(mcp_config_path)),
                preamble=shlex.quote(WORKER_PREAMBLE),
                prompt_path=shlex.quote(str(prompt_path)),
            ),
            encoding="utf-8",
        )
        launch_path.chmod(0o755)

        self._tmux.respawn_pane(
            target=pane_target,
            start_dir=project_path,
            env={"BATON_WORKER_ID": worker_id, "BATON_PROJECT": project_id},
            command=shlex.quote(str(launch_path)),
        )
        pane_info = self._tmux.pane_info(pane_target)

        return WorkerRecord(
            worker_id=worker_id,
            prompt_path=prompt_path,
            launched_at=datetime.now(UTC),
            pane_pid=pane_info.pid,
        )

    def install_skill(self, project_path: Path) -> Path:
        """Install baton's worker-protocol skill into a project.

        Overwrites any file already at the destination, so the installed
        skill cannot drift from the one the daemon ships.

        Args:
            project_path: The project directory to install the skill into.

        Returns:
            The path the skill was written to.
        """
        skill_text = (
            resources.files("baton")
            .joinpath("skill", "SKILL.md")
            .read_text(encoding="utf-8")
        )
        skill_dir = project_path / ".claude" / "skills" / "baton-worker"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_path = skill_dir / "SKILL.md"
        skill_path.write_text(skill_text, encoding="utf-8")
        return skill_path
