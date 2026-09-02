"""Tests for baton.engine."""

import re
import signal
from collections.abc import Sequence
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
from baton.state import StateStore
from baton.tmux import PaneInfo, TmuxError


class FakeTmux:
    """A stand-in for `TmuxAdapter` that records calls and runs no command."""

    def __init__(
        self,
        *,
        sessions: Sequence[str] = (),
        pane_infos: Sequence[PaneInfo] = (),
        create_error: Exception | None = None,
        kill_errors: Sequence[Exception | None] = (),
    ) -> None:
        """Store the scripted sessions, pane readings, and errors.

        Args:
            sessions: The session names that exist before any call.
            pane_infos: The `PaneInfo` values `pane_info` returns, in call
                order. The last one repeats once they run out. With none
                scripted, `pane_info` returns a dead pane with no pid.
            create_error: The exception `create_session` raises, when set,
                instead of recording the call.
            kill_errors: What `signal_pane` raises on each call, in call
                order, where `None` is a signal that lands. The last entry
                repeats once they run out.
        """
        self.sessions: set[str] = set(sessions)
        self._pane_infos = list(pane_infos)
        self._create_error = create_error
        self._kill_errors = list(kill_errors)
        self.has_session_calls: list[str] = []
        self.created: list[dict[str, object]] = []
        self.pane_info_calls: list[str] = []
        self.signals: list[tuple[int, int]] = []

    def has_session(self, session: str) -> bool:
        """Record the query and report whether the named session exists.

        Args:
            session: The session name to look up.

        Returns:
            True when session is in the scripted set of existing sessions.
        """
        self.has_session_calls.append(session)
        return session in self.sessions

    def create_session(self, session: str, start_dir: Path) -> None:
        """Record the call, or raise the scripted error.

        Args:
            session: The name to give the new session.
            start_dir: The directory the new session starts in.

        Raises:
            Exception: `create_error`, when one was scripted.
        """
        if self._create_error is not None:
            raise self._create_error
        self.created.append({"session": session, "start_dir": start_dir})
        self.sessions.add(session)

    def pane_info(self, target: str) -> PaneInfo:
        """Record the query and return the next scripted `PaneInfo`.

        Args:
            target: The pane target that was queried.

        Returns:
            The next scripted `PaneInfo`, repeating the last one once the
            script is exhausted, or a dead pane with no pid when none was
            scripted.
        """
        self.pane_info_calls.append(target)
        if not self._pane_infos:
            return PaneInfo(dead=True, pid=None)
        index = min(len(self.pane_info_calls) - 1, len(self._pane_infos) - 1)
        return self._pane_infos[index]

    def signal_pane(self, pid: int, signum: int) -> None:
        """Record the call, then raise this call's scripted error, if any.

        Args:
            pid: The process ID that was signalled.
            signum: The signal number that was sent.

        Raises:
            Exception: The error scripted for this call, when one was.
        """
        self.signals.append((pid, signum))
        if not self._kill_errors:
            return
        index = min(len(self.signals) - 1, len(self._kill_errors) - 1)
        error = self._kill_errors[index]
        if error is not None:
            raise error


class FakeLauncher:
    """A stand-in for `WorkerLauncher` that writes prompt files and runs no process."""

    def __init__(
        self,
        state_dir: Path,
        *,
        pane_pid: int | None = 4242,
        launch_errors: Sequence[Exception | None] = (),
    ) -> None:
        """Store where prompts are written and how each launch behaves.

        Args:
            state_dir: The directory prompt files are written under.
            pane_pid: The `pane_pid` every launched `WorkerRecord` carries.
            launch_errors: What `launch` raises on each call, in call
                order, where `None` is a launch that succeeds. The last
                entry repeats once they run out.
        """
        self._state_dir = state_dir
        self._pane_pid = pane_pid
        self._launch_errors = list(launch_errors)
        self.launches: list[dict[str, object]] = []
        self.installs: list[Path] = []
        self.mcp_config_writes = 0

    def launch(self, project_path: Path, pane_target: str, prompt: str) -> WorkerRecord:
        """Record the call, write the prompt file, and return a worker record.

        Args:
            project_path: The directory the worker's pane starts in.
            pane_target: The tmux pane the worker is launched into.
            prompt: The task prompt given to the worker.

        Returns:
            A `WorkerRecord` naming this launch's worker, in launch order.

        Raises:
            Exception: The error scripted for this call, when one was.
        """
        self._raise_scripted_launch_error()
        self.launches.append(
            {
                "project_path": project_path,
                "pane_target": pane_target,
                "prompt": prompt,
            }
        )
        worker_id = f"worker-{len(self.launches)}"
        worker_dir = self._state_dir / "workers" / worker_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = worker_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        return WorkerRecord(
            worker_id=worker_id,
            prompt_path=prompt_path,
            launched_at=datetime.now(UTC),
            pane_pid=self._pane_pid,
        )

    def _raise_scripted_launch_error(self) -> None:
        """Raise the error scripted for the launch about to be made.

        Raises:
            Exception: The error scripted for this call, when one was.
        """
        if not self._launch_errors:
            return
        index = min(len(self.launches), len(self._launch_errors) - 1)
        error = self._launch_errors[index]
        if error is not None:
            raise error

    def install_skill(self, project_path: Path) -> Path:
        """Record the project path and return the skill's would-be path.

        Args:
            project_path: The project directory to install the skill into.

        Returns:
            The path the skill would be written to. Nothing is written.
        """
        self.installs.append(project_path)
        return project_path / ".claude" / "skills" / "baton-worker" / "SKILL.md"

    def write_mcp_config(self) -> Path:
        """Record the call and return the mcp.json path. Nothing is written.

        Returns:
            The path the config would be written to.
        """
        self.mcp_config_writes += 1
        return self._state_dir / "mcp.json"


@pytest.fixture
def tmux() -> FakeTmux:
    """Build a default `FakeTmux` with no sessions or scripted panes.

    Returns:
        A `FakeTmux` with an empty session set and no scripted pane info.
    """
    return FakeTmux()


@pytest.fixture
def launcher(config: BatonConfig) -> FakeLauncher:
    """Build a default `FakeLauncher` over the config's state directory.

    Args:
        config: The config naming the state directory to write prompts
            under.

    Returns:
        A `FakeLauncher` with the default pane pid and no launch error.
    """
    return FakeLauncher(config.state_dir)


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


# --- Construction and reads ------------------------------------------------


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


# --- Initialization ----------------------------------------------------------


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

    assert launcher.mcp_config_writes == 1
    assert launcher.installs == [resolved]
    assert tmux.created == [{"session": expected_session, "start_dir": resolved}]
    assert launcher.launches[0] == {
        "project_path": resolved,
        "pane_target": expected_pane_target,
        "prompt": "start here",
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
    config: BatonConfig, store: StateStore, launcher: FakeLauncher, project_dir: Path
) -> None:
    """An existing tmux session is not created again."""
    tmux = FakeTmux(sessions=["baton-project"])
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
    config: BatonConfig, store: StateStore, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A TmuxError from create_session propagates, leaving no state.json."""
    tmux = FakeTmux(create_error=TmuxError("boom"))
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    with pytest.raises(TmuxError) as exc_info:
        await supervisor.initialize(project_dir, "start here")

    assert str(exc_info.value) == "boom"
    assert supervisor.snapshot().phase == ProjectPhase.uninitialized
    assert not store.state_path.exists()


@pytest.mark.anyio
async def test_tmux_error_from_launch_propagates_and_leaves_no_state(
    config: BatonConfig, store: StateStore, tmux: FakeTmux, project_dir: Path
) -> None:
    """A TmuxError from the launcher's launch propagates, leaving no state.json."""
    launcher = FakeLauncher(config.state_dir, launch_errors=[TmuxError("no claude")])
    supervisor = Supervisor(config=config, store=store, tmux=tmux, launcher=launcher)

    with pytest.raises(TmuxError) as exc_info:
        await supervisor.initialize(project_dir, "start here")

    assert str(exc_info.value) == "no claude"
    assert supervisor.snapshot().phase == ProjectPhase.uninitialized
    assert not store.state_path.exists()


# --- Milestones --------------------------------------------------------------


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


# --- Lifecycle: transitions ---------------------------------------------------


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
async def test_failed_report_terminates_and_stops_with_no_worker(
    config: BatonConfig,
    supervisor: Supervisor,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A failed report terminates the worker and stops in phase failed."""
    result = await supervisor.initialize(project_dir, "start here")

    await supervisor.report_lifecycle(
        result.worker.worker_id, LifecycleState.failed, message="it broke"
    )
    await supervisor.wait_for_finish()

    for state in _persisted_and_live(config, supervisor):
        assert state.phase == ProjectPhase.failed
        assert state.worker is None
    assert tmux.pane_info_calls == [result.pane_target]
    assert tmux.signals == []
    assert len(launcher.launches) == 1


@pytest.mark.anyio
async def test_a_failed_relaunch_stops_in_failed_rather_than_stranding_the_project(
    config: BatonConfig, store: StateStore, tmux: FakeTmux, project_dir: Path
) -> None:
    """A launch that fails during the finish stops in failed, not in terminating."""
    launcher = FakeLauncher(
        config.state_dir, launch_errors=[None, TmuxError("session gone")]
    )
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
        f"worker {result.worker.worker_id!r} already reported 'completed'; "
        "the first terminal report is final"
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


# --- Termination ---------------------------------------------------------------


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
    config: BatonConfig, store: StateStore, launcher: FakeLauncher, project_dir: Path
) -> None:
    """The live pane's pid outranks the one recorded at launch."""
    tmux = FakeTmux(
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
    config: BatonConfig, store: StateStore, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A live pane that reports no pid falls back to the recorded one."""
    tmux = FakeTmux(
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
    config: BatonConfig, store: StateStore, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A pane that stays alive after SIGTERM is escalated to SIGKILL."""
    tmux = FakeTmux(pane_infos=[PaneInfo(dead=False, pid=4242)])
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
    config: BatonConfig, store: StateStore, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A ProcessLookupError from signal_pane is swallowed; the finish completes."""
    tmux = FakeTmux(
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
    config: BatonConfig, store: StateStore, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A pane that dies while being polled is never escalated to SIGKILL."""
    patient = replace(config, termination_timeout=5)
    tmux = FakeTmux(
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
    config: BatonConfig, store: StateStore, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A worker that exits between the last poll and SIGKILL is not an error."""
    tmux = FakeTmux(
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
    config: BatonConfig, store: StateStore, project_dir: Path
) -> None:
    """A live pane with no pid, and no recorded pid, is not signalled."""
    tmux = FakeTmux(pane_infos=[PaneInfo(dead=False, pid=None)])
    launcher = FakeLauncher(config.state_dir, pane_pid=None)
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


# --- wait_for_finish -------------------------------------------------------


@pytest.mark.anyio
async def test_wait_for_finish_with_no_pending_finish_returns_immediately(
    supervisor: Supervisor,
) -> None:
    """wait_for_finish returns without error when no finish is pending."""
    await supervisor.wait_for_finish()
