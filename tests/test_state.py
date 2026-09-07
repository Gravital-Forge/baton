"""Tests for baton.state."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest

from baton.config import BatonConfig
from baton.models import (
    EventKind,
    LifecycleReport,
    LifecycleState,
    ProjectPhase,
    ProjectState,
    WorkerRecord,
)
from baton.state import (
    StateStore,
    legacy_state_path,
    list_project_ids,
    project_state_dir,
)

GOOD_STATE_JSON = {
    "project_id": "a1b2c3d4",
    "title": "Widget factory",
    "phase": "running",
    "project_path": None,
    "session_name": None,
    "pane_target": None,
    "worker": None,
    "last_report": None,
    "model": None,
    "recovery_attempts": 0,
    "updated_at": "2026-09-02T16:00:00+00:00",
}


@pytest.fixture
def populated_state(tmp_path: Path) -> ProjectState:
    """Build a fully populated ProjectState for round-trip tests.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        A ProjectState with every field set to a real, non-None value.
    """
    worker = WorkerRecord(
        worker_id="3f2b1a",
        prompt_path=tmp_path / "workers" / "3f2b1a" / "prompt.md",
        launched_at=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
        pane_pid=4242,
    )
    report = LifecycleReport(
        state=LifecycleState.success,
        message="did the thing",
        next_prompt="do the next thing",
        delay_seconds=1800,
        model="opus",
    )
    return ProjectState(
        project_id="a1b2c3d4",
        title="Widget factory",
        phase=ProjectPhase.running,
        project_path=tmp_path / "proj",
        session_name="baton",
        pane_target="baton:worker.0",
        worker=worker,
        last_report=report,
        model="sonnet",
        recovery_attempts=2,
        resume_at=datetime(2026, 9, 2, 16, 30, tzinfo=UTC),
        updated_at=datetime(2026, 9, 2, 16, 0, 1, tzinfo=UTC),
    )


def test_init_does_not_touch_the_filesystem(
    store: StateStore, config: BatonConfig, project_id: str
) -> None:
    """A store built on a nonexistent directory leaves it nonexistent."""
    assert not store.project_state_dir.exists()
    assert store.project_state_dir == config.state_dir / "projects" / project_id
    assert store.state_path == store.project_state_dir / "state.json"
    assert store.events_path == store.project_state_dir / "events.jsonl"


def test_load_with_no_state_file_returns_none(store: StateStore) -> None:
    """load() returns None when the project has no state file yet."""
    assert store.load() is None


def test_round_trip_fully_populated(
    store: StateStore, populated_state: ProjectState
) -> None:
    """A fully populated state round-trips through save and load unchanged."""
    store.save(populated_state)

    loaded = store.load()

    assert loaded == populated_state
    assert isinstance(loaded.project_path, Path)
    assert isinstance(loaded.phase, ProjectPhase)
    assert loaded.updated_at.tzinfo == UTC
    assert loaded.updated_at == populated_state.updated_at
    assert isinstance(loaded.worker, WorkerRecord)
    assert isinstance(loaded.worker.prompt_path, Path)
    assert loaded.worker.launched_at.tzinfo == UTC
    assert loaded.worker.launched_at == populated_state.worker.launched_at
    assert isinstance(loaded.last_report, LifecycleReport)
    assert isinstance(loaded.last_report.state, LifecycleState)
    assert loaded.last_report.delay_seconds == 1800
    assert loaded.last_report.model == "opus"
    assert loaded.resume_at == populated_state.resume_at
    assert loaded.resume_at.tzinfo == UTC


def test_round_trip_with_optionals_none(store: StateStore, tmp_path: Path) -> None:
    """Optional fields that are None round-trip as None, not as a sentinel."""
    worker = WorkerRecord(
        worker_id="worker-none",
        prompt_path=tmp_path / "workers" / "worker-none" / "prompt.md",
        launched_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        pane_pid=None,
    )
    report = LifecycleReport(state=LifecycleState.blocked, message="waiting on input")
    state = ProjectState(
        project_id="a1b2c3d4",
        title="Widget factory",
        phase=ProjectPhase.blocked,
        project_path=None,
        session_name=None,
        pane_target=None,
        worker=worker,
        last_report=report,
        model=None,
        recovery_attempts=5,
        resume_at=None,
        updated_at=datetime(2026, 9, 2, 12, 0, 1, tzinfo=UTC),
    )

    store.save(state)
    loaded = store.load()

    assert loaded == state
    assert loaded.project_path is None
    assert loaded.session_name is None
    assert loaded.pane_target is None
    assert loaded.worker is not None
    assert loaded.worker.pane_pid is None
    assert loaded.last_report is not None
    assert loaded.last_report.next_prompt is None
    assert loaded.last_report.delay_seconds is None
    assert loaded.last_report.model is None
    assert loaded.model is None
    assert loaded.resume_at is None


def _write_state_file(store: StateStore, text: str) -> None:
    """Write raw text straight into the store's state.json.

    Args:
        store: The store whose state file to write.
        text: The exact file contents, valid JSON or not.
    """
    store.project_state_dir.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    ("description", "text"),
    [
        ("an empty file", ""),
        ("text that is not JSON", "not json at all"),
        ("an unknown phase", json.dumps({**GOOD_STATE_JSON, "phase": "nonsense"})),
    ],
)
def test_load_rejects_a_malformed_state_file(
    store: StateStore, description: str, text: str
) -> None:
    """load() lets a malformed state file raise rather than repairing it.

    Args:
        store: The store under test.
        description: What makes this file malformed, for the test id.
        text: The malformed file contents.
    """
    _write_state_file(store, text)

    with pytest.raises(ValueError):
        store.load()


def test_load_rejects_a_state_file_missing_a_key(store: StateStore) -> None:
    """load() raises KeyError when state.json lacks a required key."""
    without_phase = {k: v for k, v in GOOD_STATE_JSON.items() if k != "phase"}
    _write_state_file(store, json.dumps(without_phase))

    with pytest.raises(KeyError):
        store.load()


def test_load_rejects_a_state_file_missing_model(store: StateStore) -> None:
    """load() raises KeyError when state.json lacks the model key."""
    without_model = {k: v for k, v in GOOD_STATE_JSON.items() if k != "model"}
    _write_state_file(store, json.dumps(without_model))

    with pytest.raises(KeyError):
        store.load()


@pytest.mark.parametrize("key", ["project_id", "title"])
def test_load_rejects_a_state_file_missing_an_identity_key(
    store: StateStore, key: str
) -> None:
    """load() raises KeyError when state.json lacks the id or the title.

    Args:
        store: The store under test.
        key: The identity key left out of the written file.
    """
    without_key = {k: v for k, v in GOOD_STATE_JSON.items() if k != key}
    _write_state_file(store, json.dumps(without_key))

    with pytest.raises(KeyError):
        store.load()


def test_load_revalidates_a_persisted_report(store: StateStore) -> None:
    """A persisted report that breaks a payload rule is rejected on load."""
    illegal = {
        **GOOD_STATE_JSON,
        "last_report": {"state": "completed", "message": None, "next_prompt": None},
    }
    _write_state_file(store, json.dumps(illegal))

    with pytest.raises(ValueError) as excinfo:
        store.load()

    assert "requires a message" in str(excinfo.value)


def test_load_reads_a_state_file_missing_resume_at_delay_and_model(
    store: StateStore,
) -> None:
    """A state.json missing resume_at, the delay, and the model loads None for each."""
    raw = {
        **GOOD_STATE_JSON,
        "last_report": {
            "state": "success",
            "message": "did the thing",
            "next_prompt": "do the next thing",
        },
    }
    _write_state_file(store, json.dumps(raw))

    loaded = store.load()

    assert loaded.resume_at is None
    assert loaded.last_report.delay_seconds is None
    assert loaded.last_report.model is None


def test_round_trip_with_no_worker_and_no_report(store: StateStore) -> None:
    """A state with no worker and no report round-trips, writing JSON nulls."""
    state = ProjectState.new("a1b2c3d4", "Widget factory").updated(
        phase=ProjectPhase.completed
    )

    store.save(state)
    loaded = store.load()

    assert loaded == state
    assert loaded.worker is None
    assert loaded.last_report is None
    raw = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert raw["worker"] is None
    assert raw["last_report"] is None


def test_save_creates_the_state_directory(
    store: StateStore, populated_state: ProjectState
) -> None:
    """save() creates the state directory when it does not yet exist."""
    assert not store.project_state_dir.exists()

    store.save(populated_state)

    assert store.project_state_dir.exists()
    assert store.state_path.exists()


def test_on_disk_json_has_pinned_shape(
    store: StateStore, populated_state: ProjectState
) -> None:
    """The on-disk state.json has the pinned keys, order, and encoding."""
    store.save(populated_state)

    raw_text = store.state_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)

    assert list(raw.keys()) == [
        "project_id",
        "title",
        "phase",
        "project_path",
        "session_name",
        "pane_target",
        "worker",
        "last_report",
        "model",
        "recovery_attempts",
        "resume_at",
        "updated_at",
    ]
    assert raw["project_id"] == "a1b2c3d4"
    assert raw["title"] == "Widget factory"
    assert raw["phase"] == "running"
    assert raw["project_path"] == str(populated_state.project_path)
    assert raw["session_name"] == "baton"
    assert raw["pane_target"] == "baton:worker.0"
    assert list(raw["worker"].keys()) == [
        "worker_id",
        "prompt_path",
        "launched_at",
        "pane_pid",
    ]
    assert raw["worker"]["worker_id"] == "3f2b1a"
    assert raw["worker"]["prompt_path"] == str(populated_state.worker.prompt_path)
    assert raw["worker"]["launched_at"] == (
        populated_state.worker.launched_at.isoformat()
    )
    assert raw["worker"]["pane_pid"] == 4242
    assert list(raw["last_report"].keys()) == [
        "state",
        "message",
        "next_prompt",
        "delay_seconds",
        "model",
    ]
    assert raw["last_report"]["state"] == "success"
    assert raw["last_report"]["message"] == "did the thing"
    assert raw["last_report"]["next_prompt"] == "do the next thing"
    assert raw["last_report"]["delay_seconds"] == 1800
    assert raw["last_report"]["model"] == "opus"
    assert raw["model"] == "sonnet"
    assert raw["recovery_attempts"] == 2
    assert raw["resume_at"] == populated_state.resume_at.isoformat()
    assert raw["updated_at"] == populated_state.updated_at.isoformat()
    assert raw_text.endswith("\n")
    assert not raw_text.endswith("\n\n")


def _raise_replace_oserror(self: Path, target: object) -> NoReturn:
    """Simulate a failed atomic rename by always raising OSError.

    Args:
        self: The path Path.replace was called on.
        target: The rename's destination path.

    Raises:
        OSError: Always, to simulate a failed rename.
    """
    raise OSError("simulated rename failure")


def test_save_failure_leaves_previous_state_unchanged(
    store: StateStore,
    populated_state: ProjectState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rename leaves state.json byte-for-byte as it was."""
    store.save(populated_state)
    before_bytes = store.state_path.read_bytes()
    other_state = populated_state.updated(session_name="a-different-session")

    monkeypatch.setattr(Path, "replace", _raise_replace_oserror)

    with pytest.raises(OSError):
        store.save(other_state)

    after_bytes = store.state_path.read_bytes()
    assert after_bytes == before_bytes
    assert store.load() == populated_state
    assert not (store.project_state_dir / "state.json.tmp").exists()


def test_save_failure_leaves_no_state_file_when_none_existed(
    store: StateStore,
    populated_state: ProjectState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed rename with no prior save leaves no state.json or temp file."""
    monkeypatch.setattr(Path, "replace", _raise_replace_oserror)

    with pytest.raises(OSError):
        store.save(populated_state)

    assert not store.state_path.exists()
    assert not (store.project_state_dir / "state.json.tmp").exists()


def test_append_event_returns_the_event(store: StateStore) -> None:
    """append_event returns an Event stamped with a UTC timestamp."""
    before = datetime.now(UTC)

    event = store.append_event(
        EventKind.milestone, "worker-1", {"message": "wrote the parser"}
    )

    after = datetime.now(UTC)
    assert event.kind == EventKind.milestone
    assert event.worker_id == "worker-1"
    assert event.payload == {"message": "wrote the parser"}
    assert event.timestamp.tzinfo == UTC
    assert before <= event.timestamp <= after


def test_append_event_copies_the_payload(store: StateStore) -> None:
    """Mutating the caller's payload dict does not reach the stored event."""
    payload = {"message": "wrote the parser"}

    event = store.append_event(EventKind.milestone, None, payload)
    payload["message"] = "mutated after the call"

    assert event.payload == {"message": "wrote the parser"}
    recent = store.recent_events(1)
    assert recent[0].payload == {"message": "wrote the parser"}


def test_append_event_writes_one_json_object_per_line(store: StateStore) -> None:
    """Three appends produce three independently parseable lines, in order."""
    store.append_event(EventKind.milestone, "w1", {"n": 1})
    store.append_event(EventKind.lifecycle, "w1", {"n": 2})
    store.append_event(EventKind.launch, None, {"n": 3})

    raw_text = store.events_path.read_text(encoding="utf-8")
    assert raw_text.count("\n") == 3
    lines = raw_text.splitlines()
    assert len(lines) == 3
    decoded = [json.loads(line) for line in lines]
    assert [entry["payload"]["n"] for entry in decoded] == [1, 2, 3]


def test_append_event_with_none_worker_id_writes_null(store: StateStore) -> None:
    """An event with no worker writes worker_id as null and reads back None."""
    store.append_event(EventKind.phase, None, {"detail": "phase changed"})

    raw = json.loads(store.events_path.read_text(encoding="utf-8").splitlines()[0])
    assert raw["worker_id"] is None
    recent = store.recent_events(1)
    assert recent[0].worker_id is None


def test_recent_events_returns_the_last_n_oldest_first(store: StateStore) -> None:
    """recent_events returns the last N entries, oldest first."""
    for n in range(1, 6):
        store.append_event(EventKind.milestone, None, {"n": n})

    recent = store.recent_events(3)

    assert [event.payload["n"] for event in recent] == [3, 4, 5]


def test_recent_events_with_count_larger_than_the_log_returns_everything(
    store: StateStore,
) -> None:
    """A count larger than the log returns every event, oldest first."""
    store.append_event(EventKind.milestone, None, {"n": 1})
    store.append_event(EventKind.milestone, None, {"n": 2})

    recent = store.recent_events(10)

    assert [event.payload["n"] for event in recent] == [1, 2]


@pytest.mark.parametrize("count", [0, -1, -5])
def test_recent_events_with_a_nonpositive_count_returns_empty(
    store: StateStore, count: int
) -> None:
    """A count of zero or less returns an empty list, never the whole log.

    lines[-0:] returns the whole list, which would be a slicing bug; this
    test pins the correct behavior explicitly.
    """
    store.append_event(EventKind.milestone, None, {"n": 1})

    assert store.recent_events(count) == []


def test_recent_events_on_a_missing_log_returns_empty(store: StateStore) -> None:
    """recent_events on a log file that does not exist returns []."""
    assert store.recent_events(5) == []


@pytest.mark.parametrize("kind", list(EventKind))
def test_every_event_kind_round_trips(store: StateStore, kind: EventKind) -> None:
    """Every EventKind member round-trips through the event log."""
    store.append_event(kind, "worker-1", {"detail": "x"})

    recent = store.recent_events(1)

    assert recent[0].kind == kind


def _write_project_state(state_dir: Path, project_id: str) -> Path:
    """Create a project directory under the state directory, with a state.json.

    Args:
        state_dir: The state directory to create the project under.
        project_id: The id naming the project's directory.

    Returns:
        The created project directory.
    """
    persisted = state_dir / "projects" / project_id
    persisted.mkdir(parents=True)
    (persisted / "state.json").write_text("{}", encoding="utf-8")
    return persisted


def test_project_state_dir_composes_projects_and_id(tmp_path: Path) -> None:
    """project_state_dir composes <state dir>/projects/<project id>."""
    state_dir = tmp_path / "state"

    composed = project_state_dir(state_dir, "a1b2c3d4")

    assert composed == state_dir / "projects" / "a1b2c3d4"


def test_project_state_dir_does_not_touch_the_filesystem(tmp_path: Path) -> None:
    """project_state_dir composes a path without creating any directory."""
    state_dir = tmp_path / "state"

    composed = project_state_dir(state_dir, "a1b2c3d4")

    assert not composed.exists()
    assert not state_dir.exists()


def test_list_project_ids_returns_every_persisted_id_sorted(tmp_path: Path) -> None:
    """list_project_ids returns the ids under projects/, sorted."""
    state_dir = tmp_path / "state"
    _write_project_state(state_dir, "c3d4e5f6")
    _write_project_state(state_dir, "a1b2c3d4")
    _write_project_state(state_dir, "b2c3d4e5")

    assert list_project_ids(state_dir) == ["a1b2c3d4", "b2c3d4e5", "c3d4e5f6"]


def test_list_project_ids_skips_a_directory_without_a_state_file(
    tmp_path: Path,
) -> None:
    """A directory under projects/ with no state.json is not a project."""
    state_dir = tmp_path / "state"
    _write_project_state(state_dir, "a1b2c3d4")
    (state_dir / "projects" / "halfbuilt").mkdir()

    assert list_project_ids(state_dir) == ["a1b2c3d4"]


def test_list_project_ids_skips_a_file_beside_the_project_directories(
    tmp_path: Path,
) -> None:
    """A plain file under projects/ is not a project."""
    state_dir = tmp_path / "state"
    _write_project_state(state_dir, "a1b2c3d4")
    (state_dir / "projects" / "stray.txt").write_text("x", encoding="utf-8")

    assert list_project_ids(state_dir) == ["a1b2c3d4"]


def test_list_project_ids_on_a_missing_projects_directory_returns_empty(
    tmp_path: Path,
) -> None:
    """list_project_ids returns [] when projects/ does not exist."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    assert list_project_ids(state_dir) == []


def test_list_project_ids_on_a_missing_state_directory_returns_empty(
    tmp_path: Path,
) -> None:
    """list_project_ids returns [] when the state directory itself is absent."""
    assert list_project_ids(tmp_path / "state") == []


def test_legacy_state_path_finds_a_root_level_state_file(tmp_path: Path) -> None:
    """legacy_state_path returns the root state.json when one exists."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = state_dir / "state.json"
    legacy.write_text("{}", encoding="utf-8")

    assert legacy_state_path(state_dir) == legacy


def test_legacy_state_path_returns_none_when_there_is_none(tmp_path: Path) -> None:
    """legacy_state_path returns None when the root holds no state.json."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    assert legacy_state_path(state_dir) is None


def test_legacy_state_path_ignores_a_project_state_file(tmp_path: Path) -> None:
    """A state.json under projects/ is the current layout, not a legacy one."""
    state_dir = tmp_path / "state"
    _write_project_state(state_dir, "a1b2c3d4")

    assert legacy_state_path(state_dir) is None


def test_legacy_state_path_does_not_parse_the_file(tmp_path: Path) -> None:
    """legacy_state_path reports the path of a file it cannot decode."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    legacy = state_dir / "state.json"
    legacy.write_text("not json at all", encoding="utf-8")

    assert legacy_state_path(state_dir) == legacy
