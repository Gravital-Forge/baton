"""Tests for baton.config."""

import dataclasses
from pathlib import Path

import pytest

from baton.config import BatonConfig


def _write_stub_binaries(directory: Path) -> Path:
    """Write executable `claude` and `tmux` stubs into a directory.

    Args:
        directory: The directory to create and fill.

    Returns:
        The directory, so a fixture can return the call.
    """
    directory.mkdir(parents=True)
    for name in ("claude", "tmux"):
        stub = directory / name
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
    return directory


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    """Create a directory of stub binaries to use as the injected PATH.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The directory containing the two stub binaries.
    """
    return _write_stub_binaries(tmp_path / "bin")


@pytest.fixture
def off_path_dir(tmp_path: Path) -> Path:
    """Create a directory of stub binaries that is never put on PATH.

    An override test that pointed at a binary on PATH would pass even if the
    override were ignored, so the override targets live here instead.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The directory containing the two stub binaries.
    """
    return _write_stub_binaries(tmp_path / "off-path")


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


def test_every_field_reads_its_environment_variable(
    bin_dir: Path, off_path_dir: Path
) -> None:
    """Every field takes its value from its matching environment variable."""
    claude_bin = off_path_dir / "claude"
    tmux_bin = off_path_dir / "tmux"
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
    bin_dir: Path, off_path_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative BATON_CLAUDE_BIN becomes an absolute path."""
    monkeypatch.chdir(off_path_dir.parent)
    environ = {"PATH": str(bin_dir), "BATON_CLAUDE_BIN": "off-path/claude"}

    config = BatonConfig.from_env(environ)

    assert config.claude_bin == off_path_dir / "claude"
    assert config.claude_bin.is_absolute()


def test_explicit_binary_path_that_does_not_exist_raises_value_error(
    tmp_path: Path,
) -> None:
    """An explicit BATON_CLAUDE_BIN that does not exist raises ValueError naming it."""
    environ = {"BATON_CLAUDE_BIN": str(tmp_path / "missing-claude")}

    with pytest.raises(ValueError, match="claude"):
        BatonConfig.from_env(environ)


def test_explicit_binary_path_that_exists_is_accepted(off_path_dir: Path) -> None:
    """An explicit binary path that exists is accepted for both binaries."""
    environ = {
        "BATON_CLAUDE_BIN": str(off_path_dir / "claude"),
        "BATON_TMUX_BIN": str(off_path_dir / "tmux"),
    }

    config = BatonConfig.from_env(environ)

    assert config.claude_bin == off_path_dir / "claude"
    assert config.tmux_bin == off_path_dir / "tmux"


def test_relative_state_dir_becomes_absolute(
    bin_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative BATON_STATE_DIR becomes absolute, under the working directory."""
    monkeypatch.chdir(tmp_path)
    environ = {"PATH": str(bin_dir), "BATON_STATE_DIR": "custom-state"}

    config = BatonConfig.from_env(environ)

    assert config.state_dir == tmp_path / "custom-state"
    assert config.state_dir.is_absolute()


def test_a_binary_symlink_is_not_followed(tmp_path: Path) -> None:
    """A launcher symlink is kept rather than replaced by its target.

    The claude launcher is a symlink into a versioned install directory, so
    following it would pin baton to one version.
    """
    versioned = _write_stub_binaries(tmp_path / "versions" / "1.0")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "tmux"):
        (bin_dir / name).symlink_to(versioned / name)

    config = BatonConfig.from_env({"PATH": str(bin_dir)})

    assert config.claude_bin == bin_dir / "claude"
    assert config.tmux_bin == bin_dir / "tmux"


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


def test_a_non_integer_interval_names_its_variable(bin_dir: Path) -> None:
    """A non-integer interval raises ValueError naming the variable."""
    environ = {"PATH": str(bin_dir), "BATON_PORT": "not-a-number"}

    with pytest.raises(ValueError, match="BATON_PORT"):
        BatonConfig.from_env(environ)


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
