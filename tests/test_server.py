"""Tests for baton.server."""

from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from baton.config import BatonConfig
from baton.engine import Supervisor, SupervisorError
from baton.models import LifecycleState, ProjectPhase, ProjectState
from baton.server import build_server, build_supervisor
from baton.tools import BatonTools


class _StubSupervisor:
    """A stand-in for `Supervisor` that `build_server` can wire into `BatonTools`.

    Returns a scripted `ProjectState` from `initialize` and `snapshot`, and
    records or raises on `record_status`, matching the calls `BatonTools`
    makes on the collaborator it is given.
    """

    def __init__(
        self,
        *,
        state: ProjectState | None = None,
        record_status_error: SupervisorError | None = None,
    ) -> None:
        """Store the state to return and any error `record_status` raises.

        Args:
            state: The `ProjectState` `initialize` and `snapshot` return.
                Defaults to `ProjectState.fresh()`.
            record_status_error: The error `record_status` raises instead
                of recording its call, when set.
        """
        self._state = state if state is not None else ProjectState.fresh()
        self._record_status_error = record_status_error
        self.record_status_calls: list[dict[str, object]] = []

    async def initialize(
        self,
        project_path: Path,
        initial_prompt: str,
        session_name: str | None = None,
    ) -> ProjectState:
        """Return the scripted state, ignoring every argument.

        Args:
            project_path: Unused; accepted to match `BatonTools`' call.
            initial_prompt: Unused; accepted to match `BatonTools`' call.
            session_name: Unused; accepted to match `BatonTools`' call.

        Returns:
            The scripted state.
        """
        return self._state

    async def record_status(self, worker_id: str, message: str) -> None:
        """Record the call, or raise the scripted error.

        Args:
            worker_id: The worker id passed in.
            message: The message passed in.

        Raises:
            SupervisorError: `record_status_error`, when one was scripted.
        """
        if self._record_status_error is not None:
            raise self._record_status_error
        self.record_status_calls.append({"worker_id": worker_id, "message": message})

    async def report_lifecycle(
        self,
        worker_id: str,
        state: LifecycleState,
        message: str | None = None,
        next_prompt: str | None = None,
    ) -> None:
        """Do nothing; not exercised by these tests.

        Args:
            worker_id: Unused; accepted to match `BatonTools`' call.
            state: Unused; accepted to match `BatonTools`' call.
            message: Unused; accepted to match `BatonTools`' call.
            next_prompt: Unused; accepted to match `BatonTools`' call.
        """
        return None

    def snapshot(self) -> ProjectState:
        """Return the scripted state.

        Returns:
            The state given to the constructor.
        """
        return self._state

    def recent_events(self, count: int = 50) -> list[object]:
        """Return an empty event list; not exercised by these tests.

        Args:
            count: Unused; accepted to match `BatonTools`' call.

        Returns:
            An empty list.
        """
        return []


# --- build_supervisor --------------------------------------------------------


def test_build_supervisor_returns_a_fresh_supervisor(config: BatonConfig) -> None:
    """build_supervisor returns a Supervisor whose project is uninitialized."""
    supervisor = build_supervisor(config)

    assert isinstance(supervisor, Supervisor)
    assert supervisor.snapshot().phase == ProjectPhase.uninitialized


def test_build_supervisor_writes_mcp_config(config: BatonConfig) -> None:
    """build_supervisor writes mcp.json into the config's state directory."""
    build_supervisor(config)

    assert (config.state_dir / "mcp.json").exists()


# --- build_server --------------------------------------------------------


@pytest.mark.anyio
async def test_build_server_registers_exactly_the_four_tool_names() -> None:
    """build_server registers only the four pinned tool names, no more, no less."""
    server = build_server(_StubSupervisor())

    tools = await server.list_tools()

    assert {tool.name for tool in tools} == {
        "initialize_project",
        "report_status",
        "report_lifecycle",
        "get_project_status",
    }


@pytest.mark.anyio
async def test_build_server_uses_each_tools_docstring_as_its_description() -> None:
    """Each tool's description is its BatonTools method's docstring, unchanged."""
    server = build_server(_StubSupervisor())

    tools = await server.list_tools()
    descriptions = {tool.name: tool.description for tool in tools}

    assert descriptions["initialize_project"] == BatonTools.initialize_project.__doc__
    assert descriptions["report_status"] == BatonTools.report_status.__doc__
    assert descriptions["report_lifecycle"] == BatonTools.report_lifecycle.__doc__
    assert descriptions["get_project_status"] == BatonTools.get_project_status.__doc__


@pytest.mark.anyio
async def test_report_lifecycle_description_states_message_and_finality_rules() -> None:
    """report_lifecycle's description is what a worker reads before it calls the tool.

    It must say the message rule is checked first, and that the first
    terminal report is final.
    """
    server = build_server(_StubSupervisor())

    tools = await server.list_tools()
    description = next(
        tool.description for tool in tools if tool.name == "report_lifecycle"
    )

    assert "The message rule is" in description
    assert "checked first" in description
    assert "terminal, and the first" in description
    assert "terminal report is final" in description


@pytest.mark.anyio
async def test_a_registered_tool_reaches_the_given_supervisor() -> None:
    """Calling a registered tool delegates to the supervisor build_server was given."""
    stub = _StubSupervisor()
    server = build_server(stub)

    await server.call_tool(
        "report_status", {"worker_id": "worker-1", "message": "progress"}
    )

    assert stub.record_status_calls == [
        {"worker_id": "worker-1", "message": "progress"}
    ]


@pytest.mark.anyio
async def test_a_supervisor_error_reaches_the_caller_as_a_tool_error() -> None:
    """A SupervisorError is how the framework delivers a refusal to a worker.

    build_server wires BatonTools' ToolError-raising path straight through
    to the client: the failure surfaces as a `ToolError`, not an
    `UnexpectedToolError`, and its wrapped message still ends with the
    engine's own text, unchanged.
    """
    message = "worker 'worker-1' is not the current worker; no worker is running"
    stub = _StubSupervisor(record_status_error=SupervisorError(message))
    server = build_server(stub)

    with pytest.raises(ToolError) as excinfo:
        await server.call_tool(
            "report_status", {"worker_id": "worker-1", "message": "progress"}
        )

    assert not isinstance(excinfo.value, UnexpectedToolError)
    assert str(excinfo.value).endswith(message)
