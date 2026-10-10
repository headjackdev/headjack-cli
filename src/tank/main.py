"""Entry point: `python3 -m tank.main`. Reads config from the environment, builds
the server, runs it over stdio — the transport Claude Code and the Agent SDK expect
for a locally launched MCP server. See docs/tank-mcp-config.example.json for how a
client registers this process."""

from __future__ import annotations

from tank.config import TankConfig
from tank.server import build_server


def main() -> None:
    config = TankConfig.from_env()
    server = build_server(config)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
