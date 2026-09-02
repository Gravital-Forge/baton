"""Tests for baton.state."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest

from baton.models import (
    EventKind,
    LifecycleReport,
    LifecycleState,
    ProjectPhase,
    ProjectState,
    WorkerRecord,
)
from baton.state import StateStore

GOOD_STATE_JSON = {
    "phase": "running",
    "project_path": None,
    "session_name": None,
    "pane_target": None,
    "worker": None,
    "last_report": None,
    "updated_at": "2026-09-02T16:00:00+00:00",
}


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    """Build a StateStore rooted under a state directory that does not exist.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        A StateStore whose state_dir has not been created on disk.
    """
    return StateStore(tmp_path / "state")


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
    )
    return ProjectState(
        phase=ProjectPhase.running,
        project_path=tmp_path / "proj",
        session_name="baton",
        pane_target="baton:worker.0",
        worker=worker,
        last_report=report,
        updated_at=datetime(2026, 9, 2, 16, 0, 1, tzinfo=UTC),
    )


def test_init_does_not_touch_the_filesystem(store: StateStore, tmp_path: Path) -> None:
    """A store built on a nonexistent directory leaves it nonexistent."""
    assert not store.state_dir.exists()
    assert store.state_dir == tmp_path / "state"
    assert store.state_path == store.state_dir / "state.json"
    assert store.events_path == store.state_dir / "events.jsonl"


def test_load_on_empty_directory_returns_fresh_state(store: StateStore) -> None:
    """load() on a missing state file returns a fresh, uninitialized state."""
    state = store.load()

    assert state.phase == ProjectPhase.uninitialized
    assert state.project_path is None
    assert state.session_name is None
    assert state.pane_target is None
    assert state.worker is None
    assert state.last_report is None
    assert state.updated_at.tzinfo == UTC


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
        phase=ProjectPhase.blocked,
        project_path=None,
        session_name=None,
        pane_target=None,
        worker=worker,
        last_report=report,
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


def _write_state_file(store: StateStore, text: str) -> None:
    """Write raw text straight into the store's state.json.

    Args:
        store: The store whose state file to write.
        text: The exact file contents, valid JSON or not.
    """
    store.state_dir.mkdir(parents=True, exist_ok=True)
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


def test_round_trip_with_no_worker_and_no_report(store: StateStore) -> None:
    """A state with no worker and no report round-trips, writing JSON nulls."""
    state = ProjectState.fresh().updated(phase=ProjectPhase.completed)

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
    assert not store.state_dir.exists()

    store.save(populated_state)

    assert store.state_dir.exists()
    assert store.state_path.exists()


def test_on_disk_json_has_pinned_shape(
    store: StateStore, populated_state: ProjectState
) -> None:
    """The on-disk state.json has the pinned keys, order, and encoding."""
    store.save(populated_state)

    raw_text = store.state_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)

    assert list(raw.keys()) == [
        "phase",
        "project_path",
        "session_name",
        "pane_target",
        "worker",
        "last_report",
        "updated_at",
    ]
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
    assert list(raw["last_report"].keys()) == ["state", "message", "next_prompt"]
    assert raw["last_report"]["state"] == "success"
    assert raw["last_report"]["message"] == "did the thing"
    assert raw["last_report"]["next_prompt"] == "do the next thing"
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
    assert not (store.state_dir / "state.json.tmp").exists()


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
    assert not (store.state_dir / "state.json.tmp").exists()


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
