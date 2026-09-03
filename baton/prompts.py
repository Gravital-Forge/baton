"""The text baton sends its workers: the preamble, and the two prompts.

Holds the system preamble appended to every worker's launch, the short
request baton sends a live worker to reconcile its lifecycle state after a
restart, and the builder for the prompt a diagnosis worker receives after
another worker fails abnormally.
"""

from pathlib import Path

WORKER_PREAMBLE: str = """\
You are a baton worker. Baton launched you to do one task and to report the
outcome when you finish.

Read `.claude/skills/baton-worker/SKILL.md` before you act, and follow it
for the whole session.

Your worker id is in the `BATON_WORKER_ID` environment variable. Pass that
id on every report you make: `report_status` and `report_lifecycle` both
take it.

Printed text is never a lifecycle report. Only a baton tool call reports
lifecycle."""

RECONCILIATION_REQUEST: str = (
    "Baton restarted and needs your current lifecycle state. Call "
    "report_lifecycle now: running if you are still working, blocked if you "
    "are waiting on a human, or the terminal state you have reached."
)


def diagnosis_prompt(
    *,
    project_path: Path,
    previous_worker_id: str,
    previous_prompt_path: Path,
    reason: str,
    events_path: Path,
    attempt: int,
    cap: int,
) -> str:
    """Build the prompt for a diagnosis worker launched after a failure.

    Args:
        project_path: The project the failed worker was working in.
        previous_worker_id: The id of the worker that ended abnormally.
        previous_prompt_path: The path to the previous worker's task
            prompt.
        reason: The reason baton recorded for the abnormal ending,
            included verbatim.
        events_path: The path to baton's event log for the project.
        attempt: This diagnosis worker's attempt number, one-based.
        cap: The number of consecutive abnormal outcomes baton allows
            before it stops and waits for a human.

    Returns:
        The prompt text to launch the diagnosis worker with.
    """
    return (
        f"Worker {previous_worker_id} ended abnormally. The reason baton "
        f"recorded is: {reason}\n\n"
        f"You are diagnosis attempt {attempt} of {cap}. Past that cap, with "
        f"no successful worker in between, baton stops launching diagnosis "
        f"workers and waits for a human.\n\n"
        f"The project is at {project_path}. The previous worker's task "
        f"prompt is at {previous_prompt_path}. Baton's event log is at "
        f"{events_path}, one JSON object per line, recording milestones, "
        f"lifecycle reports, launches, terminations, and phase changes.\n\n"
        f"Inspect the project's state files and the repository state to "
        f"work out what happened and whether autonomous work can "
        f"continue.\n\n"
        f"You have two outcomes. Report success with a next prompt when "
        f"the work can continue; that next prompt may retry the previous "
        f"task, rewritten with what you learned. Report blocked when a "
        f"human must decide. A failed report spends one of the cap's "
        f"attempts, so when you are unsure, blocked is the better report."
    )
