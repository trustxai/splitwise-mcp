"""Unit tests for the notifications tool against a fake client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from splitwise_mcp.config import get_settings
from splitwise_mcp.server import mcp
from splitwise_mcp.tools.notifications import (
    MAX_DISPLAY_ROWS,
    GetNotificationsInput,
    notification_text,
    notification_type_name,
    splitwise_get_notifications,
)


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """Routes by path; records every call (method, path, kwargs)."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self._routes = routes or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.last_retry_after: str | None = None

    async def request(self, method: str, path: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, path, kwargs))
        payload = self._routes.get(path)
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload if payload is not None else {})


def _status_error(status: int, body: dict[str, Any]) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://secure.splitwise.com/api/v3.0/get_notifications")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr("splitwise_mcp.tools.notifications.get_client", lambda: fake)


EXPENSE_ADDED: dict[str, Any] = {
    "id": 1003,
    "type": 0,
    "created_at": "2026-10-05T13:00:00Z",
    "created_by": 491923,
    "source": {"type": "Expense", "id": 51023, "url": None},
    "image_url": "https://x/img.png",
    "image_shape": "square",
    "content": (
        '<strong>Ada L.</strong> added <strong>"Dinner"</strong> in <strong>"Trip"</strong>.'
        '<br><font color="#5bc5a7">You get back $12.50</font>'
    ),
}

EXPENSE_UPDATED: dict[str, Any] = {
    "id": 1002,
    "type": 1,
    "created_at": "2026-10-04T09:30:00Z",
    "created_by": 77,
    "source": {"type": "Expense", "id": 51000},
    "content": '<strong>Bob</strong> updated "Taxi": cost <strike>$10.00</strike> $12.00',
}

UNDOCUMENTED: dict[str, Any] = {
    "id": 1001,
    "type": 99,
    "created_at": "2026-10-01T08:00:00Z",
    "created_by": None,
    "source": {"type": "News", "id": 7, "url": "https://example.com/n"},
    "content": "Something | new &amp; <small>shiny</small>",
}

FEED = {"notifications": [EXPENSE_ADDED, EXPENSE_UPDATED, UNDOCUMENTED]}


async def test_markdown_maps_types_flattens_html_and_sends_default_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_notifications": FEED})
    _install(monkeypatch, fake)

    result = await splitwise_get_notifications(GetNotificationsInput())

    assert fake.calls == [("GET", "/get_notifications", {"params": {"limit": 20}})]
    assert result.startswith("# Splitwise notifications")
    assert "3 notification(s), newest first (most recent, limit 20)." in result
    assert "| When | Type | Content | Source | By | Id |" in result
    assert (
        '| 2026-10-05 13:00 UTC | Expense added | Ada L. added "Dinner" in "Trip".<br>You get back $12.50 '
        "| Expense 51023 | user 491923 | 1003 |"
    ) in result
    assert '| 2026-10-04 09:30 UTC | Expense updated | Bob updated "Taxi": cost ~~$10.00~~ $12.00 ' in result
    assert (
        "| 2026-10-01 08:00 UTC | unknown (99) | Something \\| new & shiny "
        "| News 7 <https://example.com/n> | — | 1001 |"
    ) in result
    assert "<strong>" not in result


async def test_updated_after_is_normalised_and_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_notifications": FEED})
    _install(monkeypatch, fake)

    result = await splitwise_get_notifications(GetNotificationsInput(updated_after="2026-10-01", limit=50))

    assert fake.calls == [
        ("GET", "/get_notifications", {"params": {"updated_after": "2026-10-01T00:00:00Z", "limit": 50}})
    ]
    assert "(updated after 2026-10-01T00:00:00Z, limit 50)" in result


async def test_limit_zero_means_server_maximum_and_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_notifications": {"notifications": []}})
    _install(monkeypatch, fake)

    result = await splitwise_get_notifications(GetNotificationsInput(limit=0))

    assert fake.calls == [("GET", "/get_notifications", {"params": {"limit": 0}})]
    assert result == "# Splitwise notifications\n\n_No notifications (most recent, server maximum)._"


async def test_json_keeps_raw_html_and_names_the_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_notifications": FEED})
    _install(monkeypatch, fake)

    result = await splitwise_get_notifications(GetNotificationsInput(limit=3, response_format="json"))  # type: ignore[arg-type]

    payload = json.loads(result)
    assert payload == {
        "count": 3,
        "limit": 3,
        "updated_after": None,
        "type_names": {"0": "Expense added", "1": "Expense updated", "99": "unknown (99)"},
        "notifications": FEED["notifications"],
    }
    assert "<strong>Ada L.</strong>" in payload["notifications"][0]["content"]
    assert fake.calls == [("GET", "/get_notifications", {"params": {"limit": 3}})]


async def test_markdown_caps_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [{**EXPENSE_ADDED, "id": 5000 + i} for i in range(MAX_DISPLAY_ROWS + 10)]
    fake = _FakeClient(routes={"/get_notifications": {"notifications": many}})
    _install(monkeypatch, fake)

    result = await splitwise_get_notifications(GetNotificationsInput(limit=0))

    assert f"{MAX_DISPLAY_ROWS + 10} notification(s)" in result
    assert f"| {5000 + MAX_DISPLAY_ROWS - 1} |" in result
    assert f"| {5000 + MAX_DISPLAY_ROWS} |" not in result
    assert f"Showing the newest {MAX_DISPLAY_ROWS} of {MAX_DISPLAY_ROWS + 10}" in result


async def test_unauthorized_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={"/get_notifications": _status_error(401, {"error": "Invalid API request: you are not logged in"})}
    )
    _install(monkeypatch, fake)

    result = await splitwise_get_notifications(GetNotificationsInput())

    assert result.startswith("Error (401): Unauthorized – Invalid API request: you are not logged in.")
    assert "secure.splitwise.com/apps" in result


# -- local validation (no call is ever made) --------------------------------------


def test_bad_updated_after_is_rejected() -> None:
    with pytest.raises(ValidationError, match="updated_after must be ISO-8601"):
        GetNotificationsInput(updated_after="last tuesday")


def test_updated_after_with_offset_is_converted_to_utc() -> None:
    assert GetNotificationsInput(updated_after="2026-10-01T10:00:00-05:00").updated_after == "2026-10-01T15:00:00Z"


@pytest.mark.parametrize("limit", [-1, 101])
def test_limit_out_of_range_is_rejected(limit: int) -> None:
    with pytest.raises(ValidationError, match="limit"):
        GetNotificationsInput(limit=limit)


def test_extra_fields_are_forbidden() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GetNotificationsInput(offset=10)  # type: ignore[call-arg]


# -- helpers ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "name"),
    [(0, "Expense added"), (3, "Comment added"), (15, "Friend currency conversion"), (16, "unknown (16)")],
)
def test_notification_type_name(code: int, name: str) -> None:
    assert notification_type_name(code) == name


def test_notification_type_name_rejects_non_int_codes() -> None:
    assert notification_type_name(None) == "unknown (None)"
    assert notification_type_name(True) == "unknown (True)"
    assert notification_type_name("0") == "unknown (0)"


def test_notification_text_keeps_struck_values() -> None:
    assert notification_text("cost <STRIKE>$10.00</STRIKE> $12.00<br/>by <strong>Bob</strong>") == (
        "cost ~~$10.00~~ $12.00\nby Bob"
    )
    assert notification_text(None) == ""


async def test_notifications_tool_is_registered_read_only() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    annotations = tools["splitwise_get_notifications"].annotations
    assert annotations is not None
    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False
    assert annotations.idempotentHint is True
    assert annotations.openWorldHint is True


@pytest.mark.live
async def test_get_notifications_live_smoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real read with the developer's key: the newest notification."""
    get_settings.cache_clear()
    monkeypatch.setattr("splitwise_mcp.client._client", None)

    result = await splitwise_get_notifications(GetNotificationsInput(limit=1))

    assert result.startswith("# Splitwise notifications")
