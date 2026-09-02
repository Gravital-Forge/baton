"""Baton's MCP tool surface.

Thin methods that validate their input, delegate to the supervisor, and
shape the reply. The state machine — routing, phase logic, persistence —
stays in `baton.engine`; nothing here reimplements it.
"""

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from baton.engine import Supervisor, SupervisorError
from baton.models import Event, LifecycleReport, LifecycleState, ProjectState

RECENT_EVENT_COUNT = 50


def _worker_id(state: ProjectState) -> str | None:
    """Read the current worker's id out of a project state.

    Args:
        state: The project state to read.

    Returns:
        The current worker's id, or None when no worker is running.
    """
    return None if state.worker is None else state.worker.worker_id


def _serialize_report(report: LifecycleReport | None) -> dict[str, object] | None:
    """Shape a lifecycle report for the wire.

    Args:
        report: The report to serialize, or None when nothing has been
            reported yet.

    Returns:
        None when report is None. Otherwise a mapping of its state (as
        the enum's value string), its message, and its next prompt.
    """
    if report is None:
        return None
    return {
        "state": report.state.value,
        "message": report.message,
        "next_prompt": report.next_prompt,
    }


def _serialize_event(event: Event) -> dict[str, object]:
    """Shape an event for the wire.

    Args:
        event: The event to serialize.

    Returns:
        A mapping of the event's timestamp (as an ISO-8601 string), its
        kind (as the enum's value string), its worker id, and its
        payload, unchanged.
    """
    return {
        "timestamp": event.timestamp.isoformat(),
        "kind": event.kind.value,
        "worker_id": event.worker_id,
        "payload": event.payload,
    }


class BatonTools:
    """The MCP tools, bound to one supervisor.

    Each method's docstring is the tool description a worker reads, so it
    is written for that reader.
    """

    def __init__(self, supervisor: Supervisor) -> None:
        """Bind the tools to the supervisor every call delegates to.

        Args:
            supervisor: The supervisor that runs baton's state machine.
        """
        self._supervisor = supervisor

    async def initialize_project(
        self, project_path: str, initial_prompt: str, session_name: str | None = None
    ) -> dict[str, object]:
        """Create a project and launch its first worker.

        The setup agent calls this, not a worker. Baton creates or reuses the
        tmux session, installs the worker protocol skill in the project, and
        launches the first worker with the initial prompt. One daemon
        supervises one project, so the call is refused while a project is
        running, blocked, or terminating.

        Args:
            project_path: The project directory to supervise. It must already
                exist.
            initial_prompt: The task the first worker is launched with.
            session_name: The tmux session to use. Defaults to ``baton-``
                followed by the project directory's name.

        Returns:
            The project's phase, the tmux session name, the worker pane's
            target, and the first worker's id.

        Raises:
            ToolError: If a project is already active, or if project_path is
                not an existing directory.
        """
        try:
            state = await self._supervisor.initialize(
                Path(project_path), initial_prompt, session_name
            )
        except SupervisorError as exc:
            raise ToolError(str(exc)) from exc
        return {
            "phase": state.phase.value,
            "session_name": state.session_name,
            "pane_target": state.pane_target,
            "worker_id": _worker_id(state),
        }

    async def report_status(self, worker_id: str, message: str) -> dict[str, object]:
        """Record a milestone. Baton takes no action on it.

        Use it for meaningful progress, not for narration, and never as a
        lifecycle signal: only report_lifecycle moves the project.

        Args:
            worker_id: Your own worker id, from the ``BATON_WORKER_ID``
                environment variable.
            message: The progress to record.

        Returns:
            The project's phase and the id of the worker baton considers
            current.

        Raises:
            ToolError: If worker_id is not the current worker's id.
        """
        try:
            await self._supervisor.record_status(worker_id, message)
        except SupervisorError as exc:
            raise ToolError(str(exc)) from exc
        state = self._supervisor.snapshot()
        return {"phase": state.phase.value, "worker_id": _worker_id(state)}

    async def report_lifecycle(
        self,
        worker_id: str,
        state: str,
        message: str | None = None,
        next_prompt: str | None = None,
    ) -> dict[str, object]:
        """Report your lifecycle state. This is the only call baton acts on.

        Report exactly one state, explicitly. Terminal prose in your output is
        never a lifecycle signal.

        The states:

        - ``success``: the task is done and more work remains. Supply the next
          prompt; baton launches a fresh worker with it.
        - ``completed``: the whole project is done, not just this task.
        - ``failed``: you could not complete the task. Say what went wrong.
        - ``blocked``: you need a human's input. You stay alive for that human
          and report again once the work can move.
        - ``running``: you are still working. It is for reconciliation only:
          baton records it as the last report and moves no phase.

        The payload rules, in the order baton checks them. The message rule is
        checked first, so a report that breaks both is refused for the message
        alone.

        1. Every state except ``running`` requires a message that is not
           blank. ``running`` may carry one.
        2. ``success`` requires a next prompt that is not blank. Every other
           state, ``running`` included, forbids one.

        ``success``, ``completed`` and ``failed`` are terminal, and the first
        terminal report is final: baton refuses every later report and ends
        your session shortly after.

        Args:
            worker_id: Your own worker id, from the ``BATON_WORKER_ID``
                environment variable.
            state: One of ``running``, ``success``, ``completed``, ``failed``,
                or ``blocked``.
            message: What happened. Required for every state except
                ``running``.
            next_prompt: The prompt for the next worker, whose only context is
                the project's files. Required for ``success`` and forbidden
                otherwise.

        Returns:
            The project's phase after the report and the id of the worker
            baton now considers current, or None when there is none.

        Raises:
            ToolError: If the state is not one of the five, if the payload
                breaks a rule, if worker_id is not the current worker's id, or
                if a terminal report was already made.
        """
        try:
            lifecycle_state = LifecycleState(state)
        except ValueError as exc:
            raise ToolError(
                f"unknown lifecycle state {state!r}; expected one of "
                f"{', '.join(repr(member.value) for member in LifecycleState)}"
            ) from exc

        try:
            await self._supervisor.report_lifecycle(
                worker_id, lifecycle_state, message, next_prompt
            )
        except SupervisorError as exc:
            raise ToolError(str(exc)) from exc

        project_state = self._supervisor.snapshot()
        return {
            "phase": project_state.phase.value,
            "worker_id": _worker_id(project_state),
        }

    async def get_project_status(self) -> dict[str, object]:
        """Read back the project's phase, worker, last report, and events.

        Returns:
            The project's phase, the current worker's id or None, the last
            lifecycle report as its state, message and next prompt (None when
            nothing has been reported), and the 50 most recent events, oldest
            first, each carrying its ISO-8601 timestamp, kind, worker id, and
            payload.
        """
        state = self._supervisor.snapshot()
        events = self._supervisor.recent_events(RECENT_EVENT_COUNT)
        return {
            "phase": state.phase.value,
            "worker_id": _worker_id(state),
            "last_report": _serialize_report(state.last_report),
            "events": [_serialize_event(event) for event in events],
        }
