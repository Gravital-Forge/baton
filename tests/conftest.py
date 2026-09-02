"""Fixtures shared across baton's test suite.

A plain fixture hands over the default double; a `make_` fixture hands
over the class or a factory, for a test that scripts its own. The doubles
themselves live in `tests/doubles.py`.
"""

from collections.abc import Callable
from functools import partial
from importlib import resources
from pathlib import Path

import pytest

from baton.config import BatonConfig
from baton.state import StateStore
from tests.doubles import FakeLauncher, FakeTmux, StubSupervisor


@pytest.fixture
def anyio_backend() -> str:
    """Restrict anyio's pytest plugin to the asyncio backend.

    The plugin that reads this fixture arrives with `anyio`, which the
    `mcp` runtime dependency brings in; it is not a declared dev
    dependency, and the plan forbids adding one.

    Returns:
        The string "asyncio". The engine uses `asyncio.Lock` and
        `asyncio.create_task`, so trio must not be exercised.
    """
    return "asyncio"


@pytest.fixture
def config(tmp_path: Path) -> BatonConfig:
    """Build a `BatonConfig` with every wait collapsed to zero.

    A test that needs other values replaces them with
    `dataclasses.replace`.

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
        A `StateStore` pointed at `config.state_dir`, which does not yet
        exist on disk.
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


@pytest.fixture
def skill_text() -> str:
    """Read the worker skill file baton ships, the way baton reads it.

    Returns:
        The packaged SKILL.md text, loaded through `importlib.resources`.
    """
    return (
        resources.files("baton")
        .joinpath("skill", "SKILL.md")
        .read_text(encoding="utf-8")
    )


@pytest.fixture
def make_tmux() -> type[FakeTmux]:
    """Hand over the `FakeTmux` class, for a test that scripts its own.

    Returns:
        The `FakeTmux` class, called with the sessions, pane readings, and
        errors the test wants scripted.
    """
    return FakeTmux


@pytest.fixture
def tmux() -> FakeTmux:
    """Build a default `FakeTmux` with no sessions or scripted panes.

    Returns:
        A `FakeTmux` with an empty session set and no scripted pane info.
    """
    return FakeTmux()


@pytest.fixture
def make_launcher(config: BatonConfig) -> Callable[..., FakeLauncher]:
    """Build a factory for `FakeLauncher`s over the config's state directory.

    Args:
        config: The config naming the state directory to write prompts
            under.

    Returns:
        A callable taking `FakeLauncher`'s keyword arguments, with the
        state directory already bound.
    """
    return partial(FakeLauncher, config.state_dir)


@pytest.fixture
def launcher(config: BatonConfig) -> FakeLauncher:
    """Build a default `FakeLauncher` over the config's state directory.

    Args:
        config: The config naming the state directory to write prompts
            under.

    Returns:
        A `FakeLauncher` with the default pane pid and no launch error.
    """
    return FakeLauncher(config.state_dir)


@pytest.fixture
def make_stub_supervisor() -> type[StubSupervisor]:
    """Hand over the `StubSupervisor` class, for a test that scripts its own.

    Returns:
        The `StubSupervisor` class, called with the state, events, and
        errors the test wants scripted.
    """
    return StubSupervisor
