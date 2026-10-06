"""Unit tests for the friends tools against a fake client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from splitwise_mcp.client import SplitwiseClient, SplitwiseEnvelopeError, WritesDisabledError
from splitwise_mcp.config import Settings, get_settings
from splitwise_mcp.server import mcp
from splitwise_mcp.tools.friends import (
    MAX_DISPLAY_ROWS,
    PARTIAL_ADD_HINT,
    CreateFriendInput,
    CreateFriendsInput,
    DeleteFriendInput,
    GetFriendInput,
    GetFriendsInput,
    splitwise_create_friend,
    splitwise_create_friends,
    splitwise_delete_friend,
    splitwise_get_friend,
    splitwise_get_friends,
)


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


def _status_error(status: int, body: dict[str, Any], path: str = "/get_friend/99") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://secure.splitwise.com/api/v3.0{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _use(monkeypatch: pytest.MonkeyPatch, fake: _FakeClient) -> None:
    monkeypatch.setattr("splitwise_mcp.tools.friends.get_client", lambda: fake)


GRACE = {
    "id": 4821,
    "first_name": "Grace",
    "last_name": "Hopper",
    "email": "grace@example.com",
    "registration_status": "confirmed",
    "balance": [{"currency_code": "USD", "amount": "12.5"}, {"currency_code": "PEN", "amount": "-3.00"}],
    "groups": [
        {"group_id": 0, "balance": [{"currency_code": "USD", "amount": "2.50"}]},
        {"group_id": 77, "balance": [{"currency_code": "USD", "amount": "10.00"}]},
        {"group_id": 78, "balance": []},
    ],
    "updated_at": "2026-10-01T13:05:00Z",
}
ALAN = {
    "id": 5150,
    "first_name": "Alan",
    "last_name": None,
    "email": "alan@example.com",
    "registration_status": "invited",
    "balance": [{"currency_code": "USD", "amount": "0.0"}],
    "groups": [],
    "updated_at": "2026-09-01T00:00:00Z",
}


# -- get_friends -----------------------------------------------------------------


async def test_get_friends_markdown_renders_rows_and_sign_convention(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_friends": {"friends": [GRACE, ALAN]}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput())

    assert "positive = they owe you" in result
    assert "Showing **2** friend(s)." in result
    assert "| Grace Hopper (id 4821) | grace@example.com | confirmed | +12.50 USD, -3.00 PEN |" in result
    assert "| Alan (id 5150) | alan@example.com | invited | settled up |" in result
    assert fake.calls == [("GET", "/get_friends", {})]


async def test_get_friends_only_with_balance_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    empty_balance = {**ALAN, "id": 6000, "first_name": "Edsger", "balance": []}
    fake = _FakeClient(routes={"/get_friends": {"friends": [GRACE, ALAN, empty_balance]}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput(only_with_balance=True))

    assert "Showing **1** friend(s) with a non-zero balance (of 3 friends)." in result
    assert "Grace Hopper (id 4821)" in result
    assert "Alan (id 5150)" not in result
    assert "Edsger (id 6000)" not in result


async def test_get_friends_missing_balance_renders_na(monkeypatch: pytest.MonkeyPatch) -> None:
    no_balance = {key: value for key, value in ALAN.items() if key != "balance"}
    empty_balance = {**ALAN, "id": 6000, "first_name": "Edsger", "balance": []}
    fake = _FakeClient(routes={"/get_friends": {"friends": [no_balance, empty_balance]}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput())

    assert "| Alan (id 5150) | alan@example.com | invited | N/A |" in result
    assert "| Edsger (id 6000) | alan@example.com | invited | settled up |" in result


async def test_get_friends_only_with_balance_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_friends": {"friends": [ALAN]}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput(only_with_balance=True))

    assert "_No friends with a non-zero balance._" in result
    assert "| Friend |" not in result


async def test_get_friends_json_returns_raw_objects_with_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_friends": {"friends": [GRACE, ALAN]}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput(response_format="json", only_with_balance=True))

    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["only_with_balance"] is True
    assert payload["friends"] == [GRACE]
    assert payload["friends"][0]["groups"][1] == {
        "group_id": 77,
        "balance": [{"currency_code": "USD", "amount": "10.00"}],
    }


async def test_get_friends_caps_display_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [{**ALAN, "id": 1000 + i, "first_name": f"Friend{i}"} for i in range(MAX_DISPLAY_ROWS + 5)]
    fake = _FakeClient(routes={"/get_friends": {"friends": many}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput())

    assert f"**{MAX_DISPLAY_ROWS + 5}** friend(s); showing the first {MAX_DISPLAY_ROWS}." in result
    assert "Showing **" not in result
    assert f"Friend{MAX_DISPLAY_ROWS - 1} (id {1000 + MAX_DISPLAY_ROWS - 1})" in result
    assert f"Friend{MAX_DISPLAY_ROWS} (id" not in result
    assert "_5 more friend(s) not shown (display cap 50)" in result


async def test_get_friends_escapes_pipes_in_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    odd = {**ALAN, "first_name": "A|B"}
    fake = _FakeClient(routes={"/get_friends": {"friends": [odd]}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput())

    assert "| A\\|B (id 5150) |" in result


async def test_get_friends_no_friends(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_friends": {"friends": []}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput())

    assert "_No friends._" in result


async def test_get_friends_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={"/get_friends": _status_error(401, {"error": "Invalid API request: you are not logged in"})}
    )
    _use(monkeypatch, fake)

    result = await splitwise_get_friends(GetFriendsInput())

    assert result.startswith("Error (401)")
    assert "you are not logged in" in result


def test_get_friends_input_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        GetFriendsInput.model_validate({"limit": 5})


# -- get_friend ------------------------------------------------------------------


async def test_get_friend_markdown_renders_group_balances(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_friend/4821": {"friend": GRACE}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friend(GetFriendInput(friend_id=4821))

    assert result.startswith("# Splitwise friend: Grace Hopper (id 4821)")
    assert "- **email**: grace@example.com" in result
    assert "- **balance**: +12.50 USD, -3.00 PEN" in result
    assert "- **updated at**: 2026-10-01 13:05 UTC" in result
    assert "positive = they owe you" in result
    assert "- non-group expenses (group 0): +2.50 USD" in result
    assert "- group 77: +10.00 USD" in result
    assert "- group 78: settled up" in result
    assert fake.calls == [("GET", "/get_friend/4821", {})]


async def test_get_friend_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_friend/4821": {"friend": GRACE}})
    _use(monkeypatch, fake)

    result = await splitwise_get_friend(GetFriendInput(friend_id=4821, response_format="json"))

    assert json.loads(result) == GRACE


async def test_get_friend_not_found_routes_errors_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/get_friend/99": _status_error(404, {"errors": {"base": ["Friend not found"]}})})
    _use(monkeypatch, fake)

    result = await splitwise_get_friend(GetFriendInput(friend_id=99))

    assert result.startswith("Error (404): Not found – Friend not found.")


def test_get_friend_input_rejects_non_positive_id() -> None:
    with pytest.raises(ValidationError):
        GetFriendInput(friend_id=0)


# -- create_friend ---------------------------------------------------------------


async def test_create_friend_sends_compact_body_and_echoes_friend(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/create_friend": {"friend": {**ALAN, "last_name": "Turing"}}})
    _use(monkeypatch, fake)

    result = await splitwise_create_friend(CreateFriendInput(user_email="  alan@example.com ", user_first_name="Alan"))

    assert fake.calls == [
        ("POST", "/create_friend", {"data": {"user_email": "alan@example.com", "user_first_name": "Alan"}})
    ]
    assert "Friend added: Alan Turing (id 5150)" in result
    assert "- **email**: alan@example.com" in result
    assert "- **registration status**: invited" in result


async def test_create_friend_empty_body_does_not_claim_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/create_friend": {}})
    _use(monkeypatch, fake)

    result = await splitwise_create_friend(CreateFriendInput(user_email="alan@example.com"))

    assert "returned no friend object" in result
    assert "Friend added" not in result


async def test_create_friend_refused_when_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        exc=WritesDisabledError("POST /create_friend would change your Splitwise account, but writes are disabled.")
    )
    _use(monkeypatch, fake)

    result = await splitwise_create_friend(CreateFriendInput(user_email="alan@example.com"))

    assert result.startswith("Error: POST /create_friend")
    assert "writes are disabled" in result


def test_create_friend_input_rejects_bad_email() -> None:
    with pytest.raises(ValidationError):
        CreateFriendInput(user_email="Alan Turing")


# -- create_friends --------------------------------------------------------------


async def test_create_friends_flattens_users_and_renders_users(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/create_friends": {"users": [GRACE, {**ALAN, "last_name": "Turing"}], "errors": {}}})
    _use(monkeypatch, fake)

    result = await splitwise_create_friends(
        CreateFriendsInput.model_validate(
            {
                "friends": [
                    {"email": "grace@example.com"},
                    {"email": "alan@example.com", "first_name": "Alan", "last_name": "Turing"},
                ]
            }
        )
    )

    assert fake.calls == [
        (
            "POST",
            "/create_friends",
            {
                "data": {
                    "users__0__email": "grace@example.com",
                    "users__1__email": "alan@example.com",
                    "users__1__first_name": "Alan",
                    "users__1__last_name": "Turing",
                }
            },
        )
    ]
    assert "Splitwise returned 2 user(s) for 2 requested friend(s):" in result
    assert "- Grace Hopper (id 4821) — grace@example.com (confirmed)" in result
    assert "- Alan Turing (id 5150) — alan@example.com (invited)" in result
    assert "- **errors**: none" in result


async def test_create_friends_envelope_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        exc=SplitwiseEnvelopeError({"base": ["alan@example is not a valid email"]}, path="/create_friends")
    )
    _use(monkeypatch, fake)

    result = await splitwise_create_friends(
        CreateFriendsInput.model_validate({"friends": [{"email": "alan@example.com"}]})
    )

    assert result == (
        "Error: Splitwise rejected the request to /create_friends: alan@example is not a valid email\n"
        f"{PARTIAL_ADD_HINT}"
    )


def _real_client(handler: Any) -> SplitwiseClient:
    settings = Settings(splitwise_api_key="k" * 20, splitwise_allow_writes=True)
    return SplitwiseClient(settings=settings, transport=httpx.MockTransport(handler))


async def test_create_friends_partial_success_through_real_client_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 200 that added Grace but rejected Bob must not read as 'nobody was added'."""
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"users": [GRACE], "errors": {"base": ["bob@example.com could not be added"]}})

    client = _real_client(handler)
    monkeypatch.setattr("splitwise_mcp.tools.friends.get_client", lambda: client)

    result = await splitwise_create_friends(
        CreateFriendsInput.model_validate({"friends": [{"email": "grace@example.com"}, {"email": "bob@example.com"}]})
    )

    assert seen == [
        (
            "POST",
            "/api/v3.0/create_friends",
            {"users__0__email": "grace@example.com", "users__1__email": "bob@example.com"},
        )
    ]
    assert result.startswith("Error: Splitwise rejected the request to /create_friends: bob@example.com could not")
    # The partial body travels on the error (SplitwiseEnvelopeError.body): the people Splitwise
    # DID add are listed, so the LLM never reads this as "nobody was added".
    assert "- **added by Splitwise despite the errors** (1): Grace" in result
    assert result.endswith(PARTIAL_ADD_HINT)


async def test_create_friends_rejected_with_nobody_added_has_no_added_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"users": [], "errors": {"base": ["nothing added"]}})

    client = _real_client(handler)
    monkeypatch.setattr("splitwise_mcp.tools.friends.get_client", lambda: client)

    result = await splitwise_create_friends(
        CreateFriendsInput.model_validate({"friends": [{"email": "bob@example.com"}]})
    )

    assert result.startswith("Error: Splitwise rejected the request to /create_friends: nothing added")
    assert "added by Splitwise" not in result
    assert result.endswith(PARTIAL_ADD_HINT)


async def test_create_friends_http_400_through_real_client_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"users": [], "errors": {"base": ["bob@example is not a valid email"]}})

    client = _real_client(handler)
    monkeypatch.setattr("splitwise_mcp.tools.friends.get_client", lambda: client)

    result = await splitwise_create_friends(
        CreateFriendsInput.model_validate({"friends": [{"email": "bob@example.com"}]})
    )

    assert result.startswith("Error (400): Bad request – bob@example is not a valid email.")
    assert result.endswith(PARTIAL_ADD_HINT)


async def test_create_friends_writes_disabled_has_no_partial_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        exc=WritesDisabledError("POST /create_friends would change your account, but writes are disabled.")
    )
    _use(monkeypatch, fake)

    result = await splitwise_create_friends(
        CreateFriendsInput.model_validate({"friends": [{"email": "bob@example.com"}]})
    )

    assert "writes are disabled" in result
    assert PARTIAL_ADD_HINT not in result


def test_create_friends_input_rejects_duplicate_emails() -> None:
    with pytest.raises(ValidationError, match="duplicated: grace@example.com"):
        CreateFriendsInput.model_validate({"friends": [{"email": "grace@example.com"}, {"email": "GRACE@example.com"}]})


def test_create_friends_input_rejects_empty_list() -> None:
    with pytest.raises(ValidationError):
        CreateFriendsInput.model_validate({"friends": []})


# -- delete_friend ---------------------------------------------------------------


async def test_delete_friend_renders_success_from_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/delete_friend/4821": {"success": True, "errors": {}}})
    _use(monkeypatch, fake)

    result = await splitwise_delete_friend(DeleteFriendInput(friend_id=4821))

    assert fake.calls == [("POST", "/delete_friend/4821", {})]
    assert result.startswith("Friendship with user 4821 deleted.")
    assert "- **success**: true" in result
    assert "- **errors**: none" in result


async def test_delete_friend_without_success_flag_does_not_claim_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(routes={"/delete_friend/4821": {}})
    _use(monkeypatch, fake)

    result = await splitwise_delete_friend(DeleteFriendInput(friend_id=4821))

    assert "deleted." not in result
    assert "- **success**: not reported" in result
    assert "splitwise_get_friend" in result


async def test_delete_friend_envelope_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient(
        routes={
            "/delete_friend/4821": SplitwiseEnvelopeError(
                {"base": ["You have an outstanding balance with this person"]}, path="/delete_friend/4821"
            )
        }
    )
    _use(monkeypatch, fake)

    result = await splitwise_delete_friend(DeleteFriendInput(friend_id=4821))

    assert result.startswith("Error: Splitwise rejected the request to /delete_friend/4821:")
    assert "outstanding balance" in result


def test_delete_friend_input_rejects_bool_and_zero() -> None:
    with pytest.raises(ValidationError):
        DeleteFriendInput.model_validate({"friend_id": True})
    with pytest.raises(ValidationError):
        DeleteFriendInput(friend_id=0)


# -- registration ----------------------------------------------------------------


async def test_friends_tools_registered_with_expected_annotations() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    expected = {
        "splitwise_get_friends": (True, False, True),
        "splitwise_get_friend": (True, False, True),
        "splitwise_create_friend": (False, False, False),
        "splitwise_create_friends": (False, False, False),
        "splitwise_delete_friend": (False, True, True),
    }
    for name, (read_only, destructive, idempotent) in expected.items():
        annotations = tools[name].annotations
        assert annotations is not None, name
        assert annotations.readOnlyHint is read_only, name
        assert annotations.destructiveHint is destructive, name
        assert annotations.idempotentHint is idempotent, name
        assert annotations.openWorldHint is True, name
    for name in ("splitwise_create_friend", "splitwise_create_friends", "splitwise_delete_friend"):
        assert "SPLITWISE_ALLOW_WRITES=1" in (tools[name].description or ""), name


# -- live (read-only) ------------------------------------------------------------


@pytest.mark.live
async def test_get_friends_live_smoke() -> None:
    """Real read with the developer's key: the friends list renders."""
    get_settings.cache_clear()
    result = await splitwise_get_friends(GetFriendsInput())

    assert result.startswith("# Splitwise friends")


@pytest.mark.live
async def test_get_friend_live_smoke() -> None:
    """Real read of the first friend, if the account has any."""
    get_settings.cache_clear()
    payload = json.loads(await splitwise_get_friends(GetFriendsInput(response_format="json")))
    if not payload["friends"]:
        pytest.skip("the live account has no friends to read")
    friend_id = payload["friends"][0]["id"]

    result = await splitwise_get_friend(GetFriendInput(friend_id=friend_id))

    assert f"(id {friend_id})" in result
