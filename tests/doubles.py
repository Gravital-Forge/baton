"""Test doubles standing in for baton's adapters and its supervisor.

Each double records what it was called with and runs no real command, no
real process, and no real state machine. `tests/conftest.py` hands the
default of each one over as a fixture; a test that scripts its own
constructs it here.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from baton.engine import SupervisorError
from baton.models import (
    Event,
    LifecycleState,
    ProjectState,
    WorkerRecord,
)
from baton.tmux import PaneInfo


class FakeTmux:
    """A stand-in for `TmuxAdapter` that records calls and runs no command."""

    def __init__(
        self,
        *,
        sessions: Sequence[str] = (),
        pane_infos: Sequence[PaneInfo] = (),
        create_error: Exception | None = None,
        pane_info_error: Exception | None = None,
        kill_errors: Sequence[Exception | None] = (),
        send_error: Exception | None = None,
    ) -> None:
        """Store the scripted sessions, pane readings, and errors.

        Args:
            sessions: The session names that exist before any call.
            pane_infos: The `PaneInfo` values `pane_info` returns, in call
                order. The last one repeats once they run out. With none
                scripted, `pane_info` returns a dead pane with no pid.
            create_error: The exception `create_session` raises, when set,
                instead of recording the call.
            pane_info_error: The exception `pane_info` raises, when set,
                instead of recording the call and returning a reading.
            kill_errors: What `signal_pane` raises on each call, in call
                order, where `None` is a signal that lands. The last entry
                repeats once they run out.
            send_error: The exception `send_keys` raises, when set, instead
                of recording the call.
        """
        self.sessions: set[str] = set(sessions)
        self._pane_infos = list(pane_infos)
        self._create_error = create_error
        self._pane_info_error = pane_info_error
        self._kill_errors = list(kill_errors)
        self._send_error = send_error
        self.has_session_calls: list[str] = []
        self.created: list[dict[str, object]] = []
        self.kill_session_calls: list[str] = []
        self.respawn_calls: list[dict[str, object]] = []
        self.pane_info_calls: list[str] = []
        self.signals: list[tuple[int, int]] = []
        self.send_keys_calls: list[tuple[str, str]] = []

    def has_session(self, session: str) -> bool:
        """Record the query and report whether the named session exists.

        Args:
            session: The session name to look up.

        Returns:
            True when session is in the scripted set of existing sessions.
        """
        self.has_session_calls.append(session)
        return session in self.sessions

    def create_session(self, session: str, start_dir: Path) -> None:
        """Record the call, or raise the scripted error.

        Args:
            session: The name to give the new session.
            start_dir: The directory the new session starts in.

        Raises:
            Exception: `create_error`, when one was scripted.
        """
        if self._create_error is not None:
            raise self._create_error
        self.created.append({"session": session, "start_dir": start_dir})
        self.sessions.add(session)

    def kill_session(self, session: str) -> None:
        """Record the call and drop the session from the existing set.

        Args:
            session: The name of the session that was killed.
        """
        self.kill_session_calls.append(session)
        self.sessions.discard(session)

    def respawn_pane(
        self, target: str, start_dir: Path, env: Mapping[str, str], command: str
    ) -> None:
        """Record the call instead of respawning a real pane.

        Args:
            target: The pane target that was respawned.
            start_dir: The directory the respawned pane starts in.
            env: The environment mapping that was passed.
            command: The command string that was passed.
        """
        self.respawn_calls.append(
            {
                "target": target,
                "start_dir": start_dir,
                "env": dict(env),
                "command": command,
            }
        )

    def send_keys(self, target: str, text: str) -> None:
        """Record the call, or raise the scripted error.

        Args:
            target: The pane target that was typed into.
            text: The text that was typed.

        Raises:
            Exception: `send_error`, when one was scripted.
        """
        if self._send_error is not None:
            raise self._send_error
        self.send_keys_calls.append((target, text))

    def pane_info(self, target: str) -> PaneInfo:
        """Record the query and return the next scripted `PaneInfo`.

        Args:
            target: The pane target that was queried.

        Returns:
            The next scripted `PaneInfo`, repeating the last one once the
            script is exhausted, or a dead pane with no pid when none was
            scripted.

        Raises:
            Exception: `pane_info_error`, when one was scripted.
        """
        if self._pane_info_error is not None:
            raise self._pane_info_error
        self.pane_info_calls.append(target)
        if not self._pane_infos:
            return PaneInfo(dead=True, pid=None)
        index = min(len(self.pane_info_calls) - 1, len(self._pane_infos) - 1)
        return self._pane_infos[index]

    def signal_pane(self, pid: int, signum: int) -> None:
        """Record the call, then raise this call's scripted error, if any.

        Args:
            pid: The process ID that was signalled.
            signum: The signal number that was sent.

        Raises:
            Exception: The error scripted for this call, when one was.
        """
        self.signals.append((pid, signum))
        if not self._kill_errors:
            return
        index = min(len(self.signals) - 1, len(self._kill_errors) - 1)
        error = self._kill_errors[index]
        if error is not None:
            raise error


class FakeLauncher:
    """A stand-in for `WorkerLauncher` that writes prompt files and runs no process."""

    def __init__(
        self,
        state_dir: Path,
        *,
        pane_pid: int | None = 4242,
        launch_errors: Sequence[Exception | None] = (),
    ) -> None:
        """Store where prompts are written and how each launch behaves.

        Args:
            state_dir: The directory prompt files are written under.
            pane_pid: The `pane_pid` every launched `WorkerRecord` carries.
            launch_errors: What `launch` raises on each call, in call
                order, where `None` is a launch that succeeds. The last
                entry repeats once they run out.
        """
        self._state_dir = state_dir
        self._pane_pid = pane_pid
        self._launch_errors = list(launch_errors)
        self.launches: list[dict[str, object]] = []
        self.installs: list[Path] = []

    def launch(
        self, project_path: Path, pane_target: str, prompt: str, model: str
    ) -> WorkerRecord:
        """Record the call, write the prompt file, and return a worker record.

        Args:
            project_path: The directory the worker's pane starts in.
            pane_target: The tmux pane the worker is launched into.
            prompt: The task prompt given to the worker.
            model: The model the worker is launched with.

        Returns:
            A `WorkerRecord` naming this launch's worker, in launch order.

        Raises:
            Exception: The error scripted for this call, when one was.
        """
        self._raise_scripted_launch_error()
        self.launches.append(
            {
                "project_path": project_path,
                "pane_target": pane_target,
                "prompt": prompt,
                "model": model,
            }
        )
        worker_id = f"worker-{len(self.launches)}"
        worker_dir = self._state_dir / "workers" / worker_id
        worker_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = worker_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        return WorkerRecord(
            worker_id=worker_id,
            prompt_path=prompt_path,
            launched_at=datetime.now(UTC),
            pane_pid=self._pane_pid,
        )

    def _raise_scripted_launch_error(self) -> None:
        """Raise the error scripted for the launch about to be made.

        Raises:
            Exception: The error scripted for this call, when one was.
        """
        if not self._launch_errors:
            return
        index = min(len(self.launches), len(self._launch_errors) - 1)
        error = self._launch_errors[index]
        if error is not None:
            raise error

    def install_skill(self, project_path: Path) -> Path:
        """Record the project path and return the skill's would-be path.

        Args:
            project_path: The project directory to install the skill into.

        Returns:
            The path the skill would be written to. Nothing is written.
        """
        self.installs.append(project_path)
        return project_path / ".claude" / "skills" / "baton-worker" / "SKILL.md"


class StubSupervisor:
    """A stand-in for `Supervisor` that records calls and can raise on cue.

    Each of the four methods `BatonTools` delegates to records the
    arguments it received, unless a scripted error was given for it, in
    which case it raises that instead. `snapshot` and `recent_events`
    always return what was scripted.
    """

    def __init__(
        self,
        *,
        state: ProjectState | None = None,
        events: list[Event] | None = None,
        initialize_error: Exception | None = None,
        record_status_error: SupervisorError | None = None,
        report_lifecycle_error: SupervisorError | None = None,
    ) -> None:
        """Store the state and events to return, and the errors to raise.

        Args:
            state: The `ProjectState` `snapshot` returns, and that
                `initialize` returns unless `initialize_error` is set.
                Defaults to `ProjectState.fresh()`.
            events: The events `recent_events` returns. Defaults to none.
            initialize_error: The error `initialize` raises, when set,
                instead of recording its call. Widened beyond
                `SupervisorError` so a test can script a `TmuxError`.
            record_status_error: The error `record_status` raises, when
                set, instead of recording its call.
            report_lifecycle_error: The error `report_lifecycle` raises,
                when set, instead of recording its call.
        """
        self._state = ProjectState.fresh() if state is None else state
        self._events = [] if events is None else events
        self._initialize_error = initialize_error
        self._record_status_error = record_status_error
        self._report_lifecycle_error = report_lifecycle_error
        self.initialize_calls: list[dict[str, object]] = []
        self.record_status_calls: list[dict[str, object]] = []
        self.report_lifecycle_calls: list[dict[str, object]] = []
        self.recent_events_calls: list[int] = []
        self.hook_calls: list[str] = []

    async def initialize(
        self,
        project_path: Path,
        initial_prompt: str,
        session_name: str | None = None,
        model: str | None = None,
    ) -> ProjectState:
        """Record the call and return the scripted state, or raise.

        Args:
            project_path: The project directory passed in.
            initial_prompt: The initial prompt passed in.
            session_name: The session name passed in.
            model: The model passed in.

        Returns:
            The scripted state.

        Raises:
            Exception: `initialize_error`, when one was scripted.
        """
        if self._initialize_error is not None:
            raise self._initialize_error
        self.initialize_calls.append(
            {
                "project_path": project_path,
                "initial_prompt": initial_prompt,
                "session_name": session_name,
                "model": model,
            }
        )
        return self._state

    async def record_status(self, worker_id: str, message: str) -> None:
        """Record the call, or raise the scripted error.

        Args:
            worker_id: The worker id passed in.
            message: The message passed in.

        Raises:
            SupervisorError: `record_status_error`, when one was scripted.
        """
        if self._record_status_error is not None:
            raise self._record_status_error
        self.record_status_calls.append({"worker_id": worker_id, "message": message})

    async def report_lifecycle(
        self,
        worker_id: str,
        state: LifecycleState,
        message: str | None = None,
        next_prompt: str | None = None,
    ) -> None:
        """Record the call, or raise the scripted error.

        Args:
            worker_id: The worker id passed in.
            state: The lifecycle state passed in.
            message: The message passed in.
            next_prompt: The next prompt passed in.

        Raises:
            SupervisorError: `report_lifecycle_error`, when one was
                scripted.
        """
        if self._report_lifecycle_error is not None:
            raise self._report_lifecycle_error
        self.report_lifecycle_calls.append(
            {
                "worker_id": worker_id,
                "state": state,
                "message": message,
                "next_prompt": next_prompt,
            }
        )

    def snapshot(self) -> ProjectState:
        """Return the scripted state.

        Returns:
            The state given to the constructor.
        """
        return self._state

    def recent_events(self, count: int) -> list[Event]:
        """Record the requested count and return the scripted events.

        Args:
            count: The number of events requested.

        Returns:
            The events given to the constructor.
        """
        self.recent_events_calls.append(count)
        return self._events

    async def start(self) -> None:
        """Record that the pane watchdog was started."""
        self.hook_calls.append("start")

    async def shutdown(self) -> None:
        """Record that the pane watchdog was stopped and work drained."""
        self.hook_calls.append("shutdown")
