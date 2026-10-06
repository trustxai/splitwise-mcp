"""Registry + entry-point tests for the Stage-0 spine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from splitwise_mcp.server import main_http, mcp

EXPECTED_MODULES = [
    "balances",
    "comments",
    "expenses",
    "friends",
    "groups",
    "health",
    "lookup",
    "notifications",
    "users",
]


def test_registry_imports_every_wave_module() -> None:
    for name in EXPECTED_MODULES:
        assert f"splitwise_mcp.tools.{name}" in sys.modules, f"tools/{name}.py is not imported by register_all"


async def test_health_tool_is_registered_with_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    assert "splitwise_health_check" in tools
    health = tools["splitwise_health_check"]
    assert health.annotations is not None
    assert health.annotations.readOnlyHint is True
    assert health.annotations.openWorldHint is True
    assert (health.description or "").splitlines()[0].startswith("Verify connectivity")


async def test_every_registered_tool_has_name_prefix_and_annotations() -> None:
    for tool in await mcp.list_tools():
        assert tool.name.startswith("splitwise_"), tool.name
        assert tool.annotations is not None, tool.name
        assert tool.description, tool.name


def test_http_entry_point_is_wired_to_the_stub() -> None:
    with pytest.raises(NotImplementedError, match="t5-http-transport"):
        main_http()


def test_console_scripts_declared() -> None:
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert 'amazing-splitwise-mcp = "splitwise_mcp.server:main_stdio"' in pyproject
    assert 'amazing-splitwise-mcp-http = "splitwise_mcp.server:main_http"' in pyproject
