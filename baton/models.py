"""Lifecycle model for baton's supervisor state."""

import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Self


class LifecycleState(StrEnum):
    """The lifecycle state a worker reports through an MCP call.

    See spec section 6 for the payload rules each state enforces.
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

    See spec section 6 for the payload rule each state enforces.

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
