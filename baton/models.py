"""Lifecycle model for baton's supervisor state."""

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self


class LifecycleState(StrEnum):
    """The lifecycle state a worker reports through an MCP call.

    See "The lifecycle protocol" in docs/architecture.md for the payload
    rules each state enforces.
    """

    running = "running"
    success = "success"
    completed = "completed"
    failed = "failed"
    blocked = "blocked"


class ProjectPhase(StrEnum):
    """The phase of a baton-supervised project."""

    uninitialized = "uninitialized"
    running = "running"
    blocked = "blocked"
    terminating = "terminating"
    completed = "completed"
    failed = "failed"


class EventKind(StrEnum):
    """The kind of an entry recorded in baton's event log."""

    milestone = "milestone"
    lifecycle = "lifecycle"
    launch = "launch"
    terminate = "terminate"
    phase = "phase"


def _is_blank(value: str | None) -> bool:
    """Check whether a value is missing or a whitespace-only string.

    Args:
        value: The value to check.

    Returns:
        True if value is None or contains only whitespace.
    """
    return value is None or value.strip() == ""


@dataclass(frozen=True)
class LifecycleReport:
    """A worker's report of its lifecycle state.

    Attributes:
        state: The lifecycle state being reported.
        message: A human-readable explanation. Required for every state
            except ``running``, where it is optional.
        next_prompt: The next worker's prompt. Required for ``success``
            and forbidden for every other state.
    """

    state: LifecycleState
    message: str | None = None
    next_prompt: str | None = None

    def __post_init__(self) -> None:
        """Validate the payload against this report's state.

        Raises:
            ValueError: If the message or next prompt breaks the payload
                rule for this report's state.
        """
        message_required = self.state != LifecycleState.running
        if message_required and _is_blank(self.message):
            raise ValueError(
                f"a {self.state.value!r} report requires a message, "
                f"got {self.message!r}"
            )

        if self.state == LifecycleState.success:
            if _is_blank(self.next_prompt):
                raise ValueError(
                    f"a {self.state.value!r} report requires a next prompt, "
                    f"got {self.next_prompt!r}"
                )
        elif self.next_prompt is not None:
            raise ValueError(
                f"a {self.state.value!r} report forbids a next prompt, "
                f"got {self.next_prompt!r}"
            )

    @property
    def is_terminal(self) -> bool:
        """Whether this report ends the worker's run.

        Returns:
            True for success, completed, and failed; False otherwise.
        """
        return self.state in (
            LifecycleState.success,
            LifecycleState.completed,
            LifecycleState.failed,
        )

    def to_dict(self) -> dict[str, object]:
        """Shape this report as a JSON-ready mapping.

        Returns:
            A mapping of the state as the enum's value string, the
            message, and the next prompt, each None preserved.
        """
        return {
            "state": self.state.value,
            "message": self.message,
            "next_prompt": self.next_prompt,
        }


@dataclass(frozen=True)
class WorkerRecord:
    """The running worker a project's state is tracking.

    Attributes:
        worker_id: The identifier baton assigned to the worker.
        prompt_path: The path to the prompt file the worker was launched
            with.
        launched_at: When the worker was launched. The caller is
            responsible for passing a timezone-aware UTC value.
        pane_pid: The process ID of the tmux pane's process, or None
            before it is known.
    """

    worker_id: str
    prompt_path: Path
    launched_at: datetime
    pane_pid: int | None = None

    def to_dict(self) -> dict[str, object]:
        """Shape this record as a JSON-ready mapping.

        Returns:
            A mapping of the worker id, the prompt path as a string, the
            launch time as an ISO 8601 string, and the pane pid.
        """
        return {
            "worker_id": self.worker_id,
            "prompt_path": str(self.prompt_path),
            "launched_at": self.launched_at.isoformat(),
            "pane_pid": self.pane_pid,
        }


@dataclass(frozen=True)
class ProjectState:
    """The supervised project's current state.

    Attributes:
        phase: The project's current phase.
        project_path: The project directory baton is supervising, or
            None before initialization.
        session_name: The tmux session name baton launched, or None
            before initialization.
        pane_target: The tmux pane target the worker runs in, or None
            before initialization.
        worker: The currently running worker, or None when no worker is
            active.
        last_report: The most recent lifecycle report, or None before one
            has arrived.
        updated_at: When this state was last written. The caller is
            responsible for passing a timezone-aware UTC value.
    """

    phase: ProjectPhase
    project_path: Path | None
    session_name: str | None
    pane_target: str | None
    worker: WorkerRecord | None
    last_report: LifecycleReport | None
    updated_at: datetime

    @classmethod
    def fresh(cls) -> Self:
        """Build the state of a project baton has not yet initialized.

        Returns:
            A ProjectState in phase ``uninitialized``, with every
            optional field None and updated_at stamped to now.
        """
        return cls(
            phase=ProjectPhase.uninitialized,
            project_path=None,
            session_name=None,
            pane_target=None,
            worker=None,
            last_report=None,
            updated_at=datetime.now(UTC),
        )

    def updated(self, **changes: object) -> Self:
        """Return a copy of this state with changes applied.

        Args:
            changes: Field values to override, applied via
                ``dataclasses.replace``. updated_at is refreshed to the
                current UTC time unless changes names it explicitly.

        Returns:
            A new ProjectState with changes applied.
        """
        changes.setdefault("updated_at", datetime.now(UTC))
        return dataclasses.replace(self, **changes)

    def to_dict(self) -> dict[str, object]:
        """Shape this state as a JSON-ready mapping.

        Returns:
            A mapping of the phase as the enum's value string, the
            project path as a string, the session name, the pane target,
            the worker and last report as their own mappings, and
            updated_at as an ISO 8601 string. Every None is preserved.
        """
        return {
            "phase": self.phase.value,
            "project_path": (
                None if self.project_path is None else str(self.project_path)
            ),
            "session_name": self.session_name,
            "pane_target": self.pane_target,
            "worker": None if self.worker is None else self.worker.to_dict(),
            "last_report": (
                None if self.last_report is None else self.last_report.to_dict()
            ),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class Event:
    """An entry in baton's event log.

    Attributes:
        timestamp: When the event happened. The caller is responsible
            for passing a timezone-aware UTC value.
        kind: The kind of event.
        worker_id: The worker the event concerns, or None when the event
            has no associated worker.
        payload: The event's data. The dict is not itself frozen; callers
            pass a fresh one.
    """

    timestamp: datetime
    kind: EventKind
    worker_id: str | None
    payload: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        """Shape this event as a JSON-ready mapping.

        Returns:
            A mapping of the timestamp as an ISO 8601 string, the kind as
            the enum's value string, the worker id, and the payload,
            unchanged.
        """
        return {
            "timestamp": self.timestamp.isoformat(),
            "kind": self.kind.value,
            "worker_id": self.worker_id,
            "payload": self.payload,
        }
