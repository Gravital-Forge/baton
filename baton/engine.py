"""The supervisor engine driving baton's worker lifecycle.

See spec sections 6 (lifecycle protocol), 7 (grace period), 13.8
(termination), and 17 (state machine) for the rules this module encodes.
"""

import asyncio
import signal
import time
from pathlib import Path

from baton.config import BatonConfig
from baton.models import (
    Event,
    EventKind,
    LifecycleReport,
    LifecycleState,
    ProjectPhase,
    ProjectState,
    WorkerRecord,
)
from baton.state import StateStore
from baton.tmux import TmuxAdapter
from baton.worker import WorkerLauncher


class SupervisorError(Exception):
    """A refusal.

    Its message is what the tool layer surfaces to the worker, unchanged.
    """


class Supervisor:
    """Drives one project's worker lifecycle end to end.

    A `Supervisor` launches a project's first worker, receives its
    lifecycle reports, and — on a terminal report — terminates the
    worker and either launches the next one or stops. See spec section
    17 for the phases a project moves through.
    """

    def __init__(
        self,
        config: BatonConfig,
        store: StateStore,
        tmux: TmuxAdapter,
        launcher: WorkerLauncher,
    ) -> None:
        """Bind the supervisor to its collaborators and load persisted state.

        Args:
            config: The runtime configuration governing waits and paths.
            store: The store the supervisor loads its state from and
                persists every change to.
            tmux: The adapter used to create sessions and signal panes.
            launcher: The adapter used to launch and configure workers.
        """
        self._config = config
        self._store = store
        self._tmux = tmux
        self._launcher = launcher
        self._state = store.load()
        self._lock = asyncio.Lock()
        self._finish_task: asyncio.Task[None] | None = None

    async def initialize(
        self, project_path: Path, initial_prompt: str, session_name: str | None = None
    ) -> ProjectState:
        """Start a new project by launching its first worker. See spec §4.

        Args:
            project_path: The project directory to supervise.
            initial_prompt: The prompt the first worker is launched with.
            session_name: The tmux session to use, or None to derive one
                from the project directory's name.

        Returns:
            The committed ProjectState, in phase running.

        Raises:
            SupervisorError: If a project is already running, blocked, or
                terminating, or if project_path is not an existing
                directory.
        """
        async with self._lock:
            if self._state.phase in (
                ProjectPhase.running,
                ProjectPhase.blocked,
                ProjectPhase.terminating,
            ):
                raise SupervisorError(
                    f"cannot initialize while the project at "
                    f"{self._state.project_path} is {self._state.phase.value!r}"
                )

            resolved = project_path.expanduser().resolve()
            if not resolved.is_dir():
                raise SupervisorError(
                    f"project path must be an existing directory, got {str(resolved)!r}"
                )

            session = (
                session_name if session_name is not None else f"baton-{resolved.name}"
            )
            pane_target = f"{session}:worker.0"

            # write_mcp_config runs here, not left to the launcher's own
            # fallback, because the launcher only writes it when it is
            # absent — a changed BATON_PORT would otherwise never reach
            # a worker.
            self._launcher.write_mcp_config()
            self._launcher.install_skill(project_path=resolved)

            if not self._tmux.has_session(session=session):
                self._tmux.create_session(session=session, start_dir=resolved)

            record = self._launch_worker(resolved, pane_target, initial_prompt)
            self._commit(
                ProjectState.fresh().updated(
                    phase=ProjectPhase.running,
                    project_path=resolved,
                    session_name=session,
                    pane_target=pane_target,
                    worker=record,
                )
            )
            return self._state

    async def record_status(self, worker_id: str, message: str) -> None:
        """Record a non-authoritative progress milestone. See spec §8.

        Args:
            worker_id: The id of the worker reporting the milestone.
            message: The milestone text to record.

        Raises:
            SupervisorError: If worker_id is not the current worker's id.
        """
        async with self._lock:
            self._require_current_worker(worker_id)
            self._store.append_event(
                EventKind.milestone, worker_id, {"message": message}
            )

    async def report_lifecycle(
        self,
        worker_id: str,
        state: LifecycleState,
        message: str | None = None,
        next_prompt: str | None = None,
    ) -> None:
        """Record a worker's lifecycle report and route on it. See spec §6.

        Args:
            worker_id: The id of the worker making the report.
            state: The lifecycle state being reported.
            message: A human-readable explanation, required for every
                state except running.
            next_prompt: The next worker's prompt, required for success
                and forbidden otherwise.

        Raises:
            SupervisorError: If the report's payload breaks its state's
                rule, if worker_id is not the current worker's id, or if
                the project is already terminating on an earlier terminal
                report.
        """
        try:
            report = LifecycleReport(
                state=state, message=message, next_prompt=next_prompt
            )
        except ValueError as exc:
            raise SupervisorError(str(exc)) from exc

        async with self._lock:
            self._require_current_worker(worker_id)
            if self._state.phase == ProjectPhase.terminating:
                raise SupervisorError(
                    f"worker {worker_id!r} already reported "
                    f"{self._state.last_report.state.value!r}; the first "
                    "terminal report is final"
                )

            self._store.append_event(
                EventKind.lifecycle,
                worker_id,
                {
                    "state": report.state.value,
                    "message": report.message,
                    "next_prompt": report.next_prompt,
                },
            )

            if report.state == LifecycleState.blocked:
                self._commit(
                    self._state.updated(phase=ProjectPhase.blocked, last_report=report)
                )
                return

            if report.is_terminal:
                worker = self._state.worker
                self._commit(
                    self._state.updated(
                        phase=ProjectPhase.terminating, last_report=report
                    )
                )
                self._finish_task = asyncio.create_task(self._finish(report, worker))
                return

            self._commit(self._state.updated(last_report=report))

    def snapshot(self) -> ProjectState:
        """Return the current in-memory project state.

        Returns:
            The same ProjectState that was last saved to the store.
        """
        return self._state

    def recent_events(self, count: int = 50) -> list[Event]:
        """Return the most recently logged events, oldest first.

        Args:
            count: How many events to return.

        Returns:
            The store's most recent events.
        """
        return self._store.recent_events(count)

    async def wait_for_finish(self) -> None:
        """Wait for any pending background finish to complete.

        This is both a test seam — letting a test await the finish
        triggered by a terminal report — and the server's shutdown seam:
        an exception raised inside the finish surfaces here, at whoever
        awaits it, rather than being lost as an unretrieved task
        exception.

        Raises:
            Exception: Whatever exception the pending finish task raised,
                propagated unchanged.
        """
        if self._finish_task is not None:
            await self._finish_task

    def _commit(self, new_state: ProjectState) -> None:
        """Make a new state current, save it, and log any phase change.

        The single place a new ProjectState is assigned, saved to the
        store, and — only when the phase actually changed — logged as a
        phase event.

        Args:
            new_state: The state to make current.
        """
        old_phase = self._state.phase
        self._state = new_state
        self._store.save(new_state)
        if new_state.phase != old_phase:
            self._store.append_event(
                EventKind.phase,
                None,
                {"from": old_phase.value, "to": new_state.phase.value},
            )

    def _launch_worker(
        self, project_path: Path, pane_target: str, prompt: str
    ) -> WorkerRecord:
        """Launch a worker into the project's pane and log the launch.

        Args:
            project_path: The directory the worker's pane starts in.
            pane_target: The tmux pane the worker is launched into.
            prompt: The task prompt the worker is launched with.

        Returns:
            The record of the launched worker.
        """
        record = self._launcher.launch(
            project_path=project_path, pane_target=pane_target, prompt=prompt
        )
        self._store.append_event(
            EventKind.launch,
            record.worker_id,
            {"prompt_path": str(record.prompt_path), "pane_target": pane_target},
        )
        return record

    def _require_current_worker(self, worker_id: str) -> None:
        """Refuse a call from a worker that is not the current one.

        Args:
            worker_id: The worker id the caller claims to be.

        Raises:
            SupervisorError: If worker_id does not match the current
                worker, naming the current worker when one is running.
        """
        current = self._state.worker
        if current is None:
            raise SupervisorError(
                f"worker {worker_id!r} is not the current worker; no worker is running"
            )
        if worker_id != current.worker_id:
            raise SupervisorError(
                f"worker {worker_id!r} is not the current worker; the "
                f"current worker is {current.worker_id!r}"
            )

    async def _finish(self, report: LifecycleReport, worker: WorkerRecord) -> None:
        """Run the grace period, terminate the worker, then act on the report.

        See spec §7 for the grace period and §17 for where each terminal
        report routes the project next.

        Args:
            report: The terminal lifecycle report that triggered this
                finish, captured when it was scheduled.
            worker: The worker record to terminate, captured when this
                finish was scheduled rather than re-read from the state.
        """
        await asyncio.sleep(self._config.grace_period)
        await self._terminate(worker)

        async with self._lock:
            if report.state == LifecycleState.success:
                record = self._launch_worker(
                    self._state.project_path,
                    self._state.pane_target,
                    report.next_prompt,
                )
                self._commit(
                    self._state.updated(phase=ProjectPhase.running, worker=record)
                )
            elif report.state == LifecycleState.completed:
                self._commit(
                    self._state.updated(phase=ProjectPhase.completed, worker=None)
                )
            elif report.state == LifecycleState.failed:
                self._commit(
                    self._state.updated(phase=ProjectPhase.failed, worker=None)
                )

    async def _terminate(self, worker: WorkerRecord) -> None:
        """Terminate a worker's pane process and log the outcome. See spec §13.8.

        Args:
            worker: The worker record whose pane process is terminated.
        """
        pid = worker.pane_pid
        if pid is None:
            pane_info = self._tmux.pane_info(target=self._state.pane_target)
            pid = None if pane_info.dead else pane_info.pid

        signals: list[str] = []
        if pid is not None:
            signals.append(signal.SIGTERM.name)
            delivered = self._signal_worker(pid, signal.SIGTERM)
            if delivered and not await self._pane_died():
                signals.append(signal.SIGKILL.name)
                self._signal_worker(pid, signal.SIGKILL)

        self._store.append_event(
            EventKind.terminate, worker.worker_id, {"pid": pid, "signals": signals}
        )

    def _signal_worker(self, pid: int, signum: signal.Signals) -> bool:
        """Signal a worker's process, tolerating one that has already exited.

        Args:
            pid: The process to signal.
            signum: The signal to send.

        Returns:
            True when the signal was delivered, False when the process had
            already exited — which is termination's own goal, already met,
            and so leaves nothing to escalate to.
        """
        try:
            self._tmux.signal_pane(pid=pid, signum=signum)
        except ProcessLookupError:
            return False
        return True

    async def _pane_died(self) -> bool:
        """Poll the worker's pane until it dies or the timeout elapses.

        Returns:
            True when the pane reported dead, False when it was still
            alive once the termination timeout elapsed. The pane is read
            before any wait, so a zero timeout still asks once.
        """
        deadline = time.monotonic() + self._config.termination_timeout
        while not self._tmux.pane_info(target=self._state.pane_target).dead:
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self._config.poll_interval)
        return True
