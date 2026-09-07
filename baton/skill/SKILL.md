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
  `success` without one is not a valid report. It may also name a delay: baton
  terminates you as usual, holds for that long, and then launches the next
  worker. It may name the model that worker runs on, too.
- **`completed`** — the whole project is done, not just this task.
- **`failed`** — you could not complete the task. Say what went wrong. Baton
  terminates you and launches a diagnosis worker to investigate.
- **`blocked`** — you need a human's input to continue. You stay alive for that
  human, and you report `running` once the work can move, which tells baton you
  are working again and returns the project to `running`.
- **`running`** — you are still working. Report it to answer baton's
  reconciliation request, or to resume from `blocked`.

Every report except `running` carries a message that says what happened. Baton
rejects a report without one.

A terminal report is final. After you send one, do no new work and do not revise
your handoff. Baton ends your session shortly after.

## Handoff

When you report `success`, write the next prompt for a fresh worker. That worker's
only context is the project's files; it remembers nothing of this session. Give it
enough task-specific detail that its next action is unambiguous.

Pass `delay_seconds` when that worker should not start yet. Baton terminates you,
waits that many seconds, and then launches it. The delay is a whole number of
seconds, not negative, and no larger than the daemon's configured maximum, which
defaults to 24 hours. Baton refuses the whole report when a delay falls outside
those bounds, rather than trimming it to fit. Only `success` may carry a delay;
every other state refuses one.

Pass `model` when the next worker should run on a model other than the project's.
It governs that worker alone: the worker after it runs on the project's model
again, unless its own report names a model too. Omitting `model` is always
correct — the project's model governs. The value is whatever `claude --model`
accepts on this machine: an alias, or a model's full name, as `claude --help`
describes it. Baton refuses a blank model, and only `success` may carry one.

## Reconciliation

When baton restarts and finds you still alive, it types a request into your
session asking for your current state. Answer at once, through
`report_lifecycle`, with the state you are truly in: `running` when you are
still working, `blocked` when you are waiting on a human, or the terminal state
you reached. A milestone through `report_status` is not an answer — only
`report_lifecycle` clears the request.

Baton waits a bounded time, five minutes by default, for your answer. Silence
past that time is an abnormal end: baton terminates you and launches a
diagnosis worker.

## Diagnosis

A diagnosis worker is an ordinary worker, launched to take over when the worker
before it failed or ended without reporting. Its prompt says what happened and
points it at the previous worker's prompt, the project, and baton's event log.
Baton asks it for one of two outcomes: `success` with a next prompt, or
`blocked`.

## Baton's tools

Every report you make carries your own worker id, from the `BATON_WORKER_ID`
environment variable: `report_status` and `report_lifecycle` both take it.

Your project's id is in the `BATON_PROJECT` environment variable. That id is what
`get_project_status` takes.

These are yours:

- `report_status(worker_id, message)` — records a milestone.
- `report_lifecycle(worker_id, state, message=None, next_prompt=None, delay_seconds=None, model=None)` —
  reports your lifecycle state. The only call baton acts on.
- `get_project_status(project_id)` — reads back a project's phase, current worker,
  default model, last lifecycle report, the moment a pending handoff delay ends,
  and recent events.

These belong to the setup agent, not to you:

- `initialize_project(project_path, title, initial_prompt, session_name=None, model=None)` —
  creates a project and launches its first worker, and returns the project's id.
- `list_projects()` — lists every project baton supervises, one row each.
- `resume_project(project_id, prompt, model=None)` — launches a fresh worker on
  a project that stopped, keeping its id and its event log.
- `close_project(project_id)` — retires a project, terminates its worker, and kills
  its tmux session.
