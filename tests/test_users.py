"""Unit tests for the users tools against a fake client (plus one real-client gate test)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from splitwise_mcp.client import SplitwiseClient
from splitwise_mcp.config import Settings, get_settings
from splitwise_mcp.formatters import ResponseFormat
from splitwise_mcp.server import mcp
from splitwise_mcp.tools.users import (
    GetCurrentUserInput,
    GetUserInput,
    UpdateUserInput,
    splitwise_get_current_user,
    splitwise_get_user,
    splitwise_update_user,
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


def _status_error(status: int, body: dict[str, Any], method: str = "GET", path: str = "/") -> httpx.HTTPStatusError:
    request = httpx.Request(method, f"https://secure.splitwise.com/api/v3.0{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _install(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("splitwise_mcp.tools.users.get_client", lambda: fake)


CURRENT_USER: dict[str, Any] = {
    "id": 491923,
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "registration_status": "confirmed",
    "picture": {"small": "s.png", "medium": "m.png", "large": "l.png"},
    "default_currency": "USD",
    "locale": "en",
    "notifications_read": "2026-10-01T12:30:00Z",
    "notifications_count": 3,
    "notifications": {"added_as_friend": True, "expense_added": True, "monthly_summary": False},
}

OTHER_USER: dict[str, Any] = {
    "id": 77,
    "first_name": "Charles",
    "last_name": "Babbage",
    "email": "charles@example.com",
    "registration_status": "invited",
    "picture": {"small": "s.png", "medium": "m.png", "large": "l.png"},
}


# -- splitwise_get_current_user ---------------------------------------------


async def test_get_current_user_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_current_user": {"user": CURRENT_USER}})
    _install(monkeypatch, fake)

    result = await splitwise_get_current_user(GetCurrentUserInput())

    assert result.startswith("# Current Splitwise user")
    assert "account**: Ada Lovelace (id 491923) <ada@example.com>" in result
    assert "registration status**: confirmed" in result
    assert "default currency**: USD" in result
    assert "locale**: en" in result
    assert "unread notifications**: 3" in result
    assert "notifications last read**: 2026-10-01 12:30 UTC" in result
    assert "added_as_friend=on, expense_added=on, monthly_summary=off" in result
    assert fake.calls == [("GET", "/get_current_user", {})]


async def test_get_current_user_json_returns_raw_object(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_current_user": {"user": CURRENT_USER}})
    _install(monkeypatch, fake)

    result = await splitwise_get_current_user(GetCurrentUserInput(response_format=ResponseFormat.JSON))

    assert json.loads(result) == CURRENT_USER
    assert fake.calls == [("GET", "/get_current_user", {})]


async def test_get_current_user_missing_fields_render_na(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_current_user": {"user": {"id": 5, "first_name": "Solo"}}})
    _install(monkeypatch, fake)

    result = await splitwise_get_current_user(GetCurrentUserInput())

    assert "account**: Solo (id 5)" in result
    assert "<" not in result.split("account**:")[1].splitlines()[0]
    assert "default currency**: N/A" in result
    assert "notifications last read**: N/A" in result
    assert "notification settings**: N/A" in result


async def test_get_current_user_without_user_object(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_current_user": {}})
    _install(monkeypatch, fake)

    result = await splitwise_get_current_user(GetCurrentUserInput())

    assert result == "Error: Splitwise returned no user object for GET /get_current_user."


async def test_get_current_user_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={"/get_current_user": _status_error(401, {"error": "Invalid API request: you are not logged in"})}
    )
    _install(monkeypatch, fake)

    result = await splitwise_get_current_user(GetCurrentUserInput())

    assert result.startswith("Error (401)")
    assert "secure.splitwise.com/apps" in result


# -- splitwise_get_user -----------------------------------------------------


async def test_get_user_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_user/77": {"user": OTHER_USER}})
    _install(monkeypatch, fake)

    result = await splitwise_get_user(GetUserInput(user_id=77))

    assert result.startswith("# Splitwise user Charles Babbage (id 77)")
    assert "account**: Charles Babbage (id 77) <charles@example.com>" in result
    assert "registration status**: invited" in result
    assert fake.calls == [("GET", "/get_user/77", {})]


async def test_get_user_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_user/77": {"user": OTHER_USER}})
    _install(monkeypatch, fake)

    result = await splitwise_get_user(GetUserInput(user_id=77, response_format=ResponseFormat.JSON))

    assert json.loads(result) == OTHER_USER


async def test_get_user_forbidden_for_unconnected_user(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={"/get_user/12": _status_error(403, {"errors": {"base": ["You are not allowed to see this user"]}})}
    )
    _install(monkeypatch, fake)

    result = await splitwise_get_user(GetUserInput(user_id=12))

    assert result.startswith("Error (403): Forbidden – You are not allowed to see this user")
    assert fake.calls == [("GET", "/get_user/12", {})]


async def test_get_user_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_user/999999": _status_error(404, {"errors": {"base": ["Not found"]}})})
    _install(monkeypatch, fake)

    result = await splitwise_get_user(GetUserInput(user_id=999999))

    assert result.startswith("Error (404)")


def test_get_user_rejects_non_positive_id() -> None:
    with pytest.raises(ValidationError):
        GetUserInput(user_id=0)


# -- splitwise_update_user --------------------------------------------------


async def test_update_user_sends_only_given_fields_and_echoes_response(monkeypatch: pytest.MonkeyPatch) -> None:
    returned = {**CURRENT_USER, "first_name": "Augusta", "default_currency": "PEN"}
    fake = _FakeClient(routes={"/update_user/491923": {"user": returned}})
    _install(monkeypatch, fake)

    result = await splitwise_update_user(
        UpdateUserInput(user_id=491923, first_name="  Augusta ", default_currency=" pen ")
    )

    assert fake.calls == [
        ("POST", "/update_user/491923", {"data": {"first_name": "Augusta", "default_currency": "PEN"}})
    ]
    assert result.startswith("# Profile updated: Augusta Lovelace (id 491923)")
    assert "- **first_name**: `Augusta`\n" in result
    assert result.endswith("- **default_currency**: `PEN`")
    assert "differs" not in result
    assert "last_name" not in result


async def test_update_user_flat_response_and_mismatch_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inventory A3: the response may be the flat user object; render it as-is and flag
    # a field whose returned value is not what was sent, or that is missing.
    flat = {"id": 491923, "first_name": "Ada", "last_name": "Lovelace", "locale": "en"}
    fake = _FakeClient(routes={"/update_user/491923": flat})
    _install(monkeypatch, fake)

    result = await splitwise_update_user(
        UpdateUserInput(user_id=491923, last_name="Byron", locale="es", default_currency="EUR")
    )

    assert fake.calls == [
        ("POST", "/update_user/491923", {"data": {"last_name": "Byron", "locale": "es", "default_currency": "EUR"}})
    ]
    assert "# Profile updated: Ada Lovelace (id 491923)" in result
    assert "- **last_name**: `Lovelace` (sent `Byron` — the response differs)" in result
    assert "- **locale**: `en` (sent `es` — the response differs)" in result
    assert "- **default_currency**: sent `EUR` — not present in the response" in result


async def test_update_user_without_user_in_response(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/update_user/491923": {}})
    _install(monkeypatch, fake)

    result = await splitwise_update_user(UpdateUserInput(user_id=491923, locale="en"))

    assert "returned no user object" in result
    assert "splitwise_get_current_user" in result


async def test_update_user_forbidden_for_another_users_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={
            "/update_user/12": _status_error(
                403, {"errors": {"base": ["You cannot edit this user"]}}, method="POST", path="/update_user/12"
            )
        }
    )
    _install(monkeypatch, fake)

    result = await splitwise_update_user(UpdateUserInput(user_id=12, first_name="Mallory"))

    assert result.startswith("Error (403): Forbidden – You cannot edit this user")
    assert fake.calls == [("POST", "/update_user/12", {"data": {"first_name": "Mallory"}})]


async def test_update_user_requires_at_least_one_field_with_no_client_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient()
    _install(monkeypatch, fake)

    with pytest.raises(ValidationError, match="at least one of first_name, last_name, locale, default_currency"):
        UpdateUserInput(user_id=491923)

    # Through the MCP layer the same rejection happens before the tool body runs.
    with pytest.raises(ToolError, match="at least one of first_name"):
        await mcp.call_tool("splitwise_update_user", {"params": {"user_id": 491923}})
    assert fake.calls == []


@pytest.mark.parametrize("field", ["email", "password"])
def test_update_user_model_has_no_email_or_password(field: str) -> None:
    assert field not in UpdateUserInput.model_fields
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        UpdateUserInput.model_validate({"user_id": 491923, "first_name": "Ada", field: "x@example.com"})


@pytest.mark.parametrize("bad", ["US", "dollars", "12A"])
def test_update_user_rejects_bad_currency(bad: str) -> None:
    with pytest.raises(ValidationError, match="default_currency must be a 3-letter code"):
        UpdateUserInput(user_id=491923, default_currency=bad)


@pytest.mark.parametrize("bad", ["e", "english!", "en US"])
def test_update_user_rejects_bad_locale(bad: str) -> None:
    with pytest.raises(ValidationError):
        UpdateUserInput(user_id=491923, locale=bad)


def test_update_user_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        UpdateUserInput(user_id=491923, first_name="   ")


async def test_update_user_is_refused_by_the_real_client_when_writes_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"user": CURRENT_USER})

    client = SplitwiseClient(settings=Settings(splitwise_api_key="k" * 20), transport=httpx.MockTransport(handler))
    _install(monkeypatch, client)

    result = await splitwise_update_user(UpdateUserInput(user_id=491923, locale="es"))

    assert result.startswith("Error: POST /update_user/491923 would change your Splitwise account")
    assert "writes are disabled" in result
    assert "SPLITWISE_ALLOW_WRITES=1" in result
    assert sent == []


def test_update_user_docstring_states_the_gate_and_the_refused_fields() -> None:
    doc = splitwise_update_user.__doc__ or ""
    assert "writes are disabled" in doc
    assert "SPLITWISE_ALLOW_WRITES=1" in doc
    assert "Email and password are NOT updatable on purpose" in doc


# -- registration -----------------------------------------------------------


async def test_users_tools_registered_with_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("splitwise_get_current_user", "splitwise_get_user"):
        ann = tools[name].annotations
        assert ann is not None
        assert (ann.readOnlyHint, ann.destructiveHint, ann.idempotentHint, ann.openWorldHint) == (
            True,
            False,
            True,
            True,
        )
    ann = tools["splitwise_update_user"].annotations
    assert ann is not None
    assert (ann.readOnlyHint, ann.destructiveHint, ann.idempotentHint, ann.openWorldHint) == (False, False, True, True)


# -- live (read-only) -------------------------------------------------------


@pytest.mark.live
async def test_users_live_smoke() -> None:
    """Real read calls with the developer's key: the current user, then that user by id."""
    get_settings.cache_clear()
    raw = await splitwise_get_current_user(GetCurrentUserInput(response_format=ResponseFormat.JSON))
    me = json.loads(raw)
    assert isinstance(me.get("id"), int)

    result = await splitwise_get_user(GetUserInput(user_id=me["id"]))
    assert f"(id {me['id']})" in result
