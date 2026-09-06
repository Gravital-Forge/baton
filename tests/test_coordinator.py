"""Tests for baton.coordinator."""

import asyncio
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from baton.config import BatonConfig
from baton.coordinator import Coordinator, CoordinatorError
from baton.models import (
    EventKind,
    LifecycleReport,
    LifecycleState,
    ProjectPhase,
    ProjectState,
    WorkerRecord,
)
from baton.state import StateStore, list_project_ids, project_state_dir
from baton.tmux import PaneInfo, TmuxError
from tests.doubles import FakeLauncher, FakeTmux

TITLE = "Widget factory"


def _worker(worker_id: str) -> WorkerRecord:
    """Build a WorkerRecord standing in for a project's live worker.

    Args:
        worker_id: The id to give the record.

    Returns:
        A WorkerRecord with a placeholder prompt path and a launch time.
    """
    return WorkerRecord(
        worker_id=worker_id,
        prompt_path=Path("prompt.md"),
        launched_at=datetime.now(UTC),
        pane_pid=111,
    )


def _persist_project(
    config: BatonConfig,
    project_id: str,
    project_path: Path,
    *,
    phase: ProjectPhase,
    session_name: str,
    worker: WorkerRecord | None = None,
    last_report: LifecycleReport | None = None,
) -> StateStore:
    """Save one project's state under the daemon's state directory.

    A test that needs a project already on disk writes it here, before
    the Coordinator under test is constructed, since the coordinator
    reads the state directory once at construction.

    Args:
        config: The config naming the daemon's state directory.
        project_id: The id naming the project's own directory.
        project_path: The project directory the persisted state points to.
        phase: The phase the persisted state is saved in.
        session_name: The tmux session name the persisted state carries.
        worker: The current worker the persisted state carries, or None.
        last_report: The most recent lifecycle report the persisted state
            carries, or None.

    Returns:
        The store the state was saved to.
    """
    store = StateStore(project_state_dir(config.state_dir, project_id))
    store.save(
        ProjectState.new(project_id, TITLE).updated(
            phase=phase,
            project_path=project_path.resolve(),
            session_name=session_name,
            pane_target=f"{session_name}:worker.0",
            worker=worker,
            last_report=last_report,
            model="sonnet",
        )
    )
    return store


def _persist_reconciliation_pair(config: BatonConfig, project_dir: Path) -> None:
    """Persist the two projects a startup isolation test reconciles.

    `FakeTmux` scripts `send_error` for every target at once, so the pair
    has to differ in the path it takes rather than in the pane it sends
    to. The first project is running with a live pane, so its
    reconciliation sends a request and raises. The second is terminating
    with a terminal success report, so its resumed finish routes to
    running without ever sending.

    Args:
        config: The config naming the daemon's state directory.
        project_dir: The project directory both persisted states point to.
    """
    _persist_project(
        config,
        "aaaa1111",
        project_dir,
        phase=ProjectPhase.running,
        session_name="baton-one",
        worker=_worker("worker-1"),
    )
    _persist_project(
        config,
        "bbbb2222",
        project_dir,
        phase=ProjectPhase.terminating,
        session_name="baton-two",
        worker=_worker("worker-2"),
        last_report=LifecycleReport(
            state=LifecycleState.success,
            message="phase one done",
            next_prompt="phase two",
        ),
    )


def test_construction_refuses_a_legacy_root_state_file(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher
) -> None:
    """Construction refuses a state directory holding the old single-project layout."""
    config.state_dir.mkdir(parents=True)
    legacy_path = config.state_dir / "state.json"
    legacy_path.write_text("{}", encoding="utf-8")

    with pytest.raises(CoordinatorError) as excinfo:
        Coordinator(config, tmux, launcher)

    assert str(legacy_path) in str(excinfo.value)


def test_construction_holds_a_supervisor_for_every_persisted_project(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, project_dir: Path
) -> None:
    """Construction builds a supervisor for every project already on disk."""
    _persist_project(
        config,
        "aaaa1111",
        project_dir,
        phase=ProjectPhase.completed,
        session_name="baton-one",
    )
    _persist_project(
        config,
        "bbbb2222",
        project_dir,
        phase=ProjectPhase.failed,
        session_name="baton-two",
    )

    coordinator = Coordinator(config, tmux, launcher)

    assert coordinator.supervisor("aaaa1111").snapshot().phase == ProjectPhase.completed
    assert coordinator.supervisor("bbbb2222").snapshot().phase == ProjectPhase.failed


def test_construction_loads_each_projects_own_state(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, project_dir: Path
) -> None:
    """Construction loads each project's state from its own store."""
    _persist_project(
        config,
        "aaaa1111",
        project_dir,
        phase=ProjectPhase.completed,
        session_name="baton-one",
    )

    coordinator = Coordinator(config, tmux, launcher)

    assert coordinator.supervisor("aaaa1111").snapshot().session_name == "baton-one"


def test_construction_refuses_a_listed_project_with_no_state(
    config: BatonConfig,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction refuses a project id listed on disk that has no state to load."""
    monkeypatch.setattr(
        "baton.coordinator.list_project_ids", lambda state_dir: ["deadbeef"]
    )

    with pytest.raises(CoordinatorError) as excinfo:
        Coordinator(config, tmux, launcher)

    assert "deadbeef" in str(excinfo.value)


def test_construction_with_no_state_directory_holds_no_projects(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher
) -> None:
    """Construction with nothing on disk holds no project."""
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(CoordinatorError):
        coordinator.supervisor_for_worker("worker-1")


@pytest.mark.anyio
async def test_two_projects_hold_distinct_ids_and_session_names(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, tmp_path: Path
) -> None:
    """Two initialized projects hold distinct ids and their titles' session names."""
    coordinator = Coordinator(config, tmux, launcher)
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()

    alpha = await coordinator.initialize(alpha_dir, "Alpha", "start alpha")
    beta = await coordinator.initialize(beta_dir, "Beta", "start beta")

    assert alpha.project_id != beta.project_id
    assert alpha.session_name == "baton-alpha"
    assert beta.session_name == "baton-beta"


@pytest.mark.anyio
async def test_a_minted_id_is_eight_lowercase_hex_characters(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A minted project id is eight lowercase hex characters."""
    coordinator = Coordinator(config, tmux, launcher)

    state = await coordinator.initialize(project_dir, TITLE, "start here")

    assert re.fullmatch(r"[0-9a-f]{8}", state.project_id) is not None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Widget factory", "baton-widget-factory"),
        ("  Spaced   Out  ", "baton-spaced-out"),
        ("A/B: test!", "baton-a-b-test"),
        ("UPPER", "baton-upper"),
        ("v2.1 release", "baton-v2-1-release"),
    ],
)
async def test_the_session_name_comes_from_the_titles_slug(
    config: BatonConfig,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
    title: str,
    expected: str,
) -> None:
    """The default session name is the title's slug, prefixed with baton-."""
    coordinator = Coordinator(config, tmux, launcher)

    state = await coordinator.initialize(project_dir, title, "start here")

    assert state.session_name == expected


@pytest.mark.anyio
async def test_an_explicit_session_name_overrides_the_derived_one(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, project_dir: Path
) -> None:
    """An explicit session_name overrides the title-derived one."""
    coordinator = Coordinator(config, tmux, launcher)

    state = await coordinator.initialize(
        project_dir, TITLE, "start here", session_name="custom-session"
    )

    assert state.session_name == "custom-session"
    assert state.pane_target == "custom-session:worker.0"


@pytest.mark.anyio
async def test_a_duplicate_session_name_is_refused_and_named(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, tmp_path: Path
) -> None:
    """A second project cannot take a session name the first project already holds."""
    coordinator = Coordinator(config, tmux, launcher)
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    first = await coordinator.initialize(first_dir, TITLE, "start here")

    with pytest.raises(CoordinatorError) as excinfo:
        await coordinator.initialize(second_dir, TITLE, "start again")

    assert "baton-widget-factory" in str(excinfo.value)
    assert first.project_id in str(excinfo.value)
    assert len(launcher.launches) == 1


@pytest.mark.anyio
async def test_a_session_name_already_in_tmux_is_refused(
    config: BatonConfig,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A session name already alive in tmux is refused, though held by no project."""
    tmux = make_tmux(sessions=["baton-widget-factory"])
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(CoordinatorError) as excinfo:
        await coordinator.initialize(project_dir, TITLE, "start here")

    assert "baton-widget-factory" in str(excinfo.value)
    assert launcher.launches == []


@pytest.mark.anyio
@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
async def test_a_blank_title_is_refused(
    config: BatonConfig,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
    title: str,
) -> None:
    """A blank title is refused before anything is launched."""
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(CoordinatorError, match="title must not be blank"):
        await coordinator.initialize(project_dir, title, "start here")

    assert launcher.launches == []


@pytest.mark.anyio
@pytest.mark.parametrize("title", ["!!!", "---", "***", "проект"])
async def test_a_title_that_slugs_to_nothing_is_refused(
    config: BatonConfig,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
    title: str,
) -> None:
    """A title holding no ASCII letter or digit is refused before any launch."""
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(CoordinatorError) as excinfo:
        await coordinator.initialize(project_dir, title, "start here")

    assert "title must hold an ASCII letter or digit" in str(excinfo.value)
    assert launcher.launches == []


@pytest.mark.anyio
@pytest.mark.parametrize("session_name", ["", "  \n\t "])
async def test_a_blank_explicit_session_name_is_refused(
    config: BatonConfig,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
    session_name: str,
) -> None:
    """A session name that is given but blank is refused before anything is launched."""
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(CoordinatorError, match="session name must not be blank"):
        await coordinator.initialize(
            project_dir, TITLE, "start here", session_name=session_name
        )

    assert launcher.launches == []


@pytest.mark.anyio
async def test_a_closed_projects_session_name_may_be_reused(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, project_dir: Path
) -> None:
    """A closed project's session name may be taken by a new project."""
    _persist_project(
        config,
        "aaaa1111",
        project_dir,
        phase=ProjectPhase.closed,
        session_name="baton-widget-factory",
    )
    coordinator = Coordinator(config, tmux, launcher)

    state = await coordinator.initialize(project_dir, TITLE, "start here")

    assert state.session_name == "baton-widget-factory"


@pytest.mark.anyio
async def test_a_failed_initialize_registers_no_project(
    config: BatonConfig,
    tmux: FakeTmux,
    make_launcher: Callable[..., FakeLauncher],
    project_dir: Path,
) -> None:
    """A launch failure during initialize leaves no project registered."""
    launcher = make_launcher(launch_errors=[TmuxError("no claude")])
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(TmuxError):
        await coordinator.initialize(project_dir, TITLE, "start here")

    assert list_project_ids(config.state_dir) == []
    with pytest.raises(CoordinatorError):
        coordinator.supervisor_for_worker("worker-1")


@pytest.mark.anyio
async def test_initialize_gives_up_when_every_minted_id_collides(
    config: BatonConfig,
    tmux: FakeTmux,
    launcher: FakeLauncher,
    project_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialize refuses when every minted id collides with a project on disk."""
    monkeypatch.setattr(
        "baton.coordinator.secrets", SimpleNamespace(token_hex=lambda _: "deadbeef")
    )
    project_state_dir(config.state_dir, "deadbeef").mkdir(parents=True)
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(CoordinatorError) as excinfo:
        await coordinator.initialize(project_dir, TITLE, "start here")

    assert "could not mint an unused project id in 8 attempts" in str(excinfo.value)
    assert launcher.launches == []


@pytest.mark.anyio
async def test_a_worker_id_routes_to_its_own_project(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, tmp_path: Path
) -> None:
    """Each worker id routes back to the project that launched it."""
    coordinator = Coordinator(config, tmux, launcher)
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()

    alpha = await coordinator.initialize(alpha_dir, "Alpha", "start alpha")
    beta = await coordinator.initialize(beta_dir, "Beta", "start beta")

    for state in (alpha, beta):
        found = coordinator.supervisor_for_worker(state.worker.worker_id)
        assert found.snapshot().project_id == state.project_id


def test_an_unknown_worker_id_is_refused_and_named(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher
) -> None:
    """An unknown worker id is refused, naming the worker id in the message."""
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(CoordinatorError, match=re.escape("'nobody'")):
        coordinator.supervisor_for_worker("nobody")


@pytest.mark.anyio
async def test_supervisor_returns_the_project_with_that_id(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, project_dir: Path
) -> None:
    """Supervisor looks a project up by its own id."""
    coordinator = Coordinator(config, tmux, launcher)

    state = await coordinator.initialize(project_dir, TITLE, "start here")

    assert coordinator.supervisor(state.project_id).snapshot().project_id == (
        state.project_id
    )


def test_an_unknown_project_id_is_refused_and_named(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher
) -> None:
    """An unknown project id is refused, naming the id in the message."""
    coordinator = Coordinator(config, tmux, launcher)

    with pytest.raises(CoordinatorError, match=re.escape("'nosuch'")):
        coordinator.supervisor("nosuch")


@pytest.mark.anyio
async def test_each_project_keeps_its_own_event_log(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, tmp_path: Path
) -> None:
    """Each project's event log holds only its own two events."""
    coordinator = Coordinator(config, tmux, launcher)
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()

    alpha = await coordinator.initialize(alpha_dir, "Alpha", "start alpha")
    beta = await coordinator.initialize(beta_dir, "Beta", "start beta")

    alpha_events = coordinator.supervisor(alpha.project_id).recent_events(10)
    beta_events = coordinator.supervisor(beta.project_id).recent_events(10)

    assert [event.kind for event in alpha_events] == [
        EventKind.launch,
        EventKind.phase,
    ]
    assert [event.kind for event in beta_events] == [
        EventKind.launch,
        EventKind.phase,
    ]
    assert alpha_events[0].worker_id != beta_events[0].worker_id


@pytest.mark.anyio
async def test_start_records_a_reconciliation_failure_against_its_own_project(
    config: BatonConfig,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A project that cannot reconcile is failed, and its worker left alone."""
    _persist_reconciliation_pair(config, project_dir)
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=1)], send_error=TmuxError("pane is gone")
    )
    coordinator = Coordinator(config, tmux, launcher)

    await coordinator.start()
    await coordinator.shutdown()

    failed = coordinator.supervisor("aaaa1111")
    events = failed.recent_events(10)
    assert failed.snapshot().phase == ProjectPhase.failed
    assert [event.kind for event in events] == [EventKind.phase]
    assert "pane is gone" in str(events[0].payload["reason"])


@pytest.mark.anyio
async def test_start_serves_a_project_whose_sibling_could_not_reconcile(
    config: BatonConfig,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    project_dir: Path,
) -> None:
    """A healthy project carries on though its sibling's reconciliation raised."""
    _persist_reconciliation_pair(config, project_dir)
    tmux = make_tmux(
        pane_infos=[PaneInfo(dead=False, pid=1)], send_error=TmuxError("pane is gone")
    )
    coordinator = Coordinator(config, tmux, launcher)

    await coordinator.start()
    healthy = coordinator.supervisor("bbbb2222")
    await healthy.wait_for_finish()
    await coordinator.shutdown()

    assert healthy.snapshot().phase == ProjectPhase.running
    assert launcher.launches[0]["project_id"] == "bbbb2222"


@pytest.mark.anyio
async def test_one_watchdog_tick_checks_every_project(
    config: BatonConfig,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    tmp_path: Path,
) -> None:
    """One watchdog tick checks every project's worker pane."""
    tmux = make_tmux(pane_infos=[PaneInfo(dead=False, pid=1)])
    coordinator = Coordinator(config, tmux, launcher)
    await coordinator.start()
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()

    alpha = await coordinator.initialize(alpha_dir, "Alpha", "start alpha")
    beta = await coordinator.initialize(beta_dir, "Beta", "start beta")
    for _ in range(4):
        await asyncio.sleep(0)
    await coordinator.shutdown()

    assert set(tmux.pane_info_calls) == {alpha.pane_target, beta.pane_target}
    assert coordinator.supervisor(alpha.project_id).snapshot().phase == (
        ProjectPhase.running
    )
    assert coordinator.supervisor(beta.project_id).snapshot().phase == (
        ProjectPhase.running
    )


@pytest.mark.anyio
async def test_the_watchdog_survives_a_project_whose_tick_raises(
    config: BatonConfig,
    make_tmux: type[FakeTmux],
    launcher: FakeLauncher,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A tick that raises for one project is logged; the watchdog goes on."""
    tmux = make_tmux(pane_info_error=OSError("tmux is gone"))
    coordinator = Coordinator(config, tmux, launcher)
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()

    with caplog.at_level(logging.ERROR, logger="baton.coordinator"):
        await coordinator.start()
        alpha = await coordinator.initialize(alpha_dir, "Alpha", "start alpha")
        beta = await coordinator.initialize(beta_dir, "Beta", "start beta")
        for _ in range(6):
            await asyncio.sleep(0)
        await coordinator.shutdown()

    records = [
        record for record in caplog.records if record.name == "baton.coordinator"
    ]
    assert len(records) >= 2
    assert records[0].getMessage() == (
        f"the watchdog tick for project {alpha.project_id} failed"
    )
    assert records[0].exc_info is not None
    assert coordinator.supervisor(alpha.project_id).snapshot().phase == (
        ProjectPhase.running
    )
    assert coordinator.supervisor(beta.project_id).snapshot().phase == (
        ProjectPhase.running
    )


@pytest.mark.anyio
async def test_shutdown_drains_every_projects_pending_finish(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher, tmp_path: Path
) -> None:
    """Shutdown drains every project's pending finish."""
    coordinator = Coordinator(config, tmux, launcher)
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()
    alpha = await coordinator.initialize(alpha_dir, "Alpha", "start alpha")
    beta = await coordinator.initialize(beta_dir, "Beta", "start beta")

    await coordinator.supervisor(alpha.project_id).report_lifecycle(
        alpha.worker.worker_id, LifecycleState.completed, message="all done"
    )
    await coordinator.supervisor(beta.project_id).report_lifecycle(
        beta.worker.worker_id, LifecycleState.completed, message="all done"
    )

    await coordinator.shutdown()

    assert coordinator.supervisor(alpha.project_id).snapshot().phase == (
        ProjectPhase.completed
    )
    assert coordinator.supervisor(beta.project_id).snapshot().phase == (
        ProjectPhase.completed
    )


@pytest.mark.anyio
async def test_shutdown_logs_a_failing_drain_and_drains_the_rest(
    config: BatonConfig,
    tmux: FakeTmux,
    make_launcher: Callable[..., FakeLauncher],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A project whose drain raises is logged, and the next project still drains."""
    launcher = make_launcher(launch_errors=[None, None, TmuxError("session gone")])
    coordinator = Coordinator(config, tmux, launcher)
    alpha_dir = tmp_path / "alpha"
    alpha_dir.mkdir()
    beta_dir = tmp_path / "beta"
    beta_dir.mkdir()
    alpha = await coordinator.initialize(alpha_dir, "Alpha", "start alpha")
    beta = await coordinator.initialize(beta_dir, "Beta", "start beta")

    await coordinator.supervisor(alpha.project_id).report_lifecycle(
        alpha.worker.worker_id,
        LifecycleState.success,
        message="phase one done",
        next_prompt="phase two",
    )
    await coordinator.supervisor(beta.project_id).report_lifecycle(
        beta.worker.worker_id, LifecycleState.completed, message="all done"
    )

    with caplog.at_level(logging.ERROR, logger="baton.coordinator"):
        await coordinator.shutdown()

    records = [
        record for record in caplog.records if record.name == "baton.coordinator"
    ]
    assert len(records) == 1
    assert records[0].getMessage() == (
        f"draining project {alpha.project_id} at shutdown failed"
    )
    assert records[0].exc_info is not None
    assert coordinator.supervisor(beta.project_id).snapshot().phase == (
        ProjectPhase.completed
    )


@pytest.mark.anyio
async def test_shutdown_without_start_returns(
    config: BatonConfig, tmux: FakeTmux, launcher: FakeLauncher
) -> None:
    """Shutdown on a coordinator that never started returns without error."""
    coordinator = Coordinator(config, tmux, launcher)

    await coordinator.shutdown()
