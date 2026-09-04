"""Wires baton's collaborators into a supervisor and an MCP server."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.routing import Mount

from baton.config import BatonConfig
from baton.engine import Supervisor
from baton.state import StateStore
from baton.tmux import TmuxAdapter
from baton.tools import BatonTools
from baton.worker import WorkerLauncher


def build_supervisor(config: BatonConfig) -> Supervisor:
    """Build the supervisor and the collaborators it runs on.

    Args:
        config: The runtime configuration governing every collaborator.

    Returns:
        A Supervisor loaded from any state already on disk, or a fresh,
        uninitialized one when there is none.
    """
    tmux = TmuxAdapter(config.tmux_bin)
    return Supervisor(
        config,
        StateStore(config.state_dir),
        tmux,
        WorkerLauncher(config, tmux),
    )


def build_server(supervisor: Supervisor) -> MCPServer:
    """Build the MCP server and register baton's tools on it.

    Args:
        supervisor: The supervisor every registered tool delegates to.

    Returns:
        An `MCPServer` named ``"baton"`` with `initialize_project`,
        `report_status`, `report_lifecycle`, and `get_project_status`
        registered.
    """
    server = MCPServer("baton")
    tools = BatonTools(supervisor)
    server.tool()(tools.initialize_project)
    server.tool()(tools.report_status)
    server.tool()(tools.report_lifecycle)
    server.tool()(tools.get_project_status)
    return server


def build_app(
    supervisor: Supervisor, server: MCPServer, config: BatonConfig
) -> Starlette:
    """Build the ASGI app that runs the MCP server for the supervisor's life.

    The MCP server's SSE app is built with `host=config.host`, which keeps
    mcp's DNS-rebinding protection: `sse_app` enables that protection only
    when it is given the loopback host it is guarding. It is mounted at
    `/`, which leaves the request path and `root_path` unchanged, so the
    SSE endpoint stays at `/sse` and the message endpoint it advertises
    stays at `/messages/`.

    Args:
        supervisor: The supervisor whose `start` and `shutdown` hooks run
            around the server's life.
        server: The MCP server to mount and serve.
        config: The runtime configuration naming the host `sse_app` binds
            its DNS-rebinding protection to.

    Returns:
        A `Starlette` app whose lifespan reconciles with any worker left
        over from a previous run and starts the supervisor's pane
        watchdog before serving, then stops the watchdog and drains
        pending work after.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Reconcile, then run the supervisor's watchdog for the serving life.

        Args:
            app: The Starlette app this lifespan is bound to.

        Yields:
            Control, once the supervisor has reconciled with any worker
            left over from a previous run and its watchdog has started,
            for as long as the app serves requests.

        Raises:
            TmuxError: If the reconciliation request cannot be sent to a
                live worker's pane. The app never serves, which is the
                intent: a pane baton cannot type into needs a human.
        """
        await supervisor.start()
        try:
            yield
        finally:
            await supervisor.shutdown()

    return Starlette(
        routes=[Mount("/", app=server.sse_app(host=config.host))],
        lifespan=lifespan,
    )
