"""The coordinator holding every project one baton daemon supervises.

It mints a project's identity, resolves its tmux session name, builds its
store and its supervisor, routes each worker's call to the project that
worker belongs to, and runs the one watchdog that ticks them all. See
docs/architecture.md for the rules around it.
"""

import asyncio
import contextlib
import logging
import re
import secrets
from pathlib import Path

from baton.config import BatonConfig
from baton.engine import Supervisor
from baton.models import ProjectPhase, ProjectState
from baton.state import (
    StateStore,
    legacy_state_path,
    list_project_ids,
    project_state_dir,
)
from baton.tmux import TmuxAdapter
from baton.worker import WorkerLauncher

_log = logging.getLogger(__name__)

_ID_ATTEMPTS = 8

_NON_SLUG = re.compile(r"[^a-z0-9]+")


class CoordinatorError(Exception):
    """A refusal.

    Its message is what the tool layer surfaces to the caller, unchanged.
    """


def _slug(title: str) -> str:
    """Reduce a title to the part of it a tmux session name can carry.

    Args:
        title: The project's display name.

    Returns:
        The title lowercased, with every run of characters outside
        ``a-z0-9`` replaced by a single ``-`` and leading and trailing
        ``-`` stripped. A title holding no such character slugs to "".
    """
    return _NON_SLUG.sub("-", title.lower()).strip("-")


class Coordinator:
    """Holds every project the daemon supervises, keyed by its minted id."""

    def __init__(
        self, config: BatonConfig, tmux: TmuxAdapter, launcher: WorkerLauncher
    ) -> None:
        """Build a supervisor for every project already on disk.

        Args:
            config: The runtime configuration governing every project.
            tmux: The adapter every supervisor creates sessions and
                signals panes through.
            launcher: The adapter every supervisor launches workers
                through.

        Raises:
            CoordinatorError: If the state directory holds a
                single-project state.json at its root, or if a project
                listed on disk has no state to load.
        """
        legacy = legacy_state_path(config.state_dir)
        if legacy is not None:
            raise CoordinatorError(
                f"the state directory holds a single-project state file at "
                f"{legacy}; baton keeps each project under projects/<project id>/"
            )
        self._config = config
        self._tmux = tmux
        self._launcher = launcher
        self._supervisors: dict[str, Supervisor] = {}
        self._watchdog_task: asyncio.Task[None] | None = None
        for project_id in list_project_ids(config.state_dir):
            store = StateStore(project_state_dir(config.state_dir, project_id))
            state = store.load()
            if state is None:
                raise CoordinatorError(
                    f"project {project_id!r} was listed on disk but has no "
                    f"state to load"
                )
            self._supervisors[project_id] = self._build_supervisor(store, state)

    async def start(self) -> None:
        """Reconcile every project, then start the daemon's one watchdog.

        No project's failure reaches another, and none reaches the
        caller. A project whose reconciliation raised is given up,
        recorded as failed in its own event log with the exception named,
        and the daemon goes on to serve every other one.

        The pass gathers every project so a raise arrives as a result to
        act on rather than ending the pass at the project that raised.
        The supervisor map is read into a list before it, so each result
        still lines up with the project it came from.
        """
        projects = list(self._supervisors.items())
        results = await asyncio.gather(
            *(supervisor.reconcile() for _, supervisor in projects),
            return_exceptions=True,
        )
        for (project_id, supervisor), result in zip(projects, results, strict=True):
            if isinstance(result, BaseException):
                _log.error("reconciling project %s failed", project_id, exc_info=result)
                await supervisor.fail(f"reconciliation at startup failed: {result}")
        self._watchdog_task = asyncio.create_task(self._watch())

    async def shutdown(self) -> None:
        """Stop the watchdog and drain every project's pending finish.

        Safe to call whether or not start was ever called. Every
        project's shutdown is awaited even when an earlier one raised, so
        one project's failing finish cannot leave later projects
        undrained. The supervisor map is read into a list before the
        drain, so each result still lines up with the project it came
        from even if the map grows while the drain awaits.
        """
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
        projects = list(self._supervisors.items())
        results = await asyncio.gather(
            *(supervisor.shutdown() for _, supervisor in projects),
            return_exceptions=True,
        )
        for (project_id, _), result in zip(projects, results, strict=True):
            if isinstance(result, BaseException):
                _log.error(
                    "draining project %s at shutdown failed",
                    project_id,
                    exc_info=result,
                )

    async def initialize(
        self,
        project_path: Path,
        title: str,
        initial_prompt: str,
        session_name: str | None = None,
        model: str | None = None,
    ) -> ProjectState:
        """Mint a project, launch its first worker, and register it.

        The project is registered only once its first worker is running,
        so an initialization that raised leaves no project behind.

        Args:
            project_path: The project directory to supervise.
            title: The project's display name. It must not be blank, and
                it must hold at least one character a slug can carry.
            initial_prompt: The prompt the first worker is launched with.
            session_name: The tmux session to use, or None to derive one
                from the title.
            model: The model every worker of this project runs on, or
                None to use the daemon's configured default.

        Returns:
            The new project's committed ProjectState, in phase running.

        Raises:
            CoordinatorError: If the title is blank or slugs to nothing,
                if a given session_name is blank, if the resolved session
                name is already held by a project that is not closed or
                already exists in tmux, or if no unused project id could
                be minted.
            SupervisorError: If project_path is not an existing
                directory, if initial_prompt or a given model is blank,
                or if model is None and no default is configured.
            TmuxError: If creating the session or launching the worker
                fails.
        """
        session = self._resolve_session_name(title, session_name)
        project_id = self._mint_id()
        store = StateStore(project_state_dir(self._config.state_dir, project_id))
        supervisor = self._build_supervisor(
            store, ProjectState.new(project_id, title).updated(session_name=session)
        )
        state = await supervisor.initialize(project_path, initial_prompt, model)
        self._supervisors[project_id] = supervisor
        return state

    def supervisor_for_worker(self, worker_id: str) -> Supervisor:
        """Find the project a reporting worker belongs to.

        Worker ids are UUIDs, so one is unique across every project the
        daemon holds and a worker never has to name its project.

        Args:
            worker_id: The id of the worker making the call.

        Returns:
            The supervisor whose current worker carries that id.

        Raises:
            CoordinatorError: If no project holds that worker as its
                current one.
        """
        for supervisor in self._supervisors.values():
            worker = supervisor.snapshot().worker
            if worker is not None and worker.worker_id == worker_id:
                return supervisor
        raise CoordinatorError(
            f"worker {worker_id!r} is not the current worker of any project"
        )

    def supervisor(self, project_id: str) -> Supervisor:
        """Find a registered project by its id.

        Args:
            project_id: The id of the project to find.

        Returns:
            The supervisor registered under that id.

        Raises:
            CoordinatorError: If no project is registered under that id.
        """
        supervisor = self._supervisors.get(project_id)
        if supervisor is None:
            raise CoordinatorError(f"no project has the id {project_id!r}")
        return supervisor

    async def _watch(self) -> None:
        """Poll every project's worker pane on a fixed interval, forever.

        This is the daemon's one watchdog; it runs until shutdown cancels
        it. A tick that raises for one project is logged and the next
        project is polled, so neither a broken pane nor a missing tmux
        binary can end the loop for every other project. The supervisor
        map is read into a list per tick, because a project registered
        while a tick is awaiting would otherwise resize it mid-iteration.
        """
        while True:
            await asyncio.sleep(self._config.poll_interval)
            for project_id, supervisor in list(self._supervisors.items()):
                try:
                    await supervisor.check_worker()
                except Exception:  # noqa: BLE001 - one project must not end the loop
                    _log.exception(
                        "the watchdog tick for project %s failed", project_id
                    )

    def _build_supervisor(self, store: StateStore, state: ProjectState) -> Supervisor:
        """Build a supervisor over one project's store and state.

        Args:
            store: The store the supervisor persists every change to.
            state: The project's current state.

        Returns:
            A Supervisor bound to the coordinator's own collaborators.
        """
        return Supervisor(
            config=self._config,
            store=store,
            tmux=self._tmux,
            launcher=self._launcher,
            state=state,
        )

    def _resolve_session_name(self, title: str, session_name: str | None) -> str:
        """Resolve the tmux session name a new project takes, refusing a clash.

        Args:
            title: The project's display name, slugged into the default
                session name.
            session_name: The caller's explicit session name, or None.

        Returns:
            The session name the new project takes.

        Raises:
            CoordinatorError: If the title is blank or slugs to nothing,
                if session_name is given but blank, or if the resolved
                name is already held by a project that is not closed or
                already exists in tmux.
        """
        if title.strip() == "":
            raise CoordinatorError(f"title must not be blank, got {title!r}")
        slug = _slug(title)
        if slug == "":
            raise CoordinatorError(
                f"title must hold an ASCII letter or digit to name a session, "
                f"got {title!r}"
            )
        if session_name is not None and session_name.strip() == "":
            raise CoordinatorError(
                f"session name must not be blank, got {session_name!r}"
            )
        session = session_name if session_name is not None else f"baton-{slug}"
        for project_id, supervisor in self._supervisors.items():
            state = supervisor.snapshot()
            if state.phase != ProjectPhase.closed and state.session_name == session:
                raise CoordinatorError(
                    f"session name {session!r} is already held by project "
                    f"{project_id!r}"
                )
        if self._tmux.has_session(session=session):
            raise CoordinatorError(
                f"tmux session {session!r} already exists; pass session_name to "
                f"launch this project into another"
            )
        return session

    def _mint_id(self) -> str:
        """Mint an eight-character project id no project on disk holds.

        Returns:
            Eight lowercase hex characters naming a project directory
            that does not exist.

        Raises:
            CoordinatorError: If every attempt collided with a directory
                already on disk.
        """
        for _ in range(_ID_ATTEMPTS):
            project_id = secrets.token_hex(4)
            if not project_state_dir(self._config.state_dir, project_id).exists():
                return project_id
        raise CoordinatorError(
            f"could not mint an unused project id in {_ID_ATTEMPTS} attempts"
        )
