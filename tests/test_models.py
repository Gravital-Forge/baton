"""Tests for baton.models."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from baton.models import (
    Event,
    EventKind,
    LifecycleReport,
    LifecycleState,
    ProjectPhase,
    ProjectState,
    WorkerRecord,
)

TERMINAL_MESSAGE_REQUIRED_STATES = [
    LifecycleState.completed,
    LifecycleState.failed,
    LifecycleState.blocked,
]


def _assert_names_value_and_rule(
    excinfo: pytest.ExceptionInfo[ValueError], offending: object, rule_phrase: str
) -> None:
    """Assert a raised ValueError names the offending value and the rule.

    Args:
        excinfo: The captured exception info from ``pytest.raises``.
        offending: The value that broke the rule; its ``repr`` must appear
            in the exception message.
        rule_phrase: The phrase naming which rule failed; it must also
            appear in the exception message.
    """
    message = str(excinfo.value)
    assert repr(offending) in message
    assert rule_phrase in message


def _legal_report(state: LifecycleState) -> LifecycleReport:
    """Build a report with the minimal legal payload for a state.

    Args:
        state: The lifecycle state to build a report for.

    Returns:
        A LifecycleReport that satisfies the payload rule for state.
    """
    if state == LifecycleState.success:
        return LifecycleReport(state=state, message="ok", next_prompt="next")
    if state == LifecycleState.running:
        return LifecycleReport(state=state)
    return LifecycleReport(state=state, message="ok")


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (LifecycleState.running, "running"),
        (LifecycleState.success, "success"),
        (LifecycleState.completed, "completed"),
        (LifecycleState.failed, "failed"),
        (LifecycleState.blocked, "blocked"),
    ],
)
def test_lifecycle_state_members_spell_their_value(
    member: LifecycleState, value: str
) -> None:
    """Each LifecycleState member's value matches its documented spelling."""
    assert member.value == value


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (ProjectPhase.uninitialized, "uninitialized"),
        (ProjectPhase.running, "running"),
        (ProjectPhase.recovering, "recovering"),
        (ProjectPhase.reconciling, "reconciling"),
        (ProjectPhase.blocked, "blocked"),
        (ProjectPhase.terminating, "terminating"),
        (ProjectPhase.completed, "completed"),
        (ProjectPhase.failed, "failed"),
        (ProjectPhase.closed, "closed"),
    ],
)
def test_project_phase_members_spell_their_value(
    member: ProjectPhase, value: str
) -> None:
    """Each ProjectPhase member's value matches its documented spelling."""
    assert member.value == value


@pytest.mark.parametrize(
    ("member", "value"),
    [
        (EventKind.milestone, "milestone"),
        (EventKind.lifecycle, "lifecycle"),
        (EventKind.launch, "launch"),
        (EventKind.terminate, "terminate"),
        (EventKind.vanish, "vanish"),
        (EventKind.reconcile, "reconcile"),
        (EventKind.phase, "phase"),
    ],
)
def test_event_kind_members_spell_their_value(member: EventKind, value: str) -> None:
    """Each EventKind member's value matches its documented spelling."""
    assert member.value == value


def test_running_report_accepts_a_message() -> None:
    """A running report accepts an optional message."""
    report = LifecycleReport(state=LifecycleState.running, message="still working")

    assert report.state == LifecycleState.running
    assert report.message == "still working"
    assert report.next_prompt is None


def test_running_report_accepts_no_message() -> None:
    """A running report accepts having no message at all."""
    report = LifecycleReport(state=LifecycleState.running)

    assert report.state == LifecycleState.running
    assert report.message is None
    assert report.next_prompt is None


@pytest.mark.parametrize("next_prompt", ["", "do the next thing"])
def test_running_report_rejects_a_next_prompt(next_prompt: str) -> None:
    """A running report forbids any next prompt, blank included."""
    with pytest.raises(ValueError) as excinfo:
        LifecycleReport(state=LifecycleState.running, next_prompt=next_prompt)

    _assert_names_value_and_rule(excinfo, next_prompt, "forbids a next prompt")


def test_success_report_accepts_message_and_next_prompt() -> None:
    """A success report accepts both a message and a next prompt."""
    report = LifecycleReport(
        state=LifecycleState.success,
        message="task finished",
        next_prompt="start the next task",
    )

    assert report.state == LifecycleState.success
    assert report.message == "task finished"
    assert report.next_prompt == "start the next task"


@pytest.mark.parametrize("message", [None, "", "   "])
def test_success_report_rejects_a_missing_or_blank_message(
    message: str | None,
) -> None:
    """A success report requires a non-blank message."""
    with pytest.raises(ValueError) as excinfo:
        LifecycleReport(
            state=LifecycleState.success, message=message, next_prompt="next"
        )

    _assert_names_value_and_rule(excinfo, message, "requires a message")


@pytest.mark.parametrize("next_prompt", [None, "", "   "])
def test_success_report_rejects_a_missing_or_blank_next_prompt(
    next_prompt: str | None,
) -> None:
    """A success report requires a non-blank next prompt."""
    with pytest.raises(ValueError) as excinfo:
        LifecycleReport(
            state=LifecycleState.success, message="done", next_prompt=next_prompt
        )

    _assert_names_value_and_rule(excinfo, next_prompt, "requires a next prompt")


@pytest.mark.parametrize("state", TERMINAL_MESSAGE_REQUIRED_STATES)
def test_message_alone_report_accepts_a_message(state: LifecycleState) -> None:
    """A completed, failed, or blocked report accepts a message alone."""
    report = LifecycleReport(state=state, message="the reason")

    assert report.state == state
    assert report.message == "the reason"
    assert report.next_prompt is None


@pytest.mark.parametrize("state", TERMINAL_MESSAGE_REQUIRED_STATES)
@pytest.mark.parametrize("message", [None, "", "   "])
def test_message_alone_report_rejects_a_missing_or_blank_message(
    state: LifecycleState, message: str | None
) -> None:
    """A completed, failed, or blocked report requires a non-blank message."""
    with pytest.raises(ValueError) as excinfo:
        LifecycleReport(state=state, message=message)

    _assert_names_value_and_rule(excinfo, message, "requires a message")


@pytest.mark.parametrize("state", TERMINAL_MESSAGE_REQUIRED_STATES)
@pytest.mark.parametrize("next_prompt", ["", "an unwanted prompt"])
def test_message_alone_report_rejects_a_next_prompt(
    state: LifecycleState, next_prompt: str
) -> None:
    """A completed, failed, or blocked report forbids any next prompt."""
    with pytest.raises(ValueError) as excinfo:
        LifecycleReport(state=state, message="the reason", next_prompt=next_prompt)

    _assert_names_value_and_rule(excinfo, next_prompt, "forbids a next prompt")


def test_report_checks_the_message_before_the_next_prompt() -> None:
    """When both the message and next prompt are wrong, the message wins."""
    with pytest.raises(ValueError) as excinfo:
        LifecycleReport(
            state=LifecycleState.completed, message=None, next_prompt="unwanted"
        )

    _assert_names_value_and_rule(excinfo, None, "requires a message")
    assert "forbids a next prompt" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (LifecycleState.running, False),
        (LifecycleState.success, True),
        (LifecycleState.completed, True),
        (LifecycleState.failed, True),
        (LifecycleState.blocked, False),
    ],
)
def test_is_terminal_matches_the_state(state: LifecycleState, expected: bool) -> None:
    """is_terminal is True for success, completed, and failed only."""
    report = _legal_report(state)

    assert report.is_terminal is expected


def test_lifecycle_report_to_dict_maps_every_field() -> None:
    """to_dict maps a report to its state value, message, and next prompt."""
    report = LifecycleReport(
        state=LifecycleState.success, message="done", next_prompt="next"
    )

    assert report.to_dict() == {
        "state": "success",
        "message": "done",
        "next_prompt": "next",
    }


def test_lifecycle_report_to_dict_preserves_absent_fields_as_none() -> None:
    """to_dict keeps an absent message and next prompt as None."""
    report = LifecycleReport(state=LifecycleState.running)

    assert report.to_dict() == {
        "state": "running",
        "message": None,
        "next_prompt": None,
    }


def test_worker_record_holds_given_fields(tmp_path: Path) -> None:
    """WorkerRecord stores every field exactly as constructed."""
    launched_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    prompt_path = tmp_path / "prompt.md"

    record = WorkerRecord(
        worker_id="worker-1",
        prompt_path=prompt_path,
        launched_at=launched_at,
        pane_pid=4242,
    )

    assert record.worker_id == "worker-1"
    assert record.prompt_path == prompt_path
    assert record.launched_at == launched_at
    assert record.pane_pid == 4242


def test_worker_record_pane_pid_defaults_to_none(tmp_path: Path) -> None:
    """WorkerRecord accepts an omitted pane_pid, defaulting it to None."""
    launched_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    record = WorkerRecord(
        worker_id="worker-2",
        prompt_path=tmp_path / "other-prompt.md",
        launched_at=launched_at,
    )

    assert record.pane_pid is None


def test_worker_record_to_dict_maps_every_field(tmp_path: Path) -> None:
    """to_dict maps a record to its id, path string, ISO time, and pane pid."""
    prompt_path = tmp_path / "prompt.md"
    record = WorkerRecord(
        worker_id="worker-1",
        prompt_path=prompt_path,
        launched_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        pane_pid=4242,
    )

    assert record.to_dict() == {
        "worker_id": "worker-1",
        "prompt_path": str(prompt_path),
        "launched_at": "2026-09-02T12:00:00+00:00",
        "pane_pid": 4242,
    }


def test_project_state_fresh_is_uninitialized() -> None:
    """fresh() returns an uninitialized state with every optional field None."""
    state = ProjectState.fresh()

    assert state.phase == ProjectPhase.uninitialized
    assert state.project_path is None
    assert state.session_name is None
    assert state.pane_target is None
    assert state.worker is None
    assert state.last_report is None
    assert state.model is None
    assert state.recovery_attempts == 0


def test_project_state_fresh_updated_at_is_timezone_aware_utc() -> None:
    """fresh() stamps updated_at with a timezone-aware UTC value."""
    before = datetime.now(UTC)

    state = ProjectState.fresh()

    after = datetime.now(UTC)
    assert state.updated_at.tzinfo == UTC
    assert before <= state.updated_at <= after


def test_project_state_updated_applies_changes() -> None:
    """updated() returns a copy with the given changes applied."""
    state = ProjectState.fresh()

    changed = state.updated(phase=ProjectPhase.running, session_name="baton-1")

    assert changed.phase == ProjectPhase.running
    assert changed.session_name == "baton-1"


def test_project_state_updated_leaves_other_fields_alone(tmp_path: Path) -> None:
    """updated() leaves fields not named in changes untouched."""
    project_path = tmp_path / "project"
    state = ProjectState.fresh().updated(
        project_path=project_path, pane_target="baton:0.0"
    )

    changed = state.updated(session_name="baton-1")

    assert changed.project_path == project_path
    assert changed.pane_target == "baton:0.0"


def test_project_state_updated_refreshes_updated_at() -> None:
    """updated() refreshes updated_at to a later-or-equal UTC value."""
    state = ProjectState.fresh()

    changed = state.updated(session_name="baton-1")

    assert changed.updated_at.tzinfo == UTC
    assert changed.updated_at >= state.updated_at


def test_project_state_updated_honors_an_explicit_updated_at() -> None:
    """updated() uses an explicit updated_at instead of refreshing it."""
    state = ProjectState.fresh()
    explicit = datetime(2020, 1, 1, tzinfo=UTC)

    changed = state.updated(updated_at=explicit)

    assert changed.updated_at == explicit


def test_project_state_updated_leaves_the_original_unchanged() -> None:
    """updated() does not mutate the original, frozen instance."""
    state = ProjectState.fresh()
    original_phase = state.phase
    original_updated_at = state.updated_at

    state.updated(phase=ProjectPhase.running)

    assert state.phase == original_phase
    assert state.updated_at == original_updated_at


def test_project_state_to_dict_maps_every_field(tmp_path: Path) -> None:
    """to_dict maps a full state, nesting the worker and the last report."""
    project_path = tmp_path / "project"
    prompt_path = tmp_path / "prompt.md"
    state = ProjectState(
        phase=ProjectPhase.running,
        project_path=project_path,
        session_name="baton-project",
        pane_target="baton-project:0.0",
        worker=WorkerRecord(
            worker_id="worker-1",
            prompt_path=prompt_path,
            launched_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
            pane_pid=4242,
        ),
        last_report=LifecycleReport(
            state=LifecycleState.success, message="done", next_prompt="next"
        ),
        model="sonnet",
        recovery_attempts=2,
        updated_at=datetime(2026, 9, 2, 12, 5, tzinfo=UTC),
    )

    assert state.to_dict() == {
        "phase": "running",
        "project_path": str(project_path),
        "session_name": "baton-project",
        "pane_target": "baton-project:0.0",
        "worker": {
            "worker_id": "worker-1",
            "prompt_path": str(prompt_path),
            "launched_at": "2026-09-02T12:00:00+00:00",
            "pane_pid": 4242,
        },
        "last_report": {
            "state": "success",
            "message": "done",
            "next_prompt": "next",
        },
        "model": "sonnet",
        "recovery_attempts": 2,
        "updated_at": "2026-09-02T12:05:00+00:00",
    }


def test_project_state_to_dict_preserves_absent_fields_as_none() -> None:
    """to_dict keeps an uninitialized state's optional fields as None."""
    state = ProjectState(
        phase=ProjectPhase.uninitialized,
        project_path=None,
        session_name=None,
        pane_target=None,
        worker=None,
        last_report=None,
        model=None,
        recovery_attempts=0,
        updated_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
    )

    assert state.to_dict() == {
        "phase": "uninitialized",
        "project_path": None,
        "session_name": None,
        "pane_target": None,
        "worker": None,
        "last_report": None,
        "model": None,
        "recovery_attempts": 0,
        "updated_at": "2026-09-02T12:00:00+00:00",
    }


def test_event_holds_given_fields() -> None:
    """Event stores every field exactly as constructed."""
    timestamp = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    payload: dict[str, object] = {"detail": "started"}

    event = Event(
        timestamp=timestamp,
        kind=EventKind.launch,
        worker_id="worker-1",
        payload=payload,
    )

    assert event.timestamp == timestamp
    assert event.kind == EventKind.launch
    assert event.worker_id == "worker-1"
    assert event.payload == payload


def test_event_accepts_a_none_worker_id() -> None:
    """Event accepts worker_id=None for an event with no associated worker."""
    timestamp = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)

    event = Event(
        timestamp=timestamp,
        kind=EventKind.phase,
        worker_id=None,
        payload={},
    )

    assert event.worker_id is None
    assert event.payload == {}


def test_event_to_dict_maps_every_field() -> None:
    """to_dict maps an event to its ISO timestamp, kind value, worker, payload."""
    event = Event(
        timestamp=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        kind=EventKind.launch,
        worker_id="worker-1",
        payload={"detail": "started"},
    )

    assert event.to_dict() == {
        "timestamp": "2026-09-02T12:00:00+00:00",
        "kind": "launch",
        "worker_id": "worker-1",
        "payload": {"detail": "started"},
    }
