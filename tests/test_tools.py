"""Tests for baton.tools."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from baton.engine import SupervisorError
from baton.models import (
    Event,
    EventKind,
    LifecycleReport,
    LifecycleState,
    ProjectPhase,
    ProjectState,
    WorkerRecord,
)
from baton.tools import RECENT_EVENT_COUNT, BatonTools


def _worker(worker_id: str) -> WorkerRecord:
    """Build a minimal WorkerRecord carrying the given id.

    Args:
        worker_id: The worker id to give the record.

    Returns:
        A WorkerRecord with a placeholder prompt path and launch time;
        neither is read by anything under test.
    """
    return WorkerRecord(
        worker_id=worker_id,
        prompt_path=Path("prompt.md"),
        launched_at=datetime.now(UTC),
    )


class _StubSupervisor:
    """A stand-in for `Supervisor` that records calls and can raise on cue.

    Each of the four methods `BatonTools` delegates to records the
    arguments it received, unless a scripted `SupervisorError` was given
    for it, in which case it raises that instead. `snapshot` and
    `recent_events` always return what was scripted.
    """

    def __init__(
        self,
        *,
        state: ProjectState,
        events: list[Event] | None = None,
        initialize_error: SupervisorError | None = None,
        record_status_error: SupervisorError | None = None,
        report_lifecycle_error: SupervisorError | None = None,
    ) -> None:
        """Store the state and events to return, and the errors to raise.

        Args:
            state: The ProjectState `snapshot` returns, and that
                `initialize` returns unless `initialize_error` is set.
            events: The events `recent_events` returns. Defaults to none.
            initialize_error: The error `initialize` raises, when set,
                instead of recording its call.
            record_status_error: The error `record_status` raises, when
                set, instead of recording its call.
            report_lifecycle_error: The error `report_lifecycle` raises,
                when set, instead of recording its call.
        """
        self._state = state
        self._events = [] if events is None else events
        self._initialize_error = initialize_error
        self._record_status_error = record_status_error
        self._report_lifecycle_error = report_lifecycle_error
        self.initialize_calls: list[dict[str, object]] = []
        self.record_status_calls: list[dict[str, object]] = []
        self.report_lifecycle_calls: list[dict[str, object]] = []
        self.recent_events_calls: list[int] = []

    async def initialize(
        self,
        project_path: Path,
        initial_prompt: str,
        session_name: str | None = None,
    ) -> ProjectState:
        """Record the call and return the scripted state, or raise.

        Args:
            project_path: The project directory passed in.
            initial_prompt: The initial prompt passed in.
            session_name: The session name passed in.

        Returns:
            The scripted state.

        Raises:
            SupervisorError: `initialize_error`, when one was scripted.
        """
        if self._initialize_error is not None:
            raise self._initialize_error
        self.initialize_calls.append(
            {
                "project_path": project_path,
                "initial_prompt": initial_prompt,
                "session_name": session_name,
            }
        )
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
        """Record the call, or raise the scripted error.

        Args:
            worker_id: The worker id passed in.
            state: The lifecycle state passed in.
            message: The message passed in.
            next_prompt: The next prompt passed in.

        Raises:
            SupervisorError: `report_lifecycle_error`, when one was
                scripted.
        """
        if self._report_lifecycle_error is not None:
            raise self._report_lifecycle_error
        self.report_lifecycle_calls.append(
            {
                "worker_id": worker_id,
                "state": state,
                "message": message,
                "next_prompt": next_prompt,
            }
        )

    def snapshot(self) -> ProjectState:
        """Return the scripted state.

        Returns:
            The state given to the constructor.
        """
        return self._state

    def recent_events(self, count: int = 50) -> list[Event]:
        """Record the requested count and return the scripted events.

        Args:
            count: The number of events requested.

        Returns:
            The events given to the constructor.
        """
        self.recent_events_calls.append(count)
        return self._events


# --- initialize_project ------------------------------------------------------


@pytest.mark.anyio
async def test_initialize_project_delegates_path_prompt_and_session_name(
    tmp_path: Path,
) -> None:
    """initialize_project delegates a built Path, the prompt, and session name."""
    stub = _StubSupervisor(state=ProjectState.fresh())
    tools = BatonTools(stub)

    await tools.initialize_project(
        project_path=str(tmp_path),
        initial_prompt="start here",
        session_name="custom-session",
    )

    assert stub.initialize_calls == [
        {
            "project_path": tmp_path,
            "initial_prompt": "start here",
            "session_name": "custom-session",
        }
    ]


@pytest.mark.anyio
async def test_initialize_project_delegates_no_session_name_as_none(
    tmp_path: Path,
) -> None:
    """initialize_project delegates session_name=None when the caller omits it."""
    stub = _StubSupervisor(state=ProjectState.fresh())
    tools = BatonTools(stub)

    await tools.initialize_project(
        project_path=str(tmp_path), initial_prompt="start here"
    )

    assert stub.initialize_calls[0]["session_name"] is None


@pytest.mark.anyio
async def test_initialize_project_returns_fields_from_the_returned_state(
    tmp_path: Path,
) -> None:
    """initialize_project returns fields from the returned state.

    Returns the phase, session name, pane target, and worker id.
    """
    returned_state = ProjectState.fresh().updated(
        phase=ProjectPhase.running,
        project_path=tmp_path,
        session_name="baton-project",
        pane_target="baton-project:worker.0",
        worker=_worker("worker-1"),
    )
    stub = _StubSupervisor(state=returned_state)
    tools = BatonTools(stub)

    result = await tools.initialize_project(
        project_path=str(tmp_path), initial_prompt="start here"
    )

    assert result == {
        "phase": "running",
        "session_name": "baton-project",
        "pane_target": "baton-project:worker.0",
        "worker_id": "worker-1",
    }


@pytest.mark.anyio
async def test_initialize_project_surfaces_a_supervisor_error_as_a_tool_error(
    tmp_path: Path,
) -> None:
    """initialize_project surfaces a SupervisorError as an identical ToolError."""
    stub = _StubSupervisor(
        state=ProjectState.fresh(),
        initialize_error=SupervisorError(
            f"cannot initialize while the project at {tmp_path} is 'running'"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.initialize_project(
            project_path=str(tmp_path), initial_prompt="start here"
        )

    assert (
        str(excinfo.value)
        == f"cannot initialize while the project at {tmp_path} is 'running'"
    )


# --- report_status -------------------------------------------------------


@pytest.mark.anyio
async def test_report_status_delegates_worker_id_and_message() -> None:
    """report_status delegates the worker id and the message."""
    stub = _StubSupervisor(state=ProjectState.fresh())
    tools = BatonTools(stub)

    await tools.report_status(worker_id="worker-1", message="halfway done")

    assert stub.record_status_calls == [
        {"worker_id": "worker-1", "message": "halfway done"}
    ]


@pytest.mark.anyio
async def test_report_status_returns_phase_and_worker_id_from_the_snapshot() -> None:
    """report_status reads its reply from the post-call snapshot.

    Returns the phase and worker id from the snapshot, not from the call's
    own arguments.
    """
    state = ProjectState.fresh().updated(
        phase=ProjectPhase.running, worker=_worker("worker-9")
    )
    stub = _StubSupervisor(state=state)
    tools = BatonTools(stub)

    result = await tools.report_status(worker_id="worker-1", message="progress")

    assert result == {"phase": "running", "worker_id": "worker-9"}


@pytest.mark.anyio
async def test_report_status_surfaces_a_supervisor_error_as_a_tool_error() -> None:
    """report_status surfaces a SupervisorError as an identical-message ToolError."""
    stub = _StubSupervisor(
        state=ProjectState.fresh(),
        record_status_error=SupervisorError(
            "worker 'worker-1' is not the current worker; no worker is running"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.report_status(worker_id="worker-1", message="progress")

    assert (
        str(excinfo.value)
        == "worker 'worker-1' is not the current worker; no worker is running"
    )


# --- report_lifecycle ------------------------------------------------------


@pytest.mark.anyio
async def test_report_lifecycle_converts_state_and_delegates_every_argument() -> None:
    """report_lifecycle converts the state string, delegating every argument."""
    stub = _StubSupervisor(state=ProjectState.fresh())
    tools = BatonTools(stub)

    await tools.report_lifecycle(
        worker_id="worker-1",
        state="success",
        message="task done",
        next_prompt="do the next thing",
    )

    assert stub.report_lifecycle_calls == [
        {
            "worker_id": "worker-1",
            "state": LifecycleState.success,
            "message": "task done",
            "next_prompt": "do the next thing",
        }
    ]


@pytest.mark.anyio
async def test_report_lifecycle_returns_phase_and_worker_id_from_the_snapshot() -> None:
    """report_lifecycle reads its reply from the post-call snapshot.

    Returns the phase and worker id from the snapshot, not from the call's
    own arguments.
    """
    state = ProjectState.fresh().updated(
        phase=ProjectPhase.blocked, worker=_worker("worker-9")
    )
    stub = _StubSupervisor(state=state)
    tools = BatonTools(stub)

    result = await tools.report_lifecycle(
        worker_id="worker-1", state="blocked", message="need input"
    )

    assert result == {"phase": "blocked", "worker_id": "worker-9"}


@pytest.mark.anyio
async def test_report_lifecycle_refuses_unknown_state_without_delegating() -> None:
    """report_lifecycle refuses an unknown state before delegating.

    Names the offending value and the five valid states in its ToolError.
    """
    stub = _StubSupervisor(state=ProjectState.fresh())
    tools = BatonTools(stub)
    expected = (
        "unknown lifecycle state 'sleeping'; expected one of "
        "'running', 'success', 'completed', 'failed', 'blocked'"
    )

    with pytest.raises(ToolError) as excinfo:
        await tools.report_lifecycle(
            worker_id="worker-1", state="sleeping", message="x"
        )

    assert str(excinfo.value) == expected
    assert stub.report_lifecycle_calls == []


@pytest.mark.anyio
async def test_report_lifecycle_surfaces_a_supervisor_error_as_a_tool_error() -> None:
    """report_lifecycle surfaces a SupervisorError as an identical-message ToolError."""
    stub = _StubSupervisor(
        state=ProjectState.fresh(),
        report_lifecycle_error=SupervisorError(
            "worker 'worker-1' already reported 'success'; the first "
            "terminal report is final"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.report_lifecycle(
            worker_id="worker-1",
            state="success",
            message="done",
            next_prompt="next task",
        )

    assert str(excinfo.value) == (
        "worker 'worker-1' already reported 'success'; the first "
        "terminal report is final"
    )


# --- get_project_status ------------------------------------------------------


@pytest.mark.anyio
async def test_get_project_status_returns_phase_worker_last_report_and_events() -> None:
    """get_project_status returns the full project status.

    Asks for RECENT_EVENT_COUNT events, and returns the phase, worker id,
    serialized last report, and serialized events.
    """
    report = LifecycleReport(
        state=LifecycleState.success, message="done", next_prompt="next task"
    )
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    event = Event(
        timestamp=timestamp,
        kind=EventKind.milestone,
        worker_id="worker-1",
        payload={"message": "halfway"},
    )
    state = ProjectState.fresh().updated(
        phase=ProjectPhase.terminating,
        worker=_worker("worker-1"),
        last_report=report,
    )
    stub = _StubSupervisor(state=state, events=[event])
    tools = BatonTools(stub)

    result = await tools.get_project_status()

    assert stub.recent_events_calls == [RECENT_EVENT_COUNT]
    assert result == {
        "phase": "terminating",
        "worker_id": "worker-1",
        "last_report": {
            "state": "success",
            "message": "done",
            "next_prompt": "next task",
        },
        "events": [
            {
                "timestamp": "2026-01-01T00:00:00+00:00",
                "kind": "milestone",
                "worker_id": "worker-1",
                "payload": {"message": "halfway"},
            }
        ],
    }


@pytest.mark.anyio
async def test_get_project_status_on_an_uninitialized_project() -> None:
    """get_project_status handles an uninitialized project.

    Returns None for the worker id and the last report, and an empty
    event list.
    """
    stub = _StubSupervisor(state=ProjectState.fresh())
    tools = BatonTools(stub)

    result = await tools.get_project_status()

    assert result["worker_id"] is None
    assert result["last_report"] is None
    assert result["events"] == []


@pytest.mark.anyio
async def test_get_project_status_serializes_event_timestamp_and_payload() -> None:
    """An event reaches the reply with an ISO-8601 timestamp and unchanged payload."""
    payload = {"from": "running", "to": "blocked", "reason": "needs a human"}
    timestamp = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    event = Event(
        timestamp=timestamp, kind=EventKind.phase, worker_id=None, payload=payload
    )
    stub = _StubSupervisor(state=ProjectState.fresh(), events=[event])
    tools = BatonTools(stub)

    result = await tools.get_project_status()

    serialized = result["events"][0]
    assert serialized == {
        "timestamp": timestamp.isoformat(),
        "kind": "phase",
        "worker_id": None,
        "payload": payload,
    }
