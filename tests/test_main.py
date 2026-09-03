"""Tests for baton.__main__."""

import json
from pathlib import Path

import pytest

import baton.__main__ as main_module
from baton.config import BatonConfig


def _set_baton_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point every `BATON_*` environment variable at test-safe values.

    Writes an executable stub for each of the two binaries, because
    `BatonConfig.from_env` refuses an override that names no executable.

    Args:
        monkeypatch: The fixture used to set each variable.
        tmp_path: The directory used for the state dir and the two binaries.
    """
    monkeypatch.setenv("BATON_HOST", "127.0.0.1")
    monkeypatch.setenv("BATON_PORT", "9999")
    monkeypatch.setenv("BATON_STATE_DIR", str(tmp_path))
    claude_bin = tmp_path / "claude"
    tmux_bin = tmp_path / "tmux"
    for stub in (claude_bin, tmux_bin):
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
    monkeypatch.setenv("BATON_CLAUDE_BIN", str(claude_bin))
    monkeypatch.setenv("BATON_TMUX_BIN", str(tmux_bin))


def test_main_runs_the_app_under_uvicorn_with_the_configured_host_and_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() runs the built app under uvicorn, on the environment's host and port."""
    _set_baton_env(monkeypatch, tmp_path)
    sentinel_app = object()
    run_calls: list[dict[str, object]] = []

    def fake_run(app: object, **kwargs: object) -> None:
        """Record the app passed positionally and the keyword arguments.

        Args:
            app: The ASGI app passed to `uvicorn.run`.
            kwargs: The keyword arguments passed to `uvicorn.run`.
        """
        run_calls.append({"app": app, **kwargs})

    monkeypatch.setattr(main_module, "build_supervisor", lambda config: object())
    monkeypatch.setattr(main_module, "build_server", lambda supervisor: object())
    monkeypatch.setattr(
        main_module, "build_app", lambda supervisor, server, config: sentinel_app
    )
    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)

    main_module.main()

    assert run_calls == [{"app": sentinel_app, "host": "127.0.0.1", "port": 9999}]


def test_main_writes_the_mcp_client_config_into_the_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() writes mcp.json before running, so a session can be pointed at it."""
    _set_baton_env(monkeypatch, tmp_path)
    monkeypatch.setattr(main_module, "build_supervisor", lambda config: object())
    monkeypatch.setattr(main_module, "build_server", lambda supervisor: object())
    monkeypatch.setattr(
        main_module, "build_app", lambda supervisor, server, config: object()
    )
    monkeypatch.setattr(main_module.uvicorn, "run", lambda app, **kwargs: None)

    main_module.main()

    written = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))
    assert written == {
        "mcpServers": {"baton": {"type": "sse", "url": "http://127.0.0.1:9999/sse"}}
    }


def test_main_wires_the_built_config_and_supervisor_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() passes its config on, then hands the supervisor to build_server."""
    _set_baton_env(monkeypatch, tmp_path)
    sentinel_supervisor = object()
    sentinel_server = object()
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

    def fake_build_server(supervisor: object) -> object:
        """Record the supervisor passed in and return a sentinel server.

        Args:
            supervisor: The supervisor main() built.

        Returns:
            The sentinel server this test asserts on.
        """
        captured_supervisors.append(supervisor)
        return sentinel_server

    monkeypatch.setattr(main_module, "build_supervisor", fake_build_supervisor)
    monkeypatch.setattr(main_module, "build_server", fake_build_server)
    monkeypatch.setattr(
        main_module, "build_app", lambda supervisor, server, config: object()
    )
    monkeypatch.setattr(main_module.uvicorn, "run", lambda app, **kwargs: None)

    main_module.main()

    assert captured_configs == [BatonConfig.from_env()]
    assert captured_supervisors == [sentinel_supervisor]


def test_main_wires_the_supervisor_server_and_config_into_build_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """main() hands build_app the supervisor, the server, and the config it built.

    The fake's parameter names pin the order main() calls build_app with:
    supervisor, then server, then config.
    """
    _set_baton_env(monkeypatch, tmp_path)
    sentinel_supervisor = object()
    sentinel_server = object()
    build_app_calls: list[dict[str, object]] = []

    def fake_build_app(
        supervisor: object, server: object, config: BatonConfig
    ) -> object:
        """Record the arguments build_app was called with.

        Args:
            supervisor: The supervisor main() built.
            server: The server main() built.
            config: The config main() built.

        Returns:
            A sentinel app this test does not otherwise inspect.
        """
        build_app_calls.append(
            {"supervisor": supervisor, "server": server, "config": config}
        )
        return object()

    monkeypatch.setattr(
        main_module, "build_supervisor", lambda config: sentinel_supervisor
    )
    monkeypatch.setattr(main_module, "build_server", lambda supervisor: sentinel_server)
    monkeypatch.setattr(main_module, "build_app", fake_build_app)
    monkeypatch.setattr(main_module.uvicorn, "run", lambda app, **kwargs: None)

    main_module.main()

    assert build_app_calls == [
        {
            "supervisor": sentinel_supervisor,
            "server": sentinel_server,
            "config": BatonConfig.from_env(),
        }
    ]
