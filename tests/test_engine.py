"""Tests for baton.engine."""

import asyncio
import logging
import re
import signal
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from baton.config import BatonConfig
from baton.engine import Supervisor, SupervisorError
from baton.models import (
    EventKind,
    LifecycleReport,
    LifecycleState,
    ProjectPhase,
    ProjectState,
    WorkerRecord,
)
from baton.prompts import RECONCILIATION_REQUEST
from baton.state import StateStore
from baton.tmux import PaneInfo, TmuxError
from tests.doubles import FakeLauncher, FakeTmux


@pytest.fixture
def supervisor(
    config: BatonConfig, store: StateStore, tmux: FakeTmux, launcher: FakeLauncher
) -> Supervisor:
    """Build a `Supervisor` wired to the default fake tmux and launcher.

    Args:
        config: The zero-wait config fixture.
        store: The state store fixture, sharing config's state directory.
        tmux: The default `FakeTmux` fixture.
        launcher: The default `FakeLauncher` fixture.

    Returns:
        A `Supervisor` built from the four fixtures.
    """
    return Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)


def _persisted_and_live(
    config: BatonConfig, supervisor: Supervisor
) -> tuple[ProjectState, ProjectState]:
    """Read the supervisor's in-memory state and a freshly loaded copy.

    Args:
        config: The config naming the state directory to reload from.
        supervisor: The supervisor whose in-memory state is read.

    Returns:
        A tuple of `(supervisor.snapshot(), StateStore(...).load())`, so a
        test can assert both agree without loading twice itself.
    """
    return supervisor.snapshot(), StateStore(config.state_dir).load()


def _persist_project(
    config: BatonConfig,
    store: StateStore,
    project_dir: Path,
    *,
    phase: ProjectPhase,
    last_report: LifecycleReport | None = None,
) -> WorkerRecord:
    """Save a persisted ProjectState with a live worker, for a Supervisor built after.

    A test that needs a persisted state builds it here, before the
    Supervisor under test is constructed, since Supervisor loads its
    state at construction rather than on demand.

    Args:
        config: The config naming the state directory the worker's
            prompt path sits under.
        store: The state store to save the state to.
        project_dir: The project directory the persisted state points
            to; its resolved form is what is saved.
        phase: The phase the persisted state is saved in.
        last_report: The last report the persisted state carries, or
            None.

    Returns:
        The WorkerRecord the persisted state carries as its current
        worker.
    """
    worker = WorkerRecord(
        worker_id="worker-1",
        prompt_path=config.state_dir / "workers" / "worker-1" / "prompt.md",
        launched_at=datetime.now(UTC),
        pane_pid=111,
    )
    store.save(
        ProjectState.fresh().updated(
            phase=phase,
            project_path=project_dir.resolve(),
            session_name="baton-project",
            pane_target="baton-project:worker.0",
            worker=worker,
            model="sonnet",
            last_report=last_report,
        )
    )
    return worker


def test_supervisor_loads_persisted_state_at_construction(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    tmp_path: Path,
) -> None:
    """The supervisor loads the persisted ProjectState at construction."""
    worker = WorkerRecord(
        worker_id="worker-1",
        prompt_path=config.state_dir / "workers" / "worker-1" / "prompt.md",
        launched_at=datetime.now(UTC),
        pane_pid=111,
    )
    state = ProjectState.fresh().updated(
        phase=ProjectPhase.running,
        project_path=tmp_path / "some-project",
        session_name="baton-some-project",
        pane_target="baton-some-project:worker.0",
        worker=worker,
    )
    store.save(state)

    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    assert supervisor.snapshot() == state


def test_recent_events_returns_the_stores_events_newest_last(
    config: BatonConfig, store: StateStore, tmux: FakeTmux, launcher: FakeLauncher
) -> None:
    """recent_events returns the store's events, oldest first, newest last."""
    store.append_event(EventKind.milestone, "worker-1", {"message": "first"})
    store.append_event(EventKind.milestone, "worker-1", {"message": "second"})
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    events = supervisor.recent_events(count=2)

    assert [event.payload["message"] for event in events] == ["first", "second"]


@pytest.mark.anyio
async def test_initialize_launches_the_first_worker_and_records_phase_running(
    config: BatonConfig,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
) -> None:
    """Initialize launches the first worker and records phase running."""
    resolved = project_dir.resolve()
    expected_session = "baton-project"
    expected_pane_target = "baton-project:worker.0"

    result = await supervisor.initialize(project_dir, "start here")

    for state in (result, *_persisted_and_live(config, supervisor)):
        assert state.phase == ProjectPhase.running
        assert state.project_path == resolved
        assert state.session_name == expected_session
        assert state.pane_target == expected_pane_target
        assert state.worker is not None
        assert state.worker.worker_id == "worker-1"

    assert (config.state_dir / "mcp.json").exists()
    assert launcher.installs == [resolved]
    assert tmux.created == [{"session": expected_session, "start_dir": resolved}]
    assert launcher.launches[0] == {
        "project_path": resolved,
        "pane_target": expected_pane_target,
        "prompt": "start here",
        "model": "sonnet",
    }


@pytest.mark.anyio
async def test_initialize_appends_launch_then_phase_events(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Initialize appends a launch event, then a phase event, in that order."""
    result = await supervisor.initialize(project_dir, "start here")

    events = supervisor.recent_events(count=10)
    assert [event.kind for event in events] == [EventKind.launch, EventKind.phase]

    launch_event, phase_event = events
    assert launch_event.worker_id == result.worker.worker_id
    assert launch_event.payload == {
        "prompt_path": str(result.worker.prompt_path),
        "pane_target": result.pane_target,
    }
    assert phase_event.worker_id is None
    assert phase_event.payload == {
        "from": ProjectPhase.uninitialized.value,
        "to": ProjectPhase.running.value,
    }


@pytest.mark.anyio
async def test_initialize_with_explicit_session_name_overrides_the_default(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """An explicit session_name overrides the default; the pane target follows it."""
    result = await supervisor.initialize(
        project_dir, "start here", session_name="custom-session"
    )

    assert result.session_name == "custom-session"
    assert result.pane_target == "custom-session:worker.0"


@pytest.mark.anyio
async def test_initialize_does_not_create_an_already_existing_session(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """An existing tmux session is not created again."""
    tmux = make_tmux(sessions=["baton-project"])
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.initialize(project_dir, "start here")

    assert tmux.created == []
    assert len(launcher.launches) == 1


@pytest.mark.anyio
async def test_initialize_is_refused_while_the_phase_is_running(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Initialize is refused while the phase is running, with the pinned message."""
    await supervisor.initialize(project_dir, "start here")

    expected = (
        f"cannot initialize while the project at {project_dir.resolve()} is 'running'"
    )
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(project_dir, "start again")


@pytest.mark.anyio
async def test_initialize_is_refused_while_the_phase_is_blocked(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Initialize is refused while the phase is blocked, with the pinned message."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.blocked, message="stuck"
    )

    expected = (
        f"cannot initialize while the project at {project_dir.resolve()} is 'blocked'"
    )
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(project_dir, "start again")


@pytest.mark.anyio
async def test_initialize_is_refused_while_the_phase_is_terminating(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Initialize is refused while the phase is terminating, with the pinned message."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )

    expected = (
        f"cannot initialize while the project at {project_dir.resolve()} "
        "is 'terminating'"
    )
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(project_dir, "start again")

    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_initialize_is_refused_for_a_path_that_is_not_a_directory(
    supervisor: Supervisor, tmp_path: Path
) -> None:
    """Initialize refuses a path that is not an existing directory."""
    missing = tmp_path / "does-not-exist"

    expected = (
        f"project path must be an existing directory, got {str(missing.resolve())!r}"
    )
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(missing, "start here")


@pytest.mark.anyio
@pytest.mark.parametrize("initial_prompt", ["", "  \n\t "])
async def test_initialize_is_refused_for_a_blank_initial_prompt(
    config: BatonConfig,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
    initial_prompt: str,
) -> None:
    """A blank initial prompt is refused before anything is written."""
    expected = f"initial prompt must not be blank, got {initial_prompt!r}"

    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(project_dir, initial_prompt)

    assert supervisor.snapshot().phase == ProjectPhase.uninitialized
    assert launcher.installs == []
    assert not (config.state_dir / "mcp.json").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("session_name", ["", "  \n\t "])
async def test_initialize_is_refused_for_a_blank_session_name(
    config: BatonConfig,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
    session_name: str,
) -> None:
    """A session name that is given but blank is refused before anything is written."""
    expected = f"session name must not be blank, got {session_name!r}"

    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(
            project_dir, "start here", session_name=session_name
        )

    assert supervisor.snapshot().phase == ProjectPhase.uninitialized
    assert launcher.installs == []
    assert not (config.state_dir / "mcp.json").exists()


@pytest.mark.anyio
async def test_initialize_with_explicit_model_reaches_the_state_and_the_launcher(
    config: BatonConfig,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
) -> None:
    """An explicit model is recorded in the state and reaches the launcher."""
    result = await supervisor.initialize(project_dir, "start here", model="opus")

    assert result.model == "opus"
    assert StateStore(config.state_dir).load().model == "opus"
    assert launcher.launches[0]["model"] == "opus"


@pytest.mark.anyio
async def test_initialize_with_a_padded_model_argument_stores_it_stripped(
    config: BatonConfig,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
) -> None:
    """A padded model argument is stored, and reaches the launcher, stripped."""
    result = await supervisor.initialize(project_dir, "start here", model="  opus  ")

    assert result.model == "opus"
    assert StateStore(config.state_dir).load().model == "opus"
    assert launcher.launches[0]["model"] == "opus"


@pytest.mark.anyio
async def test_initialize_with_no_model_argument_uses_the_configurations_model(
    config: BatonConfig,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
) -> None:
    """With no model argument, the configuration's model is used."""
    result = await supervisor.initialize(project_dir, "start here")

    assert result.model == config.model
    assert launcher.launches[0]["model"] == config.model


@pytest.mark.anyio
async def test_initialize_model_argument_outranks_the_configurations_model(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A model argument outranks the configuration's model."""
    supervisor = Supervisor(
        config=replace(config, model="haiku"), store=store, tmux=tmux, launcher=launcher
    )

    result = await supervisor.initialize(project_dir, "start here", model="opus")

    assert result.model == "opus"
    assert launcher.launches[0]["model"] == "opus"


@pytest.mark.anyio
async def test_initialize_with_no_model_source_is_refused(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """No model argument and no configured model is refused with the pinned message."""
    supervisor = Supervisor(
        config=replace(config, model=None), store=store, tmux=tmux, launcher=launcher
    )

    expected = "no model chosen: pass model to initialize_project or set BATON_MODEL"
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(project_dir, "start here")

    assert supervisor.snapshot().phase == ProjectPhase.uninitialized
    assert launcher.installs == []
    assert not (config.state_dir / "mcp.json").exists()


@pytest.mark.anyio
@pytest.mark.parametrize("model", ["", "  \n\t "])
async def test_initialize_is_refused_for_a_blank_model_argument(
    config: BatonConfig,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
    model: str,
) -> None:
    """A blank model argument is refused before anything is written."""
    expected = f"model must not be blank, got {model!r}"

    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(project_dir, "start here", model=model)

    assert supervisor.snapshot().phase == ProjectPhase.uninitialized
    assert launcher.installs == []
    assert not (config.state_dir / "mcp.json").exists()


@pytest.mark.anyio
async def test_success_handoff_launches_on_the_states_model_not_the_configurations(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A success handoff launches on the state's model, not the configuration's."""
    supervisor = Supervisor(
        config=replace(config, model="haiku"), store=store, tmux=tmux, launcher=launcher
    )
    result = await supervisor.initialize(project_dir, "start here", model="opus")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
    )
    await supervisor.wait_for_finish()

    assert launcher.launches[1]["model"] == "opus"


@pytest.mark.anyio
async def test_initialize_after_completed_starts_a_new_project(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
    tmp_path: Path,
) -> None:
    """Initialize after completed starts a new project, clearing last_report."""
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    first = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        first.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()
    assert supervisor.snapshot().phase == ProjectPhase.completed

    second_project = tmp_path / "second-project"
    second_project.mkdir()

    result = await supervisor.initialize(second_project, "start again")

    assert result.project_path == second_project.resolve()
    assert result.last_report is None


@pytest.mark.anyio
async def test_tmux_error_from_create_session_propagates_and_leaves_no_state(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A TmuxError from create_session propagates, leaving no state.json."""
    tmux = make_tmux(create_error=TmuxError("boom"))
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    with pytest.raises(TmuxError) as exc_info:
        await supervisor.initialize(project_dir, "start here")

    assert str(exc_info.value) == "boom"
    assert supervisor.snapshot().phase == ProjectPhase.uninitialized
    assert not store.state_path.exists()


@pytest.mark.anyio
async def test_tmux_error_from_launch_propagates_and_leaves_no_state(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    make_launcher: Callable[..., FakeLauncher],
    project_dir: Path,
) -> None:
    """A TmuxError from the launcher's launch propagates, leaving no state.json."""
    launcher = make_launcher(launch_errors=[TmuxError("no claude")])
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    with pytest.raises(TmuxError) as exc_info:
        await supervisor.initialize(project_dir, "start here")

    assert str(exc_info.value) == "no claude"
    assert supervisor.snapshot().phase == ProjectPhase.uninitialized
    assert not store.state_path.exists()


@pytest.mark.anyio
async def test_record_status_appends_a_milestone_event_without_changing_state(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """record_status appends a milestone event and leaves the state untouched."""
    result = await supervisor.initialize(project_dir, "start here")
    before = supervisor.snapshot()

    await supervisor.record_status(result.worker.worker_id, "halfway done")

    assert supervisor.snapshot() == before
    events = supervisor.recent_events(count=10)
    assert events[-1].kind == EventKind.milestone
    assert events[-1].worker_id == result.worker.worker_id
    assert events[-1].payload == {"message": "halfway done"}


@pytest.mark.anyio
async def test_record_status_from_an_unknown_worker_is_refused(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """record_status from an id that is not the current worker is refused."""
    await supervisor.initialize(project_dir, "start here")
    events_before = supervisor.recent_events(count=10)

    expected = (
        "worker 'nope' is not the current worker; the current worker is 'worker-1'"
    )
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.record_status("nope", "halfway done")

    assert supervisor.recent_events(count=10) == events_before


@pytest.mark.anyio
async def test_record_status_when_no_worker_is_running_is_refused(
    supervisor: Supervisor,
) -> None:
    """record_status before any worker has launched names no worker as current."""
    expected = "worker 'nope' is not the current worker; no worker is running"
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.record_status("nope", "halfway done")


@pytest.mark.anyio
async def test_running_report_updates_last_report_without_a_phase_transition(
    config: BatonConfig, supervisor: Supervisor, project_dir: Path
) -> None:
    """A running report is recorded but makes no phase transition."""
    result = await supervisor.initialize(project_dir, "start here")
    events_before = supervisor.recent_events(count=10)

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.running, message="working"
    )
    await supervisor.wait_for_finish()

    expected_report = LifecycleReport(state=LifecycleState.running, message="working")
    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.running
        assert state.last_report == expected_report

    events_after = supervisor.recent_events(count=10)
    assert len(events_after) == len(events_before) + 1
    assert events_after[-1].kind == EventKind.lifecycle


@pytest.mark.anyio
async def test_blocked_report_sets_phase_blocked_and_keeps_the_worker(
    config: BatonConfig, supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
) -> None:
    """A blocked report sets phase blocked, keeps the worker, signals nothing."""
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.blocked, message="waiting on input"
    )
    await supervisor.wait_for_finish()

    expected_report = LifecycleReport(
        state=LifecycleState.blocked, message="waiting on input"
    )
    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.blocked
        assert state.worker == result.worker
        assert state.last_report == expected_report
    assert tmux.signals == []


@pytest.mark.anyio
async def test_blocked_worker_reporting_running_commits_phase_back_to_running(
    config: BatonConfig, supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
) -> None:
    """A running report from a blocked project commits the phase back to running."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.blocked, message="waiting on input"
    )

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.running, message="unblocked"
    )
    await supervisor.wait_for_finish()

    expected_report = LifecycleReport(state=LifecycleState.running, message="unblocked")
    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.running
        assert state.worker == result.worker
        assert state.last_report == expected_report

    events = supervisor.recent_events(count=20)
    assert [event.kind for event in events] == [
        EventKind.launch,
        EventKind.phase,
        EventKind.lifecycle,
        EventKind.phase,
        EventKind.lifecycle,
        EventKind.phase,
    ]
    phase_event = events[-1]
    assert phase_event.payload["from"] == ProjectPhase.blocked.value
    assert phase_event.payload["to"] == ProjectPhase.running.value
    assert tmux.signals == []


@pytest.mark.anyio
async def test_blocked_worker_can_later_report_success_like_any_worker(
    config: BatonConfig, supervisor: Supervisor, project_dir: Path
) -> None:
    """A blocked worker's later success report routes like any other report."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.blocked, message="waiting"
    )

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="unblocked",
        next_prompt="continue",
    )
    await supervisor.wait_for_finish()

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.running
        assert state.worker is not None
        assert state.worker.worker_id == "worker-2"


@pytest.mark.anyio
async def test_report_lifecycle_returns_before_the_finish_runs(
    supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
) -> None:
    """report_lifecycle returns while the finish is still pending."""
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="done"
    )

    assert supervisor.snapshot().phase == ProjectPhase.terminating
    assert tmux.signals == []

    await supervisor.wait_for_finish()

    assert supervisor.snapshot().phase == ProjectPhase.completed


@pytest.mark.anyio
async def test_success_report_terminates_and_launches_the_next_worker(
    config: BatonConfig,
    supervisor: Supervisor,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A success report launches the next worker with the report's next prompt."""
    result = await supervisor.initialize(project_dir, "start here")
    pane_target = result.pane_target

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
    )
    await supervisor.wait_for_finish()

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.running
        assert state.pane_target == pane_target
        assert state.worker is not None
        assert state.worker.worker_id == "worker-2"

    assert launcher.launches[1]["prompt"] == "phase two"
    assert launcher.launches[1]["pane_target"] == pane_target


@pytest.mark.anyio
async def test_success_report_produces_the_full_event_sequence_in_order(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """A success report produces the pinned event sequence, in order."""
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
    )
    await supervisor.wait_for_finish()

    events = supervisor.recent_events(count=20)
    assert [event.kind for event in events] == [
        EventKind.launch,
        EventKind.phase,
        EventKind.lifecycle,
        EventKind.phase,
        EventKind.terminate,
        EventKind.launch,
        EventKind.phase,
    ]
    phase_events = [event for event in events if event.kind == EventKind.phase]
    assert [(event.payload["from"], event.payload["to"]) for event in phase_events] == [
        (ProjectPhase.uninitialized.value, ProjectPhase.running.value),
        (ProjectPhase.running.value, ProjectPhase.terminating.value),
        (ProjectPhase.terminating.value, ProjectPhase.running.value),
    ]


@pytest.mark.anyio
async def test_completed_report_terminates_and_stops_with_no_worker(
    config: BatonConfig,
    supervisor: Supervisor,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A completed report terminates the worker and stops in phase completed."""
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.completed
        assert state.worker is None
    assert len(launcher.launches) == 1


@pytest.mark.anyio
async def test_failed_report_launches_a_diagnosis_worker_and_recovers(
    config: BatonConfig,
    store: StateStore,
    supervisor: Supervisor,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A failed report launches a diagnosis worker and moves to recovering."""
    result = await supervisor.initialize(project_dir, "start here")
    previous_worker = result.worker

    await supervisor.report_lifecycle(
        previous_worker.worker_id, LifecycleState.failed, message="it broke"
    )
    await supervisor.wait_for_finish()

    assert len(launcher.launches) == 2
    diagnosis_prompt = launcher.launches[1]["prompt"]
    assert "the worker reported failed: it broke" in diagnosis_prompt
    assert str(previous_worker.prompt_path) in diagnosis_prompt
    assert str(store.events_path) in diagnosis_prompt

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.recovering
        assert state.recovery_attempts == 1
        assert state.worker is not None
        assert state.worker.worker_id == "worker-2"


@pytest.mark.anyio
async def test_failed_report_produces_the_recovery_event_sequence_in_order(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """A failed report produces the pinned recovery event sequence, in order."""
    await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        supervisor.snapshot().worker.worker_id,
        LifecycleState.failed,
        message="it broke",
    )
    await supervisor.wait_for_finish()

    events = supervisor.recent_events(count=20)
    assert [event.kind for event in events] == [
        EventKind.launch,
        EventKind.phase,
        EventKind.lifecycle,
        EventKind.phase,
        EventKind.terminate,
        EventKind.launch,
        EventKind.phase,
    ]
    phase_events = [event for event in events if event.kind == EventKind.phase]
    assert [(event.payload["from"], event.payload["to"]) for event in phase_events] == [
        (ProjectPhase.uninitialized.value, ProjectPhase.running.value),
        (ProjectPhase.running.value, ProjectPhase.terminating.value),
        (ProjectPhase.terminating.value, ProjectPhase.recovering.value),
    ]
    assert events[-1].payload["reason"] == "the worker reported failed: it broke"


@pytest.mark.anyio
async def test_a_diagnosis_workers_failed_report_launches_the_next_diagnosis_worker(
    config: BatonConfig,
    supervisor: Supervisor,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A diagnosis worker's failed report launches the next diagnosis worker."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.failed, message="it broke"
    )
    await supervisor.wait_for_finish()
    second_worker = supervisor.snapshot().worker

    await supervisor.report_lifecycle(
        second_worker.worker_id, LifecycleState.failed, message="it broke again"
    )
    await supervisor.wait_for_finish()

    assert len(launcher.launches) == 3
    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.recovering
        assert state.recovery_attempts == 2


@pytest.mark.anyio
async def test_the_recovery_cap_stops_diagnosis_and_moves_to_failed(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Three consecutive failed reports hit the cap and stop in phase failed."""
    capped_config = replace(config, recovery_cap=2)
    supervisor = Supervisor(
        config=capped_config, store=store, tmux=tmux, launcher=launcher
    )
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.failed, message="first failure"
    )
    await supervisor.wait_for_finish()
    second_worker = supervisor.snapshot().worker

    await supervisor.report_lifecycle(
        second_worker.worker_id, LifecycleState.failed, message="second failure"
    )
    await supervisor.wait_for_finish()
    third_worker = supervisor.snapshot().worker

    await supervisor.report_lifecycle(
        third_worker.worker_id, LifecycleState.failed, message="third failure"
    )
    await supervisor.wait_for_finish()

    assert len(launcher.launches) == 3
    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.failed
        assert state.worker is None

    phase_event = supervisor.recent_events(count=30)[-1]
    assert phase_event.payload["reason"] == (
        "recovery cap of 2 reached: the worker reported failed: third failure"
    )


@pytest.mark.anyio
async def test_a_diagnosis_workers_success_report_resets_recovery_attempts(
    config: BatonConfig,
    supervisor: Supervisor,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A diagnosis worker's success report resets recovery_attempts to zero."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.failed, message="it broke"
    )
    await supervisor.wait_for_finish()
    diagnosis_worker = supervisor.snapshot().worker

    await supervisor.report_lifecycle(
        diagnosis_worker.worker_id,
        LifecycleState.success,
        message="fixed it",
        next_prompt="continue the task",
    )
    await supervisor.wait_for_finish()

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.running
        assert state.recovery_attempts == 0
    assert len(launcher.launches) == 3
    assert launcher.launches[2]["prompt"] == "continue the task"


@pytest.mark.anyio
async def test_a_running_report_while_recovering_moves_nothing(
    config: BatonConfig, supervisor: Supervisor, project_dir: Path
) -> None:
    """A running report while recovering is recorded without a phase move."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.failed, message="it broke"
    )
    await supervisor.wait_for_finish()
    diagnosis_worker = supervisor.snapshot().worker

    await supervisor.report_lifecycle(
        diagnosis_worker.worker_id, LifecycleState.running, message="diagnosing"
    )
    await supervisor.wait_for_finish()

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.recovering
        assert state.recovery_attempts == 1
        assert state.last_report.state == LifecycleState.running


@pytest.mark.anyio
async def test_a_failed_relaunch_stops_in_failed_rather_than_stranding_the_project(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    make_launcher: Callable[..., FakeLauncher],
    project_dir: Path,
) -> None:
    """A launch that fails during the finish stops in failed, not in terminating."""
    launcher = make_launcher(launch_errors=[None, TmuxError("session gone")])
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
    )
    with pytest.raises(TmuxError, match="session gone"):
        await supervisor.wait_for_finish()

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.failed
        assert state.worker is None

    phase_event = supervisor.recent_events(count=20)[-1]
    assert phase_event.kind == EventKind.phase
    assert phase_event.payload["from"] == ProjectPhase.terminating.value
    assert phase_event.payload["to"] == ProjectPhase.failed.value
    assert "session gone" in str(phase_event.payload["reason"])


@pytest.mark.anyio
async def test_a_failed_handoff_is_logged_when_it_happens(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    make_launcher: Callable[..., FakeLauncher],
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A handoff that fails reaches the daemon's log, with its traceback."""
    launcher = make_launcher(launch_errors=[None, TmuxError("session gone")])
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    with caplog.at_level(logging.ERROR, logger="baton.engine"):
        await supervisor.report_lifecycle(
            result.worker.worker_id,
            LifecycleState.success,
            message="phase one done",
            next_prompt="phase two",
        )
        with pytest.raises(TmuxError, match="session gone"):
            await supervisor.wait_for_finish()

    records = [record for record in caplog.records if record.name == "baton.engine"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    assert records[0].getMessage() == "the handoff after a 'success' report failed"
    assert records[0].exc_info is not None


@pytest.mark.anyio
async def test_report_lifecycle_from_a_different_worker_is_refused(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """A report from an id that is not the current worker is refused."""
    result = await supervisor.initialize(project_dir, "start here")
    events_before = supervisor.recent_events(count=10)
    snapshot_before = supervisor.snapshot()

    expected = (
        "worker 'someone-else' is not the current worker; the current "
        f"worker is {result.worker.worker_id!r}"
    )
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.report_lifecycle(
            "someone-else", LifecycleState.running, message="hi"
        )

    assert supervisor.recent_events(count=10) == events_before
    assert supervisor.snapshot() == snapshot_before


@pytest.mark.anyio
async def test_second_report_while_terminating_is_refused(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """A second report while the phase is terminating is refused."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )

    expected = (
        f"worker {result.worker.worker_id!r} is terminating and baton "
        "accepts no further report from it"
    )
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.report_lifecycle(
            result.worker.worker_id, LifecycleState.running, message="still going"
        )


@pytest.mark.anyio
async def test_payload_violation_surfaces_as_supervisor_error(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """A payload violation surfaces LifecycleReport's own message unchanged."""
    result = await supervisor.initialize(project_dir, "start here")

    expected = "a 'success' report requires a next prompt, got None"
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.report_lifecycle(
            result.worker.worker_id, LifecycleState.success, message="done"
        )


@pytest.mark.anyio
async def test_terminating_an_already_dead_pane_signals_nothing(
    supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
) -> None:
    """A pane already dead is not signalled, even when a pid was recorded."""
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    assert tmux.signals == []
    terminate_event = next(
        event
        for event in supervisor.recent_events(count=10)
        if event.kind == EventKind.terminate
    )
    assert terminate_event.worker_id == result.worker.worker_id
    assert terminate_event.payload == {"pid": None, "signals": []}


@pytest.mark.anyio
async def test_terminating_a_live_pane_prefers_the_panes_own_pid(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """The live pane's pid outranks the one recorded at launch."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=99), PaneInfo(dead=True, pid=None)]
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    assert result.worker.pane_pid == 4242
    assert tmux.signals == [(99, signal.SIGTERM)]


@pytest.mark.anyio
async def test_terminating_falls_back_to_the_recorded_pid(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A live pane that reports no pid falls back to the recorded one."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=None), PaneInfo(dead=True, pid=None)]
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    assert tmux.signals == [(4242, signal.SIGTERM)]
    terminate_event = next(
        event
        for event in supervisor.recent_events(count=10)
        if event.kind == EventKind.terminate
    )
    assert terminate_event.payload == {"pid": 4242, "signals": ["SIGTERM"]}


@pytest.mark.anyio
async def test_terminating_a_live_pane_sends_sigterm_then_sigkill(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A pane that stays alive after SIGTERM is escalated to SIGKILL."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=4242)])
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    assert tmux.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    terminate_event = next(
        event
        for event in supervisor.recent_events(count=10)
        if event.kind == EventKind.terminate
    )
    assert terminate_event.payload == {
        "pid": 4242,
        "signals": ["SIGTERM", "SIGKILL"],
    }


@pytest.mark.anyio
async def test_process_lookup_error_from_signal_pane_does_not_stop_the_finish(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A ProcessLookupError from signal_pane is swallowed; the finish completes."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=4242)],
        kill_errors=[ProcessLookupError()],
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    assert supervisor.snapshot().phase == ProjectPhase.completed
    terminate_event = next(
        event
        for event in supervisor.recent_events(count=10)
        if event.kind == EventKind.terminate
    )
    assert terminate_event.payload == {"pid": 4242, "signals": []}


@pytest.mark.anyio
async def test_termination_polls_until_the_pane_dies_before_escalating(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A pane that dies while being polled is never escalated to SIGKILL."""
    patient = replace(config, termination_timeout=5)
    tmux = make_tmux(
        pane_infos=[
            PaneInfo(dead=False, pid=4242),
            PaneInfo(dead=False, pid=4242),
            PaneInfo(dead=True, pid=None),
        ]
    )
    supervisor = Supervisor(config=patient, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    assert tmux.signals == [(4242, signal.SIGTERM)]
    assert tmux.pane_info_calls == [result.pane_target] * 3


@pytest.mark.anyio
async def test_a_pane_that_exits_just_before_sigkill_is_not_an_error(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A worker that exits between the last poll and SIGKILL is not an error."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=4242)],
        kill_errors=[None, ProcessLookupError()],
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    assert tmux.signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert supervisor.snapshot().phase == ProjectPhase.completed


@pytest.mark.anyio
async def test_termination_signals_nothing_when_no_pid_is_known(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    make_launcher: Callable[..., FakeLauncher],
    project_dir: Path,
) -> None:
    """A live pane with no pid, and no recorded pid, is not signalled."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=None)])
    launcher = make_launcher(pane_pid=None)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    assert tmux.signals == []
    terminate_event = next(
        event
        for event in supervisor.recent_events(count=10)
        if event.kind == EventKind.terminate
    )
    assert terminate_event.payload == {"pid": None, "signals": []}


@pytest.mark.anyio
async def test_wait_for_finish_with_no_pending_finish_returns_immediately(
    supervisor: Supervisor,
) -> None:
    """wait_for_finish returns without error when no finish is pending."""
    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_check_worker_with_a_dead_pane_while_running_recovers(
    supervisor: Supervisor, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A dead pane found while running is logged as a vanish and recovered."""
    result = await supervisor.initialize(project_dir, "start here")
    first_worker = result.worker

    await supervisor.check_worker()

    vanish_event = next(
        event
        for event in supervisor.recent_events(count=10)
        if event.kind == EventKind.vanish
    )
    assert vanish_event.worker_id == first_worker.worker_id
    assert vanish_event.payload == {
        "pane_target": result.pane_target,
        "phase": "running",
    }
    assert len(launcher.launches) == 2
    assert (
        "the worker's pane died without a terminal report"
        in launcher.launches[1]["prompt"]
    )
    assert supervisor.snapshot().phase == ProjectPhase.recovering
    assert supervisor.snapshot().recovery_attempts == 1


@pytest.mark.anyio
async def test_check_worker_with_a_dead_pane_while_blocked_recovers(
    supervisor: Supervisor, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A dead pane found while blocked is logged as a vanish and recovered."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.blocked, message="stuck"
    )

    await supervisor.check_worker()

    vanish_event = next(
        event
        for event in supervisor.recent_events(count=10)
        if event.kind == EventKind.vanish
    )
    assert vanish_event.payload["phase"] == "blocked"
    assert supervisor.snapshot().phase == ProjectPhase.recovering
    assert len(launcher.launches) == 2


@pytest.mark.anyio
async def test_check_worker_with_a_live_pane_changes_nothing(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A live pane during check_worker changes nothing, but is read."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    result = await supervisor.initialize(project_dir, "start here")
    before = supervisor.snapshot()

    await supervisor.check_worker()

    assert supervisor.snapshot() == before
    assert len(launcher.launches) == 1
    assert tmux.pane_info_calls == [result.pane_target]


@pytest.mark.anyio
async def test_check_worker_with_no_worker_reads_no_pane(
    supervisor: Supervisor, tmux: FakeTmux
) -> None:
    """check_worker with no worker running changes nothing and reads no pane."""
    await supervisor.check_worker()

    assert tmux.pane_info_calls == []
    assert supervisor.snapshot().phase == ProjectPhase.uninitialized


@pytest.mark.anyio
async def test_check_worker_while_terminating_reads_no_pane(
    supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
) -> None:
    """check_worker while terminating changes nothing and reads no pane."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )

    await supervisor.check_worker()

    assert tmux.pane_info_calls == []
    assert supervisor.snapshot().phase == ProjectPhase.terminating

    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_check_worker_recovery_that_fails_to_launch_ends_in_failed(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    make_launcher: Callable[..., FakeLauncher],
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recovery launch that raises ends the project in failed, not raising."""
    launcher = make_launcher(launch_errors=[None, TmuxError("session gone")])
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    await supervisor.initialize(project_dir, "start here")

    with caplog.at_level(logging.ERROR, logger="baton.engine"):
        await supervisor.check_worker()

    records = [record for record in caplog.records if record.name == "baton.engine"]
    assert len(records) == 1
    assert records[0].getMessage() == (
        "recovery after the worker's pane died without a terminal report failed"
    )
    assert records[0].exc_info is not None
    assert supervisor.snapshot().phase == ProjectPhase.failed
    assert supervisor.snapshot().worker is None
    phase_event = supervisor.recent_events(count=20)[-1]
    assert phase_event.payload["reason"] == (
        "recovery after the worker's pane died without a terminal report "
        "failed: session gone"
    )


@pytest.mark.anyio
async def test_check_worker_recreates_the_session_if_it_vanished(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A recovery after the tmux session vanished creates it again."""
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    await supervisor.initialize(project_dir, "start here")
    tmux.sessions.clear()

    await supervisor.check_worker()

    assert len(tmux.created) == 2
    assert tmux.created[1] == {
        "session": "baton-project",
        "start_dir": project_dir.resolve(),
    }


@pytest.mark.anyio
async def test_reconcile_from_a_persisted_running_state_sends_the_request(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Reconcile from a persisted running state sends the request and commits."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    worker = _persist_project(config, store, project_dir, phase=ProjectPhase.running)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()

    assert tmux.send_keys_calls == [("baton-project:worker.0", RECONCILIATION_REQUEST)]
    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.reconciling

    events = supervisor.recent_events(count=10)
    assert [event.kind for event in events] == [EventKind.reconcile, EventKind.phase]
    assert events[0].worker_id == worker.worker_id
    assert events[0].payload == {"timeout": config.reconciliation_timeout}
    assert events[1].payload["reason"] == "the daemon restarted with a live worker"
    assert events[1].payload["from"] == "running"
    assert events[1].payload["to"] == "reconciling"


@pytest.mark.anyio
@pytest.mark.parametrize("phase", [ProjectPhase.blocked, ProjectPhase.recovering])
async def test_reconcile_from_blocked_or_recovering_sends_the_request(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
    phase: ProjectPhase,
) -> None:
    """Reconcile from a persisted blocked or recovering state sends the request too."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(config, store, project_dir, phase=phase)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()

    assert tmux.send_keys_calls == [("baton-project:worker.0", RECONCILIATION_REQUEST)]
    assert supervisor.snapshot().phase == ProjectPhase.reconciling


@pytest.mark.anyio
async def test_reconcile_from_a_persisted_reconciling_state_sends_the_request_again(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Reconcile from a persisted reconciling state sends the request again."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(config, store, project_dir, phase=ProjectPhase.reconciling)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()

    assert tmux.send_keys_calls == [("baton-project:worker.0", RECONCILIATION_REQUEST)]
    assert supervisor.snapshot().phase == ProjectPhase.reconciling
    events = supervisor.recent_events(count=10)
    assert [event.kind for event in events] == [EventKind.reconcile]


@pytest.mark.anyio
async def test_reconcile_with_a_dead_pane_sends_nothing_and_check_worker_recovers(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Reconcile with a dead pane sends nothing; the watchdog's first tick recovers."""
    _persist_project(config, store, project_dir, phase=ProjectPhase.running)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()

    assert tmux.send_keys_calls == []
    assert supervisor.recent_events(count=10) == []
    assert supervisor.snapshot().phase == ProjectPhase.running

    await supervisor.check_worker()

    assert [event.kind for event in supervisor.recent_events(count=10)] == [
        EventKind.vanish,
        EventKind.launch,
        EventKind.phase,
    ]
    assert len(launcher.launches) == 1
    assert supervisor.snapshot().phase == ProjectPhase.recovering


@pytest.mark.anyio
async def test_a_running_report_from_reconciling_commits_running(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A running report from reconciling commits phase running."""
    worker = _persist_project(
        config, store, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.report_lifecycle(
        worker.worker_id, LifecycleState.running, message="back"
    )

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.running


@pytest.mark.anyio
async def test_a_blocked_report_from_reconciling_commits_blocked(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A blocked report from reconciling commits phase blocked."""
    worker = _persist_project(
        config, store, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.report_lifecycle(
        worker.worker_id, LifecycleState.blocked, message="stuck"
    )

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.blocked


@pytest.mark.anyio
async def test_a_success_report_from_reconciling_terminates_and_launches_next(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A success report from reconciling terminates the worker and launches the next."""
    worker = _persist_project(
        config, store, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.report_lifecycle(
        worker.worker_id,
        LifecycleState.success,
        message="done",
        next_prompt="do the next thing",
    )

    assert supervisor.snapshot().phase == ProjectPhase.terminating

    await supervisor.wait_for_finish()

    assert launcher.launches[0]["prompt"] == "do the next thing"
    assert supervisor.snapshot().phase == ProjectPhase.running


@pytest.mark.anyio
async def test_a_completed_report_from_reconciling_terminates_and_stops(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A completed report from reconciling terminates the worker and stops."""
    worker = _persist_project(
        config, store, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.report_lifecycle(
        worker.worker_id, LifecycleState.completed, message="all done"
    )

    assert supervisor.snapshot().phase == ProjectPhase.terminating

    await supervisor.wait_for_finish()

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.completed
        assert state.worker is None
    assert launcher.launches == []


@pytest.mark.anyio
async def test_a_failed_report_from_reconciling_terminates_and_recovers(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A failed report from reconciling terminates the worker and recovers."""
    worker = _persist_project(
        config, store, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.report_lifecycle(
        worker.worker_id, LifecycleState.failed, message="it broke"
    )

    assert supervisor.snapshot().phase == ProjectPhase.terminating

    await supervisor.wait_for_finish()

    assert len(launcher.launches) == 1
    diagnosis_prompt = launcher.launches[0]["prompt"]
    assert "the worker reported failed: it broke" in diagnosis_prompt
    assert str(worker.prompt_path) in diagnosis_prompt
    assert str(store.events_path) in diagnosis_prompt

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.recovering
        assert state.recovery_attempts == 1
        assert state.worker is not None
        assert state.worker.worker_id == "worker-1"


@pytest.mark.anyio
async def test_check_worker_past_the_reconciliation_deadline_terminates_and_recovers(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """check_worker past the reconciliation deadline terminates and recovers."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(config, store, project_dir, phase=ProjectPhase.running)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    await supervisor.reconcile()

    await supervisor.check_worker()

    assert supervisor.snapshot().phase == ProjectPhase.terminating
    phase_event = supervisor.recent_events(count=10)[-1]
    assert phase_event.payload["reason"] == (
        "the worker did not answer the reconciliation request within 0 seconds"
    )

    await supervisor.wait_for_finish()

    assert signal.SIGTERM in [signum for _, signum in tmux.signals]
    assert any(
        event.kind == EventKind.terminate
        for event in supervisor.recent_events(count=10)
    )
    assert len(launcher.launches) == 1
    assert (
        "the worker did not answer the reconciliation request within 0 seconds"
        in launcher.launches[0]["prompt"]
    )
    assert supervisor.snapshot().phase == ProjectPhase.recovering


@pytest.mark.anyio
async def test_the_reconciliation_timeout_path_clears_last_report(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """The reconciliation timeout clears a last_report left over from a handoff."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    leftover = LifecycleReport(
        state=LifecycleState.success, message="done", next_prompt="do the next thing"
    )
    _persist_project(
        config, store, project_dir, phase=ProjectPhase.running, last_report=leftover
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    await supervisor.reconcile()

    await supervisor.check_worker()

    for state in _persisted_and_live(config, supervisor):
        assert state.last_report is None

    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_check_worker_with_a_generous_reconciliation_timeout_changes_nothing(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """check_worker with a generous reconciliation timeout changes nothing."""
    config = replace(config, reconciliation_timeout=3600)
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(config, store, project_dir, phase=ProjectPhase.running)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    await supervisor.reconcile()

    await supervisor.check_worker()

    assert supervisor.snapshot().phase == ProjectPhase.reconciling
    assert launcher.launches == []
    assert tmux.signals == []


@pytest.mark.anyio
async def test_a_milestone_during_reconciling_never_answers_the_request(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A milestone during reconciling never answers the reconciliation request."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    worker = _persist_project(config, store, project_dir, phase=ProjectPhase.running)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)
    await supervisor.reconcile()

    await supervisor.record_status(worker.worker_id, "still going")

    assert supervisor.snapshot().phase == ProjectPhase.reconciling
    events = supervisor.recent_events(count=10)
    assert events[-1].kind == EventKind.milestone

    await supervisor.check_worker()

    assert supervisor.snapshot().phase == ProjectPhase.terminating

    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_check_worker_on_a_reconciling_state_with_no_deadline_changes_nothing(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """check_worker on a reconciling state with no deadline set changes nothing."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(config, store, project_dir, phase=ProjectPhase.reconciling)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.check_worker()

    assert supervisor.snapshot().phase == ProjectPhase.reconciling
    assert launcher.launches == []
    assert tmux.signals == []


@pytest.mark.anyio
async def test_reconcile_in_terminating_with_a_terminal_last_report_resumes_the_finish(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Reconcile in terminating with a terminal last report resumes the finish."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    last_report = LifecycleReport(
        state=LifecycleState.success, message="done", next_prompt="do the next thing"
    )
    _persist_project(
        config,
        store,
        project_dir,
        phase=ProjectPhase.terminating,
        last_report=last_report,
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()
    await supervisor.wait_for_finish()

    assert len(launcher.launches) == 1
    assert launcher.launches[0]["prompt"] == "do the next thing"
    assert supervisor.snapshot().phase == ProjectPhase.running
    assert tmux.send_keys_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "last_report",
    [LifecycleReport(state=LifecycleState.running, message="working"), None],
)
async def test_reconcile_in_terminating_without_a_terminal_last_report_recovers(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
    last_report: LifecycleReport | None,
) -> None:
    """Reconcile in terminating with no terminal last report terminates and recovers."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(
        config,
        store,
        project_dir,
        phase=ProjectPhase.terminating,
        last_report=last_report,
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()
    await supervisor.wait_for_finish()

    assert len(launcher.launches) == 1
    assert (
        "the daemon restarted while terminating the worker"
        in launcher.launches[0]["prompt"]
    )
    assert supervisor.snapshot().phase == ProjectPhase.recovering
    assert supervisor.snapshot().recovery_attempts == 1
    assert any(
        event.kind == EventKind.terminate
        for event in supervisor.recent_events(count=10)
    )


@pytest.mark.anyio
@pytest.mark.parametrize("phase", [ProjectPhase.completed, ProjectPhase.failed])
async def test_reconcile_in_a_terminal_phase_sends_nothing(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
    phase: ProjectPhase,
) -> None:
    """Reconcile in completed or failed, with no worker on record, sends nothing."""
    store.save(
        ProjectState.fresh().updated(
            phase=phase, project_path=project_dir.resolve(), worker=None
        )
    )
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()

    assert tmux.send_keys_calls == []
    assert tmux.pane_info_calls == []
    assert supervisor.snapshot().phase == phase


@pytest.mark.anyio
async def test_reconcile_in_failed_with_a_worker_on_record_sends_nothing(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Reconcile in failed sends nothing: the phase decides, not a missing worker."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(config, store, project_dir, phase=ProjectPhase.failed)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()

    assert tmux.send_keys_calls == []
    assert tmux.pane_info_calls == []
    assert supervisor.snapshot().phase == ProjectPhase.failed


@pytest.mark.anyio
async def test_reconcile_on_a_fresh_uninitialized_supervisor_sends_nothing(
    supervisor: Supervisor, tmux: FakeTmux
) -> None:
    """Reconcile on a fresh, uninitialized supervisor sends nothing."""
    await supervisor.reconcile()

    assert tmux.send_keys_calls == []
    assert tmux.pane_info_calls == []
    assert supervisor.snapshot().phase == ProjectPhase.uninitialized


@pytest.mark.anyio
async def test_a_tmux_error_from_send_keys_propagates_out_of_reconcile(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A TmuxError from send_keys propagates out of reconcile, phase unmoved."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=1)],
        send_error=TmuxError("pane is gone"),
    )
    _persist_project(config, store, project_dir, phase=ProjectPhase.running)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    with pytest.raises(TmuxError):
        await supervisor.reconcile()

    assert supervisor.snapshot().phase == ProjectPhase.running


@pytest.mark.anyio
async def test_finish_abnormal_that_fails_to_launch_ends_in_failed(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    make_launcher: Callable[..., FakeLauncher],
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recovery launch inside the abnormal finish that fails ends in failed."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    launcher = make_launcher(launch_errors=[TmuxError("session gone")])
    _persist_project(config, store, project_dir, phase=ProjectPhase.terminating)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.reconcile()

    with (
        caplog.at_level(logging.ERROR, logger="baton.engine"),
        pytest.raises(TmuxError),
    ):
        await supervisor.wait_for_finish()

    assert supervisor.snapshot().phase == ProjectPhase.failed
    assert supervisor.snapshot().worker is None
    phase_event = supervisor.recent_events(count=20)[-1]
    assert phase_event.payload["reason"] == (
        "the abnormal finish after the daemon restarted while terminating "
        "the worker failed: session gone"
    )
    records = [record for record in caplog.records if record.name == "baton.engine"]
    assert len(records) == 1
    assert records[0].getMessage() == (
        "the abnormal finish after the daemon restarted while terminating "
        "the worker failed"
    )
    assert records[0].exc_info is not None


@pytest.mark.anyio
async def test_start_reconciles_then_runs_the_watchdog(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Start reconciles a live worker, then the watchdog's own tick polls the pane."""
    config = replace(config, reconciliation_timeout=3600)
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(config, store, project_dir, phase=ProjectPhase.running)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    await supervisor.start()
    for _ in range(3):
        await asyncio.sleep(0)
    await supervisor.shutdown()

    assert tmux.send_keys_calls == [("baton-project:worker.0", RECONCILIATION_REQUEST)]
    assert tmux.pane_info_calls[0] == "baton-project:worker.0"
    assert len(tmux.pane_info_calls) >= 2
    assert supervisor.snapshot().phase == ProjectPhase.reconciling


@pytest.mark.anyio
async def test_the_watchdog_survives_a_tick_that_raises(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tick that raises is logged and the watchdog goes on to the next one.

    check_worker swallows a failed recovery, but the pane read before it
    does not: a tmux binary that has gone missing raises OSError there. If
    that escaped, the loop would end for the daemon's whole life and
    shutdown would re-raise instead of draining. start runs before
    initialize here, so reconcile finds no worker yet and never touches
    the raising pane read itself; only the watchdog's later ticks do,
    once initialize gives the project a worker to poll.
    """
    tmux = make_tmux(pane_info_error=OSError("tmux is gone"))
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    with caplog.at_level(logging.ERROR, logger="baton.engine"):
        await supervisor.start()
        await supervisor.initialize(project_dir, "start here")
        for _ in range(6):
            await asyncio.sleep(0)
        await supervisor.shutdown()

    records = [record for record in caplog.records if record.name == "baton.engine"]
    assert len(records) >= 2
    assert records[0].getMessage() == "the watchdog tick failed"
    assert records[0].exc_info is not None
    assert supervisor.snapshot().phase == ProjectPhase.running


@pytest.mark.anyio
async def test_shutdown_drains_a_pending_finish(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Shutdown drains a finish left pending by a terminal report, with no start."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )

    await supervisor.shutdown()

    assert supervisor.snapshot().phase == ProjectPhase.completed


@pytest.mark.anyio
async def test_shutdown_without_start_returns(supervisor: Supervisor) -> None:
    """Shutdown with no start and no initialize returns without error."""
    await supervisor.shutdown()


@pytest.mark.anyio
async def test_initialize_is_refused_while_the_phase_is_recovering(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Initialize is refused while the phase is recovering, with the pinned message."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.failed, message="it broke"
    )
    await supervisor.wait_for_finish()

    expected = (
        f"cannot initialize while the project at {project_dir.resolve()} "
        "is 'recovering'"
    )
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(project_dir, "start again")


@pytest.mark.anyio
async def test_initialize_is_refused_while_the_phase_is_reconciling(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Initialize is refused while the phase is reconciling, with the pinned message."""
    resolved = project_dir.resolve()
    _persist_project(config, store, project_dir, phase=ProjectPhase.reconciling)
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    expected = f"cannot initialize while the project at {resolved} is 'reconciling'"
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.initialize(project_dir, "start again")
