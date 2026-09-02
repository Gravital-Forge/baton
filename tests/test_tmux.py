"""Tests for baton.tmux."""

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import baton.tmux
from baton.tmux import PaneInfo, TmuxAdapter, TmuxError

TMUX_BIN = Path("/usr/bin/tmux")
TMUX = str(TMUX_BIN)


class FakeRunner:
    """A scripted stand-in for `subprocess.run`, used in place of a real one.

    Each call is recorded as a `list[str]` of the argument list it was
    called with, and answered with the next scripted response.
    """

    def __init__(self, responses: Sequence[subprocess.CompletedProcess[str]]) -> None:
        """Store the scripted responses to hand back in call order.

        Args:
            responses: The `CompletedProcess` values to return, one per
                call, in order. A call past the end of the queue is a test
                that did not script the run it triggered.
        """
        self._responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Record the argument list and return the next scripted response.

        Args:
            argv: The argument list the adapter would have run.

        Returns:
            The next queued `CompletedProcess`.
        """
        self.calls.append(list(argv))
        return self._responses.pop(0)


def _completed(
    argv: Sequence[str], returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a `CompletedProcess` with every field set explicitly.

    Args:
        argv: The argument list to record as `args`.
        returncode: The exit code to report.
        stdout: The captured standard output.
        stderr: The captured standard error.

    Returns:
        A `CompletedProcess` populated from the given fields.
    """
    return subprocess.CompletedProcess(
        args=list(argv), returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_has_session_returns_true_on_zero_exit() -> None:
    """A zero exit from `has-session` reports the session as present."""
    runner = FakeRunner([_completed([], 0)])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    result = adapter.has_session("baton-1")

    assert result is True
    assert runner.calls == [[TMUX, "has-session", "-t", "baton-1"]]


def test_has_session_returns_false_on_nonzero_exit() -> None:
    """A non-zero exit from `has-session` reports the session as absent."""
    runner = FakeRunner([_completed([], 1)])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    result = adapter.has_session("baton-1")

    assert result is False
    assert runner.calls == [[TMUX, "has-session", "-t", "baton-1"]]


def test_create_session_issues_new_session_then_set_option() -> None:
    """Creating a session runs `new-session` then `set-option`, in order."""
    runner = FakeRunner([_completed([], 0), _completed([], 0)])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    adapter.create_session("baton-1", Path("/work/baton-1"))

    assert runner.calls == [
        [
            TMUX,
            "new-session",
            "-d",
            "-s",
            "baton-1",
            "-n",
            "worker",
            "-c",
            "/work/baton-1",
        ],
        [
            TMUX,
            "set-option",
            "-w",
            "-t",
            "baton-1:worker",
            "remain-on-exit",
            "on",
        ],
    ]


def test_create_session_raises_tmux_error_on_nonzero_exit() -> None:
    """A non-zero exit from `new-session` raises TmuxError."""
    runner = FakeRunner([_completed([], 1, stderr="duplicate session")])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    with pytest.raises(TmuxError, match="duplicate session"):
        adapter.create_session("baton-1", Path("/work/baton-1"))


def test_respawn_pane_issues_one_call_with_env_and_command_last() -> None:
    """Respawning a pane passes each env var as `-e KEY=VALUE`, command last."""
    runner = FakeRunner([_completed([], 0)])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    adapter.respawn_pane(
        "baton-1:worker",
        Path("/work/baton-1"),
        {"FOO": "bar", "BAZ": "qux"},
        "/state/workers/w1/launch.sh",
    )

    assert runner.calls == [
        [
            TMUX,
            "respawn-pane",
            "-k",
            "-t",
            "baton-1:worker",
            "-c",
            "/work/baton-1",
            "-e",
            "FOO=bar",
            "-e",
            "BAZ=qux",
            "/state/workers/w1/launch.sh",
        ]
    ]


def test_respawn_pane_raises_tmux_error_on_nonzero_exit() -> None:
    """A non-zero exit from `respawn-pane` raises TmuxError."""
    runner = FakeRunner([_completed([], 1, stderr="no such pane")])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    with pytest.raises(TmuxError, match="no such pane"):
        adapter.respawn_pane("baton-1:worker", Path("/work/baton-1"), {}, "claude")


def test_pane_info_parses_a_live_pane() -> None:
    """A `pane_dead` of 0 parses into `PaneInfo(dead=False, pid=<pid>)`."""
    runner = FakeRunner([_completed([], 0, stdout="0 1234\n")])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    info = adapter.pane_info("baton-1:worker")

    assert info == PaneInfo(dead=False, pid=1234)
    assert runner.calls == [
        [
            TMUX,
            "display-message",
            "-p",
            "-t",
            "baton-1:worker",
            "#{pane_dead} #{pane_pid}",
        ]
    ]


def test_pane_info_parses_a_dead_pane() -> None:
    """A `pane_dead` of 1 parses into `PaneInfo(dead=True, pid=<pid>)`."""
    runner = FakeRunner([_completed([], 0, stdout="1 1234\n")])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    info = adapter.pane_info("baton-1:worker")

    assert info == PaneInfo(dead=True, pid=1234)


def test_pane_info_on_nonzero_exit_is_dead_with_no_pid() -> None:
    """A non-zero exit (no such target) is treated as a dead pane."""
    runner = FakeRunner([_completed([], 1, stderr="can't find pane")])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    info = adapter.pane_info("baton-1:worker")

    assert info == PaneInfo(dead=True, pid=None)


def test_pane_info_on_empty_stdout_is_dead_with_no_pid() -> None:
    """Empty stdout tells us nothing, so it is treated as a dead pane."""
    runner = FakeRunner([_completed([], 0, stdout="")])
    adapter = TmuxAdapter(TMUX_BIN, runner)

    info = adapter.pane_info("baton-1:worker")

    assert info == PaneInfo(dead=True, pid=None)


def test_signal_pane_calls_os_kill_with_pid_and_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Signaling a pane calls `os.kill` with the given pid and signal."""
    calls: list[tuple[int, int]] = []

    def fake_kill(pid: int, signum: int) -> None:
        """Record the pid and signal instead of sending a real one.

        Args:
            pid: The process ID that would have been signaled.
            signum: The signal number that would have been sent.
        """
        calls.append((pid, signum))

    monkeypatch.setattr(baton.tmux.os, "kill", fake_kill)
    adapter = TmuxAdapter(TMUX_BIN)

    adapter.signal_pane(4321, 15)

    assert calls == [(4321, 15)]
