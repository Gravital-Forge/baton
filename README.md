# Baton

Baton is a supervisor daemon. It runs Claude Code worker sessions in tmux, one worker at a time per
project, and passes work from each finished worker to the next. It serves an MCP tool surface over
SSE (Server-Sent Events); a worker reports to it through those tools, and baton decides what happens
next. One daemon supervises many projects at once, each with its own tmux session, its own worker,
and its own event log.

See [`docs/architecture.md`](docs/architecture.md) for how baton works.

## Requirements

- Python 3.12 or later.
- [uv](https://docs.astral.sh/uv/), which installs baton's dependencies and runs it.
- `tmux` 3.0 or later. Baton launches every worker with `respawn-pane`, and passes the worker's
  environment through the `-e` flag, which older tmux versions do not have.
- The `claude` binary, on `PATH` or named by `BATON_CLAUDE_BIN`.

## Install

`poe` comes from [poethepoet](https://poethepoet.natn.io/); install it once with
`uv tool install poethepoet`. Then run this from the repository root:

```sh
poe configure
```

It installs baton's dependencies and its commit hooks.

## Run

Run this from the repository root:

```sh
uv run python -m baton
```

The daemon runs in the foreground and serves until you stop it.

## Configure

Baton reads its configuration from the environment once, at startup. Every variable below has a
default except `BATON_MODEL`, and one configuration governs every project the daemon holds.

- `BATON_HOST` — the address the MCP server binds to. Default `127.0.0.1`.
- `BATON_PORT` — the port it listens on. Default `8910`.
- `BATON_STATE_DIR` — the directory holding baton's state. Default `~/.local/state/baton`.
- `BATON_CLAUDE_BIN` — the absolute path to the `claude` binary. A value starting with `~` is
  expanded to the user's home directory first. Defaults to the first `claude` found on `PATH`.
- `BATON_TMUX_BIN` — the absolute path to the `tmux` binary. A value starting with `~` is expanded
  to the user's home directory first. Defaults to the first `tmux` found on `PATH`.
- `BATON_GRACE_PERIOD` — seconds baton waits before it terminates a worker, after that worker's
  terminal report or after a `close_project` call. Default `20`.
- `BATON_TERMINATION_TIMEOUT` — seconds baton waits after `SIGTERM` before it sends `SIGKILL`.
  Default `5`.
- `BATON_POLL_INTERVAL` — seconds between polls of the worker panes. Default `2`.
- `BATON_MODEL` — the model a project's workers run on by default, when `initialize_project` names
  none of its own. One of the two must name a model; baton never lets Claude Code's own default
  choose it.
- `BATON_RECONCILIATION_TIMEOUT` — seconds baton waits for a live worker to answer a reconciliation
  request before giving up on it and starting recovery. Default `300`.
- `BATON_RECOVERY_CAP` — the number of diagnosis workers baton launches in a row before it stops and
  holds that project in phase `failed`. Default `3`.
- `BATON_MAX_HANDOFF_DELAY` — the largest delay, in seconds, a `success` report may ask baton to
  hold for before it launches the next worker. Default `86400`, which is 24 hours. Baton refuses a
  report that asks for more.

Baton refuses to start when it cannot find `claude` or `tmux`.

## The state directory

`BATON_STATE_DIR` names one directory for the whole daemon. Two paths in it are yours: `mcp.json`,
the MCP client config every project shares, and `projects/<project id>/`, where one project keeps
its state, its event log, and the record of every worker it ran. "The state directory" in
[`docs/architecture.md`](docs/architecture.md) says what each file holds.

Baton finds its projects by scanning `projects/`, so nothing has to be registered anywhere else. A
`state.json` at the root of the state directory belongs to a layout baton no longer writes: the
daemon refuses to start when it finds one, and names the file. Baton never reads it. Deal with
whatever worker it describes, move the file out of the state directory, and start that project again
with `initialize_project` — it comes back as a new project, with an id of its own.

## Point a project at the daemon

The daemon writes an MCP client config to `<state directory>/mcp.json` when it starts, so the file
is there before any project exists. Start a Claude Code session pointed at the daemon's default
state directory like this:

```sh
claude --mcp-config ~/.local/state/baton/mcp.json
```

Then ask that session to call `initialize_project` with the project's directory, a title, and the
first worker's prompt — and a `model` too when `BATON_MODEL` is unset. Baton mints the project an
eight-character id and hands it back; that id names the project in every later call. That session is
the setup agent, not a worker, and it can start as many projects as you ask it for.

A title is a display name, and titles need not be unique. It also names the project's tmux session
(see "Watch the work"), so baton refuses a title whose session name is taken already. An
initialization that created its tmux session and then failed to launch the worker leaves that
session running and registers no project. Baton refuses the same title next time and names the
session: kill it, or pass a `session_name` of your own.

## Watch the work

Each project runs in a tmux session of its own, named `baton-` followed by the title's slug. See
"Project identity" in [`docs/architecture.md`](docs/architecture.md) for how a title becomes a slug.
Pass `session_name` to `initialize_project` to choose the name yourself. `list_projects` gives you
every project's session name, along with its id, its phase, and its current worker.

Attach to a session like this:

```sh
tmux attach -t =baton-<slug>
```

The `=` is tmux's exact-match prefix. The worker runs in the pane `baton-<slug>:worker.0`.

## After a restart

A restarted daemon carries each project forward on its own:

- It reconciles with a worker still alive from before: it types a request into the worker's pane
  asking for its current state, and waits a bounded time — `BATON_RECONCILIATION_TIMEOUT` — for the
  answer.
- It resumes a handoff it was in the middle of when it stopped.
- It waits out the rest of a handoff delay it was holding, and launches the next worker at once when
  that moment has already passed.
- It recovers a worker that died while the daemon was down, by launching a diagnosis worker in its
  place — up to the recovery cap.

No project's trouble reaches another. When baton cannot type into a live worker's pane, it gives
that one project up: the project moves to phase `failed` with the tmux error as the reason, and the
daemon serves every other project as usual. `get_project_status` shows you the reason. The pane is
left alive, because what it holds is what you need to read; kill it when you are done with it, or
call `resume_project`, which respawns it with a fresh worker.

## Continue or retire a project

Baton holds a project in phase `completed` when a worker reports the whole project done, and in
phase `failed` when it cannot carry the work forward on its own. Read `get_project_status` for the
phase and the recent events; the phase event that moved the project carries baton's reason.

Call `resume_project` with the project's id and a prompt to carry the project on. It keeps its id,
its title, its tmux session and its event log. The new worker runs on the model the project was
created with, unless you name another for that one worker in the call. Write that prompt as a whole
context: the new worker remembers nothing of the workers before it.

Call `close_project` with the project's id to retire it for good. Baton gives any running worker the
grace period, terminates it, kills the project's tmux session, and frees that session name for a new
project. A closed project leaves `list_projects` and cannot be resumed; its state directory stays on
disk, and `get_project_status` still reads it back by id. A project that is finishing with its
current worker is refused for the seconds that takes — call again. A close that could not terminate
the worker or kill the session reports the error and leaves the project in phase `failed` rather
than retired; call `close_project` again to finish retiring it. A daemon that stops in the middle of
a close does not carry it through either: the project comes back unretired, and the restart launches
a diagnosis worker into it, so expect a live Claude Code session in the project you retired. Close
it again.

See "Recovery" and "Restart reconciliation" in [`docs/architecture.md`](docs/architecture.md) for
how baton reaches phase `failed` and how it reconciles after a restart.
