"""Allow running the server with `python -m splitwise_mcp` (stdio)."""

from __future__ import annotations

from splitwise_mcp.server import main_stdio

if __name__ == "__main__":
    main_stdio()
