"""The supervisor engine driving baton's worker lifecycle.

See docs/architecture.md for the rules this module encodes: "The lifecycle
protocol" and "The normal loop" state the protocol, and "Recovery",
"Restart reconciliation" and "Shutdown" state what the daemon does around
it.
"""

import asyncio
import logging
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
from baton.prompts import RECONCILIATION_REQUEST, diagnosis_prompt
from baton.state import StateStore
from baton.tmux import TmuxAdapter
from baton.worker import WorkerLauncher, write_mcp_config

_log = logging.getLogger(__name__)


def _pane_target(session: str) -> str:
    """Compute the tmux pane a project's worker is launched into.

    Args:
        session: The tmux session name.

    Returns:
        The pane target within the session's sole worker window.
    """
    return f"{session}:worker.0"


class SupervisorError(Exception):
    """A refusal.

    Its message is what the tool layer surfaces to the worker, unchanged.
    """


class Supervisor:
    """Drives one project's worker lifecycle end to end.

    A `Supervisor` launches its project's first worker, receives its
    lifecycle reports, and — on a terminal report — terminates the
    worker and either launches the next one or stops. See "The normal
    loop" in docs/architecture.md for the phases a project moves through.
    The project it drives is named by the state it is built with:
    `baton.coordinator` mints that identity and runs the one watchdog
    that ticks every supervisor.
    """

    def __init__(
        self,
        config: BatonConfig,
        store: StateStore,
        tmux: TmuxAdapter,
        launcher: WorkerLauncher,
        state: ProjectState,
    ) -> None:
        """Bind the supervisor to its collaborators and its project's state.

        Args:
            config: The runtime configuration governing waits and paths.
            store: The store the supervisor persists every change to.
            tmux: The adapter used to create sessions and signal panes.
            launcher: The adapter used to launch and configure workers.
            state: The project's current state, either loaded from the
                store or newly minted by the coordinator.
        """
        self._config = config
        self._store = store
        self._tmux = tmux
        self._launcher = launcher
        self._state = state
        self._lock = asyncio.Lock()
        self._finish_task: asyncio.Task[None] | None = None
        self._reconciliation_deadline: float | None = None

    async def initialize(
        self,
        project_path: Path,
        initial_prompt: str,
        model: str | None = None,
    ) -> ProjectState:
        """Start this project by launching its first worker.

        See "The tmux layout" in docs/architecture.md for the session
        layout, and "The lifecycle protocol" for the skill every worker
        is given. The project's id, title and tmux session name are
        already on the state the coordinator built this supervisor with.

        Args:
            project_path: The project directory to supervise.
            initial_prompt: The prompt the first worker is launched with.
            model: The model every worker of this project runs on, or
                None to use the daemon's configured default (`BATON_MODEL`).

        Returns:
            The committed ProjectState, in phase running.

        Raises:
            SupervisorError: If project_path is not an existing directory;
                if initial_prompt or a given model is blank; or if model
                is None and no default is configured.
            TmuxError: If checking whether the session exists, creating
                it, or launching the worker fails.
        """
        async with self._lock:
            resolved = project_path.expanduser().resolve()
            if not resolved.is_dir():
                raise SupervisorError(
                    f"project path must be an existing directory, got {str(resolved)!r}"
                )
            if initial_prompt.strip() == "":
                raise SupervisorError(
                    f"initial prompt must not be blank, got {initial_prompt!r}"
                )
            if model is not None and model.strip() == "":
                raise SupervisorError(f"model must not be blank, got {model!r}")
            chosen_model = model.strip() if model is not None else self._config.model
            if chosen_model is None:
                raise SupervisorError(
                    "no model chosen: pass model to initialize_project or set "
                    "BATON_MODEL"
                )

            session = self._state.session_name
            pane_target = _pane_target(session)

            write_mcp_config(self._config)
            self._launcher.install_skill(project_path=resolved)

            record = self._launch_worker(
                resolved, session, initial_prompt, chosen_model
            )
            self._commit(
                self._state.updated(
                    phase=ProjectPhase.running,
                    project_path=resolved,
                    pane_target=pane_target,
                    worker=record,
                    model=chosen_model,
                )
            )
            return self._state

    async def resume(self, prompt: str) -> ProjectState:
        """Carry a stopped project on with a fresh worker.

        The project keeps its id, title, session name, path, model and
        event log; only its worker is new. The recovery attempts reset
        and the last report is cleared, so a stopped worker's outcome is
        not read back as though it were the new worker's.

        Args:
            prompt: The task the new worker is launched with.

        Returns:
            The committed ProjectState, in phase running.

        Raises:
            SupervisorError: If the project still has a worker of its own
                — any phase but completed or failed — or if prompt is
                blank.
            TmuxError: If creating the session or launching the worker
                fails.
        """
        async with self._lock:
            phase = self._state.phase
            if phase not in (ProjectPhase.completed, ProjectPhase.failed):
                raise SupervisorError(
                    f"only a project in phase 'completed' or 'failed' can be "
                    f"resumed, got {phase.value!r}"
                )
            if prompt.strip() == "":
                raise SupervisorError(f"prompt must not be blank, got {prompt!r}")

            record = self._launch_worker(
                self._state.project_path,
                self._state.session_name,
                prompt,
                self._state.model,
            )
            self._commit(
                self._state.updated(
                    phase=ProjectPhase.running,
                    worker=record,
                    last_report=None,
                    recovery_attempts=0,
                )
            )
            return self._state

    async def close(self) -> ProjectState:
        """Retire the project, terminating any worker it still has.

        The lock is held in two stretches, the way the finish path holds
        it, with the grace period and the termination poll between them.
        A lock held across both would stall the coordinator's watchdog:
        check_worker takes each supervisor's lock in turn, so blocking
        here blocks vanish detection for every other project for as long
        as the two waits take.

        The terminating commit clears last_report for the reason the
        reconciliation timeout does: a terminal report left over from
        the previous worker's handoff would otherwise be resumed as a
        finish at the next startup, relaunching a project the operator
        had retired.

        Returns:
            The committed ProjectState, in phase closed.

        Raises:
            SupervisorError: If the project is already terminating, which
                a finish already under way would commit over the top of.
                That phase lasts seconds; the caller retries.
            TmuxError: If killing the project's session fails. A worker
                terminated before that failure leaves the project failed
                rather than terminating, so a later close can still
                retire it.
        """
        async with self._lock:
            if self._state.phase == ProjectPhase.terminating:
                raise SupervisorError(
                    f"the project is in phase {self._state.phase.value!r} and is "
                    "finishing with its current worker; close it again once "
                    "that finishes"
                )
            worker = self._state.worker
            if worker is None:
                return self._retire()
            self._commit(
                self._state.updated(phase=ProjectPhase.terminating, last_report=None),
                reason="the project was closed",
            )

        try:
            await asyncio.sleep(self._config.grace_period)
            await self._terminate(worker)
            async with self._lock:
                return self._retire()
        except Exception as exc:
            _log.exception("retiring the project failed")
            # The project must not be left in terminating: that phase
            # refuses every later close, every report and every resume,
            # and check_worker skips it, so nothing would move the project
            # until the daemon restarted. failed is a phase a later close
            # can retire.
            async with self._lock:
                self._commit(
                    self._state.updated(phase=ProjectPhase.failed, worker=None),
                    reason=f"retiring the project failed: {exc}",
                )
            raise

    async def record_status(self, worker_id: str, message: str) -> None:
        """Record a non-authoritative progress milestone.

        See "The normal loop" in docs/architecture.md.

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
        """Record a worker's lifecycle report and route on it.

        See "The lifecycle protocol" in docs/architecture.md. Any report
        from the current worker answers an outstanding reconciliation
        request, so it clears the reconciliation deadline.

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
                the project is already terminating.
        """
        try:
            report = LifecycleReport(
                state=state, message=message, next_prompt=next_prompt
            )
        except ValueError as exc:
            raise SupervisorError(str(exc)) from exc

        async with self._lock:
            self._require_current_worker(worker_id)
            self._reconciliation_deadline = None
            if self._state.phase == ProjectPhase.terminating:
                raise SupervisorError(
                    f"worker {worker_id!r} is terminating and baton accepts "
                    "no further report from it"
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

            match report.state:
                case LifecycleState.blocked:
                    self._commit(
                        self._state.updated(
                            phase=ProjectPhase.blocked, last_report=report
                        )
                    )
                case (
                    LifecycleState.success
                    | LifecycleState.completed
                    | LifecycleState.failed
                ):
                    worker = self._state.worker
                    self._commit(
                        self._state.updated(
                            phase=ProjectPhase.terminating, last_report=report
                        )
                    )
                    self._finish_task = asyncio.create_task(
                        self._finish(report, worker)
                    )
                case LifecycleState.running:
                    if self._state.phase in (
                        ProjectPhase.blocked,
                        ProjectPhase.reconciling,
                    ):
                        self._commit(
                            self._state.updated(
                                phase=ProjectPhase.running, last_report=report
                            )
                        )
                    else:
                        self._commit(self._state.updated(last_report=report))

    def snapshot(self) -> ProjectState:
        """Return the current in-memory project state.

        Returns:
            The same ProjectState that was last saved to the store.
        """
        return self._state

    def recent_events(self, count: int) -> list[Event]:
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

    async def check_worker(self) -> None:
        """Run one watchdog tick: recover the current worker if its pane vanished.

        A vanish is a dead pane found with no terminal report on file: the
        worker's process ended without ever calling report_lifecycle. The
        coordinator's watchdog loop is this method's only production
        caller, so a recovery launch that fails here is logged and moves
        the project to failed rather than raised — that loop must keep
        running for the next tick. Contrast _finish, whose failure is
        re-raised, because wait_for_finish exists to surface it to
        whoever awaits it.

        A live pane is also checked against the reconciliation deadline,
        for a project waiting in phase reconciling.
        """
        async with self._lock:
            worker = self._state.worker
            if worker is None or self._state.phase == ProjectPhase.terminating:
                return
            if not self._tmux.pane_info(target=self._state.pane_target).dead:
                self._check_reconciliation_deadline(worker)
                return
            self._store.append_event(
                EventKind.vanish,
                worker.worker_id,
                {
                    "pane_target": self._state.pane_target,
                    "phase": self._state.phase.value,
                },
            )
            reason = "the worker's pane died without a terminal report"
            try:
                self._recover(reason)
            except Exception as exc:  # noqa: BLE001 - the loop must survive this
                _log.exception("recovery after %s failed", reason)
                self._commit(
                    self._state.updated(phase=ProjectPhase.failed, worker=None),
                    reason=f"recovery after {reason} failed: {exc}",
                )

    async def reconcile(self) -> None:
        """Reconcile with a worker left over from before the daemon started.

        This is the daemon's one-time startup work, run once by the
        coordinator's `start` before the watchdog begins. It works under
        the lock and acts by phase. A project in running, blocked,
        recovering, or reconciling has a worker whose current lifecycle
        state baton no longer knows, so its pane is asked to report it. A
        project in terminating has a finish that never ran to completion,
        so that finish is resumed. A project in uninitialized, completed,
        or failed has no worker to reconcile with and is left alone.

        Raises:
            TmuxError: If sending the reconciliation request to the pane
                fails. The coordinator records this project as failed and
                goes on to serve every other one.
        """
        async with self._lock:
            worker = self._state.worker
            if worker is None:
                return
            if self._state.phase in (
                ProjectPhase.running,
                ProjectPhase.blocked,
                ProjectPhase.recovering,
                ProjectPhase.reconciling,
            ):
                self._request_reconciliation(worker)
            elif self._state.phase == ProjectPhase.terminating:
                self._resume_finish(worker)

    async def fail(self, reason: str) -> None:
        """Give the project up, recording why in its own event log.

        The coordinator calls this for a project whose reconciliation
        raised at startup, so that failure is recorded against the
        project it belongs to and reaches no other. The worker's pane is
        left alive: it belongs to no project baton will route to any
        more, and what it holds is what a human needs to read.

        Args:
            reason: Why the project was given up, carried in the phase
                event's payload.
        """
        async with self._lock:
            self._commit(
                self._state.updated(phase=ProjectPhase.failed, worker=None),
                reason=reason,
            )

    async def shutdown(self) -> None:
        """Drain any finish still pending.

        Safe to call whether or not a finish is pending. The coordinator
        stops the watchdog; a supervisor has none of its own.
        """
        await self.wait_for_finish()

    def _commit(self, new_state: ProjectState, reason: str | None = None) -> None:
        """Make a new state current, save it, and log any phase change.

        The single place a new ProjectState is assigned, saved to the
        store, and — only when the phase actually changed — logged as a
        phase event.

        Args:
            new_state: The state to make current.
            reason: Why the phase moved, for a transition the normal loop
                did not choose. Carried in the phase event's payload, and
                only there — so a reason given with a phase that does not
                change is dropped, since no phase event is written. A
                caller that needs the record either way appends its own
                event first, as the vanish and reconcile paths do.
        """
        old_phase = self._state.phase
        self._state = new_state
        self._store.save(new_state)
        if new_state.phase == old_phase:
            return
        payload: dict[str, object] = {
            "from": old_phase.value,
            "to": new_state.phase.value,
        }
        if reason is not None:
            payload["reason"] = reason
        self._store.append_event(EventKind.phase, None, payload)

    def _launch_worker(
        self, project_path: Path, session: str, prompt: str, model: str
    ) -> WorkerRecord:
        """Launch a worker into the project's pane and log the launch.

        Creates the tmux session first when it does not already exist.

        Args:
            project_path: The directory the worker's pane starts in.
            session: The tmux session the worker is launched into.
            prompt: The task prompt the worker is launched with.
            model: The model the worker is launched with.

        Returns:
            The record of the launched worker.

        Raises:
            TmuxError: If creating the session or launching the worker
                fails.
        """
        if not self._tmux.has_session(session=session):
            self._tmux.create_session(session=session, start_dir=project_path)
        pane_target = _pane_target(session)
        record = self._launcher.launch(
            project_id=self._state.project_id,
            project_path=project_path,
            pane_target=pane_target,
            prompt=prompt,
            model=model,
        )
        self._store.append_event(
            EventKind.launch,
            record.worker_id,
            {"prompt_path": str(record.prompt_path), "pane_target": pane_target},
        )
        return record

    def _retire(self) -> ProjectState:
        """Kill the project's tmux session, if it has one, and commit closed.

        Called with the lock held. The session kill is guarded by
        has_session, because an absent session is an ordinary state — a
        tmux server restart, or an operator who killed it — and
        kill_session raises on one.

        Returns:
            The committed ProjectState, in phase closed.

        Raises:
            TmuxError: If killing the session fails.
        """
        session = self._state.session_name
        if self._tmux.has_session(session=session):
            self._tmux.kill_session(session=session)
        self._commit(self._state.updated(phase=ProjectPhase.closed, worker=None))
        return self._state

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

        See "The normal loop" in docs/architecture.md for the grace period
        and for where each terminal report routes the project next.

        Args:
            report: The terminal lifecycle report that triggered this
                finish, captured when it was scheduled.
            worker: The worker record to terminate, captured when this
                finish was scheduled rather than re-read from the state.
        """
        await asyncio.sleep(self._config.grace_period)
        try:
            await self._terminate(worker)
            async with self._lock:
                self._route(report)
        except Exception as exc:
            _log.exception("the handoff after a %r report failed", report.state.value)
            # Whatever went wrong, the project must not be left in
            # terminating: that phase refuses every later report and every
            # new initialization, so nothing would move until the daemon
            # restarted and resumed the finish. The raise keeps the failure
            # retrievable through wait_for_finish.
            async with self._lock:
                self._commit(
                    self._state.updated(phase=ProjectPhase.failed, worker=None),
                    reason=f"the handoff after a {report.state.value!r} "
                    f"report failed: {exc}",
                )
            raise

    async def _finish_abnormal(self, worker: WorkerRecord, reason: str) -> None:
        """Terminate a worker baton gave up on, then recover, with no grace period.

        The counterpart to _finish for a worker with no report to route
        on: one left over from a restart that stayed silent, or one whose
        reconciliation request timed out. There is no report to wait a
        grace period for, and nothing to route — only the worker to
        terminate and recovery to start.

        Args:
            worker: The worker record to terminate, captured when this
                finish was scheduled rather than re-read from the state.
            reason: Why the worker was given up on. Passed to _recover on
                success, and folded into the phase event's reason on
                failure.

        Raises:
            Exception: Whatever _terminate or _recover raised, after the
                project is committed to failed. The raise keeps the
                failure retrievable through wait_for_finish.
        """
        try:
            await self._terminate(worker)
            async with self._lock:
                self._recover(reason)
        except Exception as exc:
            _log.exception("the abnormal finish after %s failed", reason)
            async with self._lock:
                self._commit(
                    self._state.updated(phase=ProjectPhase.failed, worker=None),
                    reason=f"the abnormal finish after {reason} failed: {exc}",
                )
            raise

    def _route(self, report: LifecycleReport) -> None:
        """Move the project on from a terminated worker's report.

        See "The normal loop" in docs/architecture.md.

        Args:
            report: The terminal lifecycle report to act on.
        """
        if report.state == LifecycleState.success:
            record = self._launch_worker(
                self._state.project_path,
                self._state.session_name,
                report.next_prompt,
                self._state.model,
            )
            self._commit(
                self._state.updated(
                    phase=ProjectPhase.running, worker=record, recovery_attempts=0
                )
            )
        elif report.state == LifecycleState.completed:
            self._commit(self._state.updated(phase=ProjectPhase.completed, worker=None))
        elif report.state == LifecycleState.failed:
            self._recover(f"the worker reported failed: {report.message}")

    def _recover(self, reason: str) -> None:
        """Launch a diagnosis worker, or stop at the recovery cap.

        Called with the lock held and a current worker in the state. See
        "Recovery" in docs/architecture.md for the recovery cycle.

        Args:
            reason: Why recovery was triggered. Recorded as the phase
                event's reason, and, when a diagnosis worker is launched,
                included in its prompt.

        Raises:
            TmuxError: If launching the diagnosis worker fails.
        """
        previous = self._state.worker
        attempts = self._state.recovery_attempts
        if attempts >= self._config.recovery_cap:
            self._commit(
                self._state.updated(phase=ProjectPhase.failed, worker=None),
                reason=f"recovery cap of {self._config.recovery_cap} reached: {reason}",
            )
            return
        prompt = diagnosis_prompt(
            project_path=self._state.project_path,
            previous_worker_id=previous.worker_id,
            previous_prompt_path=previous.prompt_path,
            reason=reason,
            events_path=self._store.events_path,
            attempt=attempts + 1,
            cap=self._config.recovery_cap,
        )
        record = self._launch_worker(
            self._state.project_path,
            self._state.session_name,
            prompt,
            self._state.model,
        )
        self._commit(
            self._state.updated(
                phase=ProjectPhase.recovering,
                worker=record,
                recovery_attempts=attempts + 1,
            ),
            reason=reason,
        )

    def _request_reconciliation(self, worker: WorkerRecord) -> None:
        """Ask a live worker to report its current lifecycle state.

        Called with the lock held, for a worker whose pane may still be
        live from before the daemon started. A dead pane is left
        untouched: the watchdog's first tick finds it, records the
        vanish, and recovers, the same as any other vanish. The reason
        given to the phase commit below never reaches the event log when
        the phase is already reconciling, since _commit logs a phase
        event only on a real change; the reconcile event appended here is
        what records the request in that case.

        Args:
            worker: The current worker, to log the reconcile event
                against.

        Raises:
            TmuxError: If sending the request to the pane fails. Reading
                the pane cannot raise it: a failed read is a dead pane.
        """
        if self._tmux.pane_info(target=self._state.pane_target).dead:
            return
        self._tmux.send_keys(
            target=self._state.pane_target, text=RECONCILIATION_REQUEST
        )
        self._store.append_event(
            EventKind.reconcile,
            worker.worker_id,
            {"timeout": self._config.reconciliation_timeout},
        )
        self._reconciliation_deadline = (
            time.monotonic() + self._config.reconciliation_timeout
        )
        self._commit(
            self._state.updated(phase=ProjectPhase.reconciling),
            reason="the daemon restarted with a live worker",
        )

    def _resume_finish(self, worker: WorkerRecord) -> None:
        """Resume a finish that the daemon was in the middle of before restarting.

        Called with the lock held, for a project found in terminating at
        startup. A terminal last report means the worker did report
        before the daemon stopped, so its finish resumes exactly as it
        would have; anything else means the worker's outcome is unknown,
        so the finish that runs is the abnormal one.

        Args:
            worker: The worker the resumed finish terminates.
        """
        last_report = self._state.last_report
        if last_report is not None and last_report.is_terminal:
            self._finish_task = asyncio.create_task(self._finish(last_report, worker))
            return
        reason = "the daemon restarted while terminating the worker"
        self._finish_task = asyncio.create_task(self._finish_abnormal(worker, reason))

    def _check_reconciliation_deadline(self, worker: WorkerRecord) -> None:
        """Give up on a live worker that never answered its reconciliation request.

        Called with the lock held, for a pane check_worker found alive
        while the project is in phase reconciling. An unset deadline counts
        as not passed, since there is no deadline to have passed.

        Clearing last_report on the commit below is deliberate: a
        success report left over from the previous worker's handoff
        would otherwise be resumed as a finish at the next startup,
        relaunching the task with no diagnosis worker.

        Args:
            worker: The current worker, handed to the abnormal finish
                scheduled once the deadline has passed.
        """
        deadline = self._reconciliation_deadline
        if self._state.phase != ProjectPhase.reconciling or deadline is None:
            return
        if time.monotonic() >= deadline:
            reason = (
                "the worker did not answer the reconciliation request within "
                f"{self._config.reconciliation_timeout} seconds"
            )
            self._commit(
                self._state.updated(phase=ProjectPhase.terminating, last_report=None),
                reason=reason,
            )
            self._finish_task = asyncio.create_task(
                self._finish_abnormal(worker, reason)
            )

    async def _terminate(self, worker: WorkerRecord) -> None:
        """Terminate a worker's pane process and log the outcome.

        See "The normal loop" in docs/architecture.md.

        Args:
            worker: The worker record whose pane process is terminated.
        """
        pid = self._worker_pid(worker)

        signals: list[str] = []
        if pid is not None and self._signal_worker(pid, signal.SIGTERM):
            signals.append(signal.SIGTERM.name)
            if not await self._pane_died() and self._signal_worker(pid, signal.SIGKILL):
                signals.append(signal.SIGKILL.name)

        self._store.append_event(
            EventKind.terminate, worker.worker_id, {"pid": pid, "signals": signals}
        )

    def _worker_pid(self, worker: WorkerRecord) -> int | None:
        """Find the process to terminate, asking the pane rather than the record.

        Args:
            worker: The worker whose launch-time pid is the fallback.

        Returns:
            The pid to signal, or None when there is nothing to signal.
            A dead pane has no process left, and its recorded pid may by
            now belong to something else. A live pane's own pid outranks
            the recorded one, which a respawn would have made stale; the
            record stands in only where tmux reports no pid at all.
        """
        pane_info = self._tmux.pane_info(target=self._state.pane_target)
        if pane_info.dead:
            return None
        return pane_info.pid if pane_info.pid is not None else worker.pane_pid

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
