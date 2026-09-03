"""Baton's entry point. Run the daemon with `python -m baton`."""

import uvicorn

from baton.config import BatonConfig
from baton.server import build_app, build_server, build_supervisor
from baton.worker import write_mcp_config


def main() -> None:
    """Build baton's collaborators and serve its MCP app under uvicorn.

    The app's lifespan runs the supervisor's `start` and `shutdown` hooks
    around uvicorn's serving life.
    """
    config = BatonConfig.from_env()
    write_mcp_config(config)
    supervisor = build_supervisor(config)
    server = build_server(supervisor)
    app = build_app(supervisor, server, config)
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
