"""Baton's entry point. Run the daemon with `python -m baton`."""

from baton.config import BatonConfig
from baton.server import build_server, build_supervisor


def main() -> None:
    """Build baton's collaborators from the environment and run its MCP server."""
    config = BatonConfig.from_env()
    supervisor = build_supervisor(config)
    server = build_server(supervisor)
    server.run(transport="sse", host=config.host, port=config.port)


if __name__ == "__main__":
    main()
