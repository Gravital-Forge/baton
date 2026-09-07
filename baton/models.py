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
    recovering = "recovering"
    reconciling = "reconciling"
    blocked = "blocked"
    waiting = "waiting"
    terminating = "terminating"
    completed = "completed"
    failed = "failed"
    closed = "closed"


class EventKind(StrEnum):
    """The kind of an entry recorded in baton's event log."""

    milestone = "milestone"
    lifecycle = "lifecycle"
    launch = "launch"
    terminate = "terminate"
    vanish = "vanish"
    reconcile = "reconcile"
    phase = "phase"


def _is_blank(value: str | None) -> bool:
    """Check whether a value is missing or a whitespace-only string.

    Args:
        value: The value to check.

    Returns:
        True if value is None or contains only whitespace.
    """
    return value is None or value.strip() == ""


def _is_legal_delay(value: object) -> bool:
    """Check whether a value is a delay a success report may carry.

    Args:
        value: The value to check.

    Returns:
        True if value is a non-negative integer. A bool is refused
        because ``isinstance(True, int)`` holds, so a JSON ``true``
        would otherwise read as a one-second delay.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


@dataclass(frozen=True)
class LifecycleReport:
    """A worker's report of its lifecycle state.

    Attributes:
        state: The lifecycle state being reported.
        message: A human-readable explanation. Required for every state
            except ``running``, where it is optional.
        next_prompt: The next worker's prompt. Required for ``success``
            and forbidden for every other state.
        delay_seconds: How long baton holds after terminating this
            worker, before it launches the next one. Allowed only on
            ``success``, where it must be a whole, non-negative number.
            None or 0 launches the next worker at once.
    """

    state: LifecycleState
    message: str | None = None
    next_prompt: str | None = None
    delay_seconds: int | None = None

    def __post_init__(self) -> None:
        """Validate the payload against this report's state.

        Raises:
            ValueError: If the message, the next prompt, or the delay
                breaks the payload rule for this report's state.
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

        if self.delay_seconds is not None:
            if self.state != LifecycleState.success:
                raise ValueError(
                    f"a {self.state.value!r} report forbids a delay, "
                    f"got {self.delay_seconds!r}"
                )
            if not _is_legal_delay(self.delay_seconds):
                raise ValueError(
                    "a delay must be a whole number of seconds and not negative, "
                    f"got {self.delay_seconds!r}"
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
            message, the next prompt, and the delay in seconds, each
            None preserved.
        """
        return {
            "state": self.state.value,
            "message": self.message,
            "next_prompt": self.next_prompt,
            "delay_seconds": self.delay_seconds,
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
        project_id: The identifier baton minted for this project.
        title: The project's display name, chosen by whoever created it.
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
        model: The model every worker of this project runs on, or None
            before initialization.
        recovery_attempts: The number of diagnosis workers launched since
            the last success report.
        resume_at: The moment a pending hold ends and the next worker
            launches, or None when no hold is pending. The caller is
            responsible for passing a timezone-aware UTC value.
        updated_at: When this state was last written. The caller is
            responsible for passing a timezone-aware UTC value.
    """

    project_id: str
    title: str
    phase: ProjectPhase
    project_path: Path | None
    session_name: str | None
    pane_target: str | None
    worker: WorkerRecord | None
    last_report: LifecycleReport | None
    model: str | None
    recovery_attempts: int
    resume_at: datetime | None
    updated_at: datetime

    @classmethod
    def new(cls, project_id: str, title: str) -> Self:
        """Build the state of a project baton has minted but not initialized.

        Args:
            project_id: The identifier baton minted for the project.
            title: The project's display name.

        Returns:
            A ProjectState in phase ``uninitialized``, carrying the given
            id and title, with every optional field None,
            recovery_attempts at 0, and updated_at stamped to now.
        """
        return cls(
            project_id=project_id,
            title=title,
            phase=ProjectPhase.uninitialized,
            project_path=None,
            session_name=None,
            pane_target=None,
            worker=None,
            last_report=None,
            model=None,
            recovery_attempts=0,
            resume_at=None,
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
            A mapping of the project id, the title, the phase as the
            enum's value string, the project path as a string, the
            session name, the pane target, the worker and last report as
            their own mappings, the model, the recovery attempt count,
            and the resume moment and updated_at as ISO 8601 strings.
            Every None is preserved.
        """
        return {
            "project_id": self.project_id,
            "title": self.title,
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
            "model": self.model,
            "recovery_attempts": self.recovery_attempts,
            "resume_at": None if self.resume_at is None else self.resume_at.isoformat(),
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
