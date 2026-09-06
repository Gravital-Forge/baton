"""A thin adapter over the `tmux` CLI, used to host and probe worker panes."""

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class TmuxError(RuntimeError):
    """Raised when a `tmux` command that must succeed exits non-zero."""


@dataclass(frozen=True)
class PaneInfo:
    """The liveness and process ID of a tmux pane.

    Attributes:
        dead: Whether the pane has no running process, or does not exist.
        pid: The pane's process ID, or `None` when it cannot be determined.
    """

    dead: bool
    pid: int | None


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and capture its output, without raising on failure.

    Args:
        argv: The argument list to run. Never a shell string.

    Returns:
        The completed process, including its exit code and captured
        output.
    """
    # argv is built from a configured absolute path and fixed flags, never
    # from a shell string.
    return subprocess.run(  # noqa: S603
        list(argv), text=True, capture_output=True, check=False
    )


def _exact_target(target: str) -> str:
    """Prefix a tmux target so its session name is matched exactly.

    Args:
        target: A tmux target, such as `"baton-app"` or
            `"baton-app:worker.0"`.

    Returns:
        The target with a leading `=`. tmux otherwise accepts a name
        prefix or an fnmatch pattern, so `baton-app` would also resolve to
        a leftover `baton-app-api` session — and `respawn-pane -k` would
        kill that project's pane.
    """
    return target if target.startswith("=") else f"={target}"


def _parse_pane_info(stdout: str) -> PaneInfo:
    """Parse `display-message`'s `#{pane_dead} #{pane_pid}` output.

    Args:
        stdout: The captured standard output of the `display-message` call.

    Returns:
        A `PaneInfo` built from the first two whitespace-separated fields.
        Empty output tells us nothing about the pane, so it is treated as
        dead rather than assumed alive. A pid is parsed only when the
        second field is present and made up entirely of digits.
    """
    fields = stdout.split()
    if not fields:
        return PaneInfo(dead=True, pid=None)
    dead = fields[0] == "1"
    pid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
    return PaneInfo(dead=dead, pid=pid)


class TmuxAdapter:
    """Runs `tmux` commands to manage and inspect a worker's pane."""

    def __init__(self, tmux_bin: Path, runner: Runner | None = None) -> None:
        """Set up the adapter.

        Args:
            tmux_bin: The absolute path to the `tmux` binary.
            runner: The callable used to run each command. Defaults to a
                wrapper around `subprocess.run`.
        """
        self._tmux = str(tmux_bin)
        self._runner = runner if runner is not None else _run

    def has_session(self, session: str) -> bool:
        """Report whether a tmux session with the given name exists.

        Args:
            session: The session name to look up.

        Returns:
            `True` when `has-session` exits zero, `False` otherwise. A
            non-zero exit here means the session is absent, not an error.
        """
        result = self._runner([self._tmux, "has-session", "-t", _exact_target(session)])
        return result.returncode == 0

    def create_session(self, session: str, start_dir: Path) -> None:
        """Create a detached session with a `worker` window.

        Args:
            session: The name to give the new session.
            start_dir: The directory the `worker` window starts in.

        Raises:
            TmuxError: If either the `new-session` or the `set-option` call
                exits non-zero.
        """
        self._run_checked(
            [
                self._tmux,
                "new-session",
                "-d",
                "-s",
                session,
                "-n",
                "worker",
                "-c",
                str(start_dir),
            ]
        )
        self._run_checked(
            [
                self._tmux,
                "set-option",
                "-w",
                "-t",
                _exact_target(f"{session}:worker"),
                "remain-on-exit",
                "on",
            ]
        )

    def kill_session(self, session: str) -> None:
        """Kill a session and every pane in it.

        Args:
            session: The name of the session to kill.

        Raises:
            TmuxError: If the `kill-session` call exits non-zero, which
                includes the session already being absent. Callers guard
                with `has_session`.
        """
        self._run_checked([self._tmux, "kill-session", "-t", _exact_target(session)])

    def respawn_pane(
        self,
        target: str,
        start_dir: Path,
        env: Mapping[str, str],
        command: str,
    ) -> None:
        """Replace a pane's process with a freshly started command.

        Args:
            target: The pane to respawn, e.g. `"<session>:worker"`.
            start_dir: The directory the respawned pane starts in.
            env: Environment variables to set on the respawned pane, passed
                in the mapping's iteration order.
            command: The command line the respawned pane runs.

        Raises:
            TmuxError: If the `respawn-pane` call exits non-zero.
        """
        argv = [
            self._tmux,
            "respawn-pane",
            "-k",
            "-t",
            _exact_target(target),
            "-c",
            str(start_dir),
        ]
        for key, value in env.items():
            argv.extend(["-e", f"{key}={value}"])
        argv.append(command)
        self._run_checked(argv)

    def send_keys(self, target: str, text: str) -> None:
        """Type one line of text into a pane, then press Enter.

        Args:
            target: The pane to type into, e.g. `"<session>:worker"`.
            text: The text to type. Sent literally, with `-l`, so tmux
                performs no key-name lookup on it.

        Raises:
            TmuxError: If either the literal send or the Enter key press
                exits non-zero.
        """
        exact = _exact_target(target)
        self._run_checked([self._tmux, "send-keys", "-l", "-t", exact, text])
        self._run_checked([self._tmux, "send-keys", "-t", exact, "Enter"])

    def pane_info(self, target: str) -> PaneInfo:
        """Read a pane's liveness and process ID.

        Args:
            target: The pane to query, e.g. `"<session>:worker"`.

        Returns:
            The pane's `PaneInfo`. Any non-zero exit is reported as
            `PaneInfo(dead=True, pid=None)` rather than raised, so a
            missing target reads as a dead pane — and so does a tmux
            server that is not running.
        """
        result = self._runner(
            [
                self._tmux,
                "display-message",
                "-p",
                "-t",
                _exact_target(target),
                "#{pane_dead} #{pane_pid}",
            ]
        )
        if result.returncode != 0:
            return PaneInfo(dead=True, pid=None)
        return _parse_pane_info(result.stdout)

    def signal_pane(self, pid: int, signum: int) -> None:
        """Send a signal directly to a pane's process.

        Args:
            pid: The process ID to signal.
            signum: The signal number to send.

        Raises:
            ProcessLookupError: If the process is already gone, which is
                ordinary when a worker exits between a poll and a signal.
            PermissionError: If the process belongs to another user.
        """
        os.kill(pid, signum)

    def _run_checked(self, argv: Sequence[str]) -> None:
        """Run a command and raise TmuxError if it exits non-zero.

        Args:
            argv: The argument list to run.

        Raises:
            TmuxError: If the command's exit code is non-zero.
        """
        result = self._runner(argv)
        if result.returncode != 0:
            raise TmuxError(f"{list(argv)} failed: {result.stderr}")
