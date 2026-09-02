"""Tests for baton.config."""

import dataclasses
from pathlib import Path

import pytest

from baton.config import BatonConfig


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """Create a directory holding executable `claude` and `tmux` stub binaries.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The directory containing the two stub binaries.
    """
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in ("claude", "tmux"):
        stub = directory / name
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
    return directory


def test_defaults_come_from_the_documented_values(bin_dir: Path) -> None:
    """Every field takes its documented default when unset."""
    config = BatonConfig.from_env({"PATH": str(bin_dir)})

    assert config.host == "127.0.0.1"
    assert config.port == 8910
    assert config.state_dir == Path.home() / ".local" / "state" / "baton"
    assert config.claude_bin == bin_dir / "claude"
    assert config.tmux_bin == bin_dir / "tmux"
    assert config.grace_period == 20
    assert config.termination_timeout == 5
    assert config.poll_interval == 2


def test_every_field_reads_its_environment_variable(bin_dir: Path) -> None:
    """Every field takes its value from its matching environment variable."""
    claude_bin = bin_dir / "claude"
    tmux_bin = bin_dir / "tmux"
    environ = {
        "PATH": str(bin_dir),
        "BATON_HOST": "192.0.2.10",
        "BATON_PORT": "9999",
        "BATON_STATE_DIR": "/custom/state",
        "BATON_CLAUDE_BIN": str(claude_bin),
        "BATON_TMUX_BIN": str(tmux_bin),
        "BATON_GRACE_PERIOD": "30",
        "BATON_TERMINATION_TIMEOUT": "10",
        "BATON_POLL_INTERVAL": "4",
    }

    config = BatonConfig.from_env(environ)

    assert config.host == "192.0.2.10"
    assert config.port == 9999
    assert config.state_dir == Path("/custom/state")
    assert config.claude_bin == claude_bin
    assert config.tmux_bin == tmux_bin
    assert config.grace_period == 30
    assert config.termination_timeout == 10
    assert config.poll_interval == 4


def test_state_dir_expands_a_tilde(bin_dir: Path) -> None:
    """A tilde in BATON_STATE_DIR expands to the user's home directory."""
    environ = {"PATH": str(bin_dir), "BATON_STATE_DIR": "~/custom-state"}

    config = BatonConfig.from_env(environ)

    assert config.state_dir == Path.home() / "custom-state"


def test_relative_binary_path_becomes_absolute(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative BATON_CLAUDE_BIN resolves to an absolute path."""
    monkeypatch.chdir(bin_dir.parent)
    environ = {"PATH": str(bin_dir), "BATON_CLAUDE_BIN": "bin/claude"}

    config = BatonConfig.from_env(environ)

    assert config.claude_bin == bin_dir / "claude"
    assert config.claude_bin.is_absolute()


def test_missing_claude_binary_raises_value_error(bin_dir: Path) -> None:
    """A missing claude binary raises ValueError naming the binary."""
    (bin_dir / "claude").unlink()

    with pytest.raises(ValueError, match="claude"):
        BatonConfig.from_env({"PATH": str(bin_dir)})


def test_missing_tmux_binary_raises_value_error(bin_dir: Path) -> None:
    """A missing tmux binary raises ValueError naming the binary."""
    (bin_dir / "tmux").unlink()

    with pytest.raises(ValueError, match="tmux"):
        BatonConfig.from_env({"PATH": str(bin_dir)})


def test_from_env_reads_the_process_environment_by_default(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Calling from_env with no argument reads the process environment."""
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("BATON_PORT", "7777")

    config = BatonConfig.from_env()

    assert config.port == 7777


def test_config_is_frozen(bin_dir: Path) -> None:
    """Assigning to a field of BatonConfig raises FrozenInstanceError."""
    config = BatonConfig.from_env({"PATH": str(bin_dir)})

    with pytest.raises(dataclasses.FrozenInstanceError):
        config.host = "changed"
