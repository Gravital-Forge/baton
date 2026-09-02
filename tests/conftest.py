"""Fixtures shared across baton's test suite.

Only what more than one test module needs lives here: the anyio backend the
async tests run on, and the config, state store, and project directory a
supervisor test builds on. A fake that one module uses lives in that module,
because a test module cannot import this one under every pytest import mode.
"""

from pathlib import Path

import pytest

from baton.config import BatonConfig
from baton.state import StateStore


@pytest.fixture
def anyio_backend() -> str:
    """Restrict anyio's pytest plugin to the asyncio backend.

    Returns:
        The string "asyncio". The engine uses `asyncio.Lock` and
        `asyncio.create_task`, so trio must not be exercised.
    """
    return "asyncio"


@pytest.fixture
def config(tmp_path: Path) -> BatonConfig:
    """Build a `BatonConfig` with every wait collapsed to zero.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        A `BatonConfig` usable without touching a real binary, session, or
        clock.
    """
    return BatonConfig(
        host="127.0.0.1",
        port=8910,
        state_dir=tmp_path / "state",
        claude_bin=Path("/usr/bin/claude"),
        tmux_bin=Path("/usr/bin/tmux"),
        grace_period=0,
        termination_timeout=0,
        poll_interval=0,
    )


@pytest.fixture
def store(config: BatonConfig) -> StateStore:
    """Build a `StateStore` over the config's state directory.

    Args:
        config: The config naming the state directory to use.

    Returns:
        A `StateStore` pointed at `config.state_dir`.
    """
    return StateStore(config.state_dir)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create and return a project directory distinct from the state dir.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The created `tmp_path / "project"` directory.
    """
    path = tmp_path / "project"
    path.mkdir()
    return path
