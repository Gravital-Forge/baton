"""Persistent supervisor state and baton's append-only event log."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from baton.models import (
    Event,
    EventKind,
    LifecycleReport,
    LifecycleState,
    ProjectPhase,
    ProjectState,
    WorkerRecord,
)


def _projects_dir(state_dir: Path) -> Path:
    """Compose the directory every project's own state directory sits under.

    Args:
        state_dir: The daemon's state directory.

    Returns:
        The path `<state_dir>/projects`.
    """
    return state_dir / "projects"


def project_state_dir(state_dir: Path, project_id: str) -> Path:
    """Compose the directory holding one project's state and event log.

    Args:
        state_dir: The daemon's state directory.
        project_id: The id of the project.

    Returns:
        The path `<state_dir>/projects/<project_id>`. Nothing is created.
    """
    return _projects_dir(state_dir) / project_id


def list_project_ids(state_dir: Path) -> list[str]:
    """List the id of every project persisted under the state directory.

    Args:
        state_dir: The daemon's state directory.

    Returns:
        The sorted ids of the directories under `projects/` that hold a
        state.json, or an empty list when `projects/` is absent. A
        directory with no state.json is not a project baton wrote.
    """
    projects_dir = _projects_dir(state_dir)
    if not projects_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in projects_dir.iterdir()
        if entry.is_dir() and (entry / "state.json").exists()
    )


def legacy_state_path(state_dir: Path) -> Path | None:
    """Find the single-project layout's state file at the state directory root.

    Args:
        state_dir: The daemon's state directory.

    Returns:
        The path `<state_dir>/state.json` when it exists, or None. The
        file is never read: its shape is by definition the one baton no
        longer writes.
    """
    path = state_dir / "state.json"
    return path if path.exists() else None


def _decode_optional_path(value: object) -> Path | None:
    """Decode a JSON value into an optional Path.

    Args:
        value: The decoded JSON value, expected to be a string or None.

    Returns:
        A Path built from value, or None when value is None.
    """
    return None if value is None else Path(value)


def _decode_time(value: object) -> datetime:
    """Decode an ISO 8601 string into a datetime.

    Args:
        value: The decoded JSON value, expected to be an ISO 8601 string.

    Returns:
        The datetime parsed from value.
    """
    return datetime.fromisoformat(value)


def _decode_worker(raw: Mapping[str, object] | None) -> WorkerRecord | None:
    """Decode a WorkerRecord from its JSON mapping.

    Args:
        raw: The decoded JSON mapping for a worker, or None.

    Returns:
        A WorkerRecord built from raw, or None when raw is None.
    """
    if raw is None:
        return None
    return WorkerRecord(
        worker_id=raw["worker_id"],
        prompt_path=Path(raw["prompt_path"]),
        launched_at=_decode_time(raw["launched_at"]),
        pane_pid=raw["pane_pid"],
    )


def _decode_report(raw: Mapping[str, object] | None) -> LifecycleReport | None:
    """Decode a LifecycleReport from its JSON mapping.

    Args:
        raw: The decoded JSON mapping for a report, or None.

    Returns:
        A LifecycleReport built from raw, or None when raw is None.
    """
    if raw is None:
        return None
    return LifecycleReport(
        state=LifecycleState(raw["state"]),
        message=raw["message"],
        next_prompt=raw["next_prompt"],
    )


def _decode_state(raw: Mapping[str, object]) -> ProjectState:
    """Decode a ProjectState from its JSON mapping.

    Args:
        raw: The decoded JSON mapping for a state.json file.

    Returns:
        A ProjectState built from raw, with every type restored.
    """
    return ProjectState(
        phase=ProjectPhase(raw["phase"]),
        project_path=_decode_optional_path(raw["project_path"]),
        session_name=raw["session_name"],
        pane_target=raw["pane_target"],
        worker=_decode_worker(raw["worker"]),
        last_report=_decode_report(raw["last_report"]),
        model=raw["model"],
        recovery_attempts=raw["recovery_attempts"],
        updated_at=_decode_time(raw["updated_at"]),
    )


def _decode_event(raw: Mapping[str, object]) -> Event:
    """Decode an Event from its JSON mapping.

    Args:
        raw: The decoded JSON mapping for one events.jsonl line.

    Returns:
        An Event built from raw, with every type restored.
    """
    return Event(
        timestamp=_decode_time(raw["timestamp"]),
        kind=EventKind(raw["kind"]),
        worker_id=raw["worker_id"],
        payload=dict(raw["payload"]),
    )


class StateStore:
    """Reads and writes baton's persistent state and append-only event log."""

    def __init__(self, state_dir: Path) -> None:
        """Compute the store's paths without touching the filesystem.

        Args:
            state_dir: The directory holding state.json and events.jsonl.
        """
        self.state_dir = state_dir
        self.state_path = state_dir / "state.json"
        self.events_path = state_dir / "events.jsonl"

    def load(self) -> ProjectState:
        """Load the current project state.

        Returns:
            The ProjectState decoded from state_path, or a fresh,
            uninitialized state when state_path does not exist.

        Raises:
            ValueError: If state.json is not valid JSON, holds a value no
                lifecycle type accepts, or holds a lifecycle report that
                breaks a payload rule.
            KeyError: If state.json is missing a key the shape requires.
            TypeError: If a timestamp in state.json is not a string.
        """
        if not self.state_path.exists():
            return ProjectState.fresh()
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        return _decode_state(raw)

    def save(self, state: ProjectState) -> None:
        """Write state to state_path as an atomic replace.

        Args:
            state: The project state to persist.

        Raises:
            OSError: If the state directory cannot be created, or the
                temporary file cannot be written or renamed over the
                target. The previous state.json is left untouched.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp_path = self.state_dir / "state.json.tmp"
        temp_path.write_text(
            json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        try:
            temp_path.replace(self.state_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def append_event(
        self,
        kind: EventKind,
        worker_id: str | None,
        payload: Mapping[str, object],
    ) -> Event:
        """Append one event to the event log.

        Args:
            kind: The kind of event.
            worker_id: The worker the event concerns, or None.
            payload: The event's data; a copy is stored, not the mapping
                itself.

        Returns:
            The Event that was appended, timestamped with the current UTC
            time.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        event = Event(
            timestamp=datetime.now(UTC),
            kind=kind,
            worker_id=worker_id,
            payload=dict(payload),
        )
        with self.events_path.open("a", encoding="utf-8") as events_file:
            events_file.write(json.dumps(event.to_dict()) + "\n")
        return event

    def recent_events(self, count: int) -> list[Event]:
        """Return the most recent events, oldest first.

        Args:
            count: How many events to return. A non-positive count
                returns an empty list.

        Returns:
            The last count events, oldest first, or [] when count is
            non-positive or the event log does not exist.
        """
        if count <= 0 or not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()
        return [_decode_event(json.loads(line)) for line in lines[-count:]]
