"""Wires baton's collaborators into a supervisor and an MCP server."""

from mcp.server.mcpserver import MCPServer

from baton.config import BatonConfig
from baton.engine import Supervisor
from baton.state import StateStore
from baton.tmux import TmuxAdapter
from baton.tools import BatonTools
from baton.worker import WorkerLauncher


def build_supervisor(config: BatonConfig) -> Supervisor:
    """Build the supervisor and prime the daemon's MCP config for workers.

    Writes ``<state dir>/mcp.json`` before returning, so an operator can
    point a Claude Code session at this daemon before any project has been
    initialized. `Supervisor.initialize` writes the same file again on
    every project start, so a `BATON_PORT` change made between runs still
    reaches a worker; neither write may be removed.

    Args:
        config: The runtime configuration governing every collaborator.

    Returns:
        A Supervisor loaded from any state already on disk, or a fresh,
        uninitialized one when there is none.
    """
    store = StateStore(config.state_dir)
    tmux = TmuxAdapter(config.tmux_bin)
    launcher = WorkerLauncher(config, tmux)
    supervisor = Supervisor(config, store, tmux, launcher)
    launcher.write_mcp_config()
    return supervisor


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
