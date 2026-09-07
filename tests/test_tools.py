"""Tests for baton.tools."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from baton.coordinator import CoordinatorError
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
from baton.tmux import TmuxError
from baton.tools import RECENT_EVENT_COUNT, BatonTools
from tests.doubles import StubCoordinator, StubSupervisor


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


@pytest.mark.anyio
async def test_initialize_project_delegates_path_title_prompt_session_name_and_model(
    make_stub_coordinator: type[StubCoordinator],
    tmp_path: Path,
) -> None:
    """initialize_project delegates a built Path, title, prompt, session, and model."""
    stub = make_stub_coordinator()
    tools = BatonTools(stub)

    await tools.initialize_project(
        project_path=str(tmp_path),
        title="Widget factory",
        initial_prompt="start here",
        session_name="custom-session",
        model="opus",
    )

    assert stub.initialize_calls == [
        {
            "project_path": tmp_path,
            "title": "Widget factory",
            "initial_prompt": "start here",
            "session_name": "custom-session",
            "model": "opus",
        }
    ]


@pytest.mark.anyio
async def test_initialize_project_delegates_no_session_name_or_model_as_none(
    make_stub_coordinator: type[StubCoordinator],
    tmp_path: Path,
) -> None:
    """initialize_project delegates session_name=None and model=None when omitted."""
    stub = make_stub_coordinator()
    tools = BatonTools(stub)

    await tools.initialize_project(
        project_path=str(tmp_path),
        title="Widget factory",
        initial_prompt="start here",
    )

    assert stub.initialize_calls[0]["session_name"] is None
    assert stub.initialize_calls[0]["model"] is None


@pytest.mark.anyio
async def test_initialize_project_returns_fields_from_the_returned_state(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
    tmp_path: Path,
) -> None:
    """initialize_project returns fields from the returned state.

    Returns the project id, phase, session name, pane target, worker id,
    and model.
    """
    returned_state = ProjectState.new("a1b2c3d4", "Widget factory").updated(
        phase=ProjectPhase.running,
        project_path=tmp_path,
        session_name="baton-widget-factory",
        pane_target="baton-widget-factory:worker.0",
        worker=_worker("worker-1"),
        model="sonnet",
    )
    stub = make_stub_coordinator(supervisor=make_stub_supervisor(state=returned_state))
    tools = BatonTools(stub)

    result = await tools.initialize_project(
        project_path=str(tmp_path),
        title="Widget factory",
        initial_prompt="start here",
    )

    assert result == {
        "project_id": "a1b2c3d4",
        "phase": "running",
        "session_name": "baton-widget-factory",
        "pane_target": "baton-widget-factory:worker.0",
        "worker_id": "worker-1",
        "model": "sonnet",
    }


@pytest.mark.anyio
async def test_initialize_project_surfaces_a_coordinator_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
    tmp_path: Path,
) -> None:
    """initialize_project surfaces a CoordinatorError as an identical ToolError."""
    stub = make_stub_coordinator(
        initialize_error=CoordinatorError(
            "session name 'baton-widget-factory' is already held by project 'a1b2c3d4'"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.initialize_project(
            project_path=str(tmp_path),
            title="Widget factory",
            initial_prompt="start here",
        )

    assert str(excinfo.value) == (
        "session name 'baton-widget-factory' is already held by project 'a1b2c3d4'"
    )


@pytest.mark.anyio
async def test_initialize_project_surfaces_a_supervisor_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
    tmp_path: Path,
) -> None:
    """initialize_project surfaces a SupervisorError as an identical ToolError."""
    stub = make_stub_coordinator(
        initialize_error=SupervisorError(
            "project path must be an existing directory, got '/nope'"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.initialize_project(
            project_path=str(tmp_path),
            title="Widget factory",
            initial_prompt="start here",
        )

    assert (
        str(excinfo.value) == "project path must be an existing directory, got '/nope'"
    )


@pytest.mark.anyio
async def test_initialize_project_surfaces_a_tmux_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
    tmp_path: Path,
) -> None:
    """initialize_project surfaces a TmuxError as an identical ToolError."""
    stub = make_stub_coordinator(
        initialize_error=TmuxError(
            "['tmux', 'new-session', '-d', '-s', 'baton-project'] failed: "
            "duplicate session: baton-project"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.initialize_project(
            project_path=str(tmp_path),
            title="Widget factory",
            initial_prompt="start here",
        )

    assert str(excinfo.value) == (
        "['tmux', 'new-session', '-d', '-s', 'baton-project'] failed: "
        "duplicate session: baton-project"
    )


@pytest.mark.anyio
async def test_report_status_delegates_worker_id_and_message(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """report_status delegates the worker id and the message."""
    supervisor = make_stub_supervisor()
    stub = make_stub_coordinator(supervisor=supervisor)
    tools = BatonTools(stub)

    await tools.report_status(worker_id="worker-1", message="halfway done")

    assert stub.supervisor_for_worker_calls == ["worker-1"]
    assert supervisor.record_status_calls == [
        {"worker_id": "worker-1", "message": "halfway done"}
    ]


@pytest.mark.anyio
async def test_report_status_returns_phase_and_worker_id_from_the_snapshot(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """report_status reads its reply from the post-call snapshot.

    Returns the phase and worker id from the snapshot, not from the call's
    own arguments.
    """
    state = ProjectState.new("a1b2c3d4", "Widget factory").updated(
        phase=ProjectPhase.running, worker=_worker("worker-9")
    )
    stub = make_stub_coordinator(supervisor=make_stub_supervisor(state=state))
    tools = BatonTools(stub)

    result = await tools.report_status(worker_id="worker-1", message="progress")

    assert result == {"phase": "running", "worker_id": "worker-9"}


@pytest.mark.anyio
async def test_report_status_surfaces_a_supervisor_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """report_status surfaces a SupervisorError as an identical-message ToolError."""
    supervisor = make_stub_supervisor(
        record_status_error=SupervisorError(
            "worker 'worker-1' is not the current worker; no worker is running"
        ),
    )
    stub = make_stub_coordinator(supervisor=supervisor)
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.report_status(worker_id="worker-1", message="progress")

    assert (
        str(excinfo.value)
        == "worker 'worker-1' is not the current worker; no worker is running"
    )


@pytest.mark.anyio
async def test_report_status_from_an_unrouted_worker_is_refused(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """report_status surfaces a lookup CoordinatorError as an identical ToolError."""
    stub = make_stub_coordinator(
        lookup_error=CoordinatorError(
            "worker 'worker-1' is not the current worker of any project"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.report_status(worker_id="worker-1", message="progress")

    assert (
        str(excinfo.value)
        == "worker 'worker-1' is not the current worker of any project"
    )


@pytest.mark.anyio
async def test_report_lifecycle_converts_state_and_delegates_every_argument(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """report_lifecycle converts the state string, delegating every argument."""
    supervisor = make_stub_supervisor()
    stub = make_stub_coordinator(supervisor=supervisor)
    tools = BatonTools(stub)

    await tools.report_lifecycle(
        worker_id="worker-1",
        state="success",
        message="task done",
        next_prompt="do the next thing",
    )

    assert stub.supervisor_for_worker_calls == ["worker-1"]
    assert supervisor.report_lifecycle_calls == [
        {
            "worker_id": "worker-1",
            "state": LifecycleState.success,
            "message": "task done",
            "next_prompt": "do the next thing",
        }
    ]


@pytest.mark.anyio
async def test_report_lifecycle_returns_phase_and_worker_id_from_the_snapshot(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """report_lifecycle reads its reply from the post-call snapshot.

    Returns the phase and worker id from the snapshot, not from the call's
    own arguments.
    """
    state = ProjectState.new("a1b2c3d4", "Widget factory").updated(
        phase=ProjectPhase.blocked, worker=_worker("worker-9")
    )
    stub = make_stub_coordinator(supervisor=make_stub_supervisor(state=state))
    tools = BatonTools(stub)

    result = await tools.report_lifecycle(
        worker_id="worker-1", state="blocked", message="need input"
    )

    assert result == {"phase": "blocked", "worker_id": "worker-9"}


@pytest.mark.anyio
async def test_report_lifecycle_refuses_unknown_state_without_delegating(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """report_lifecycle refuses an unknown state before delegating.

    Names the offending value and the five valid states in its ToolError.
    """
    supervisor = make_stub_supervisor()
    stub = make_stub_coordinator(supervisor=supervisor)
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
    assert supervisor.report_lifecycle_calls == []
    assert stub.supervisor_for_worker_calls == []


@pytest.mark.anyio
async def test_report_lifecycle_surfaces_a_supervisor_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """report_lifecycle surfaces a SupervisorError as an identical-message ToolError."""
    supervisor = make_stub_supervisor(
        report_lifecycle_error=SupervisorError(
            "worker 'worker-1' is terminating and baton accepts no further "
            "report from it"
        ),
    )
    stub = make_stub_coordinator(supervisor=supervisor)
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.report_lifecycle(
            worker_id="worker-1",
            state="success",
            message="done",
            next_prompt="next task",
        )

    assert str(excinfo.value) == (
        "worker 'worker-1' is terminating and baton accepts no further report from it"
    )


@pytest.mark.anyio
async def test_report_lifecycle_from_an_unrouted_worker_is_refused(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """report_lifecycle surfaces a lookup CoordinatorError as an identical ToolError."""
    stub = make_stub_coordinator(
        lookup_error=CoordinatorError(
            "worker 'worker-1' is not the current worker of any project"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.report_lifecycle(
            worker_id="worker-1", state="success", message="done", next_prompt="next"
        )

    assert (
        str(excinfo.value)
        == "worker 'worker-1' is not the current worker of any project"
    )


@pytest.mark.anyio
async def test_get_project_status_returns_phase_worker_last_report_and_events(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """get_project_status returns the full project status.

    Asks for RECENT_EVENT_COUNT events, and returns the phase, worker id,
    model, serialized last report, and serialized events.
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
    state = ProjectState.new("a1b2c3d4", "Widget factory").updated(
        phase=ProjectPhase.terminating,
        worker=_worker("worker-1"),
        last_report=report,
        model="sonnet",
    )
    supervisor = make_stub_supervisor(state=state, events=[event])
    stub = make_stub_coordinator(supervisor=supervisor)
    tools = BatonTools(stub)

    result = await tools.get_project_status(project_id="a1b2c3d4")

    assert stub.supervisor_calls == ["a1b2c3d4"]
    assert supervisor.recent_events_calls == [RECENT_EVENT_COUNT]
    assert result == {
        "phase": "terminating",
        "worker_id": "worker-1",
        "model": "sonnet",
        "last_report": {
            "state": "success",
            "message": "done",
            "next_prompt": "next task",
            "delay_seconds": None,
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
async def test_get_project_status_returns_none_for_every_absent_field(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """get_project_status maps an absent worker, model and report to None."""
    stub = make_stub_coordinator()
    tools = BatonTools(stub)

    result = await tools.get_project_status(project_id="a1b2c3d4")

    assert result["worker_id"] is None
    assert result["model"] is None
    assert result["last_report"] is None
    assert result["events"] == []


@pytest.mark.anyio
async def test_get_project_status_serializes_event_timestamp_and_payload(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """An event reaches the reply with an ISO-8601 timestamp and unchanged payload."""
    payload = {"from": "running", "to": "blocked", "reason": "needs a human"}
    timestamp = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
    event = Event(
        timestamp=timestamp, kind=EventKind.phase, worker_id=None, payload=payload
    )
    stub = make_stub_coordinator(supervisor=make_stub_supervisor(events=[event]))
    tools = BatonTools(stub)

    result = await tools.get_project_status(project_id="a1b2c3d4")

    serialized = result["events"][0]
    assert serialized == {
        "timestamp": timestamp.isoformat(),
        "kind": "phase",
        "worker_id": None,
        "payload": payload,
    }


@pytest.mark.anyio
async def test_get_project_status_for_an_unknown_project_is_refused(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """get_project_status surfaces an unknown project id as an identical ToolError."""
    stub = make_stub_coordinator(
        lookup_error=CoordinatorError("no project has the id 'nosuch'"),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.get_project_status(project_id="nosuch")

    assert str(excinfo.value) == "no project has the id 'nosuch'"


@pytest.mark.anyio
async def test_list_projects_returns_a_row_per_project(
    make_stub_coordinator: type[StubCoordinator], tmp_path: Path
) -> None:
    """list_projects shapes each project's state into its own compact row."""
    state = ProjectState.new("a1b2c3d4", "Widget factory").updated(
        phase=ProjectPhase.running,
        project_path=tmp_path,
        session_name="baton-widget-factory",
        pane_target="baton-widget-factory:worker.0",
        worker=_worker("worker-1"),
        model="sonnet",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    tools = BatonTools(make_stub_coordinator(projects=[state]))

    result = await tools.list_projects()

    assert result == {
        "projects": [
            {
                "project_id": "a1b2c3d4",
                "title": "Widget factory",
                "session_name": "baton-widget-factory",
                "project_path": str(tmp_path),
                "phase": "running",
                "worker_id": "worker-1",
                "model": "sonnet",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
        ]
    }


@pytest.mark.anyio
async def test_list_projects_with_no_project_returns_an_empty_projects_list(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """list_projects returns an empty projects list when baton holds none."""
    tools = BatonTools(make_stub_coordinator())

    assert await tools.list_projects() == {"projects": []}


@pytest.mark.anyio
async def test_resume_project_delegates_the_project_id_and_prompt(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """resume_project delegates the project id and the prompt."""
    stub = make_stub_coordinator()
    tools = BatonTools(stub)

    await tools.resume_project(project_id="a1b2c3d4", prompt="carry on")

    assert stub.resume_calls == [{"project_id": "a1b2c3d4", "prompt": "carry on"}]


@pytest.mark.anyio
async def test_resume_project_returns_phase_and_worker_id_from_the_returned_state(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """resume_project reads its reply from the state the coordinator returned."""
    state = ProjectState.new("a1b2c3d4", "Widget factory").updated(
        phase=ProjectPhase.running, worker=_worker("worker-2")
    )
    stub = make_stub_coordinator(supervisor=make_stub_supervisor(state=state))
    tools = BatonTools(stub)

    result = await tools.resume_project(project_id="a1b2c3d4", prompt="carry on")

    assert result == {"phase": "running", "worker_id": "worker-2"}


@pytest.mark.anyio
async def test_resume_project_for_an_unknown_project_is_refused(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """resume_project surfaces an unknown project id as an identical ToolError."""
    stub = make_stub_coordinator(
        resume_error=CoordinatorError("no project has the id 'nosuch'"),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.resume_project(project_id="nosuch", prompt="carry on")

    assert str(excinfo.value) == "no project has the id 'nosuch'"


@pytest.mark.anyio
async def test_resume_project_surfaces_a_supervisor_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """resume_project surfaces a SupervisorError as an identical ToolError."""
    stub = make_stub_coordinator(
        resume_error=SupervisorError(
            "only a project in phase 'completed' or 'failed' can be resumed, "
            "got 'running'"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.resume_project(project_id="a1b2c3d4", prompt="carry on")

    assert str(excinfo.value) == (
        "only a project in phase 'completed' or 'failed' can be resumed, got 'running'"
    )


@pytest.mark.anyio
async def test_resume_project_surfaces_a_tmux_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """resume_project surfaces a TmuxError as an identical ToolError."""
    stub = make_stub_coordinator(resume_error=TmuxError("no such session"))
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.resume_project(project_id="a1b2c3d4", prompt="carry on")

    assert str(excinfo.value) == "no such session"


@pytest.mark.anyio
async def test_close_project_delegates_the_project_id(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """close_project delegates the project id."""
    stub = make_stub_coordinator()
    tools = BatonTools(stub)

    await tools.close_project(project_id="a1b2c3d4")

    assert stub.close_calls == ["a1b2c3d4"]


@pytest.mark.anyio
async def test_close_project_returns_phase_and_worker_id_from_the_returned_state(
    make_stub_coordinator: type[StubCoordinator],
    make_stub_supervisor: type[StubSupervisor],
) -> None:
    """close_project reads its reply from the state the coordinator returned."""
    state = ProjectState.new("a1b2c3d4", "Widget factory").updated(
        phase=ProjectPhase.closed
    )
    stub = make_stub_coordinator(supervisor=make_stub_supervisor(state=state))
    tools = BatonTools(stub)

    result = await tools.close_project(project_id="a1b2c3d4")

    assert result == {"phase": "closed", "worker_id": None}


@pytest.mark.anyio
async def test_close_project_for_an_unknown_project_is_refused(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """close_project surfaces an unknown project id as an identical ToolError."""
    stub = make_stub_coordinator(
        close_error=CoordinatorError("no project has the id 'nosuch'"),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.close_project(project_id="nosuch")

    assert str(excinfo.value) == "no project has the id 'nosuch'"


@pytest.mark.anyio
async def test_close_project_surfaces_a_supervisor_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """close_project surfaces a SupervisorError as an identical ToolError."""
    stub = make_stub_coordinator(
        close_error=SupervisorError(
            "the project is in phase 'terminating' and is finishing with its "
            "current worker; close it again once that finishes"
        ),
    )
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.close_project(project_id="a1b2c3d4")

    assert str(excinfo.value) == (
        "the project is in phase 'terminating' and is finishing with its "
        "current worker; close it again once that finishes"
    )


@pytest.mark.anyio
async def test_close_project_surfaces_a_tmux_error_as_a_tool_error(
    make_stub_coordinator: type[StubCoordinator],
) -> None:
    """close_project surfaces a TmuxError as an identical ToolError."""
    stub = make_stub_coordinator(close_error=TmuxError("kill-session failed"))
    tools = BatonTools(stub)

    with pytest.raises(ToolError) as excinfo:
        await tools.close_project(project_id="a1b2c3d4")

    assert str(excinfo.value) == "kill-session failed"
