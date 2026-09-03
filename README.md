# Baton

Baton is a supervisor daemon. It runs Claude Code worker sessions inside one tmux session, one
worker at a time, and passes work from each finished worker to the next. It serves an MCP tool
surface over SSE (Server-Sent Events); a worker reports to it through those tools, and baton
decides what happens next. One daemon supervises one project.

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
default except `BATON_MODEL`.

- `BATON_HOST` — the address the MCP server binds to. Default `127.0.0.1`.
- `BATON_PORT` — the port it listens on. Default `8910`.
- `BATON_STATE_DIR` — the directory holding baton's state. Default `~/.local/state/baton`.
- `BATON_CLAUDE_BIN` — the absolute path to the `claude` binary. Defaults to the first `claude`
  found on `PATH`.
- `BATON_TMUX_BIN` — the absolute path to the `tmux` binary. Defaults to the first `tmux` found on
  `PATH`.
- `BATON_GRACE_PERIOD` — seconds baton waits after a terminal report before it terminates the
  worker. Default `20`.
- `BATON_TERMINATION_TIMEOUT` — seconds baton waits after `SIGTERM` before it sends `SIGKILL`.
  Default `5`.
- `BATON_POLL_INTERVAL` — seconds between polls of the worker pane. Default `2`.
- `BATON_MODEL` — the model every worker runs on. Required unless `initialize_project` is given a
  `model` argument; baton never lets Claude Code's own default choose it.
- `BATON_RECONCILIATION_TIMEOUT` — seconds baton waits for a live worker to answer a
  reconciliation request before giving up on it and starting recovery. Default `300`.
- `BATON_RECOVERY_CAP` — the number of diagnosis workers baton launches in a row before it
  stops and holds the project in phase `failed`. Default `3`.

Baton refuses to start when it cannot find `claude` or `tmux`.

## Point a project at the daemon

The daemon writes an MCP client config to `<state directory>/mcp.json` when it starts, so the file
is there before any project exists. Start a Claude Code session pointed at the daemon's default
state directory like this:

```sh
claude --mcp-config ~/.local/state/baton/mcp.json
```

Then ask that session to call `initialize_project` with the project's directory and the first
worker's prompt, and a `model` too when `BATON_MODEL` is unset. That session is the setup agent; it
is not a worker.

## Watch the work

Baton names the tmux session `baton-` followed by the project directory's name, unless the caller
supplies a name. Attach to it like this:

```sh
tmux attach -t =baton-<project directory name>
```

The `=` is tmux's exact-match prefix. The worker runs in the pane
`baton-<project directory name>:worker.0`.

## After a restart

A restarted daemon carries the project forward on its own:

- It reconciles with a worker still alive from before: it types a request into the worker's pane
  asking for its current state, and waits a bounded time — `BATON_RECONCILIATION_TIMEOUT` — for
  the answer.
- It resumes a handoff it was in the middle of when it stopped.
- It recovers a worker that died while the daemon was down, by launching a diagnosis worker in its
  place — up to the recovery cap.

One restart needs you. When baton cannot type into a live worker's pane, it stops with the tmux
error instead of serving. Kill that pane, or its whole tmux session, and start the daemon again:
baton reads the dead pane as a worker that vanished and recovers it.

## Continue a stopped project

Baton holds a project in phase `failed` when it cannot carry the work forward on its own. Read
`get_project_status` for the phase and the recent events; the phase event that moved the project to
`failed` carries baton's reason. Resolve the cause, then call `initialize_project` again — baton
accepts it from `failed`.

See "Recovery" and "Restart reconciliation" in [`docs/architecture.md`](docs/architecture.md) for
how baton reaches phase `failed` and how it reconciles after a restart.
