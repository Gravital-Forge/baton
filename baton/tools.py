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


def _project_row(state: ProjectState) -> dict[str, object]:
    """Shape one project's state as a row for list_projects.

    Args:
        state: The project state to shape.

    Returns:
        A mapping of the project's id, title, session name, project path
        as a string, phase, current worker id, model, and the time its
        state last changed as an ISO-8601 string.
    """
    return {
        "project_id": state.project_id,
        "title": state.title,
        "session_name": state.session_name,
        "project_path": (
            None if state.project_path is None else str(state.project_path)
        ),
        "phase": state.phase.value,
        "worker_id": _worker_id(state),
        "model": state.model,
        "updated_at": state.updated_at.isoformat(),
    }


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
        by a single ``-``, and leading and trailing ``-`` stripped. The call
        is refused when that session name is already held by one of baton's
        projects, or already exists in tmux; pass ``session_name`` to use
        another. The title and the initial prompt must not be blank, and
        neither may a session name when you give one.

        A worker of this project runs on one explicitly chosen model when
        nothing overrides it: the ``model`` argument, else the daemon's
        ``BATON_MODEL``. The call is refused when neither sets one — baton
        never lets Claude Code's own default decide.

        Args:
            project_path: The project directory to supervise. It must already
                exist.
            title: The project's display name, and the source of its tmux
                session name.
            initial_prompt: The task the first worker is launched with.
            session_name: The tmux session to use. Defaults to ``baton-``
                followed by the title's slug.
            model: The project's default model, which a worker runs on when
                nothing overrides it. Defaults to the daemon's
                ``BATON_MODEL``.

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

    async def list_projects(self) -> dict[str, object]:
        """List every project baton is supervising, one compact row each.

        The setup agent calls this, not a worker. A project you closed is
        retired and is not listed. Read one project in full, with its
        recent events, through ``get_project_status``.

        Returns:
            A ``projects`` list holding one row per project, in no
            meaningful order, and empty when baton supervises none. Each
            row carries the project's id, title, tmux session name,
            project directory, phase, the current worker's id or None,
            the project's default model, and the time its state last
            changed, as an ISO-8601 string.
        """
        return {
            "projects": [
                _project_row(state) for state in self._coordinator.list_projects()
            ]
        }

    async def resume_project(self, project_id: str, prompt: str) -> dict[str, object]:
        """Carry a stopped project on by launching a fresh worker.

        The setup agent calls this, not a worker. Use it on a project that
        has stopped — one whose phase is ``completed`` or ``failed``. The
        project keeps its id, title, tmux session and event log, and the
        new worker runs on the model the project was created with. A
        project that still has a worker is refused, and so is one you have
        closed: closing retires a project for good.

        The prompt is the whole of the new worker's context, exactly as an
        initial prompt is. That worker remembers nothing of the workers
        before it, so give it enough that its next action is unambiguous.

        Args:
            project_id: The id of the project to carry on.
            prompt: The task the new worker is launched with. It must not
                be blank.

        Returns:
            The project's phase and the id of the worker baton now
            considers current.

        Raises:
            ToolError: If baton holds no project with that id; if the
                project has not stopped; if the prompt is blank; or if the
                tmux command needed to launch the worker fails.
        """
        try:
            state = await self._coordinator.resume(project_id, prompt)
        except (CoordinatorError, SupervisorError, TmuxError) as exc:
            raise ToolError(str(exc)) from exc
        return {"phase": state.phase.value, "worker_id": _worker_id(state)}

    async def close_project(self, project_id: str) -> dict[str, object]:
        """Retire a project for good, stopping any worker it still has.

        The setup agent calls this, not a worker. Baton gives a running
        worker the same grace period a terminal report gets, terminates
        it, kills the project's tmux session, and frees that session name
        for a new project. The project leaves ``list_projects``; its state
        directory stays on disk as the record of what ran, and
        ``get_project_status`` still reads it back. A closed project
        cannot be resumed.

        A project that is finishing with its current worker is refused.
        That lasts seconds — call again.

        Args:
            project_id: The id of the project to retire.

        Returns:
            The project's phase, which is ``closed``, and its current
            worker id, which is None.

        Raises:
            ToolError: If baton holds no project with that id; if the
                project is finishing with its current worker; or if the
                tmux command needed to kill its session fails. A worker
                stopped before such a failure leaves the project
                ``failed`` and not retired, so call again to retire it.
        """
        try:
            state = await self._coordinator.close(project_id)
        except (CoordinatorError, SupervisorError, TmuxError) as exc:
            raise ToolError(str(exc)) from exc
        return {"phase": state.phase.value, "worker_id": _worker_id(state)}

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
        delay_seconds: int | None = None,
        model: str | None = None,
    ) -> dict[str, object]:
        """Report your lifecycle state. This is the only call baton acts on.

        Report exactly one state, explicitly. Terminal prose in your output is
        never a lifecycle signal.

        The states:

        - ``success``: the task is done and more work remains. Supply the next
          prompt; baton launches a fresh worker with it. Name a delay to hold
          the handoff for that long first, and a model to launch that worker
          on one other than the project's.
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
        checked first, and a report that breaks more than one is refused for
        the first of them.

        1. Every state except ``running`` requires a message that is not
           blank. ``running`` may carry one.
        2. ``success`` requires a next prompt that is not blank. Every other
           state, ``running`` included, forbids one.
        3. ``success`` alone may carry a delay. It is a whole number of
           seconds, and not negative.
        4. ``success`` alone may carry a model. It must not be blank, and it
           governs the next worker only: the worker after that one runs on
           the project's model again, as does every worker whose report
           names no model.

        A delay is also no larger than the daemon's configured maximum, which
        defaults to 24 hours. Baton checks that after every payload rule, and
        refuses the whole report when a delay exceeds it, rather than trimming
        it to fit.

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
            delay_seconds: How long baton holds after terminating you, before
                it launches the next worker. Allowed only with ``success``.
                Omit it, or pass ``0``, to launch the next worker at once.
            model: The model the next worker runs on, for that worker alone.
                Allowed only with ``success``. Omit it to launch on the
                project's model. Whatever ``claude --model`` accepts on this
                machine is legal — an alias, or a model's full name, as
                ``claude --help`` describes it.

        Returns:
            The project's phase after the report and the id of the worker
            baton now considers current, or None when there is none.

        Raises:
            ToolError: If the state is not one of the five, if the payload
                breaks a rule, if the model is blank or is given with any
                state but ``success``, if the delay exceeds the daemon's
                configured maximum, if worker_id is not the current worker
                of any project baton holds, or if the project is already
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
                worker_id, lifecycle_state, message, next_prompt, delay_seconds, model
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
            a worker of this project runs on when nothing overrides it, the
            moment a holding project launches its next worker (None when no
            hold is pending), the last lifecycle report as its state,
            message, next prompt, delay and model (None when nothing has
            been reported), and the most recent events, oldest first, each
            carrying its ISO-8601 timestamp, kind, worker id, and payload.
            Each ``launch`` event among them names the model that worker
            actually started on.

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
            "resume_at": (
                None if state.resume_at is None else state.resume_at.isoformat()
            ),
            "last_report": None if last_report is None else last_report.to_dict(),
            "events": [event.to_dict() for event in events],
        }
