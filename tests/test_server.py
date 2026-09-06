"""Tests for baton.server."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from starlette.routing import Mount

from baton.config import BatonConfig
from baton.coordinator import Coordinator, CoordinatorError
from baton.engine import SupervisorError
from baton.server import build_app, build_coordinator, build_server
from baton.tools import BatonTools
from tests.doubles import StubCoordinator, StubSupervisor


def test_build_coordinator_returns_a_coordinator_holding_no_projects(
    config: BatonConfig,
) -> None:
    """build_coordinator returns a Coordinator with no project on disk to hold."""
    coordinator = build_coordinator(config)

    assert isinstance(coordinator, Coordinator)
    with pytest.raises(CoordinatorError):
        coordinator.supervisor_for_worker("worker-1")


def test_build_coordinator_refuses_a_legacy_root_state_file(
    config: BatonConfig,
) -> None:
    """A root-level state.json refuses the build, proving the config's dir is read."""
    config.state_dir.mkdir(parents=True)
    legacy = config.state_dir / "state.json"
    legacy.write_text("{}", encoding="utf-8")

    with pytest.raises(CoordinatorError) as excinfo:
        build_coordinator(config)

    assert str(legacy) in str(excinfo.value)


@pytest.mark.anyio
async def test_build_server_registers_exactly_the_seven_tool_names(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """build_server registers only the seven pinned tool names, no more, no less."""
    server = build_server(make_stub_coordinator())

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "initialize_project",
        "list_projects",
        "resume_project",
        "close_project",
        "report_status",
        "report_lifecycle",
        "get_project_status",
    }


@pytest.mark.anyio
async def test_build_server_uses_each_tools_docstring_as_its_description(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """Each tool's description is its BatonTools method's docstring, unchanged."""
    server = build_server(make_stub_coordinator())

    tools = await server.list_tools()
    descriptions = {tool.name: tool.description for tool in tools}

    assert descriptions["initialize_project"] == BatonTools.initialize_project.__doc__
    assert descriptions["list_projects"] == BatonTools.list_projects.__doc__
    assert descriptions["resume_project"] == BatonTools.resume_project.__doc__
    assert descriptions["close_project"] == BatonTools.close_project.__doc__
    assert descriptions["report_status"] == BatonTools.report_status.__doc__
    assert descriptions["report_lifecycle"] == BatonTools.report_lifecycle.__doc__
    assert descriptions["get_project_status"] == BatonTools.get_project_status.__doc__


@pytest.mark.anyio
async def test_report_lifecycle_description_states_message_and_finality_rules(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """report_lifecycle's description is what a worker reads before it calls the tool.

    It must say the message rule is checked first, and that the first
    terminal report is final.
    """
    server = build_server(make_stub_coordinator())

    tools = await server.list_tools()
    description = next(
        tool.description for tool in tools if tool.name == "report_lifecycle"
    )

    assert "checked first" in description
    assert "terminal report is final" in description


@pytest.mark.anyio
async def test_a_registered_tool_reaches_the_coordinators_supervisor(
    make_stub_supervisor: type[StubSupervisor],
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """Calling a registered tool reaches the supervisor the coordinator routes to."""
    supervisor = make_stub_supervisor()
    stub = make_stub_coordinator(supervisor=supervisor)
    server = build_server(stub)

    await server.call_tool(
        "report_status", {"worker_id": "worker-1", "message": "progress"}
    )

    assert supervisor.record_status_calls == [
        {"worker_id": "worker-1", "message": "progress"}
    ]


@pytest.mark.anyio
async def test_a_supervisor_error_reaches_the_caller_as_a_tool_error(
    make_stub_supervisor: type[StubSupervisor],
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """A SupervisorError is how the framework delivers a refusal to a worker.

    build_server wires BatonTools' ToolError-raising path straight through
    to the client: the failure surfaces as a `ToolError`, not an
    `UnexpectedToolError`, and its wrapped message still ends with the
    engine's own text, unchanged.
    """
    message = "worker 'worker-1' is not the current worker; no worker is running"
    supervisor = make_stub_supervisor(record_status_error=SupervisorError(message))
    stub = make_stub_coordinator(supervisor=supervisor)
    server = build_server(stub)

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool(
            "report_status", {"worker_id": "worker-1", "message": "progress"}
        )

    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert str(excinfo.value).endswith(message)


@pytest.mark.anyio
async def test_build_app_lifespan_starts_and_shuts_down_the_coordinator(
    config: BatonConfig, make_stub_coordinator: type[StubCoordinator]
) -> None:
    """Driving the app's lifespan runs the coordinator's hooks around it.

    The drive is done by hand, with a scripted `receive` and a recording
    `send`, rather than through Starlette's `TestClient`: the by-hand drive
    runs the lifespan on the test's own loop, with no portal thread and no
    HTTP client, and asserts the protocol messages directly.
    """
    stub = make_stub_coordinator()
    server = build_server(stub)
    app = build_app(stub, server, config)

    scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
    incoming = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        """Hand back the next scripted lifespan message."""
        return next(incoming)

    async def send(message: dict[str, object]) -> None:
        """Record a lifespan protocol message sent by the app."""
        sent.append(message)

    await app(scope, receive, send)

    assert [message["type"] for message in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert stub.hook_calls == ["start", "shutdown"]


def test_build_app_mounts_the_sse_server_at_the_root(
    config: BatonConfig, make_stub_coordinator: type[StubCoordinator]
) -> None:
    """The app's one route mounts the MCP server, with /sse reachable at it."""
    stub = make_stub_coordinator()
    server = build_server(stub)
    app = build_app(stub, server, config)

    [mount] = app.routes
    assert isinstance(mount, Mount)
    assert any(route.path == "/sse" for route in mount.app.routes)
