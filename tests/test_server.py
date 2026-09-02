"""Tests for baton.server."""

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from baton.config import BatonConfig
from baton.engine import Supervisor, SupervisorError
from baton.models import ProjectPhase
from baton.server import build_server, build_supervisor
from baton.tools import BatonTools
from tests.doubles import StubSupervisor


def test_build_supervisor_returns_a_fresh_supervisor(config: BatonConfig) -> None:
    """build_supervisor returns a Supervisor whose project is uninitialized."""
    supervisor = build_supervisor(config)

    assert isinstance(supervisor, Supervisor)
    assert supervisor.snapshot().phase == ProjectPhase.uninitialized


@pytest.mark.anyio
async def test_build_server_registers_exactly_the_four_tool_names(
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """build_server registers only the four pinned tool names, no more, no less."""
    server = build_server(make_stub_supervisor())

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "initialize_project",
        "report_status",
        "report_lifecycle",
        "get_project_status",
    }


@pytest.mark.anyio
async def test_build_server_uses_each_tools_docstring_as_its_description(
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """Each tool's description is its BatonTools method's docstring, unchanged."""
    server = build_server(make_stub_supervisor())

    tools = await server.list_tools()
    descriptions = {tool.name: tool.description for tool in tools}

    assert descriptions["initialize_project"] == BatonTools.initialize_project.__doc__
    assert descriptions["report_status"] == BatonTools.report_status.__doc__
    assert descriptions["report_lifecycle"] == BatonTools.report_lifecycle.__doc__
    assert descriptions["get_project_status"] == BatonTools.get_project_status.__doc__


@pytest.mark.anyio
async def test_report_lifecycle_description_states_message_and_finality_rules(
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """report_lifecycle's description is what a worker reads before it calls the tool.

    It must say the message rule is checked first, and that the first
    terminal report is final.
    """
    server = build_server(make_stub_supervisor())

    tools = await server.list_tools()
    description = next(
        tool.description for tool in tools if tool.name == "report_lifecycle"
    )

    assert "checked first" in description
    assert "terminal report is final" in description


@pytest.mark.anyio
async def test_a_registered_tool_reaches_the_given_supervisor(
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """Calling a registered tool delegates to the supervisor build_server was given."""
    stub = make_stub_supervisor()
    server = build_server(stub)

    await server.call_tool(
        "report_status", {"worker_id": "worker-1", "message": "progress"}
    )

    assert stub.record_status_calls == [
        {"worker_id": "worker-1", "message": "progress"}
    ]


@pytest.mark.anyio
async def test_a_supervisor_error_reaches_the_caller_as_a_tool_error(
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """A SupervisorError is how the framework delivers a refusal to a worker.

    build_server wires BatonTools' ToolError-raising path straight through
    to the client: the failure surfaces as a `ToolError`, not an
    `UnexpectedToolError`, and its wrapped message still ends with the
    engine's own text, unchanged.
    """
    message = "worker 'worker-1' is not the current worker; no worker is running"
    stub = make_stub_supervisor(record_status_error=SupervisorError(message))
    server = build_server(stub)

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool(
            "report_status", {"worker_id": "worker-1", "message": "progress"}
        )

    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert str(excinfo.value).endswith(message)
