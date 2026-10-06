"""Unit tests for SplitwiseClient using httpx.MockTransport."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from splitwise_mcp.client import (
    FORBIDDEN_FIELDS,
    ForbiddenFieldError,
    SplitwiseClient,
    SplitwiseEnvelopeError,
    WritesDisabledError,
    build_query,
    flatten_data,
    forbidden_fields_for,
    is_write,
)
from splitwise_mcp.config import Settings

KEY = "FAKE-PERSONAL-API-KEY-0123456789"


def _client_with(handler: Any, **settings_kwargs: Any) -> SplitwiseClient:
    settings = Settings(splitwise_api_key=KEY, **settings_kwargs)
    return SplitwiseClient(settings=settings, transport=httpx.MockTransport(handler))


def _capturing_handler(
    captured: dict[str, Any], *, body: Any = None, status: int = 200, headers: dict[str, str] | None = None
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["query"] = request.url.query.decode()
        captured["headers"] = dict(request.headers)
        captured["method"] = request.method
        captured["content"] = request.content
        return httpx.Response(status, json=body if body is not None else {"ok": True}, headers=headers)

    return handler


# --- pure helpers -------------------------------------------------------------


def test_build_query_drops_none_and_lowercases_bools() -> None:
    assert build_query({"group_id": 12, "friend_id": None, "flag": True, "other": False, "s": "a b"}) == (
        "group_id=12&flag=true&other=false&s=a+b"
    )
    assert build_query(None) == ""
    assert build_query({}) == ""


def test_flatten_data_users_list() -> None:
    flat = flatten_data(
        {
            "cost": "25.50",
            "group_id": 0,
            "note": None,
            "split_equally": True,
            "users": [
                {"user_id": 1, "paid_share": "25.50", "owed_share": Decimal("12.75"), "extra": None},
                {
                    "email": "b@example.com",
                    "first_name": "B",
                    "last_name": "C",
                    "paid_share": "0.00",
                    "owed_share": "12.75",
                },
            ],
        }
    )
    assert flat == {
        "cost": "25.50",
        "group_id": 0,
        "split_equally": "true",
        "users__0__user_id": 1,
        "users__0__paid_share": "25.50",
        "users__0__owed_share": "12.75",
        "users__1__email": "b@example.com",
        "users__1__first_name": "B",
        "users__1__last_name": "C",
        "users__1__paid_share": "0.00",
        "users__1__owed_share": "12.75",
    }


def test_flatten_data_leaves_plain_lists_and_empty_lists_alone() -> None:
    assert flatten_data({"ids": [1, 2], "users": []}) == {"ids": [1, 2], "users": []}
    assert flatten_data(None) == {}


def test_forbidden_fields_for() -> None:
    assert forbidden_fields_for("/create_expense") == frozenset({"password"})
    assert forbidden_fields_for("/update_user/42") == frozenset({"password", "email"})
    assert forbidden_fields_for("/update_user") == frozenset({"password", "email"})
    assert forbidden_fields_for("/update_username") == frozenset({"password"})
    assert "" in FORBIDDEN_FIELDS


def test_is_write() -> None:
    assert is_write("GET") is False
    assert is_write("get") is False
    assert is_write("POST") is True


# --- requests -----------------------------------------------------------------


async def test_get_sends_bearer_and_query_without_body() -> None:
    captured: dict[str, Any] = {}
    client = _client_with(_capturing_handler(captured))

    resp = await client.request("GET", "/get_expenses", params={"group_id": 5, "friend_id": None, "limit": 20})

    assert resp.status_code == 200
    assert captured["method"] == "GET"
    assert captured["url"] == "https://secure.splitwise.com/api/v3.0/get_expenses?group_id=5&limit=20"
    assert captured["headers"]["authorization"] == f"Bearer {KEY}"
    assert captured["headers"]["accept"] == "application/json"
    assert captured["content"] == b""


async def test_post_sends_flattened_json_body_when_writes_enabled() -> None:
    captured: dict[str, Any] = {}
    client = _client_with(_capturing_handler(captured), splitwise_allow_writes=True)

    await client.request(
        "POST",
        "/create_expense",
        data={
            "cost": "10.00",
            "description": "Lunch",
            "group_id": 0,
            "users": [{"user_id": 1, "paid_share": "10.00", "owed_share": "5.00"}],
        },
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://secure.splitwise.com/api/v3.0/create_expense"
    assert captured["headers"]["content-type"] == "application/json"
    assert json.loads(captured["content"]) == {
        "cost": "10.00",
        "description": "Lunch",
        "group_id": 0,
        "users__0__user_id": 1,
        "users__0__paid_share": "10.00",
        "users__0__owed_share": "5.00",
    }


async def test_post_refused_when_writes_disabled() -> None:
    captured: dict[str, Any] = {}
    client = _client_with(_capturing_handler(captured))

    with pytest.raises(WritesDisabledError, match="SPLITWISE_ALLOW_WRITES=1"):
        await client.request("POST", "/delete_expense/1")
    assert captured == {}  # never reached the network


async def test_password_is_forbidden_everywhere_even_with_writes() -> None:
    captured: dict[str, Any] = {}
    client = _client_with(_capturing_handler(captured), splitwise_allow_writes=True)

    with pytest.raises(ForbiddenFieldError, match="password"):
        await client.request("POST", "/create_group", data={"name": "x", "password": "y"})
    assert captured == {}


async def test_email_is_forbidden_only_for_update_user() -> None:
    captured: dict[str, Any] = {}
    client = _client_with(_capturing_handler(captured), splitwise_allow_writes=True)

    with pytest.raises(ForbiddenFieldError, match="email"):
        await client.request("POST", "/update_user/7", data={"email": "new@example.com"})
    assert captured == {}

    await client.request(
        "POST",
        "/add_user_to_group",
        data={"group_id": 1, "email": "x@example.com", "first_name": "X", "last_name": "Y"},
    )
    assert json.loads(captured["content"])["email"] == "x@example.com"


async def test_forbidden_field_check_runs_before_the_write_gate() -> None:
    # Even with writes OFF the forbidden-field error names the real problem.
    client = _client_with(_capturing_handler({}))
    with pytest.raises(ForbiddenFieldError):
        await client.request("POST", "/update_user/7", data={"password": "x"})


async def test_missing_key_errors_before_any_request() -> None:
    captured: dict[str, Any] = {}
    client = SplitwiseClient(settings=Settings(), transport=httpx.MockTransport(_capturing_handler(captured)))
    with pytest.raises(RuntimeError, match="No Splitwise API key configured"):
        await client.request("GET", "/get_current_user")
    assert captured == {}


async def test_envelope_success_false_raises() -> None:
    client = _client_with(
        _capturing_handler({}, body={"success": False, "errors": {"base": ["Group not found"]}}),
        splitwise_allow_writes=True,
    )
    with pytest.raises(SplitwiseEnvelopeError, match="Group not found") as info:
        await client.request("POST", "/undelete_group/9")
    assert info.value.errors == {"base": ["Group not found"]}
    assert info.value.path == "/undelete_group/9"


async def test_envelope_non_empty_errors_raises_even_with_success_missing() -> None:
    client = _client_with(
        _capturing_handler({}, body={"expenses": [], "errors": {"cost": ["is invalid"]}}), splitwise_allow_writes=True
    )
    with pytest.raises(SplitwiseEnvelopeError, match="cost: is invalid"):
        await client.request("POST", "/create_expense", data={"cost": "x"})


async def test_envelope_empty_errors_is_success() -> None:
    body = {"expenses": [{"id": 1}], "errors": {}}
    client = _client_with(_capturing_handler({}, body=body), splitwise_allow_writes=True)
    resp = await client.request("POST", "/create_expense", data={"cost": "1.00"})
    assert resp.json() == body

    body_list = {"users": [{"id": 1}], "errors": []}
    client = _client_with(_capturing_handler({}, body=body_list), splitwise_allow_writes=True)
    resp = await client.request("POST", "/create_friends", data={"users": [{"email": "a@b.c"}]})
    assert resp.json() == body_list


async def test_envelope_success_false_without_errors_still_raises() -> None:
    client = _client_with(_capturing_handler({}, body={"success": False}), splitwise_allow_writes=True)
    with pytest.raises(SplitwiseEnvelopeError, match="no error message"):
        await client.request("POST", "/delete_expense/1")


async def test_non_json_200_is_returned_untouched() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    client = _client_with(handler)
    resp = await client.request("GET", "/get_currencies")
    assert resp.text == "ok"


async def test_http_error_raises_status_error() -> None:
    client = _client_with(
        _capturing_handler({}, body={"error": "Invalid API request: you are not logged in"}, status=401)
    )
    with pytest.raises(httpx.HTTPStatusError) as info:
        await client.request("GET", "/get_current_user")
    assert info.value.response.status_code == 401


async def test_retry_after_is_captured() -> None:
    client = _client_with(_capturing_handler({}, headers={"retry-after": "12"}))
    assert client.last_retry_after is None
    await client.request("GET", "/get_groups")
    assert client.last_retry_after == "12"


async def test_custom_base_url_and_path_join() -> None:
    captured: dict[str, Any] = {}
    client = _client_with(_capturing_handler(captured), splitwise_api_url="https://proxy.example.test/sw/")
    await client.request("GET", "get_groups")
    assert captured["url"] == "https://proxy.example.test/sw/get_groups"


async def test_timeout_override_is_applied() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={})

    client = _client_with(handler, splitwise_request_timeout_seconds=3.0)
    await client.request("GET", "/get_groups")
    assert seen["timeout"]["read"] == 3.0
    await client.request("GET", "/get_groups", timeout=0.5)
    assert seen["timeout"]["read"] == 0.5
