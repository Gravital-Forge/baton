"""Wires baton's collaborators into a supervisor and an MCP server."""

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from baton.config import BatonConfig
from baton.engine import Supervisor
from baton.state import StateStore
from baton.tmux import TmuxAdapter
from baton.tools import BatonTools
from baton.worker import WorkerLauncher


def write_client_config(config: BatonConfig) -> Path:
    """Write the MCP client config that points a session at this daemon.

    The daemon writes it at startup so an operator can point a Claude Code
    session at baton before any project has been initialized.
    `Supervisor.initialize` writes the same file again on every project
    start, so a `BATON_PORT` change made between runs still reaches a
    worker; neither write may be removed.

    Args:
        config: The runtime configuration naming the address to publish
            and the directory to publish it in.

    Returns:
        The path the config was written to.
    """
    launcher = WorkerLauncher(config, TmuxAdapter(config.tmux_bin))
    return launcher.write_mcp_config()


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
