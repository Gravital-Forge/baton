"""Tests for baton.prompts.

Covers the worker preamble, the reconciliation request, the diagnosis
prompt builder, and the packaged skill file.
"""

from pathlib import Path

import pytest

from baton.prompts import RECONCILIATION_REQUEST, WORKER_PREAMBLE, diagnosis_prompt

TOOL_SIGNATURES = [
    "initialize_project(project_path, title, initial_prompt, "
    "session_name=None, model=None)",
    "list_projects()",
    "resume_project(project_id, prompt)",
    "close_project(project_id)",
    "report_status(worker_id, message)",
    "report_lifecycle(worker_id, state, message=None, next_prompt=None, "
    "delay_seconds=None)",
    "get_project_status(project_id)",
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


def test_preamble_names_the_project_variable() -> None:
    """WORKER_PREAMBLE names the BATON_PROJECT environment variable."""
    assert "BATON_PROJECT" in WORKER_PREAMBLE


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


def test_packaged_skill_names_the_project_variable(skill_text: str) -> None:
    """The packaged skill names the BATON_PROJECT environment variable."""
    assert "BATON_PROJECT" in skill_text


@pytest.mark.parametrize("tool_signature", TOOL_SIGNATURES)
def test_packaged_skill_names_each_tool(skill_text: str, tool_signature: str) -> None:
    """The packaged skill body names each baton tool by its exact signature."""
    assert tool_signature in skill_text


@pytest.mark.parametrize("state", LIFECYCLE_STATES)
def test_packaged_skill_names_each_lifecycle_state(skill_text: str, state: str) -> None:
    """The packaged skill body names each of baton's lifecycle states."""
    assert state in skill_text


def test_reconciliation_request_has_no_newline() -> None:
    """RECONCILIATION_REQUEST is one line: it carries no newline."""
    assert "\n" not in RECONCILIATION_REQUEST


@pytest.mark.parametrize("term", ["report_lifecycle", "running", "blocked"])
def test_reconciliation_request_names_each_term(term: str) -> None:
    """RECONCILIATION_REQUEST names the tool and the states it asks about."""
    assert term in RECONCILIATION_REQUEST


REASON = "the tmux pane vanished without a terminal lifecycle report"


@pytest.fixture
def diagnosis_kwargs(tmp_path: Path) -> dict[str, object]:
    """Build a full set of `diagnosis_prompt` arguments under `tmp_path`.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        The keyword arguments `diagnosis_prompt` takes, with every path
        argument a real absolute path under `tmp_path` and `attempt`/`cap`
        set to 2 and 3.
    """
    return {
        "project_path": tmp_path / "project",
        "previous_worker_id": "worker-f3c9a1",
        "previous_prompt_path": tmp_path / "state" / "prompts" / "worker-f3c9a1.txt",
        "reason": REASON,
        "events_path": tmp_path / "state" / "events.jsonl",
        "attempt": 2,
        "cap": 3,
    }


@pytest.mark.parametrize(
    "key",
    [
        "previous_worker_id",
        "reason",
        "project_path",
        "previous_prompt_path",
        "events_path",
    ],
)
def test_diagnosis_prompt_contains_each_argument_value(
    diagnosis_kwargs: dict[str, object], key: str
) -> None:
    """diagnosis_prompt's return contains each argument's value.

    A path argument appears as its `str`; `previous_worker_id` and `reason`
    are already strings.
    """
    prompt = diagnosis_prompt(**diagnosis_kwargs)
    assert str(diagnosis_kwargs[key]) in prompt


def test_diagnosis_prompt_states_attempt_against_cap(
    diagnosis_kwargs: dict[str, object],
) -> None:
    """diagnosis_prompt's return states the attempt count against the cap."""
    prompt = diagnosis_prompt(**diagnosis_kwargs)
    assert "attempt 2 of 3" in prompt


def test_diagnosis_prompt_does_not_claim_no_terminal_report(
    diagnosis_kwargs: dict[str, object],
) -> None:
    """diagnosis_prompt's return says only that the worker ended abnormally.

    It must not also claim baton got no terminal lifecycle report: a
    ``failed`` report is itself terminal, so that claim is false on the
    route where the previous worker reported failed.
    """
    prompt = diagnosis_prompt(**diagnosis_kwargs)
    assert "ended abnormally" in prompt
    assert "did not get a terminal lifecycle report" not in prompt
