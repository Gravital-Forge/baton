# Baton architecture

This document is for somebody who wants to change baton: its parts, the lifecycle protocol, the
normal loop, the tmux layout, and the state directory. See [`README.md`](../README.md) for what
baton is, how to run it, and how to configure it.

## The parts of the system

Each part of baton lives in its own module.

- **Configuration** (`baton/config.py`) is a frozen value object, read from the environment once
  at startup. The readme lists the variables it reads and their defaults.
- **The state store** (`baton/state.py`) writes `state.json` atomically and appends to
  `events.jsonl`. It loads the project's state when the daemon starts.
- **The tmux adapter and the worker launcher** (`baton/tmux.py`, `baton/worker.py`) are the only
  code that runs a subprocess. The launcher writes a worker its prompt file and its launch script,
  and respawns the pane on that script. It also installs the worker protocol skill in the project,
  once, when the project is initialized.
- **The worker preamble** (`baton/prompts.py`) is the short text every worker is launched with. It
  is what sends a worker to the installed skill and tells it to carry its own id.
- **The supervisor engine** (`baton/engine.py`) is the state machine. It holds the project's
  phase, decides what a report means, and owns the grace period and the termination sequence. It
  reaches tmux and `claude` only through the adapters above, so it can be tested without either.
- **The MCP tool surface** (`baton/tools.py`, `baton/server.py`, `baton/__main__.py`) validates a
  call, delegates to the engine, and shapes the reply. It holds no state machine of its own.

## The lifecycle protocol

A worker reports one state through `report_lifecycle`, and that call is the only thing baton acts
on: prose in a worker's output is never a signal.

- **`success`** — the task is done, and more work remains.
- **`completed`** — the whole project is done.
- **`failed`** — the task could not be done.
- **`blocked`** — a human's input is needed.
- **`running`** — still working. It exists for reconciliation. Baton records it as the last report
  and moves no phase.

Baton checks a report's payload against two rules, in this order, so a report that breaks both is
refused for the message alone:

1. Every state except `running` requires a message that is not blank. `running` may carry one.
2. `success` requires a next prompt that is not blank. Every other state forbids one.

`success`, `completed`, and `failed` are terminal. The first terminal report is final: baton
refuses every later report from that worker.

A worker meets these rules twice. `baton/skill/SKILL.md` is the protocol it follows for the whole
session, and the launcher installs it in the project at `.claude/skills/baton-worker/SKILL.md`. The
rules in full, including the order they are checked in, are the description of the
`report_lifecycle` tool, which a worker reads before it calls it. Change one and change the other.

## The normal loop

One daemon supervises one project at a time. `initialize_project` refuses to start a new project
while the current one is running, blocked, or terminating.

Baton logs the loop as a sequence of events in `events.jsonl`.

`initialize_project` launches the first worker: a `launch` event, then a `phase` event that moves
the project to `running`. The worker calls `report_status` as it goes, and each call is a
`milestone` event.

A `success` report starts the handoff. Baton logs a `lifecycle` event, then a `phase` event moving
the project to `terminating`. It waits the grace period, so the worker can finish its own shutdown.
It sends `SIGTERM`, and `SIGKILL` after that if the pane outlives the termination timeout. It logs
a `terminate` event. Then it launches the next worker with the prompt the last one wrote — a
`launch` event, and a `phase` event back to `running`.

A `completed` report ends the loop in phase `completed`. A `failed` report terminates the worker
the same way a `success` does, and stops the project in phase `failed`. A `blocked` report moves
the project to phase `blocked` and leaves the worker alive for the human who must unblock it.

## The tmux layout

One session serves the project: one window, named `worker`, with one pane and `remain-on-exit`
set on. Every launch is a `respawn-pane -k` on that pane, so a new worker replaces its predecessor
in place, and an attached human's view survives the handoff. Pane death is worker death: baton
reads the pane's own state rather than trusting a recorded process id.

Baton puts the worker's id in the pane environment as `BATON_WORKER_ID`. The pane runs the
worker's `launch.sh`, which `exec`s `claude` with that id as its `--session-id`, with `mcp.json` as
its `--mcp-config`, and with the worker preamble appended to its system prompt. The preamble is
what sends the worker to the installed skill and tells it to read its id out of the environment.

## The state directory

The state directory holds:

- `state.json` — the project's current phase, worker, and last report.
- `events.jsonl` — the append-only log that `get_project_status` reads back.
- `mcp.json` — the MCP client config a worker's Claude Code session is pointed at.
- `workers/<worker id>/` — the `prompt.md` a worker was launched with, and the `launch.sh` that
  ran it. These files stay after the worker ends, as the record of what ran.

## What is not built

Baton runs the normal loop only. It does not notice a worker that has vanished, it runs no
diagnosis worker, it has no recovery loop, and it does not reconcile with a live worker after a
restart. A reader who knows the design otherwise assumes those paths are here.

A daemon that starts and finds `state.json` in phase `running`, `blocked`, or `terminating`
refuses `initialize_project` until somebody removes that file by hand. That is also what happens
when the daemon stops in the middle of a handoff: the worker keeps running, and the state file
still says `terminating`.
