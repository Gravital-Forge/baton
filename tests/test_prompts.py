"""Tests for baton.prompts: the worker preamble and the packaged skill file."""

import pytest

from baton.prompts import WORKER_PREAMBLE

TOOL_SIGNATURES = [
    "initialize_project(project_path, initial_prompt, session_name=None)",
    "report_status(worker_id, message)",
    "report_lifecycle(worker_id, state, message=None, next_prompt=None)",
    "get_project_status()",
]

LIFECYCLE_STATES = ["success", "completed", "failed", "blocked", "running"]


def _skill_frontmatter_name(text: str) -> str:
    """Read the ``name:`` value out of a skill file's frontmatter block.

    Args:
        text: The full contents of a skill file, including its leading and
            trailing ``---`` frontmatter fences.

    Returns:
        The value that follows ``name:`` in the frontmatter, with
        surrounding whitespace removed.

    Raises:
        ValueError: If the frontmatter has no ``name:`` line.
    """
    _, frontmatter, _ = text.split("---", 2)
    for line in frontmatter.splitlines():
        if line.startswith("name:"):
            return line.removeprefix("name:").strip()
    raise ValueError("frontmatter has no name: line")


def test_preamble_names_the_skill_file() -> None:
    """WORKER_PREAMBLE points the worker at the packaged skill file."""
    assert ".claude/skills/baton-worker/SKILL.md" in WORKER_PREAMBLE


def test_preamble_names_the_worker_id_variable() -> None:
    """WORKER_PREAMBLE names the BATON_WORKER_ID environment variable."""
    assert "BATON_WORKER_ID" in WORKER_PREAMBLE


def test_preamble_has_no_surrounding_whitespace() -> None:
    """WORKER_PREAMBLE is non-empty and carries no leading or trailing whitespace."""
    assert WORKER_PREAMBLE
    assert WORKER_PREAMBLE == WORKER_PREAMBLE.strip()


def test_packaged_skill_file_loads(skill_text: str) -> None:
    """The packaged skill file loads through importlib.resources and is not empty."""
    assert skill_text


def test_packaged_skill_frontmatter_names_baton_worker(skill_text: str) -> None:
    """The packaged skill's frontmatter names it baton-worker."""
    assert _skill_frontmatter_name(skill_text) == "baton-worker"


@pytest.mark.parametrize("tool_signature", TOOL_SIGNATURES)
def test_packaged_skill_names_each_tool(skill_text: str, tool_signature: str) -> None:
    """The packaged skill body names each baton tool by its exact signature."""
    assert tool_signature in skill_text


@pytest.mark.parametrize("state", LIFECYCLE_STATES)
def test_packaged_skill_names_each_lifecycle_state(skill_text: str, state: str) -> None:
    """The packaged skill body names each of baton's lifecycle states."""
    assert state in skill_text
