# Baton architecture

This document is for somebody who wants to change baton: how it is built and what it does. See
[`README.md`](../README.md) for what baton is, how to run it, and how to configure it.

## The parts of the system

Baton is five parts: the daemon, the projects it supervises, the tmux session each project runs in,
the Claude Code workers it runs one at a time in that session, and the worker protocol skill that
tells a worker how to talk to the daemon. One daemon holds many projects at once, each with its own
identity, its own session, its own worker, and its own event log.

The daemon itself is built from smaller modules, each holding one part of its job.

- **Configuration** (`baton/config.py`) is a frozen value object, read from the environment once
  at startup. The readme lists the variables it reads and their defaults. One configuration governs
  every project.
- **The state store** (`baton/state.py`) writes one project's `state.json` atomically and appends
  to its `events.jsonl`, both inside that project's own directory. It also composes that directory's
  path, lists the project ids on disk, and finds a root-level `state.json` (see "The state
  directory").
- **The tmux adapter and the worker launcher** (`baton/tmux.py`, `baton/worker.py`) are the only
  code that runs a subprocess. The launcher writes a worker its prompt file and its launch script,
  and respawns the pane on that script. It also installs the worker protocol skill in the project,
  once, when the project is initialized. The adapter creates and kills sessions, and types into a
  pane, which is how the reconciliation request reaches a worker (see "Restart reconciliation").
- **The prompts module** (`baton/prompts.py`) holds every fixed piece of text baton sends a worker:
  the preamble every worker is launched with, the request a live worker answers after a restart, and
  the prompt a diagnosis worker is launched with. The tmux layout section describes what the
  preamble sends a worker to do; "Recovery" and "Restart reconciliation" describe the other two.
- **The supervisor engine** (`baton/engine.py`) is the state machine, one `Supervisor` to a
  project. It holds that project's phase, decides what a report means, and owns the grace period and
  the termination sequence. It reaches tmux and `claude` only through the adapters above, so it can
  be tested without either.
- **The coordinator** (`baton/coordinator.py`) holds every project's supervisor, keyed by the id it
  minted for it. It resolves a new project's session name, routes each worker's call to the project
  that worker belongs to, reconciles every project at startup, and runs the one watchdog that ticks
  them all. It is the daemon's single entry point: nothing above it owns one.
- **The MCP tool surface** (`baton/tools.py`, `baton/server.py`, `baton/__main__.py`) validates a
  call, delegates to the coordinator, and shapes the reply. It holds no state machine of its own.
  `build_app`, in `baton/server.py`, builds the ASGI application the daemon serves; its lifespan is
  where the coordinator starts and stops (see "Shutdown").

## Project identity

The coordinator mints a project its id when it creates it: eight lowercase hex characters, minted
again when the id collides with a directory already under `projects/`, and refused when a bounded
run of attempts all collide. That id names the project in every tool argument that names one, and
it names the project's directory under `projects/`. A worker never names its project in a report:
worker ids are UUIDs, so one is unique across every project the daemon holds, and the coordinator
finds a reporting worker's project by scanning its supervisors for the one holding that worker as
current.

A title is a display name and carries no other job, so titles need not be unique. It must not be
blank. It also seeds the project's tmux session name, which is `baton-` followed by the title's
slug: the title lowercased, with every run of characters outside `a-z0-9` replaced by a single `-`,
and leading and trailing `-` stripped. A title that slugs to nothing is refused, because it names no
session. `initialize_project` takes a `session_name` to override the derivation, and refuses a blank
one.

The resolved session name has to be free, and the coordinator checks that twice: no project it holds
outside phase `closed` may carry that name already, and tmux itself must not have a session of that
name already. Either clash is refused and named. Closing a project frees its name for the next
project to take.

A project is registered only once its first worker is running, so an initialization that raised
leaves no project behind. What it can leave behind is a tmux session: a launch creates the session
before it respawns the pane into it, so a launch that fails after that point leaves the session
running with no project attached. The next initialization under the same title finds that session in
tmux and is refused, until somebody kills it or the caller passes a `session_name` of its own.

A project's path identifies nothing. Several projects may supervise one directory, and baton does
not look.

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

Baton checks a report's payload against three rules, in this order, so a report that breaks more
than one is refused for the first of them:

1. Every state except `running` requires a message that is not blank. `running` may carry one.
2. `success` requires a next prompt that is not blank. Every other state forbids one.
3. `success` alone may carry a delay. It is a whole number of seconds, not negative, and no larger
   than `BATON_MAX_HANDOFF_DELAY` (see the readme). Baton refuses the whole report when a delay
   falls outside those bounds, rather than trimming it to fit.

`success`, `completed`, and `failed` are terminal, and a terminal report is final: it moves the
project to phase `terminating`. Baton refuses every report from the current worker while the
project stays there. A terminal report is not the only way in. A live worker whose reconciliation
deadline passes unanswered reaches `terminating` with no report at all (see "Restart
reconciliation"), and baton refuses its next report just the same. `close_project` reaches the
phase too, when an operator retires the project (see "Resuming and retiring").

A worker meets these rules twice. `baton/skill/SKILL.md` is the protocol it follows for the whole
session, and the launcher installs it in the project at `.claude/skills/baton-worker/SKILL.md`. The
skill and the `report_lifecycle` tool description carry these rules to the worker, and a worker
reads the tool description before it calls the tool. Change one and change the other.

## The normal loop

Every project runs the loop below on its own, and a daemon runs as many of them at once as it holds.
`initialize_project` starts a new project each time it is called, whatever phase the projects
already running are in.

A daemon that starts with a project already in flight takes it over itself: it reconciles with the
worker on record and carries that project forward from there (see "Restart reconciliation").

Baton logs each project's loop as a sequence of events in that project's own `events.jsonl`. A
`launch`, `phase`, `milestone`, `lifecycle`, or `terminate` event marks a step of the loop below. A
`vanish` event marks a worker whose pane died without reporting (see "Recovery"), and a `reconcile`
event marks a restart's request to a live worker (see "Restart reconciliation").

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

A `success` report may name a delay, and baton holds that long between the two workers. A delay of
`0` holds nothing, and that handoff runs exactly as an undelayed one does. Otherwise baton
terminates the finished worker the usual way, then logs a `phase` event moving the project to
`waiting`, whose reason carries the delay and the moment the hold ends. The `launch` and the
`phase` event back to `running` come once that moment has passed. A holding project has no current
worker, which is what keeps the watchdog off it: a dead pane held with a current worker is what
recovery reads as a vanish (see "Recovery").

A `running` report can arrive while the project is `blocked`, `reconciling`, `running`, or
`recovering`. From `blocked` or from `reconciling` it returns the project to phase `running` — a
`phase` event recording the move — and leaves the worker alive. From `running` or from `recovering`
baton records the report and moves nothing.

## Resuming and retiring

A project that has stopped — phase `completed` or phase `failed` — has no worker of its own, and
`resume_project` carries it on with a fresh one. The project keeps its id, its title, its session
name, its path, its model and its event log; only the worker is new. The recovery attempt count
resets and the last report is cleared, so a stopped worker's outcome is not read back as though it
were the new worker's. A project in any other phase is refused: it still has a worker of its own,
it is holding between two workers, or it has been closed.

`close_project` retires a project for good. It moves the project to phase `terminating`, waits the
grace period, terminates the worker, kills the project's tmux session, and commits phase `closed`
with no worker. A project with no worker skips straight to the kill and the commit. A project
holding between two workers has its pending hold cancelled first, before the lock and before that
shortcut: a hold left running would wake after the retirement and launch a worker into a closed
project. The kill is guarded by `has_session`, because an absent session is an ordinary state — a
tmux server restart, or an operator who killed it — and killing a session that is not there raises.

`close` holds the supervisor's lock in two stretches, the way a finish does, with the grace period
and the termination poll between them. A lock held across both would stall the watchdog:
`check_worker` takes each supervisor's lock in turn, so holding one here would block vanish
detection for every other project for as long as the two waits take. The price of releasing it is a
finish running alongside, which would commit `running` over the top of `closed`, so `close` refuses
a project already in phase `terminating` and names the phase. That phase lasts seconds; the caller
retries.

A close whose termination or session kill raises commits phase `failed` with the reason, rather than
leaving the project where the close stopped. A project left in `terminating` refuses every later
close, every report and every resume, and `check_worker` skips it, so nothing would move it until
the daemon restarted. One left in `waiting` keeps its deadline, and the next start would wait that
hold out and launch a worker into the project the operator had retired. The failure still reaches
the caller, and a later close can retire the project.

A closed project leaves `list_projects` but stays reachable by its id. Its state directory is the
record of what ran, and `get_project_status` reads it back.

## Recovery

The watchdog is a background task the coordinator creates in `start` and cancels in `shutdown`, one
for the whole daemon. It sleeps the poll interval, runs a `check_worker` tick for every project the
daemon holds, and repeats for as long as the daemon serves. A tick that raises for one project is
logged and the next project is polled, so no one project can end the loop for the rest.

The watchdog detects a vanish. On each tick, `check_worker` reads the current worker's pane. A dead
pane found with a current worker, outside phase `terminating`, is a worker that ended without a
terminal report. Baton appends a `vanish` event and starts recovery.

A `failed` lifecycle report, a vanished pane, and a worker that lets its reconciliation deadline
pass unanswered after a restart (see "Restart reconciliation") all reach the same recovery: baton
launches a diagnosis worker into the project's pane and moves the project to phase `recovering`,
counting the attempt.

The diagnosis worker's prompt, built by `diagnosis_prompt` in `baton/prompts.py`, tells it what
happened and the reason baton recorded, and where to look: the previous worker's prompt file, the
project, and baton's event log for that project. It asks for one of two outcomes: `success` with a
next prompt that carries the work on, or `blocked` for a human to decide.

Baton counts recovery attempts in `ProjectState.recovery_attempts`, and resets the count to zero
each time a `success` report launches the next worker. `BATON_RECOVERY_CAP` (see the readme) bounds
how many diagnosis workers baton launches in a row without a `success` between them. At the cap,
baton commits phase `failed` with a reason and stops. It commits `failed` with a reason too when its
own machinery raises — a handoff, an abnormal finish, a recovery launch, a close, or a startup
reconciliation. `failed` is a holding phase: baton does nothing further with that project and waits
for a human, who carries it on with `resume_project` (see "Resuming and retiring").

Every launch — the first worker, the next one after a `success`, a resumed worker, and a diagnosis
worker — ensures the tmux session exists first, creating it if it does not. A recovery whose session
died gets the session back.

## Restart reconciliation

Baton reads a project's `state.json` only at startup. `Coordinator.start` runs
`Supervisor.reconcile` for every project it built, before the watchdog starts, and each supervisor
acts on the phase it loaded.

A project in phase `running`, `blocked`, `recovering`, or `reconciling` has a worker whose current
state baton does not know, so baton reads its pane. A live pane gets the reconciliation request —
`RECONCILIATION_REQUEST` in `baton/prompts.py` — typed into it through the tmux adapter's
`send_keys`. Baton appends a `reconcile` event carrying the timeout, starts the clock, and moves the
project to phase `reconciling`. A dead pane is left alone: the watchdog's first tick finds it,
records the vanish, and recovers (see "Recovery"). A project with no worker on record — `completed`,
`failed`, or `closed` — has nothing to reconcile with, and is left alone. A project in phase
`waiting` has no worker either, but it is owed the rest of a hold. Baton waits out whatever remains
of its persisted deadline and then launches the next worker, or launches at once when that deadline
has already passed.

A project in phase `terminating` has a finish that never ran to completion, so it is resumed. A
terminal last report means the worker did report before the daemon stopped, and its finish resumes
exactly as it would have, taking its usual route. Any other last report, or none, means the
worker's outcome is unknown, so baton terminates it with no grace period and recovers, the same as
a vanish.

`close_project` is another way into `terminating`, and a daemon that stops between that commit
and the retirement leaves the project sitting there. What the restart resumes is a finish, not the
close, so the project comes back unretired. `close` clears the last report when it commits that
phase, so that finish is the abnormal one: baton diagnoses the project rather than relaunching it
on the previous handoff's next prompt. Closing it again retires it.

No project's reconciliation failure reaches another, and none reaches the daemon. The pass gathers
every reconciliation with `return_exceptions=True`, so a raise comes back as a result to act on
instead of ending the pass at the project that raised. Collecting those results is what the gather
is for, not overlapping the projects' waiting: nothing in `reconcile` waits for a worker's answer.
The watchdog does that, and it starts once the pass is over.

A project whose reconciliation raised is given up. `Supervisor.fail` commits phase `failed` with the
exception named in the phase event's reason, and clears the current worker. The daemon goes on to
serve every other project. That project's pane is left alive, and what it holds is what a human
needs to read. What baton stops routing to is the worker: `supervisor_for_worker` no longer finds
it, so a late report from it reaches nothing. `resume_project` respawns the pane with a fresh worker
once the human has read it.

Every report from `reconciling` routes as any lifecycle report does. A `running` report returns the
project to phase `running`. A `blocked` report moves it to phase `blocked`. Each terminal report —
`success`, `completed`, or `failed` — moves the project to phase `terminating` and then takes its
usual route (see "The normal loop").

The watchdog waits out the request. `BATON_RECONCILIATION_TIMEOUT` (see the readme) bounds how
long a live pane has to answer before baton gives up on it, terminates it with no grace period,
and recovers. That deadline lives in memory, not in `state.json` — a second restart starts the
clock over.

## Shutdown

The ASGI lifespan calls `Coordinator.shutdown` once it stops serving. It cancels the watchdog, then
drains every project's pending finish, so a termination already in flight — the grace period, the
signal, and the route or recovery that follows — runs to completion before the process exits. The
drain gathers every project with `return_exceptions=True` and logs whatever comes back as an
exception, so one project's failing finish cannot leave the projects after it undrained.

A shutdown never waits out a handoff delay. A project already holding is cancelled rather than
drained, and a finish that has not yet begun its hold stops at the commit that enters `waiting`.
Either way the deadline is on disk, so the next start resumes whatever remains of it (see "Restart
reconciliation"). Draining a hold would instead keep the daemon open for the length of the delay.

## The tmux layout

One session serves each project: one window, named `worker`, with one pane and `remain-on-exit`
set on. Every launch is a `respawn-pane -k` on that pane, so a new worker replaces its predecessor
in place, and an attached human's view survives the handoff. Pane death is worker death: baton
reads the pane's own state rather than trusting a recorded process id.

Baton puts the worker's id in the pane environment as `BATON_WORKER_ID`, and the project's id as
`BATON_PROJECT`. The pane runs the worker's `launch.sh`, which `exec`s `claude` with the worker id
as its `--session-id`, with the project's model as its `--model`, with `mcp.json` as its
`--mcp-config`, and with the worker preamble appended to its system prompt. The preamble is what
sends the worker to the installed skill, and what tells it to read both ids out of the environment:
its own id goes on every report it makes, and the project's id is what `get_project_status` takes.

The model is chosen once, when `initialize_project` runs: the call's `model` argument, else the
daemon's `BATON_MODEL`. Baton refuses to initialize when neither names one, so Claude Code's own
default never decides. The chosen model goes into the project's `state.json` and launches every
later worker of that project — the next worker after a `success`, a resumed worker, and a diagnosis
worker alike.

## The state directory

One state directory serves the daemon. It holds the MCP client config every project shares, and a
directory per project:

```text
<state dir>/
├── mcp.json
└── projects/
    └── <project id>/
        ├── state.json
        ├── events.jsonl
        └── workers/
            └── <worker id>/
                ├── prompt.md
                └── launch.sh
```

- `mcp.json` — the MCP client config a worker's Claude Code session is pointed at. Its content
  depends only on the daemon's host and port, so one daemon publishes one of them.
- `state.json` — the project's id and title, its current phase, worker and last report, the tmux
  session and pane its workers run in, the directory they work in, the model they run on, the
  recovery attempt count, and the moment a pending hold ends.
- `events.jsonl` — the append-only log `get_project_status` reads back for that project.
- `workers/<worker id>/` — the `prompt.md` a worker was launched with, and the `launch.sh` that ran
  it. These files stay after the worker ends, as the record of what ran.

The daemon finds its projects by scanning `projects/` for directories that hold a `state.json`, so
no registry file can disagree with the project states themselves.

A `state.json` at the root of the state directory belongs to the single-project layout baton no
longer writes. Building the coordinator raises when it finds one, and names the file, so the daemon
never serves. Baton never reads that file: ignoring it would orphan whatever worker it describes,
and migrating it would need a title baton cannot invent.
