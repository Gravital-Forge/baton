"""Wires baton's collaborators into a coordinator and an MCP server."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.routing import Mount

from baton.config import BatonConfig
from baton.coordinator import Coordinator
from baton.tmux import TmuxAdapter
from baton.tools import BatonTools
from baton.worker import WorkerLauncher


def build_coordinator(config: BatonConfig) -> Coordinator:
    """Build the coordinator and the collaborators it runs on.

    Args:
        config: The runtime configuration governing every collaborator.

    Returns:
        A Coordinator holding a supervisor for every project already
        persisted under the state directory.

    Raises:
        CoordinatorError: If the state directory holds a single-project
            state.json at its root.
    """
    tmux = TmuxAdapter(config.tmux_bin)
    return Coordinator(config, tmux, WorkerLauncher(config, tmux))


def build_server(coordinator: Coordinator) -> MCPServer:
    """Build the MCP server and register baton's tools on it.

    Args:
        coordinator: The coordinator every registered tool delegates to.

    Returns:
        An `MCPServer` named ``"baton"`` with `initialize_project`,
        `report_status`, `report_lifecycle`, and `get_project_status`
        registered.
    """
    server = MCPServer("baton")
    tools = BatonTools(coordinator)
    server.tool()(tools.initialize_project)
    server.tool()(tools.report_status)
    server.tool()(tools.report_lifecycle)
    server.tool()(tools.get_project_status)
    return server


def build_app(
    coordinator: Coordinator, server: MCPServer, config: BatonConfig
) -> Starlette:
    """Build the ASGI app that runs the MCP server for the coordinator's life.

    The MCP server's SSE app is built with `host=config.host`, which keeps
    mcp's DNS-rebinding protection: `sse_app` enables that protection only
    when it is given the loopback host it is guarding. It is mounted at
    `/`, which leaves the request path and `root_path` unchanged, so the
    SSE endpoint stays at `/sse` and the message endpoint it advertises
    stays at `/messages/`.

    Args:
        coordinator: The coordinator whose `start` and `shutdown` hooks
            run around the server's life.
        server: The MCP server to mount and serve.
        config: The runtime configuration naming the host `sse_app` binds
            its DNS-rebinding protection to.

    Returns:
        A `Starlette` app whose lifespan reconciles every project with
        any worker left over from a previous run and starts the daemon's
        pane watchdog before serving, then stops the watchdog and drains
        every project's pending work after.
    """

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        """Reconcile every project, then run the watchdog for the serving life.

        Args:
            app: The Starlette app this lifespan is bound to.

        Yields:
            Control, once every project has reconciled with any worker
            left over from a previous run and the daemon's watchdog has
            started, for as long as the app serves requests. A project
            that could not reconcile is recorded as failed and the app
            serves the rest.
        """
        await coordinator.start()
        try:
            yield
        finally:
            await coordinator.shutdown()

    return Starlette(
        routes=[Mount("/", app=server.sse_app(host=config.host))],
        lifespan=lifespan,
    )
