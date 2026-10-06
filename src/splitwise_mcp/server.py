"""FastMCP server definition and entry points for splitwise_mcp."""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("splitwise_mcp")

# FastMCP configures root logging at INFO to stderr, which MCP clients persist to log
# files. At INFO, httpx logs one "HTTP Request: ..." line per call — the URL carries no
# secret on this API, but the line still records every query the account makes, so
# quiet it down to WARNING. httpcore only logs at DEBUG today; it is pinned too.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Register all tools (side-effect imports via decorators)
from splitwise_mcp.tools import register_all  # noqa: E402

register_all(mcp)


def main_stdio() -> None:
    """Entry point for local / Docker stdio transport (`amazing-splitwise-mcp`)."""
    mcp.run()


def main_http() -> None:
    """Entry point for the streamable-http transport behind a bearer (`amazing-splitwise-mcp-http`)."""
    from splitwise_mcp.http import serve

    serve(mcp)


if __name__ == "__main__":
    main_stdio()
