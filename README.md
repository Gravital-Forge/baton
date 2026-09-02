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

Baton reads its configuration from the environment once, at startup. Every variable below is
optional.

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

Baton refuses to start when it cannot find `claude` or `tmux`.

## Point a project at the daemon

The daemon writes an MCP client config to `<state directory>/mcp.json` when it starts, so the file
is there before any project exists. Start a Claude Code session pointed at the daemon's default
state directory like this:

```sh
claude --mcp-config ~/.local/state/baton/mcp.json
```

Then ask that session to call `initialize_project` with the project's directory and the first
worker's prompt. That session is the setup agent; it is not a worker.

## Watch the work

Baton names the tmux session `baton-` followed by the project directory's name, unless the caller
supplies a name. Attach to it like this:

```sh
tmux attach -t =baton-<project directory name>
```

The `=` is tmux's exact-match prefix. The worker runs in the pane
`baton-<project directory name>:worker.0`.

## After a restart

When a restarted daemon refuses to initialize a project, delete `state.json` from the state
directory by hand. Then initialize the project again.

See "Failure behavior" in [`docs/architecture.md`](docs/architecture.md) for how baton behaves
when a project fails.
