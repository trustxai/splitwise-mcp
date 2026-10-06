"""Unit tests for the health-check tool against a fake client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from splitwise_mcp.config import Settings, get_settings
from splitwise_mcp.tools.health import splitwise_health_check


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """Routes by path; records every call (method, path, kwargs)."""

    def __init__(self, routes: dict[str, Any] | None = None, exc: Exception | None = None) -> None:
        self._routes = routes or {}
        self._exc = exc
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.last_retry_after: str | None = None

    async def request(self, method: str, path: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, path, kwargs))
        if self._exc is not None:
            raise self._exc
        payload = self._routes.get(path)
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload if payload is not None else {})


def _status_error(status: int, body: dict[str, Any]) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://secure.splitwise.com/api/v3.0/get_current_user")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


CURRENT_USER = {
    "user": {
        "id": 491923,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "registration_status": "confirmed",
        "default_currency": "USD",
        "locale": "en",
        "notifications_count": 3,
        "notifications": {"expense_added": True, "monthly_summary": False},
    }
}


async def test_health_without_key_reports_config_only(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    monkeypatch.setattr("splitwise_mcp.tools.health.get_client", lambda: fake)
    monkeypatch.setattr("splitwise_mcp.tools.health.get_settings", lambda: Settings())

    result = await splitwise_health_check()

    assert "base URL**: https://secure.splitwise.com/api/v3.0" in result
    assert "disabled (read-only" in result
    assert "none configured" in result
    assert fake.calls == []


async def test_health_with_key_reports_account(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_current_user": CURRENT_USER})
    monkeypatch.setattr("splitwise_mcp.tools.health.get_client", lambda: fake)
    monkeypatch.setattr(
        "splitwise_mcp.tools.health.get_settings",
        lambda: Settings(splitwise_api_key="k" * 20, splitwise_allow_writes=True),
    )

    result = await splitwise_health_check()

    assert "ENABLED — creating/updating/deleting is allowed" in result
    assert "connectivity**: OK" in result
    assert "account**: Ada Lovelace (id 491923) <ada@example.com>" in result
    assert "default currency**: USD" in result
    assert "unread notifications**: 3" in result
    assert "expense_added=on, monthly_summary=off" in result
    assert fake.calls == [("GET", "/get_current_user", {})]


async def test_health_reports_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_current_user": CURRENT_USER})
    fake.last_retry_after = "9"
    monkeypatch.setattr("splitwise_mcp.tools.health.get_client", lambda: fake)
    monkeypatch.setattr("splitwise_mcp.tools.health.get_settings", lambda: Settings(splitwise_api_key="k" * 20))

    result = await splitwise_health_check()

    assert "retry-after**: 9s" in result


async def test_health_unauthorized_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={"/get_current_user": _status_error(401, {"error": "Invalid API request: you are not logged in"})}
    )
    monkeypatch.setattr("splitwise_mcp.tools.health.get_client", lambda: fake)
    monkeypatch.setattr("splitwise_mcp.tools.health.get_settings", lambda: Settings(splitwise_api_key="k" * 20))

    result = await splitwise_health_check()

    assert result.startswith("Error (401)")
    assert "secure.splitwise.com/apps" in result


async def test_health_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(exc=httpx.ConnectError("refused"))
    monkeypatch.setattr("splitwise_mcp.tools.health.get_client", lambda: fake)
    monkeypatch.setattr("splitwise_mcp.tools.health.get_settings", lambda: Settings(splitwise_api_key="k" * 20))

    result = await splitwise_health_check()

    assert result.startswith("Error")
    assert "could not connect" in result


@pytest.mark.live
async def test_health_live_smoke() -> None:
    """Real call with the developer's key: connectivity and the current user."""
    get_settings.cache_clear()
    result = await splitwise_health_check()

    assert "connectivity**: OK" in result
    assert "account**:" in result
