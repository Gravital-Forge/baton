"""Tests for baton.engine."""

import asyncio
import logging
import re
import signal
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from baton.config import BatonConfig
from baton.engine import Supervisor, SupervisorError, _sleep_until
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

TITLE = "Widget factory"
SESSION_NAME = "baton-project"
PANE_TARGET = "baton-project:worker.0"


def _new_state(project_id: str) -> ProjectState:
    """Build the state a coordinator hands a supervisor before initialize.

    Args:
        project_id: The id the coordinator minted for the project.

    Returns:
        An uninitialized ProjectState carrying the project's id, title,
        and resolved session name.
    """
    return ProjectState.new(project_id, TITLE).updated(session_name=SESSION_NAME)


@pytest.fixture
def supervisor(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
) -> Supervisor:
    """Build a `Supervisor` over a minted state and the default fakes.

    Args:
        config: The zero-wait config fixture.
        store: The state store fixture, over the project's own directory.
        tmux: The default `FakeTmux` fixture.
        launcher: The default `FakeLauncher` fixture.
        project_id: The id the project's state carries.

    Returns:
        A `Supervisor` over a project a coordinator has minted but not
        yet initialized.
    """
    return Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )


def _persisted_and_live(
    store: StateStore, supervisor: Supervisor
) -> tuple[ProjectState, ProjectState]:
    """Read the supervisor's in-memory state and a freshly loaded copy.

    Args:
        store: The store naming the project directory to reload from.
        supervisor: The supervisor whose in-memory state is read.

    Returns:
        A tuple of `(supervisor.snapshot(), StateStore(...).load())`, so a
        test can assert both agree without loading twice itself.
    """
    return supervisor.snapshot(), StateStore(store.project_state_dir).load()


def _persist_project(
    store: StateStore,
    project_id: str,
    project_dir: Path,
    *,
    phase: ProjectPhase,
    last_report: LifecycleReport | None = None,
) -> WorkerRecord:
    """Save a persisted ProjectState with a live worker, for a Supervisor built after.

    Args:
        store: The store to save the state to.
        project_id: The id the persisted state carries.
        project_dir: The project directory the persisted state points to;
            its resolved form is what is saved.
        phase: The phase the persisted state is saved in.
        last_report: The last report the persisted state carries, or
            None.

    Returns:
        The WorkerRecord the persisted state carries as its current
        worker.
    """
    worker = WorkerRecord(
        worker_id="worker-1",
        prompt_path=store.project_state_dir / "workers" / "worker-1" / "prompt.md",
        launched_at=datetime.now(UTC),
        pane_pid=111,
    )
    store.save(
        ProjectState.new(project_id, TITLE).updated(
            phase=phase,
            project_path=project_dir.resolve(),
            session_name=SESSION_NAME,
            pane_target=PANE_TARGET,
            worker=worker,
            model="sonnet",
            last_report=last_report,
        )
    )
    return worker


def _persisted_supervisor(
    config: BatonConfig, store: StateStore, tmux: FakeTmux, launcher: FakeLauncher
) -> Supervisor:
    """Build a `Supervisor` over the state its store already holds.

    Args:
        config: The config governing waits and paths.
        store: The store holding the persisted state to load.
        tmux: The tmux double the supervisor works through.
        launcher: The launcher double the supervisor works through.

    Returns:
        A `Supervisor` over the state loaded from the store.
    """
    return Supervisor(
        config=config, store=store, tmux=tmux, launcher=launcher, state=store.load()
    )


class _FakeHold:
    """A stand-in for the engine's `_sleep_until`, released by hand.

    Patched over the module function a hold sleeps through, so a test
    drives the release itself and waits on no real clock.
    """

    def __init__(self) -> None:
        """Start with no deadline seen, not yet entered, and not released."""
        self.deadlines: list[datetime] = []
        self._entered = asyncio.Event()
        self._released = asyncio.Event()

    async def sleep_until(self, deadline: datetime) -> None:
        """Record the deadline and wait there until the test releases it.

        Args:
            deadline: The moment the supervisor asked to sleep until.
        """
        self.deadlines.append(deadline)
        self._entered.set()
        await self._released.wait()

    def release(self) -> None:
        """Let a supervisor waiting in the hold carry on."""
        self._released.set()

    async def wait_until_held(self) -> None:
        """Wait for a supervisor to reach the hold.

        Raises:
            TimeoutError: If no supervisor reaches the hold within a
                second. The suite sets no timeout of its own, so a hold
                that is never reached would otherwise hang the run.
        """
        await asyncio.wait_for(self._entered.wait(), timeout=1)


def _hold_the_sleep(monkeypatch: pytest.MonkeyPatch) -> _FakeHold:
    """Patch the hold's sleep with a `_FakeHold` and hand it back.

    Args:
        monkeypatch: The fixture the patch is undone by.

    Returns:
        The `_FakeHold` now standing in for `baton.engine._sleep_until`.
    """
    hold = _FakeHold()
    monkeypatch.setattr("baton.engine._sleep_until", hold.sleep_until)
    return hold


def test_supervisor_holds_the_state_it_was_given(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
) -> None:
    """The supervisor's snapshot is the state it was constructed with."""
    state = _new_state(project_id).updated(phase=ProjectPhase.running)

    supervisor = Supervisor(
        config=config, store=store, tmux=tmux, launcher=launcher, state=state
    )

    assert supervisor.snapshot() == state


def test_recent_events_returns_the_stores_events_newest_last(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
) -> None:
    """recent_events returns the store's events, oldest first, newest last."""
    store.append_event(EventKind.milestone, "worker-1", {"message": "first"})
    store.append_event(EventKind.milestone, "worker-1", {"message": "second"})
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )

    events = supervisor.recent_events(count=2)

    assert [event.payload["message"] for event in events] == ["first", "second"]


@pytest.mark.anyio
async def test_initialize_launches_the_first_worker_and_records_phase_running(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_id: str,
    project_dir: Path,
) -> None:
    """Initialize launches the first worker and records phase running."""
    resolved = project_dir.resolve()
    expected_session = "baton-project"
    expected_pane_target = "baton-project:worker.0"

    result = await supervisor.initialize(project_dir, "start here")

    for state in (result, *_persisted_and_live(store, supervisor)):
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
        "project_id": project_id,
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
async def test_initialize_keeps_the_projects_id_and_title(
    store: StateStore, supervisor: Supervisor, project_dir: Path, project_id: str
) -> None:
    """Initialize leaves the project's minted id and title untouched."""
    await supervisor.initialize(project_dir, "start here")

    for state in _persisted_and_live(store, supervisor):
        assert state.project_id == project_id
        assert state.title == TITLE


@pytest.mark.anyio
async def test_initialize_launches_into_the_session_name_on_the_state(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """Initialize launches into the session name its state already carries."""
    state = ProjectState.new(project_id, TITLE).updated(session_name="chosen-session")
    supervisor = Supervisor(
        config=config, store=store, tmux=tmux, launcher=launcher, state=state
    )

    result = await supervisor.initialize(project_dir, "start here")

    assert result.session_name == "chosen-session"
    assert result.pane_target == "chosen-session:worker.0"
    assert tmux.created == [
        {"session": "chosen-session", "start_dir": project_dir.resolve()}
    ]


@pytest.mark.anyio
async def test_initialize_does_not_create_an_already_existing_session(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """An existing tmux session is not created again."""
    tmux = make_tmux(sessions=["baton-project"])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )

    await supervisor.initialize(project_dir, "start here")

    assert tmux.created == []
    assert len(launcher.launches) == 1


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
async def test_initialize_with_explicit_model_reaches_the_state_and_the_launcher(
    store: StateStore,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
) -> None:
    """An explicit model is recorded in the state and reaches the launcher."""
    result = await supervisor.initialize(project_dir, "start here", model="opus")

    assert result.model == "opus"
    assert StateStore(store.project_state_dir).load().model == "opus"
    assert launcher.launches[0]["model"] == "opus"


@pytest.mark.anyio
async def test_initialize_with_a_padded_model_argument_stores_it_stripped(
    store: StateStore,
    launcher: FakeLauncher,
    supervisor: Supervisor,
    project_dir: Path,
) -> None:
    """A padded model argument is stored, and reaches the launcher, stripped."""
    result = await supervisor.initialize(project_dir, "start here", model="  opus  ")

    assert result.model == "opus"
    assert StateStore(store.project_state_dir).load().model == "opus"
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A model argument outranks the configuration's model."""
    supervisor = Supervisor(
        config=replace(config, model="haiku"),
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
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
    project_id: str,
    project_dir: Path,
) -> None:
    """No model argument and no configured model is refused with the pinned message."""
    supervisor = Supervisor(
        config=replace(config, model=None),
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A success handoff launches on the state's model, not the configuration's."""
    supervisor = Supervisor(
        config=replace(config, model="haiku"),
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
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
async def test_tmux_error_from_create_session_propagates_and_leaves_no_state(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """A TmuxError from create_session propagates, leaving no state.json."""
    tmux = make_tmux(create_error=TmuxError("boom"))
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )

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
    project_id: str,
    project_dir: Path,
) -> None:
    """A TmuxError from the launcher's launch propagates, leaving no state.json."""
    launcher = make_launcher(launch_errors=[TmuxError("no claude")])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )

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
    store: StateStore, supervisor: Supervisor, project_dir: Path
) -> None:
    """A running report is recorded but makes no phase transition."""
    result = await supervisor.initialize(project_dir, "start here")
    events_before = supervisor.recent_events(count=10)

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.running, message="working"
    )
    await supervisor.wait_for_finish()

    expected_report = LifecycleReport(state=LifecycleState.running, message="working")
    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.running
        assert state.last_report == expected_report

    events_after = supervisor.recent_events(count=10)
    assert len(events_after) == len(events_before) + 1
    assert events_after[-1].kind == EventKind.lifecycle


@pytest.mark.anyio
async def test_blocked_report_sets_phase_blocked_and_keeps_the_worker(
    store: StateStore, supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
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
    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.blocked
        assert state.worker == result.worker
        assert state.last_report == expected_report
    assert tmux.signals == []


@pytest.mark.anyio
async def test_blocked_worker_reporting_running_commits_phase_back_to_running(
    store: StateStore, supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
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
    for state in _persisted_and_live(store, supervisor):
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
    store: StateStore, supervisor: Supervisor, project_dir: Path
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

    for state in _persisted_and_live(store, supervisor):
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
    store: StateStore,
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

    for state in _persisted_and_live(store, supervisor):
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
    store: StateStore,
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

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.completed
        assert state.worker is None
    assert len(launcher.launches) == 1


@pytest.mark.anyio
async def test_failed_report_launches_a_diagnosis_worker_and_recovers(
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

    for state in _persisted_and_live(store, supervisor):
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
    store: StateStore,
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
    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.recovering
        assert state.recovery_attempts == 2


@pytest.mark.anyio
async def test_the_recovery_cap_stops_diagnosis_and_moves_to_failed(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """Three consecutive failed reports hit the cap and stop in phase failed."""
    capped_config = replace(config, recovery_cap=2)
    supervisor = Supervisor(
        config=capped_config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
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
    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.failed
        assert state.worker is None

    phase_event = supervisor.recent_events(count=30)[-1]
    assert phase_event.payload["reason"] == (
        "recovery cap of 2 reached: the worker reported failed: third failure"
    )


@pytest.mark.anyio
async def test_a_diagnosis_workers_success_report_resets_recovery_attempts(
    store: StateStore,
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

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.running
        assert state.recovery_attempts == 0
    assert len(launcher.launches) == 3
    assert launcher.launches[2]["prompt"] == "continue the task"


@pytest.mark.anyio
async def test_a_running_report_while_recovering_moves_nothing(
    store: StateStore, supervisor: Supervisor, project_dir: Path
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

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.recovering
        assert state.recovery_attempts == 1
        assert state.last_report.state == LifecycleState.running


@pytest.mark.anyio
async def test_a_failed_relaunch_stops_in_failed_rather_than_stranding_the_project(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    make_launcher: Callable[..., FakeLauncher],
    project_id: str,
    project_dir: Path,
) -> None:
    """A launch that fails during the finish stops in failed, not in terminating."""
    launcher = make_launcher(launch_errors=[None, TmuxError("session gone")])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
    )
    with pytest.raises(TmuxError, match="session gone"):
        await supervisor.wait_for_finish()

    for state in _persisted_and_live(store, supervisor):
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
    project_id: str,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A handoff that fails reaches the daemon's log, with its traceback."""
    launcher = make_launcher(launch_errors=[None, TmuxError("session gone")])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
async def test_sleep_until_a_deadline_already_past_returns_at_once() -> None:
    """A deadline in the past is not slept on."""
    started = datetime.now(UTC)

    await _sleep_until(started - timedelta(seconds=30))

    assert datetime.now(UTC) - started < timedelta(seconds=1)


@pytest.mark.anyio
async def test_sleep_until_a_future_deadline_returns_after_it() -> None:
    """A deadline ahead is slept out before the call returns."""
    deadline = datetime.now(UTC) + timedelta(milliseconds=20)

    await _sleep_until(deadline)

    assert datetime.now(UTC) >= deadline


@pytest.mark.anyio
async def test_a_delayed_success_moves_through_waiting_to_running(
    store: StateStore,
    supervisor: Supervisor,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delayed success holds in waiting with no worker, then runs on release."""
    hold = _hold_the_sleep(monkeypatch)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await hold.wait_until_held()

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.waiting
        assert state.worker is None
        assert state.resume_at is not None
    assert hold.deadlines == [supervisor.snapshot().resume_at]

    hold.release()
    await supervisor.wait_for_finish()

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.running
        assert state.worker is not None
        assert state.worker.worker_id == "worker-2"
        assert state.resume_at is None


@pytest.mark.anyio
async def test_a_delayed_success_records_the_phase_move_into_waiting(
    supervisor: Supervisor,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The phase events run terminating, waiting, running, and name the delay."""
    hold = _hold_the_sleep(monkeypatch)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await hold.wait_until_held()
    deadline = supervisor.snapshot().resume_at
    hold.release()
    await supervisor.wait_for_finish()

    phase_events = [
        event
        for event in supervisor.recent_events(count=20)
        if event.kind == EventKind.phase
    ]
    assert [(event.payload["from"], event.payload["to"]) for event in phase_events] == [
        (ProjectPhase.uninitialized.value, ProjectPhase.running.value),
        (ProjectPhase.running.value, ProjectPhase.terminating.value),
        (ProjectPhase.terminating.value, ProjectPhase.waiting.value),
        (ProjectPhase.waiting.value, ProjectPhase.running.value),
    ]
    assert deadline is not None
    waiting_reason = str(phase_events[2].payload["reason"])
    assert "60 seconds" in waiting_reason
    assert deadline.isoformat() in waiting_reason


@pytest.mark.anyio
async def test_the_next_worker_launches_only_after_the_hold_is_released(
    supervisor: Supervisor,
    launcher: FakeLauncher,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No next worker is launched while the hold is still pending."""
    hold = _hold_the_sleep(monkeypatch)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await hold.wait_until_held()

    assert len(launcher.launches) == 1

    hold.release()
    await supervisor.wait_for_finish()

    assert len(launcher.launches) == 2
    assert launcher.launches[1]["prompt"] == "phase two"


@pytest.mark.anyio
@pytest.mark.parametrize("delay_seconds", [None, 0])
async def test_a_success_report_without_a_positive_delay_never_waits(
    supervisor: Supervisor,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    delay_seconds: int | None,
) -> None:
    """An omitted or zero delay hands off with no hold and no resume moment."""
    hold = _hold_the_sleep(monkeypatch)
    # Released up front, so a hold entered wrongly fails the assertion
    # below rather than hanging a suite that sets no timeout.
    hold.release()
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=delay_seconds,
    )
    await supervisor.wait_for_finish()

    assert hold.deadlines == []
    state = supervisor.snapshot()
    assert state.phase == ProjectPhase.running
    assert state.resume_at is None


@pytest.mark.anyio
async def test_a_delay_above_the_cap_is_refused_before_any_phase_moves(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """A delay past the configured maximum is refused, moving nothing."""
    config = replace(config, max_handoff_delay=60)
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    result = await supervisor.initialize(project_dir, "start here")
    snapshot_before = supervisor.snapshot()
    events_before = supervisor.recent_events(count=10)

    expected = "a delay of 61 seconds exceeds the maximum of 60 seconds"
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.report_lifecycle(
            result.worker.worker_id,
            LifecycleState.success,
            message="phase one done",
            next_prompt="phase two",
            delay_seconds=61,
        )

    assert supervisor.snapshot() == snapshot_before
    assert supervisor.recent_events(count=10) == events_before


@pytest.mark.anyio
async def test_a_delay_at_exactly_the_cap_is_accepted(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delay equal to the configured maximum holds rather than being refused."""
    hold = _hold_the_sleep(monkeypatch)
    hold.release()
    config = replace(config, max_handoff_delay=60)
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await supervisor.wait_for_finish()

    assert len(hold.deadlines) == 1
    assert supervisor.snapshot().phase == ProjectPhase.running


@pytest.mark.anyio
async def test_the_lifecycle_event_carries_the_delay(
    supervisor: Supervisor,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifecycle event records the delay the report asked for."""
    hold = _hold_the_sleep(monkeypatch)
    hold.release()
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await supervisor.wait_for_finish()

    lifecycle_event = next(
        event
        for event in supervisor.recent_events(count=20)
        if event.kind == EventKind.lifecycle
    )
    assert lifecycle_event.payload == {
        "state": "success",
        "message": "phase one done",
        "next_prompt": "phase two",
        "delay_seconds": 60,
    }


@pytest.mark.anyio
async def test_a_watchdog_tick_during_a_hold_recovers_nothing(
    supervisor: Supervisor,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog reads no pane during a hold, because there is no worker."""
    hold = _hold_the_sleep(monkeypatch)
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await hold.wait_until_held()
    pane_reads = len(tmux.pane_info_calls)

    await asyncio.wait_for(supervisor.check_worker(), timeout=1)

    assert len(tmux.pane_info_calls) == pane_reads
    assert supervisor.snapshot().phase == ProjectPhase.waiting
    assert len(launcher.launches) == 1

    hold.release()
    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_a_hold_holds_no_lock_across_the_wait(
    supervisor: Supervisor,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchdog tick still completes while a project waits out its hold.

    check_worker takes the same lock the finish does. A hold that kept
    that lock across its wait would block vanish detection for every
    other project for the length of the delay, so this call would never
    return.
    """
    hold = _hold_the_sleep(monkeypatch)
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await hold.wait_until_held()

    await asyncio.wait_for(supervisor.check_worker(), timeout=1)

    assert supervisor.snapshot().phase == ProjectPhase.waiting

    hold.release()
    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_a_report_arriving_during_a_hold_is_refused(
    supervisor: Supervisor,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hold has no current worker, so a report from the finished one is refused."""
    hold = _hold_the_sleep(monkeypatch)
    result = await supervisor.initialize(project_dir, "start here")
    worker_id = result.worker.worker_id
    await supervisor.report_lifecycle(
        worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await hold.wait_until_held()

    expected = f"worker {worker_id!r} is not the current worker; no worker is running"
    with pytest.raises(SupervisorError, match=re.escape(expected)):
        await supervisor.report_lifecycle(
            worker_id, LifecycleState.running, message="still here"
        )

    hold.release()
    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_a_finish_that_sees_a_shutdown_stops_in_waiting_without_holding(
    store: StateStore,
    supervisor: Supervisor,
    launcher: FakeLauncher,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown reaching a delayed finish leaves it in waiting, never sleeping."""
    hold = _hold_the_sleep(monkeypatch)
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    monkeypatch.setattr(supervisor, "_shutting_down", True)
    await supervisor.wait_for_finish()

    assert hold.deadlines == []
    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.waiting
        assert state.resume_at is not None
        assert state.worker is None
    assert len(launcher.launches) == 1


@pytest.mark.anyio
async def test_a_route_that_fails_after_a_hold_ends_in_failed_with_no_resume_at(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    make_launcher: Callable[..., FakeLauncher],
    project_id: str,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A launch that fails after the hold lands in failed with no deadline left."""
    hold = _hold_the_sleep(monkeypatch)
    launcher = make_launcher(launch_errors=[None, TmuxError("session gone")])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
        delay_seconds=60,
    )
    await hold.wait_until_held()
    hold.release()
    with pytest.raises(TmuxError, match="session gone"):
        await supervisor.wait_for_finish()

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.failed
        assert state.worker is None
        assert state.resume_at is None


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
    project_id: str,
    project_dir: Path,
) -> None:
    """The live pane's pid outranks the one recorded at launch."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=99), PaneInfo(dead=True, pid=None)]
    )
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A live pane that reports no pid falls back to the recorded one."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=None), PaneInfo(dead=True, pid=None)]
    )
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A pane that stays alive after SIGTERM is escalated to SIGKILL."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=4242)])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A ProcessLookupError from signal_pane is swallowed; the finish completes."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=4242)],
        signal_errors=[ProcessLookupError()],
    )
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
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
    supervisor = Supervisor(
        config=patient,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A worker that exits between the last poll and SIGKILL is not an error."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=4242)],
        signal_errors=[None, ProcessLookupError()],
    )
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A live pane with no pid, and no recorded pid, is not signalled."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=None)])
    launcher = make_launcher(pane_pid=None)
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A live pane during check_worker changes nothing, but is read."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recovery launch that raises ends the project in failed, not raising."""
    launcher = make_launcher(launch_errors=[None, TmuxError("session gone")])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A recovery after the tmux session vanished creates it again."""
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
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
    project_id: str,
    project_dir: Path,
) -> None:
    """Reconcile from a persisted running state sends the request and commits."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    worker = _persist_project(
        store, project_id, project_dir, phase=ProjectPhase.running
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

    await supervisor.reconcile()

    assert tmux.send_keys_calls == [("baton-project:worker.0", RECONCILIATION_REQUEST)]
    for state in _persisted_and_live(store, supervisor):
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
    project_id: str,
    project_dir: Path,
    phase: ProjectPhase,
) -> None:
    """Reconcile from a persisted blocked or recovering state sends the request too."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(store, project_id, project_dir, phase=phase)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

    await supervisor.reconcile()

    assert tmux.send_keys_calls == [("baton-project:worker.0", RECONCILIATION_REQUEST)]
    assert supervisor.snapshot().phase == ProjectPhase.reconciling


@pytest.mark.anyio
async def test_reconcile_from_a_persisted_reconciling_state_sends_the_request_again(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """Reconcile from a persisted reconciling state sends the request again."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(store, project_id, project_dir, phase=ProjectPhase.reconciling)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
    project_id: str,
    project_dir: Path,
) -> None:
    """Reconcile with a dead pane sends nothing; the watchdog's first tick recovers."""
    _persist_project(store, project_id, project_dir, phase=ProjectPhase.running)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
    project_id: str,
    project_dir: Path,
) -> None:
    """A running report from reconciling commits phase running."""
    worker = _persist_project(
        store, project_id, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

    await supervisor.report_lifecycle(
        worker.worker_id, LifecycleState.running, message="back"
    )

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.running


@pytest.mark.anyio
async def test_a_blocked_report_from_reconciling_commits_blocked(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """A blocked report from reconciling commits phase blocked."""
    worker = _persist_project(
        store, project_id, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

    await supervisor.report_lifecycle(
        worker.worker_id, LifecycleState.blocked, message="stuck"
    )

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.blocked


@pytest.mark.anyio
async def test_a_success_report_from_reconciling_terminates_and_launches_next(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """A success report from reconciling terminates the worker and launches the next."""
    worker = _persist_project(
        store, project_id, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
    project_id: str,
    project_dir: Path,
) -> None:
    """A completed report from reconciling terminates the worker and stops."""
    worker = _persist_project(
        store, project_id, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

    await supervisor.report_lifecycle(
        worker.worker_id, LifecycleState.completed, message="all done"
    )

    assert supervisor.snapshot().phase == ProjectPhase.terminating

    await supervisor.wait_for_finish()

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.completed
        assert state.worker is None
    assert launcher.launches == []


@pytest.mark.anyio
async def test_a_failed_report_from_reconciling_terminates_and_recovers(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """A failed report from reconciling terminates the worker and recovers."""
    worker = _persist_project(
        store, project_id, project_dir, phase=ProjectPhase.reconciling
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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

    for state in _persisted_and_live(store, supervisor):
        assert state.phase == ProjectPhase.recovering
        assert state.recovery_attempts == 1
        assert state.worker is not None
        # FakeLauncher's default pid (4242) differs from the persisted
        # worker's (111); an unchanged pid would mean recovery never
        # launched a replacement.
        assert state.worker.pane_pid != worker.pane_pid


@pytest.mark.anyio
async def test_check_worker_past_the_reconciliation_deadline_terminates_and_recovers(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """check_worker past the reconciliation deadline terminates and recovers."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(store, project_id, project_dir, phase=ProjectPhase.running)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)
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
    project_id: str,
    project_dir: Path,
) -> None:
    """The reconciliation timeout clears a last_report left over from a handoff."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    leftover = LifecycleReport(
        state=LifecycleState.success, message="done", next_prompt="do the next thing"
    )
    _persist_project(
        store,
        project_id,
        project_dir,
        phase=ProjectPhase.running,
        last_report=leftover,
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)
    await supervisor.reconcile()

    await supervisor.check_worker()

    for state in _persisted_and_live(store, supervisor):
        assert state.last_report is None

    await supervisor.wait_for_finish()


@pytest.mark.anyio
async def test_check_worker_with_a_generous_reconciliation_timeout_changes_nothing(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """check_worker with a generous reconciliation timeout changes nothing."""
    config = replace(config, reconciliation_timeout=3600)
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(store, project_id, project_dir, phase=ProjectPhase.running)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)
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
    project_id: str,
    project_dir: Path,
) -> None:
    """A milestone during reconciling never answers the reconciliation request."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    worker = _persist_project(
        store, project_id, project_dir, phase=ProjectPhase.running
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)
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
    project_id: str,
    project_dir: Path,
) -> None:
    """check_worker on a reconciling state with no deadline set changes nothing."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(store, project_id, project_dir, phase=ProjectPhase.reconciling)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
    project_id: str,
    project_dir: Path,
) -> None:
    """Reconcile in terminating with a terminal last report resumes the finish."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    last_report = LifecycleReport(
        state=LifecycleState.success, message="done", next_prompt="do the next thing"
    )
    _persist_project(
        store,
        project_id,
        project_dir,
        phase=ProjectPhase.terminating,
        last_report=last_report,
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
    project_id: str,
    project_dir: Path,
    last_report: LifecycleReport | None,
) -> None:
    """Reconcile in terminating with no terminal last report terminates and recovers."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(
        store,
        project_id,
        project_dir,
        phase=ProjectPhase.terminating,
        last_report=last_report,
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
    project_id: str,
    project_dir: Path,
    phase: ProjectPhase,
) -> None:
    """Reconcile in completed or failed, with no worker on record, sends nothing."""
    store.save(
        ProjectState.new(project_id, TITLE).updated(
            phase=phase, project_path=project_dir.resolve(), worker=None
        )
    )
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
    project_id: str,
    project_dir: Path,
) -> None:
    """Reconcile in failed sends nothing: the phase decides, not a missing worker."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    _persist_project(store, project_id, project_dir, phase=ProjectPhase.failed)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
    project_id: str,
    project_dir: Path,
) -> None:
    """A TmuxError from send_keys propagates out of reconcile, phase unmoved."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=1)],
        send_error=TmuxError("pane is gone"),
    )
    _persist_project(store, project_id, project_dir, phase=ProjectPhase.running)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

    with pytest.raises(TmuxError):
        await supervisor.reconcile()

    assert supervisor.snapshot().phase == ProjectPhase.running


@pytest.mark.anyio
async def test_finish_abnormal_that_fails_to_launch_ends_in_failed(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    make_launcher: Callable[..., FakeLauncher],
    project_id: str,
    project_dir: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A recovery launch inside the abnormal finish that fails ends in failed."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    launcher = make_launcher(launch_errors=[TmuxError("session gone")])
    _persist_project(store, project_id, project_dir, phase=ProjectPhase.terminating)
    supervisor = _persisted_supervisor(config, store, tmux, launcher)

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
async def test_shutdown_with_no_pending_finish_returns(supervisor: Supervisor) -> None:
    """Shutdown with nothing pending returns without error."""
    await supervisor.shutdown()


@pytest.mark.anyio
async def test_resume_launches_a_worker_on_a_completed_project(
    store: StateStore,
    supervisor: Supervisor,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """Resume launches a fresh worker on a project that completed."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    state = await supervisor.resume("carry on")

    for persisted in _persisted_and_live(store, supervisor):
        assert persisted.phase == ProjectPhase.running
        assert persisted.worker.worker_id == "worker-2"
    assert state.phase == ProjectPhase.running
    assert launcher.launches[1]["prompt"] == "carry on"
    assert launcher.launches[1]["pane_target"] == PANE_TARGET


@pytest.mark.anyio
async def test_resume_launches_a_worker_on_a_failed_project(
    supervisor: Supervisor, launcher: FakeLauncher, project_dir: Path
) -> None:
    """Resume launches a fresh worker on a project baton gave up on."""
    await supervisor.initialize(project_dir, "start here")
    await supervisor.fail("reconciliation at startup failed")

    state = await supervisor.resume("try again")

    assert state.phase == ProjectPhase.running
    assert launcher.launches[1]["prompt"] == "try again"


@pytest.mark.anyio
async def test_resume_keeps_the_projects_identity_and_event_log(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Resume keeps the project's id, title, session name, model and event log."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    state = await supervisor.resume("carry on")

    assert state.project_id == result.project_id
    assert state.title == TITLE
    assert state.session_name == SESSION_NAME
    assert state.model == result.model
    assert [event.kind for event in supervisor.recent_events(count=20)] == [
        EventKind.launch,
        EventKind.phase,
        EventKind.lifecycle,
        EventKind.phase,
        EventKind.terminate,
        EventKind.phase,
        EventKind.launch,
        EventKind.phase,
    ]


@pytest.mark.anyio
async def test_resume_clears_the_stopped_workers_report(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Resume clears the last report, so a stopped worker's outcome is not read back."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    state = await supervisor.resume("carry on")

    assert state.last_report is None


@pytest.mark.anyio
async def test_resume_resets_the_recovery_attempts(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """Resume resets the recovery attempts a failed project accumulated."""
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id).updated(
            phase=ProjectPhase.failed,
            project_path=project_dir.resolve(),
            pane_target=PANE_TARGET,
            model="sonnet",
            recovery_attempts=3,
        ),
    )

    state = await supervisor.resume("try again")

    assert state.recovery_attempts == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    "phase",
    [
        ProjectPhase.uninitialized,
        ProjectPhase.running,
        ProjectPhase.recovering,
        ProjectPhase.reconciling,
        ProjectPhase.blocked,
        ProjectPhase.terminating,
        ProjectPhase.closed,
    ],
)
async def test_resume_is_refused_in_every_other_phase(
    config: BatonConfig,
    store: StateStore,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_id: str,
    phase: ProjectPhase,
) -> None:
    """Resume is refused for a project that is not completed or failed."""
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id).updated(phase=phase),
    )

    with pytest.raises(SupervisorError) as excinfo:
        await supervisor.resume("carry on")

    assert phase.value in str(excinfo.value)
    assert launcher.launches == []


@pytest.mark.anyio
@pytest.mark.parametrize("prompt", ["", "  \n\t "])
async def test_resume_is_refused_for_a_blank_prompt(
    supervisor: Supervisor, launcher: FakeLauncher, project_dir: Path, prompt: str
) -> None:
    """Resume is refused for a blank prompt, before anything is launched."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    with pytest.raises(SupervisorError, match="prompt must not be blank"):
        await supervisor.resume(prompt)

    assert len(launcher.launches) == 1


@pytest.mark.anyio
async def test_close_terminates_the_live_worker_and_kills_the_session(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """Close terminates the running worker, kills its session, and retires it."""
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=999), PaneInfo(dead=True, pid=None)]
    )
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    await supervisor.initialize(project_dir, "start here")

    state = await supervisor.close()

    assert tmux.signals == [(999, signal.SIGTERM)]
    assert tmux.kill_session_calls == [SESSION_NAME]
    assert state.phase == ProjectPhase.closed
    for persisted in _persisted_and_live(store, supervisor):
        assert persisted.phase == ProjectPhase.closed
        assert persisted.worker is None


@pytest.mark.anyio
async def test_close_produces_the_retirement_event_sequence_in_order(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Close produces the pinned event sequence, saying why the worker was stopped."""
    await supervisor.initialize(project_dir, "start here")

    await supervisor.close()

    events = supervisor.recent_events(count=20)
    assert [event.kind for event in events] == [
        EventKind.launch,
        EventKind.phase,
        EventKind.phase,
        EventKind.terminate,
        EventKind.phase,
    ]
    phase_events = [event for event in events if event.kind == EventKind.phase]
    assert [(event.payload["from"], event.payload["to"]) for event in phase_events] == [
        (ProjectPhase.uninitialized.value, ProjectPhase.running.value),
        (ProjectPhase.running.value, ProjectPhase.terminating.value),
        (ProjectPhase.terminating.value, ProjectPhase.closed.value),
    ]
    assert phase_events[1].payload["reason"] == "the project was closed"


@pytest.mark.anyio
async def test_close_waits_the_grace_period_out_before_terminating(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """Through the grace period the project is terminating, its worker unsignalled."""
    config = replace(config, grace_period=60)
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=999)])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    await supervisor.initialize(project_dir, "start here")

    closing = asyncio.create_task(supervisor.close())
    await asyncio.sleep(0)

    assert supervisor.snapshot().phase == ProjectPhase.terminating
    assert tmux.signals == []

    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing


@pytest.mark.anyio
async def test_close_holds_no_lock_across_the_grace_period(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """A watchdog tick still completes while a close waits out the grace period.

    check_worker takes the same lock the close does. A close holding that
    lock across the grace period and the termination poll would block
    vanish detection for every other project for as long as both take, so
    this call would never return.
    """
    config = replace(config, grace_period=60)
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=999)])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    await supervisor.initialize(project_dir, "start here")
    closing = asyncio.create_task(supervisor.close())
    await asyncio.sleep(0)

    await asyncio.wait_for(supervisor.check_worker(), timeout=1)

    assert supervisor.snapshot().phase == ProjectPhase.terminating

    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing


@pytest.mark.anyio
async def test_close_with_no_worker_signals_nothing_and_retires(
    supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
) -> None:
    """Close on a project with no worker signals nothing and retires it."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await supervisor.wait_for_finish()

    state = await supervisor.close()

    assert state.phase == ProjectPhase.closed
    assert tmux.kill_session_calls == [SESSION_NAME]
    assert tmux.signals == []


@pytest.mark.anyio
async def test_close_kills_nothing_when_the_session_is_already_gone(
    supervisor: Supervisor, tmux: FakeTmux, project_dir: Path
) -> None:
    """Close retires the project though its tmux session is already absent."""
    await supervisor.initialize(project_dir, "start here")
    tmux.sessions.discard(SESSION_NAME)

    state = await supervisor.close()

    assert tmux.kill_session_calls == []
    assert state.phase == ProjectPhase.closed


@pytest.mark.anyio
async def test_close_is_refused_while_the_project_is_terminating(
    supervisor: Supervisor, project_dir: Path
) -> None:
    """Close is refused for a project finishing with its current worker."""
    result = await supervisor.initialize(project_dir, "start here")
    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.completed, message="all done"
    )

    with pytest.raises(SupervisorError) as excinfo:
        await supervisor.close()

    assert ProjectPhase.terminating.value in str(excinfo.value)

    await supervisor.wait_for_finish()

    assert supervisor.snapshot().phase == ProjectPhase.completed


@pytest.mark.anyio
async def test_a_close_whose_session_kill_fails_leaves_the_project_failed(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """A close that cannot kill the session gives the project up rather than wedge it.

    A project left in terminating refuses every later close, every report
    and every resume, and the watchdog skips that phase, so nothing would
    move it until the daemon restarted.
    """
    tmux = make_tmux(kill_session_errors=[TmuxError("server gone")])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    await supervisor.initialize(project_dir, "start here")

    with pytest.raises(TmuxError):
        await supervisor.close()

    for persisted in _persisted_and_live(store, supervisor):
        assert persisted.phase == ProjectPhase.failed
        assert persisted.worker is None
    reason = supervisor.recent_events(count=20)[-1].payload["reason"]
    assert "server gone" in str(reason)


@pytest.mark.anyio
async def test_a_project_a_failed_close_gave_up_can_be_closed_again(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_id: str,
    project_dir: Path,
) -> None:
    """A close that failed can be run again, and retires the project."""
    tmux = make_tmux(kill_session_errors=[TmuxError("server gone"), None])
    supervisor = Supervisor(
        config=config,
        store=store,
        tmux=tmux,
        launcher=launcher,
        state=_new_state(project_id),
    )
    await supervisor.initialize(project_dir, "start here")
    with pytest.raises(TmuxError):
        await supervisor.close()

    state = await supervisor.close()

    assert state.phase == ProjectPhase.closed
    assert tmux.kill_session_calls == [SESSION_NAME, SESSION_NAME]


@pytest.mark.anyio
async def test_a_close_interrupted_by_a_restart_resumes_as_an_abnormal_finish(
    config: BatonConfig,
    store: StateStore,
    make_tmux: type[FakeTmux],
    make_launcher: Callable[..., FakeLauncher],
    project_id: str,
    project_dir: Path,
) -> None:
    """A close cut short by a restart diagnoses the project, never relaunching it.

    The worker being closed was launched by a success handoff, so the
    previous worker's terminal report is still the last one on file. A
    restart that resumed that report would route it a second time and
    relaunch the project the operator was retiring.
    """
    handoff = LifecycleReport(
        state=LifecycleState.success, message="done", next_prompt="do the next thing"
    )
    _persist_project(
        store,
        project_id,
        project_dir,
        phase=ProjectPhase.running,
        last_report=handoff,
    )
    closing = _persisted_supervisor(
        replace(config, grace_period=60),
        store,
        make_tmux(pane_infos=[PaneInfo(dead=False, pid=999)]),
        make_launcher(),
    )
    close_task = asyncio.create_task(closing.close())
    await asyncio.sleep(0)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task

    launcher = make_launcher()
    restarted = _persisted_supervisor(
        config, store, make_tmux(pane_infos=[PaneInfo(dead=False, pid=999)]), launcher
    )

    await restarted.reconcile()
    await restarted.wait_for_finish()

    assert len(launcher.launches) == 1
    assert (
        "the daemon restarted while terminating the worker"
        in launcher.launches[0]["prompt"]
    )
    assert restarted.snapshot().phase == ProjectPhase.recovering
