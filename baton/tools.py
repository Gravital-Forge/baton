"""Baton's MCP tool surface.

Thin methods that validate their input, delegate to the coordinator, and
shape the reply. The state machine — routing, phase logic, persistence —
stays in `baton.engine`, and project identity in `baton.coordinator`;
nothing here reimplements either.
"""

from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from baton.coordinator import Coordinator, CoordinatorError
from baton.engine import SupervisorError
from baton.models import LifecycleState, ProjectState
from baton.tmux import TmuxError

RECENT_EVENT_COUNT = 50


def _worker_id(state: ProjectState) -> str | None:
    """Read the current worker's id out of a project state.

    Args:
        state: The project state to read.

    Returns:
        The current worker's id, or None when no worker is running.
    """
    return None if state.worker is None else state.worker.worker_id


class BatonTools:
    """The MCP tools, bound to one coordinator.

    Each method's docstring is the tool description a worker reads, so it
    is written for that reader.
    """

    def __init__(self, coordinator: Coordinator) -> None:
        """Bind the tools to the coordinator every call delegates to.

        Args:
            coordinator: The coordinator holding every project baton
                supervises.
        """
        self._coordinator = coordinator

    async def initialize_project(
        self,
        project_path: str,
        title: str,
        initial_prompt: str,
        session_name: str | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        """Create a project and launch its first worker.

        The setup agent calls this, not a worker. Baton mints the project an
        id, creates its tmux session, installs the worker protocol skill in
        the project, and launches the first worker with the initial prompt.
        One daemon supervises many projects at once, and two projects may
        share one project directory.

        The title is a display name and need not be unique. It also names the
        tmux session, as ``baton-`` followed by the title's slug: the title
        lowercased, with every run of characters outside ``a-z0-9`` replaced
        by a single ``-``. The call is refused when that session name is
        already held by one of baton's projects, or already exists in tmux;
        pass ``session_name`` to use another. The title and the initial
        prompt must not be blank, and neither may a session name when you
        give one.

        Every worker of this project runs on one explicitly chosen model:
        the ``model`` argument, else the daemon's ``BATON_MODEL``. The call
        is refused when neither sets one — baton never lets Claude Code's own
        default decide.

        Args:
            project_path: The project directory to supervise. It must already
                exist.
            title: The project's display name, and the source of its tmux
                session name.
            initial_prompt: The task the first worker is launched with.
            session_name: The tmux session to use. Defaults to ``baton-``
                followed by the title's slug.
            model: The model every worker of this project runs on. Defaults
                to the daemon's ``BATON_MODEL``.

        Returns:
            The new project's id, its phase, the tmux session name, the
            worker pane's target, the first worker's id, and the chosen
            model. The id is what every later call names this project by.

        Raises:
            ToolError: If the title is blank or holds no letter or digit; if
                the session name is blank, already held by another project,
                or already exists in tmux; if project_path is not an existing
                directory; if the initial prompt or a given model is blank;
                if model is omitted and BATON_MODEL is not set; or if the
                tmux command needed to set up the session fails.
        """
        try:
            state = await self._coordinator.initialize(
                Path(project_path), title, initial_prompt, session_name, model
            )
        except (CoordinatorError, SupervisorError, TmuxError) as exc:
            raise ToolError(str(exc)) from exc
        return {
            "project_id": state.project_id,
            "phase": state.phase.value,
            "session_name": state.session_name,
            "pane_target": state.pane_target,
            "worker_id": _worker_id(state),
            "model": state.model,
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
            ToolError: If worker_id is not the current worker of any project
                baton holds.
        """
        try:
            supervisor = self._coordinator.supervisor_for_worker(worker_id)
            await supervisor.record_status(worker_id, message)
        except (CoordinatorError, SupervisorError) as exc:
            raise ToolError(str(exc)) from exc
        state = supervisor.snapshot()
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
          Baton terminates you and launches a diagnosis worker to
          investigate.
        - ``blocked``: you need a human's input. You stay alive for that human
          and report again once the work can move.
        - ``running``: you are still working. It answers baton's
          reconciliation request after a restart and resumes work from
          ``blocked``: baton returns the project to ``running`` from
          ``blocked`` or from ``reconciling``, and moves no phase
          otherwise.

        The payload rules, in the order baton checks them. The message rule is
        checked first, so a report that breaks both is refused for the message
        alone.

        1. Every state except ``running`` requires a message that is not
           blank. ``running`` may carry one.
        2. ``success`` requires a next prompt that is not blank. Every other
           state, ``running`` included, forbids one.

        ``success``, ``completed`` and ``failed`` are terminal, and a
        terminal report is final: baton refuses every report that follows
        it, and ends your session shortly after.

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
                breaks a rule, if worker_id is not the current worker of any
                project baton holds, or if the project is already
                terminating.
        """
        try:
            lifecycle_state = LifecycleState(state)
        except ValueError as exc:
            raise ToolError(
                f"unknown lifecycle state {state!r}; expected one of "
                f"{', '.join(repr(member.value) for member in LifecycleState)}"
            ) from exc

        try:
            supervisor = self._coordinator.supervisor_for_worker(worker_id)
            await supervisor.report_lifecycle(
                worker_id, lifecycle_state, message, next_prompt
            )
        except (CoordinatorError, SupervisorError) as exc:
            raise ToolError(str(exc)) from exc

        project_state = supervisor.snapshot()
        return {
            "phase": project_state.phase.value,
            "worker_id": _worker_id(project_state),
        }

    async def get_project_status(self, project_id: str) -> dict[str, object]:
        """Read back a project's phase, worker, model, last report, and events.

        Args:
            project_id: The id of the project to read. Your own project's id
                is in the ``BATON_PROJECT`` environment variable.

        Returns:
            The project's phase, the current worker's id or None, the model
            every worker of this project runs on, the last lifecycle report
            as its state, message and next prompt (None when nothing has been
            reported), and the most recent events, oldest first, each
            carrying its ISO-8601 timestamp, kind, worker id, and payload.

        Raises:
            ToolError: If baton holds no project with that id.
        """
        try:
            supervisor = self._coordinator.supervisor(project_id)
        except CoordinatorError as exc:
            raise ToolError(str(exc)) from exc
        state = supervisor.snapshot()
        events = supervisor.recent_events(RECENT_EVENT_COUNT)
        last_report = state.last_report
        return {
            "phase": state.phase.value,
            "worker_id": _worker_id(state),
            "model": state.model,
            "last_report": None if last_report is None else last_report.to_dict(),
            "events": [event.to_dict() for event in events],
        }
