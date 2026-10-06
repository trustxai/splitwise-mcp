"""Unit tests for the groups tools against a fake client (and the real client's rails via MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from splitwise_mcp.client import SplitwiseClient
from splitwise_mcp.config import Settings, get_settings
from splitwise_mcp.formatters import ResponseFormat
from splitwise_mcp.tools.groups import (
    MAX_DISPLAY_ROWS,
    AddUserToGroupInput,
    CreateGroupInput,
    GetGroupInput,
    GetGroupsInput,
    GroupIdInput,
    MemberInput,
    RemoveUserFromGroupInput,
    splitwise_add_user_to_group,
    splitwise_create_group,
    splitwise_delete_group,
    splitwise_get_group,
    splitwise_get_groups,
    splitwise_remove_user_from_group,
    splitwise_undelete_group,
)

MODULE = "splitwise_mcp.tools.groups"


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


def _install(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    monkeypatch.setattr(f"{MODULE}.get_client", lambda: client)


def _status_error(status: int, body: dict[str, Any], path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://secure.splitwise.com/api/v3.0{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _real_client(handler: Any, *, allow_writes: bool) -> SplitwiseClient:
    settings = Settings(splitwise_api_key="k" * 20, splitwise_allow_writes=allow_writes)
    return SplitwiseClient(settings=settings, transport=httpx.MockTransport(handler))


ADA = {"id": 1, "first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com"}
BOB = {"id": 2, "first_name": "Bob", "last_name": "Byte", "email": "bob@example.com"}
CY = {"id": 3, "first_name": "Cy", "last_name": None, "email": "cy@example.com"}

TRIP = {
    "id": 77,
    "name": "Lima trip",
    "group_type": "trip",
    "updated_at": "2026-10-01T12:30:00Z",
    "simplify_by_default": True,
    "invite_link": "https://www.splitwise.com/join/abc123",
    "members": [
        {**ADA, "balance": [{"currency_code": "USD", "amount": "12.5"}, {"currency_code": "PEN", "amount": "0.0"}]},
        {**BOB, "balance": [{"currency_code": "USD", "amount": "-12.50"}]},
        {**CY, "balance": []},
    ],
    "original_debts": [{"from": 2, "to": 1, "amount": "12.5", "currency_code": "USD"}],
    "simplified_debts": [
        {"from": 2, "to": 1, "amount": "12.5", "currency_code": "USD"},
        {"from": 99, "to": 1, "amount": "3.00", "currency_code": "PEN"},
    ],
}
NON_GROUP = {
    "id": 0,
    "name": "Non-group expenses",
    "group_type": None,
    "members": [{**ADA, "balance": [{"currency_code": "USD", "amount": "-4.00"}]}],
    "simplified_debts": [],
    "original_debts": [],
}


# -- splitwise_get_groups ---------------------------------------------------------


async def test_get_groups_with_current_user(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/get_groups": {"groups": [NON_GROUP, TRIP]}})
    _install(monkeypatch, fake)

    result = await splitwise_get_groups(GetGroupsInput(current_user_id=1))

    assert fake.calls == [("GET", "/get_groups", {})]
    assert "# Splitwise groups (2)" in result
    assert "| Group | Type | Members | Your balance |" in result
    assert "| Lima trip (id 77) | trip | 3 | +12.50 USD |" in result
    assert "| Non-group expenses (id 0) — non-group expenses | N/A | 1 | -4.00 USD |" in result
    assert "Group id 0 is Splitwise's pseudo-group for **non-group expenses**" in result
    assert "positive balance means that member is owed money" in result


async def test_get_groups_without_current_user_lists_member_balances(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/get_groups": {"groups": [TRIP]}})
    _install(monkeypatch, fake)

    result = await splitwise_get_groups(GetGroupsInput())

    assert "Members' balances" in result
    assert "Ada Lovelace (id 1) +12.50 USD; Bob Byte (id 2) -12.50 USD" in result
    assert "Cy (id 3)" not in result  # zero balances are omitted
    assert "PEN" not in result
    assert "non-group expenses" not in result  # no id-0 row → no note


async def test_get_groups_current_user_not_member(monkeypatch: pytest.MonkeyPatch) -> None:
    settled = {**TRIP, "members": [{**ADA, "balance": [{"currency_code": "USD", "amount": "0.00"}]}]}
    _install(monkeypatch, _FakeClient({"/get_groups": {"groups": [settled, TRIP]}}))

    mine = await splitwise_get_groups(GetGroupsInput(current_user_id=1))
    stranger = await splitwise_get_groups(GetGroupsInput(current_user_id=4242))
    everyone = await splitwise_get_groups(GetGroupsInput())

    assert "| settled up |" in mine
    assert "you are not listed" in stranger
    assert "everyone settled up" in everyone


async def test_get_groups_json_returns_raw_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeClient({"/get_groups": {"groups": [TRIP]}}))

    result = await splitwise_get_groups(GetGroupsInput(response_format=ResponseFormat.JSON))

    payload = json.loads(result)
    assert payload["count"] == 1
    assert payload["shown"] == 1
    assert payload["groups"][0] == TRIP


async def test_get_groups_caps_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [{"id": i, "name": f"G{i}", "group_type": "other", "members": []} for i in range(1, 61)]
    _install(monkeypatch, _FakeClient({"/get_groups": {"groups": many}}))

    result = await splitwise_get_groups(GetGroupsInput())
    as_json = json.loads(await splitwise_get_groups(GetGroupsInput(response_format=ResponseFormat.JSON)))

    assert f"_Showing the first {MAX_DISPLAY_ROWS} of 60 groups._" in result
    assert "G50 (id 50)" in result
    assert "G51 (id 51)" not in result
    assert as_json["count"] == 60
    assert as_json["shown"] == MAX_DISPLAY_ROWS


async def test_get_groups_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeClient({"/get_groups": {"groups": []}}))

    result = await splitwise_get_groups(GetGroupsInput())

    assert "# Splitwise groups (0)" in result
    assert "_You are not in any group._" in result


def test_get_groups_rejects_bad_input() -> None:
    with pytest.raises(ValidationError):
        GetGroupsInput(current_user_id=0)
    with pytest.raises(ValidationError):
        GetGroupsInput.model_validate({"limit": 5})


# -- splitwise_get_group ------------------------------------------------------------


async def test_get_group_renders_members_and_simplified_debts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/get_group/77": {"group": TRIP}})
    _install(monkeypatch, fake)

    result = await splitwise_get_group(GetGroupInput(group_id=77))

    assert fake.calls == [("GET", "/get_group/77", {})]
    assert result.startswith("# Lima trip (id 77)")
    assert "- **type**: trip" in result
    assert "- **updated**: 2026-10-01 12:30 UTC" in result
    assert "- **invite link**: https://www.splitwise.com/join/abc123" in result
    assert "| Ada Lovelace (id 1) | +12.50 USD |" in result
    assert "| Bob Byte (id 2) | -12.50 USD |" in result
    assert "| Cy (id 3) | settled up |" in result
    assert "## Simplified debts (A → B = A owes B)" in result
    assert "- Bob Byte (id 2) → Ada Lovelace (id 1) 12.50 USD" in result
    assert "- user 99 → Ada Lovelace (id 1) 3.00 PEN" in result  # member missing from `members`


async def test_get_group_falls_back_to_original_debts(monkeypatch: pytest.MonkeyPatch) -> None:
    group = {**TRIP, "simplified_debts": []}
    _install(monkeypatch, _FakeClient({"/get_group/77": {"group": group}}))

    result = await splitwise_get_group(GetGroupInput(group_id=77))

    assert "## Original debts (no simplified debts returned" in result
    assert "- Bob Byte (id 2) → Ada Lovelace (id 1) 12.50 USD" in result


async def test_get_group_zero_is_non_group_and_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/get_group/0": {"group": NON_GROUP}})
    _install(monkeypatch, fake)

    result = await splitwise_get_group(GetGroupInput(group_id=0))

    assert fake.calls == [("GET", "/get_group/0", {})]
    assert "pseudo-group for **non-group expenses**" in result
    assert "_No outstanding debts — everyone is settled up._" in result


async def test_get_group_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeClient({"/get_group/77": {"group": TRIP}}))

    result = await splitwise_get_group(GetGroupInput(group_id=77, response_format=ResponseFormat.JSON))

    assert json.loads(result) == TRIP


async def test_get_group_forbidden_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    error = _status_error(403, {"errors": {"base": ["You are not a member of this group"]}}, "/get_group/5")
    _install(monkeypatch, _FakeClient({"/get_group/5": error}))

    result = await splitwise_get_group(GetGroupInput(group_id=5))

    assert result.startswith("Error (403): Forbidden – You are not a member of this group")


def test_get_group_rejects_negative_id() -> None:
    with pytest.raises(ValidationError):
        GetGroupInput(group_id=-1)


# -- splitwise_create_group -----------------------------------------------------------


async def test_create_group_flattens_members(monkeypatch: pytest.MonkeyPatch) -> None:
    created = {
        "id": 501,
        "name": "Flat",
        "group_type": "home",
        "simplify_by_default": True,
        "members": [ADA, BOB, {"id": 9, "first_name": "Ana", "last_name": "Diaz"}],
        "invite_link": "https://www.splitwise.com/join/xyz",
    }
    fake = _FakeClient({"/create_group": {"group": created}})
    _install(monkeypatch, fake)

    result = await splitwise_create_group(
        CreateGroupInput(
            name="  Flat ",
            group_type="apartment",  # type: ignore[arg-type]  # legacy alias, normalised to home
            simplify_by_default=True,
            members=[
                MemberInput(user_id=2),
                MemberInput(email=" ana@example.com ", first_name="Ana", last_name="Diaz"),
                MemberInput(email="lu@example.com", first_name="Lu"),
            ],
        )
    )

    assert fake.calls == [
        (
            "POST",
            "/create_group",
            {
                "data": {
                    "name": "Flat",
                    "group_type": "home",
                    "simplify_by_default": True,
                    "users__0__user_id": 2,
                    "users__1__email": "ana@example.com",
                    "users__1__first_name": "Ana",
                    "users__1__last_name": "Diaz",
                    "users__2__email": "lu@example.com",
                    "users__2__first_name": "Lu",
                }
            },
        )
    ]
    assert "Created group **Flat (id 501)**." in result
    assert "- **type**: home" in result
    assert "- **members (3)**: Ada Lovelace (id 1), Bob Byte (id 2), Ana Diaz (id 9)" in result
    assert "- **invite link**: https://www.splitwise.com/join/xyz" in result
    assert "Undo with `splitwise_delete_group` (group_id=501)." in result


async def test_create_group_minimal_drops_none(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/create_group": {"group": {"id": 8, "name": "Solo", "group_type": "other", "members": [ADA]}}})
    _install(monkeypatch, fake)

    result = await splitwise_create_group(CreateGroupInput(name="Solo"))

    assert fake.calls == [("POST", "/create_group", {"data": {"name": "Solo"}})]
    assert "Created group **Solo (id 8)**." in result
    assert "invite link" not in result


@pytest.mark.parametrize(
    "member",
    [
        {"user_id": 2, "email": "x@example.com", "first_name": "X"},  # both identities
        {},  # neither
        {"email": "x@example.com"},  # email without first_name
        {"first_name": "X"},  # name without email
        {"email": "not-an-email", "first_name": "X"},  # malformed email
        {"user_id": 2, "nickname": "x"},  # extra field
    ],
)
def test_member_input_requires_exactly_one_identity(member: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        MemberInput.model_validate(member)


def test_create_group_rejects_bad_group_type_and_blank_name() -> None:
    with pytest.raises(ValidationError):
        CreateGroupInput.model_validate({"name": "X", "group_type": "castle"})
    with pytest.raises(ValidationError):
        CreateGroupInput.model_validate({"name": "   "})
    assert CreateGroupInput.model_validate({"name": "X", "group_type": "House"}).group_type == "home"


async def test_create_group_refused_when_writes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={})

    _install(monkeypatch, _real_client(handler, allow_writes=False))

    result = await splitwise_create_group(CreateGroupInput(name="Nope"))

    assert result.startswith("Error: POST /create_group would change your Splitwise account")
    assert "writes are disabled" in result
    assert sent == []


# -- splitwise_delete_group / splitwise_undelete_group -------------------------------


async def test_delete_group_echoes_success(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/delete_group/77": {"success": True}})
    _install(monkeypatch, fake)

    result = await splitwise_delete_group(GroupIdInput(group_id=77))

    assert fake.calls == [("POST", "/delete_group/77", {})]
    assert result.startswith("Deleted group id 77 (success: true).")
    assert "restore both with `splitwise_undelete_group` (group_id=77)" in result


async def test_undelete_group_echoes_success_and_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/undelete_group/77": {"success": True, "errors": []}})
    _install(monkeypatch, fake)

    result = await splitwise_undelete_group(GroupIdInput(group_id=77))

    assert fake.calls == [("POST", "/undelete_group/77", {})]
    assert result.startswith("Restored group id 77 (success: true; errors: none).")


async def test_undelete_group_without_success_field_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeClient({"/undelete_group/77": {}}))

    result = await splitwise_undelete_group(GroupIdInput(group_id=77))

    assert "(success: not reported)" in result


async def test_undelete_group_envelope_failure_through_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/undelete_group/77")
        return httpx.Response(200, json={"success": False, "errors": {"base": ["Group not found"]}})

    _install(monkeypatch, _real_client(handler, allow_writes=True))

    result = await splitwise_undelete_group(GroupIdInput(group_id=77))

    assert result == "Error: Splitwise rejected the request to /undelete_group/77: Group not found"


def test_group_id_input_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        GroupIdInput(group_id=0)


# -- splitwise_add_user_to_group ------------------------------------------------------


async def test_add_user_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/add_user_to_group": {"success": True, "user": BOB, "errors": {}}})
    _install(monkeypatch, fake)

    result = await splitwise_add_user_to_group(AddUserToGroupInput(group_id=77, user_id=2))

    assert fake.calls == [("POST", "/add_user_to_group", {"data": {"group_id": 77, "user_id": 2}})]
    assert result.startswith("Added Bob Byte (id 2) to group id 77 (success: true; errors: none).")
    assert "Undo with `splitwise_remove_user_from_group` (group_id=77, user_id=2)." in result


async def test_add_user_by_invite_trio(monkeypatch: pytest.MonkeyPatch) -> None:
    invited = {"id": 9, "first_name": "Ana", "last_name": "Diaz", "email": "ana@example.com"}
    fake = _FakeClient({"/add_user_to_group": {"success": True, "user": invited, "errors": {}}})
    _install(monkeypatch, fake)

    result = await splitwise_add_user_to_group(
        AddUserToGroupInput(group_id=77, first_name="Ana", last_name="Diaz", email="ana@example.com")
    )

    assert fake.calls == [
        (
            "POST",
            "/add_user_to_group",
            {"data": {"group_id": 77, "first_name": "Ana", "last_name": "Diaz", "email": "ana@example.com"}},
        )
    ]
    assert "Added Ana Diaz (id 9) to group id 77" in result


async def test_add_user_without_user_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _FakeClient({"/add_user_to_group": {"success": True}}))

    result = await splitwise_add_user_to_group(AddUserToGroupInput(group_id=77, user_id=2))

    assert "Added the user (no user object returned) to group id 77 (success: true)." in result
    assert result.endswith("Undo with `splitwise_remove_user_from_group`.")


@pytest.mark.parametrize(
    "payload",
    [
        {"group_id": 77},  # neither shape
        {"group_id": 77, "user_id": 2, "email": "x@example.com"},  # both shapes
        {"group_id": 77, "first_name": "Ana", "email": "ana@example.com"},  # trio incomplete
        {"group_id": 77, "first_name": "Ana", "last_name": "Diaz", "email": "nope"},  # bad email
        {"group_id": 0, "user_id": 2},  # not a real group
    ],
)
def test_add_user_one_of_rejections(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AddUserToGroupInput.model_validate(payload)


def test_add_user_names_missing_fields() -> None:
    with pytest.raises(ValidationError, match="missing: last_name, email"):
        AddUserToGroupInput.model_validate({"group_id": 77, "first_name": "Ana"})


# -- splitwise_remove_user_from_group -------------------------------------------------


async def test_remove_user(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeClient({"/remove_user_from_group": {"success": True, "errors": {}}})
    _install(monkeypatch, fake)

    result = await splitwise_remove_user_from_group(RemoveUserFromGroupInput(group_id=77, user_id=2))

    assert fake.calls == [("POST", "/remove_user_from_group", {"data": {"group_id": 77, "user_id": 2}})]
    assert result.startswith("Removed user id 2 from group id 77 (success: true; errors: none).")
    assert "Undo with `splitwise_add_user_to_group` (group_id=77, user_id=2)." in result


async def test_remove_user_with_balance_is_rejected_by_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"success": False, "errors": {"base": ["Bob has a non-zero balance in this group"]}},
        )

    _install(monkeypatch, _real_client(handler, allow_writes=True))

    result = await splitwise_remove_user_from_group(RemoveUserFromGroupInput(group_id=77, user_id=2))

    assert bodies == [{"group_id": 77, "user_id": 2}]
    assert result == (
        "Error: Splitwise rejected the request to /remove_user_from_group: Bob has a non-zero balance in this group"
    )


def test_remove_user_requires_both_ids() -> None:
    with pytest.raises(ValidationError):
        RemoveUserFromGroupInput.model_validate({"group_id": 77})
    with pytest.raises(ValidationError):
        RemoveUserFromGroupInput(group_id=77, user_id=0)


# -- live (read-only) -------------------------------------------------------------------


@pytest.mark.live
async def test_get_groups_live_smoke() -> None:
    """Real read with the developer's key: the group list renders (group 0 is always present)."""
    get_settings.cache_clear()
    result = await splitwise_get_groups(GetGroupsInput())

    assert result.startswith("# Splitwise groups (")
