# Baton architecture

This document is for somebody who wants to change baton: how it is built and what it does. See
[`README.md`](../README.md) for what baton is, how to run it, and how to configure it.

## The parts of the system

Baton is five parts: the daemon, the tmux session it supervises, the Claude Code workers the daemon
runs one at a time, the project directory those workers share, and the worker protocol skill that
tells a worker how to talk to the daemon.

The daemon itself is built from smaller modules, each holding one part of its job.

- **Configuration** (`baton/config.py`) is a frozen value object, read from the environment once
  at startup. The readme lists the variables it reads and their defaults.
- **The state store** (`baton/state.py`) writes `state.json` atomically and appends to
  `events.jsonl`. It loads the project's state when the daemon starts.
- **The tmux adapter and the worker launcher** (`baton/tmux.py`, `baton/worker.py`) are the only
  code that runs a subprocess. The launcher writes a worker its prompt file and its launch script,
  and respawns the pane on that script. It also installs the worker protocol skill in the project,
  once, when the project is initialized. The adapter also types into a pane, which is how the
  reconciliation request reaches a worker (see "Restart reconciliation").
- **The prompts module** (`baton/prompts.py`) holds every fixed piece of text baton sends a worker:
  the preamble every worker is launched with, the request a live worker answers after a restart, and
  the prompt a diagnosis worker is launched with. The tmux layout section describes what the
  preamble sends a worker to do; "Recovery" and "Restart reconciliation" describe the other two.
- **The supervisor engine** (`baton/engine.py`) is the state machine. It holds the project's
  phase, decides what a report means, and owns the grace period and the termination sequence. It
  reaches tmux and `claude` only through the adapters above, so it can be tested without either.
- **The MCP tool surface** (`baton/tools.py`, `baton/server.py`, `baton/__main__.py`) validates a
  call, delegates to the engine, and shapes the reply. It holds no state machine of its own.
  `build_app`, in `baton/server.py`, builds the ASGI application the daemon serves; its lifespan is
  where the supervisor starts and stops (see "Shutdown").

## The lifecycle protocol

A worker reports one state through `report_lifecycle`, and that call is the only thing baton acts
on: prose in a worker's output is never a signal.

- **`success`** — the task is done, and more work remains.
- **`completed`** — the whole project is done.
- **`failed`** — the task could not be done.
- **`blocked`** — a human's input is needed.
- **`running`** — still working. It answers baton's reconciliation request after a restart, and
  resumes work from phase `blocked`. Baton returns the project to `running` from `blocked` or from
  `reconciling`, and moves nothing otherwise (see "Restart reconciliation" for the request).

Baton checks a report's payload against two rules, in this order, so a report that breaks both is
refused for the message alone:

1. Every state except `running` requires a message that is not blank. `running` may carry one.
2. `success` requires a next prompt that is not blank. Every other state forbids one.

`success`, `completed`, and `failed` are terminal, and a terminal report is final: it moves the
project to phase `terminating`. Baton refuses every report from the current worker while the
project stays there. A terminal report is not the only way in: a live worker whose reconciliation
deadline passes unanswered reaches `terminating` with no report at all (see "Restart
reconciliation"), and baton refuses its next report just the same.

A worker meets these rules twice. `baton/skill/SKILL.md` is the protocol it follows for the whole
session, and the launcher installs it in the project at `.claude/skills/baton-worker/SKILL.md`. The
skill and the `report_lifecycle` tool description carry these rules to the worker, and a worker
reads the tool description before it calls the tool. Change one and change the other.

## The normal loop

One daemon supervises one project at a time. `initialize_project` refuses to start a new project
while the current one still has a worker running. It accepts before any project has been
initialized, and once a project has stopped, in phase `completed` or in phase `failed`.

A daemon that starts with a project already in flight takes it over itself: it reconciles with the
worker on record and carries the project forward from there (see "Restart reconciliation").

Baton logs the loop as a sequence of events in `events.jsonl`. A `launch`, `phase`, `milestone`,
`lifecycle`, or `terminate` event marks a step of the loop below. A `vanish` event marks a worker
whose pane died without reporting (see "Recovery"), and a `reconcile` event marks a restart's
request to a live worker (see "Restart reconciliation").

`initialize_project` launches the first worker: a `launch` event, then a `phase` event that moves
the project to `running`. The worker calls `report_status` as it goes, and each call is a
`milestone` event.

A `success`, `completed`, or `failed` report is terminal, and all three terminate the worker the
same way. Baton logs a `lifecycle` event, then a `phase` event moving the project to `terminating`.
It waits the grace period, so the worker can finish its own shutdown. It sends `SIGTERM`, and
`SIGKILL` after that if the pane outlives the termination timeout. It logs a `terminate` event.

What baton does next depends on which report it was. A `success` report launches the next worker
with the prompt the last one wrote — a `launch` event, and a `phase` event back to `running`. A
`completed` report stops the project in phase `completed`. A `failed` report enters recovery (see
"Recovery"). A `blocked` report skips termination: it moves the project to phase `blocked` and
leaves the worker alive for the human who must unblock it.

A `running` report can arrive while the project is `blocked`, `reconciling`, `running`, or
`recovering`. From `blocked` or from `reconciling` it returns the project to phase `running` — a
`phase` event recording the move — and leaves the worker alive. From `running` or from `recovering`
baton records the report and moves nothing.

## Recovery

The watchdog is a background task `Supervisor.start` creates and `shutdown` cancels. It sleeps the
poll interval, runs one `check_worker` tick, and repeats for as long as the daemon serves. A tick
that raises is logged and the loop goes on to the next one.

The watchdog detects a vanish. On each tick, `check_worker` reads the current worker's pane. A dead
pane found with a current worker, outside phase `terminating`, is a worker that ended without a
terminal report. Baton appends a `vanish` event and starts recovery.

A `failed` lifecycle report, a vanished pane, and a worker baton gives up on after a restart (see
"Restart reconciliation") all reach the same recovery: baton launches a diagnosis worker into the
project's pane and moves the project to phase `recovering`, counting the attempt.

The diagnosis worker's prompt, built by `diagnosis_prompt` in `baton/prompts.py`, tells it what
happened and the reason baton recorded, and where to look: the previous worker's prompt file, the
project, and baton's event log. It asks for one of two outcomes: `success` with a next prompt that
carries the work on, or `blocked` for a human to decide.

Baton counts recovery attempts in `ProjectState.recovery_attempts`, and resets the count to zero
each time a `success` report launches the next worker. `BATON_RECOVERY_CAP` (see the readme)
bounds how many diagnosis workers baton launches in a row without a `success` between them. At the
cap, baton commits phase `failed` with a reason and stops. It commits `failed` with a reason too
when its own machinery raises — a handoff, an abnormal finish, or a recovery launch. `failed` is a
holding phase: baton does nothing further and waits for a human. `initialize_project` is accepted
from it.

Every launch — the first worker, the next one after a `success`, and a diagnosis worker — ensures
the tmux session exists first, creating it if it does not. A recovery whose session died gets the
session back.

## Restart reconciliation

Baton reads `state.json` only at startup. `Supervisor.reconcile` runs once, before the watchdog
starts, and acts on the phase it loaded.

A project in phase `running`, `blocked`, `recovering`, or `reconciling` has a worker whose current
state baton does not know, so baton reads its pane. A live pane gets the reconciliation request —
`RECONCILIATION_REQUEST` in `baton/prompts.py` — typed into it through the tmux adapter's
`send_keys`. Baton appends a `reconcile` event carrying the timeout, starts the clock, and moves
the project to phase `reconciling`. A dead pane is left alone: the watchdog's first tick finds it,
records the vanish, and recovers (see "Recovery"). A project loaded in phase `uninitialized`,
`completed`, or `failed` — or with no worker on record — needs no reconciliation, and is left
alone.

A project in phase `terminating` has a finish that never ran to completion, so it is resumed. A
terminal last report means the worker did report before the daemon stopped, and its finish resumes
exactly as it would have, taking its usual route. Any other last report, or none, means the
worker's outcome is unknown, so baton terminates it with no grace period and recovers, the same as
a vanish.

A failure sending the request propagates out of `reconcile`, out of `start`, and out of the ASGI
lifespan in `build_app` (`baton/server.py`), so the daemon never serves. A pane baton cannot type
into needs a human, not a watchdog looping over it.

Every report from `reconciling` routes as any lifecycle report does. A `running` report returns the
project to phase `running`. A `blocked` report moves it to phase `blocked`. Each terminal report —
`success`, `completed`, or `failed` — moves the project to phase `terminating` and then takes its
usual route (see "The normal loop").

The watchdog waits out the request. `BATON_RECONCILIATION_TIMEOUT` (see the readme) bounds how
long a live pane has to answer before baton gives up on it, terminates it with no grace period,
and recovers. That deadline lives in memory, not in `state.json` — a second restart starts the
clock over.

## Shutdown

The ASGI lifespan calls `Supervisor.shutdown` once it stops serving. `shutdown` cancels the
watchdog, then drains any finish still pending, so a termination already in flight — the grace
period, the signal, and the route or recovery that follows — runs to completion before the process
exits.

## The tmux layout

One session serves the project: one window, named `worker`, with one pane and `remain-on-exit`
set on. Every launch is a `respawn-pane -k` on that pane, so a new worker replaces its predecessor
in place, and an attached human's view survives the handoff. Pane death is worker death: baton
reads the pane's own state rather than trusting a recorded process id.

Baton puts the worker's id in the pane environment as `BATON_WORKER_ID`. The pane runs the
worker's `launch.sh`, which `exec`s `claude` with that id as its `--session-id`, with the project's
model as its `--model`, with `mcp.json` as its `--mcp-config`, and with the worker preamble appended
to its system prompt. The preamble is what sends the worker to the installed skill and tells it to
read its id out of the environment.

The model is chosen once, when `initialize_project` runs: the call's `model` argument, else the
daemon's `BATON_MODEL`. Baton refuses to initialize when neither names one, so Claude Code's own
default never decides. The chosen model goes into `state.json` and launches every later worker of
that project, diagnosis workers included.

## The state directory

The state directory holds:

- `state.json` — the project's current phase, worker, and last report, the model every worker of
  the project runs on, and the recovery attempt count.
- `events.jsonl` — the append-only log that `get_project_status` reads back.
- `mcp.json` — the MCP client config a worker's Claude Code session is pointed at.
- `workers/<worker id>/` — the `prompt.md` a worker was launched with, and the `launch.sh` that
  ran it. These files stay after the worker ends, as the record of what ran.
