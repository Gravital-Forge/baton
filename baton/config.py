"""Runtime configuration for baton, read from environment variables."""

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Self

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8910
DEFAULT_STATE_DIR = "~/.local/state/baton"
DEFAULT_GRACE_PERIOD = 20
DEFAULT_TERMINATION_TIMEOUT = 5
DEFAULT_POLL_INTERVAL = 2


def _read_int(environ: Mapping[str, str], key: str, default: int) -> int:
    """Read an integer value from the environment, falling back to a default.

    Args:
        environ: The environment to read from.
        key: The environment variable name to look up.
        default: The value to use when the variable is absent.

    Returns:
        The parsed integer value, or ``default`` when the key is absent.

    Raises:
        ValueError: If the variable is set to something that is not an
            integer.
    """
    value = environ.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {value!r}") from exc


def _resolve_binary(environ: Mapping[str, str], key: str, name: str) -> Path:
    """Find the absolute path to an executable, preferring an override.

    Args:
        environ: The environment to read from.
        key: The environment variable that may hold an explicit path.
        name: The executable name to search PATH for when the variable is
            absent or empty.

    Returns:
        The absolute path to the executable, with any symlink left
        unfollowed.

    Raises:
        ValueError: If the environment variable is set to a value that does
            not resolve to an executable, or if it is absent and a PATH
            search does not locate the executable.
    """
    value = environ.get(key)
    if value:
        found = shutil.which(value)
    else:
        found = shutil.which(name, path=environ.get("PATH", ""))
    if not found:
        raise ValueError(
            f"cannot find the {name!r} binary on PATH; set {key} to its absolute path"
        )
    # The claude launcher is a symlink into a versioned install directory, so
    # resolving it would pin baton to whatever version is installed today and
    # defeat the installer's upgrade path. absolute() is enough: it gives the
    # absolute path a subprocess call needs without following the link.
    return Path(found).expanduser().absolute()


@dataclass(frozen=True)
class BatonConfig:
    """Runtime configuration for baton.

    Attributes:
        host: The address baton's MCP server binds to. Read from
            ``BATON_HOST``. Defaults to ``"127.0.0.1"``.
        port: The port baton's MCP server listens on. Read from
            ``BATON_PORT``. Defaults to ``8910``.
        state_dir: The directory holding baton's state file and event log.
            Read from ``BATON_STATE_DIR``. Defaults to
            ``"~/.local/state/baton"``, expanded to the user's home.
        claude_bin: The absolute path to the ``claude`` binary. Read from
            ``BATON_CLAUDE_BIN``. Defaults to the first ``claude`` found on
            ``PATH``.
        tmux_bin: The absolute path to the ``tmux`` binary. Read from
            ``BATON_TMUX_BIN``. Defaults to the first ``tmux`` found on
            ``PATH``.
        grace_period: Seconds to wait after a worker's terminal report
            before terminating it. Read from ``BATON_GRACE_PERIOD``.
            Defaults to ``20``.
        termination_timeout: Seconds to wait after ``SIGTERM`` for a pane to
            go dead before sending ``SIGKILL``. Read from
            ``BATON_TERMINATION_TIMEOUT``. Defaults to ``5``.
        poll_interval: Seconds between polls of the worker pane's liveness.
            Read from ``BATON_POLL_INTERVAL``. Defaults to ``2``.
    """

    host: str
    port: int
    state_dir: Path
    claude_bin: Path
    tmux_bin: Path
    grace_period: int
    termination_timeout: int
    poll_interval: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Build a config by reading the ``BATON_*`` environment variables.

        Args:
            environ: The environment to read from. When ``None``, reads
                ``os.environ``.

        Returns:
            A config populated from ``environ``, falling back to the
            documented defaults for any variable that is absent.

        Raises:
            ValueError: If an interval variable holds something that is not
                an integer, or if either binary cannot be found.
        """
        if environ is None:
            environ = os.environ

        state_dir = environ.get("BATON_STATE_DIR", DEFAULT_STATE_DIR)

        return cls(
            host=environ.get("BATON_HOST", DEFAULT_HOST),
            port=_read_int(environ, "BATON_PORT", DEFAULT_PORT),
            state_dir=Path(state_dir).expanduser().absolute(),
            claude_bin=_resolve_binary(environ, "BATON_CLAUDE_BIN", "claude"),
            tmux_bin=_resolve_binary(environ, "BATON_TMUX_BIN", "tmux"),
            grace_period=_read_int(environ, "BATON_GRACE_PERIOD", DEFAULT_GRACE_PERIOD),
            termination_timeout=_read_int(
                environ, "BATON_TERMINATION_TIMEOUT", DEFAULT_TERMINATION_TIMEOUT
            ),
            poll_interval=_read_int(
                environ, "BATON_POLL_INTERVAL", DEFAULT_POLL_INTERVAL
            ),
        )
