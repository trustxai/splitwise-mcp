"""Tool registration for splitwise_mcp.

ALL wave modules are listed here from day one (even while some are still empty
stubs) so that parallel worktrees implementing individual modules never need to
touch this file — eliminating merge conflicts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Import every tool module; @mcp.tool decorators self-register on import."""
    from splitwise_mcp.tools import (  # noqa: F401
        balances,
        comments,
        expenses,
        friends,
        groups,
        health,
        lookup,
        notifications,
        users,
    )
