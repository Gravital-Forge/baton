"""Tests for baton.__main__."""

from pathlib import Path

import pytest

import baton.__main__ as main_module
from baton.config import BatonConfig


class _FakeServer:
    """A stand-in for the `MCPServer` `main()` builds, recording its `run` call."""

    def __init__(self) -> None:
        """Start with no recorded call."""
        self.run_calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> None:
        """Record the keyword arguments `run` was called with.

        Args:
            kwargs: The keyword arguments passed to `run`.
        """
        self.run_calls.append(kwargs)


def _set_baton_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every `BATON_*` environment variable at test-safe values.

    Args:
        monkeypatch: The fixture used to set each variable.
        tmp_path: The directory used for the state dir and the two binaries.
    """
    monkeypatch.setenv("BATON_HOST", "127.0.0.1")
    monkeypatch.setenv("BATON_PORT", "9999")
    monkeypatch.setenv("BATON_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BATON_CLAUDE_BIN", str(tmp_path / "claude"))
    monkeypatch.setenv("BATON_TMUX_BIN", str(tmp_path / "tmux"))


def test_main_runs_the_server_over_sse_with_the_configured_host_and_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() runs the server over sse, on the host and port from the environment."""
    _set_baton_env(monkeypatch, tmp_path)
    fake_server = _FakeServer()
    monkeypatch.setattr(main_module, "build_supervisor", lambda config: object())
    monkeypatch.setattr(main_module, "build_server", lambda supervisor: fake_server)

    main_module.main()

    assert fake_server.run_calls == [
        {"transport": "sse", "host": "127.0.0.1", "port": 9999}
    ]


def test_main_wires_the_built_config_and_supervisor_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() passes its config on, then hands the supervisor to build_server."""
    _set_baton_env(monkeypatch, tmp_path)
    fake_server = _FakeServer()
    sentinel_supervisor = object()
    captured_configs: list[BatonConfig] = []
    captured_supervisors: list[object] = []

    def fake_build_supervisor(config: BatonConfig) -> object:
        """Record the config passed in and return a sentinel supervisor.

        Args:
            config: The config main() built.

        Returns:
            The sentinel supervisor this test asserts on.
        """
        captured_configs.append(config)
        return sentinel_supervisor

    def fake_build_server(supervisor: object) -> _FakeServer:
        """Record the supervisor passed in and return the fake server.

        Args:
            supervisor: The supervisor main() built.

        Returns:
            The fake server this test asserts on.
        """
        captured_supervisors.append(supervisor)
        return fake_server

    monkeypatch.setattr(main_module, "build_supervisor", fake_build_supervisor)
    monkeypatch.setattr(main_module, "build_server", fake_build_server)

    main_module.main()

    assert captured_configs == [BatonConfig.from_env()]
    assert captured_supervisors == [sentinel_supervisor]
