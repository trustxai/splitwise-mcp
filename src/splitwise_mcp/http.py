"""Streamable-HTTP transport behind a bearer token — owned by the t5-http-transport worktree.

Stage-0 stub. The contract (research/03 §"Design for t5"): `build_app(mcp, settings)`
returns the SDK's streamable-http ASGI app wrapped in a constant-time bearer middleware;
`serve(mcp)` applies the refuse-to-start rules and runs uvicorn without an access log.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from splitwise_mcp.config import Settings

_STUB_MESSAGE = "the streamable-http transport is owned by the t5-http-transport worktree and is not implemented yet"


def build_app(mcp: FastMCP, settings: Settings) -> Any:
    """Return the ASGI app (bearer middleware around the SDK's streamable-http app)."""
    raise NotImplementedError(_STUB_MESSAGE)


def serve(mcp: FastMCP) -> None:
    """Run the HTTP server from `get_settings()` (refuses to start without a proper bearer)."""
    raise NotImplementedError(_STUB_MESSAGE)
