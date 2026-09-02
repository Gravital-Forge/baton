---
name: baton-worker
description: Use for the whole session of a Claude Code worker that baton launched.
---

# Baton worker protocol

Baton launched this session to do one task in a project it is coordinating. Read
this skill now. Follow it for the whole session, until you report a terminal
state.

## Project state

Read the project's state files before you act. A worker before you may have left
progress and decisions there, and conversational memory carries nothing between
workers — the files are the only record that survives. Write meaningful progress
and decisions back as you go. Leave enough that a fresh worker with no memory of
this session can continue.

## Lifecycle

Report exactly one lifecycle state, explicitly, through `report_lifecycle`. Emit a
milestone through `report_status` only for meaningful progress. Terminal prose in
your output is never a lifecycle signal — baton acts only on the tool call.

The states:

- **`success`** — the task is done and more work remains. Supply the next prompt;
  `success` without one is not a valid report.
- **`completed`** — the whole project is done, not just this task.
- **`failed`** — you could not complete the task. Say what went wrong.
- **`blocked`** — you need a human's input to continue. You stay alive for that
  human, and you report again once the work can move.

Every report except `running` carries a message that says what happened. Baton
rejects a report without one.

A terminal report is final. After you send one, do no new work and do not revise
your handoff. Baton ends your session shortly after.

## Handoff

When you report `success`, write the next prompt for a fresh worker. That worker's
only context is the project's files; it remembers nothing of this session. Give it
enough task-specific detail that its next action is unambiguous.

## Reconciliation

When baton restarts, it asks you for your current state. Answer at once, through
`report_lifecycle`, and report the state you are truly in. Use `running` when you
are still working on the task: it tells baton nothing has to change.

## Baton's tools

Every call you make carries your own worker id, from the `BATON_WORKER_ID`
environment variable.

- `initialize_project(project_path, initial_prompt, session_name=None)` — creates a
  project and launches its first worker. The setup agent calls this, not you.
- `report_status(worker_id, message)` — records a milestone.
- `report_lifecycle(worker_id, state, message=None, next_prompt=None)` — reports
  your lifecycle state. The only call baton acts on.
- `get_project_status()` — reads back the project's phase, current worker, last
  lifecycle report, and recent events.
