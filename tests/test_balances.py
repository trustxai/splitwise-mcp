"""Unit tests for the derived balance tools against a fake client."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from splitwise_mcp.config import get_settings
from splitwise_mcp.tools.balances import (
    FRIEND_SIGN_CONVENTION,
    GROUP_SIGN_CONVENTION,
    MAX_DISPLAY_ROWS,
    GetBalancesInput,
    GetGroupBalancesInput,
    splitwise_get_balances,
    splitwise_get_group_balances,
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


def _install(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> _FakeClient:
    fake = _FakeClient(routes)
    monkeypatch.setattr("splitwise_mcp.tools.balances.get_client", lambda: fake)
    return fake


def _status_error(status: int, body: dict[str, Any], path: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://secure.splitwise.com/api/v3.0{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def _friend(user_id: int, first: str, last: str, balance: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "id": user_id,
        "first_name": first,
        "last_name": last,
        "email": f"{first.lower()}@example.com",
        "registration_status": "confirmed",
        "balance": balance,
        "groups": [{"group_id": 0, "balance": balance}],
        "updated_at": "2026-10-01T12:00:00Z",
    }


FRIENDS = {
    "friends": [
        _friend(
            11,
            "Linus",
            "Torvalds",
            [{"currency_code": "USD", "amount": "-12.5"}, {"currency_code": "PEN", "amount": "0.0"}],
        ),
        _friend(
            12,
            "Grace",
            "Hopper",
            [{"currency_code": "USD", "amount": "30.0"}, {"currency_code": "PEN", "amount": "-5.0"}],
        ),
        _friend(13, "Margaret", "Hamilton", [{"currency_code": "USD", "amount": "0.0"}]),
        _friend(14, "Ken", "Thompson", []),
        _friend(15, "Alan", "Kay", [{"currency_code": "pen", "amount": "7.25"}]),
    ]
}


# -- splitwise_get_balances ---------------------------------------------------------


async def test_get_balances_markdown_totals_and_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_get_balances(GetBalancesInput())

    assert fake.calls == [("GET", "/get_friends", {})]
    assert FRIEND_SIGN_CONVENTION in result
    assert "| Currency | You are owed | You owe | Net |" in result
    assert "| PEN | 7.25 PEN | 5.00 PEN | +2.25 PEN |" in result
    assert "| USD | 30.00 USD | 12.50 USD | +17.50 USD |" in result
    assert "## Friends with a balance (3)" in result
    # Sorted by name; every non-zero currency shown, zero entries hidden.
    assert (
        "| Alan Kay (id 15) | +7.25 PEN |\n"
        "| Grace Hopper (id 12) | +30.00 USD, -5.00 PEN |\n"
        "| Linus Torvalds (id 11) | -12.50 USD |"
    ) in result
    assert "0.00 PEN" not in result.split("## Friends")[1]


async def test_get_balances_hides_zero_balances_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_get_balances(GetBalancesInput())

    assert "Margaret Hamilton" not in result
    assert "Ken Thompson" not in result
    assert "settled up" not in result


async def test_get_balances_include_zero_lists_settled_friends(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_get_balances(GetBalancesInput(include_zero=True))

    assert fake.calls == [("GET", "/get_friends", {})]
    assert "## Friends (5)" in result
    assert "| Ken Thompson (id 14) | settled up |" in result
    assert "| Margaret Hamilton (id 13) | settled up |" in result
    # Totals unchanged by include_zero.
    assert "| USD | 30.00 USD | 12.50 USD | +17.50 USD |" in result


async def test_get_balances_currency_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_get_balances(GetBalancesInput(currency=" usd "))

    # The filter is local: the endpoint takes no params.
    assert fake.calls == [("GET", "/get_friends", {})]
    assert "Currency filter: USD." in result
    assert "| USD | 30.00 USD | 12.50 USD | +17.50 USD |" in result
    assert "PEN" not in result
    assert "| Grace Hopper (id 12) | +30.00 USD |" in result
    assert "Alan Kay" not in result
    assert "## Friends with a balance (2)" in result


async def test_get_balances_currency_filter_lowercase_api_code(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_get_balances(GetBalancesInput(currency="PEN"))

    assert "| PEN | 7.25 PEN | 5.00 PEN | +2.25 PEN |" in result
    assert "| Alan Kay (id 15) | +7.25 PEN |" in result
    assert "| Grace Hopper (id 12) | -5.00 PEN |" in result
    assert "Linus" not in result


@pytest.mark.parametrize("bad", ["US", "dollars", "12A", ""])
def test_get_balances_rejects_bad_currency(bad: str) -> None:
    with pytest.raises(ValidationError, match="3-letter code"):
        GetBalancesInput(currency=bad)


def test_get_balances_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        GetBalancesInput.model_validate({"only_with_balance": True})


async def test_get_balances_all_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        {"/get_friends": {"friends": [_friend(13, "Margaret", "Hamilton", [{"currency_code": "USD", "amount": "0"}])]}},
    )

    result = await splitwise_get_balances(GetBalancesInput(currency="EUR"))

    assert "_You are settled up with every friend in EUR._" in result
    assert "Totals by currency" not in result
    assert "Friends" not in result


async def test_get_balances_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_get_balances(GetBalancesInput(response_format="json"))
    payload = json.loads(result)

    assert fake.calls == [("GET", "/get_friends", {})]
    assert payload["sign_convention"] == FRIEND_SIGN_CONVENTION
    assert payload["currency_filter"] is None
    assert payload["totals"] == {
        "PEN": {"owed_to_you": "7.25", "you_owe": "5.00", "net": "2.25"},
        "USD": {"owed_to_you": "30.00", "you_owe": "12.50", "net": "17.50"},
    }
    assert payload["friend_count"] == 3
    assert [f["id"] for f in payload["friends"]] == [15, 12, 11]
    assert payload["friends"][1]["email"] == "grace@example.com"


async def test_get_balances_caps_rows_but_totals_cover_all(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [
        _friend(100 + i, f"F{i:03d}", "Owes", [{"currency_code": "USD", "amount": "1.00"}])
        for i in range(MAX_DISPLAY_ROWS + 5)
    ]
    _install(monkeypatch, {"/get_friends": {"friends": many}})

    result = await splitwise_get_balances(GetBalancesInput())

    assert f"## Friends with a balance ({MAX_DISPLAY_ROWS + 5})" in result
    assert result.count("| +1.00 USD |") == MAX_DISPLAY_ROWS
    assert f"Showing {MAX_DISPLAY_ROWS} of {MAX_DISPLAY_ROWS + 5} friends (totals cover all of them)" in result
    assert "| USD | 55.00 USD | 0.00 USD | +55.00 USD |" in result


async def test_get_balances_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(
        monkeypatch,
        {"/get_friends": _status_error(401, {"error": "Invalid API request: you are not logged in"}, "/get_friends")},
    )

    result = await splitwise_get_balances(GetBalancesInput())

    assert fake.calls == [("GET", "/get_friends", {})]
    assert result.startswith("Error (401)")
    assert "not logged in" in result


# -- splitwise_get_group_balances ---------------------------------------------------


def _member(user_id: int, first: str, last: str | None, balance: list[dict[str, str]]) -> dict[str, Any]:
    return {"id": user_id, "first_name": first, "last_name": last, "email": None, "balance": balance}


def _group(simplified: list[dict[str, Any]], original: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "group": {
            "id": 77,
            "name": "Lisbon Trip",
            "group_type": "trip",
            "members": [
                _member(1, "Ada", "Lovelace", [{"currency_code": "USD", "amount": "20.00"}]),
                _member(
                    2,
                    "Bob",
                    None,
                    [{"currency_code": "USD", "amount": "-20.00"}, {"currency_code": "EUR", "amount": "0.00"}],
                ),
                _member(3, "Carl", "Sagan", [{"currency_code": "USD", "amount": "0.0"}]),
            ],
            "simplified_debts": simplified,
            "original_debts": original,
        }
    }


SIMPLIFIED = [
    {"from": 2, "to": 1, "amount": "20.0", "currency_code": "USD"},
    {"from": 999, "to": 1, "amount": "4.5", "currency_code": "EUR"},
]
ORIGINAL = [
    {"from": 2, "to": 3, "amount": "10.0", "currency_code": "USD"},
    {"from": 3, "to": 1, "amount": "10.0", "currency_code": "USD"},
]


async def test_group_balances_simplified_debts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_group/77": _group(SIMPLIFIED, ORIGINAL)})

    result = await splitwise_get_group_balances(GetGroupBalancesInput(group_id=77))

    assert fake.calls == [("GET", "/get_group/77", {})]
    assert result.startswith("# Balances in Lisbon Trip (id 77)")
    assert GROUP_SIGN_CONVENTION in result
    assert "## Members with a balance (2)" in result
    assert "| Ada Lovelace (id 1) | +20.00 USD |" in result
    assert "| Bob (id 2) | -20.00 USD |" in result
    assert "0.00 EUR" not in result
    assert "Settled up (1): Carl Sagan (id 3)." in result
    assert "## Debts (source: simplified_debts; From → To = From owes To)" in result
    assert "- Bob (id 2) → Ada Lovelace (id 1) 20.00 USD" in result
    # An id not among the members is rendered as `user N`.
    assert "- user 999 → Ada Lovelace (id 1) 4.50 EUR" in result
    assert "original_debts" not in result
    assert "Carl Sagan (id 3) →" not in result


async def test_group_balances_falls_back_to_original_debts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_group/77": _group([], ORIGINAL)})

    result = await splitwise_get_group_balances(GetGroupBalancesInput(group_id=77))

    assert fake.calls == [("GET", "/get_group/77", {})]
    assert "## Debts (source: original_debts — Splitwise returned no simplified debts;" in result
    assert "- Bob (id 2) → Carl Sagan (id 3) 10.00 USD" in result
    assert "- Carl Sagan (id 3) → Ada Lovelace (id 1) 10.00 USD" in result


async def test_group_balances_no_debts(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _group([], [])
    for member in body["group"]["members"]:
        member["balance"] = [{"currency_code": "USD", "amount": "0.0"}]
    _install(monkeypatch, {"/get_group/77": body})

    result = await splitwise_get_group_balances(GetGroupBalancesInput(group_id=77))

    assert "_Every member is settled up._" in result
    assert "Settled up (3): Ada Lovelace (id 1), Bob (id 2), Carl Sagan (id 3)." in result
    assert "_No debts: neither simplified_debts nor original_debts has an entry._" in result
    assert "## Debts" not in result


async def test_group_balances_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_group/0": _group([], ORIGINAL)})

    result = await splitwise_get_group_balances(GetGroupBalancesInput(group_id=0, response_format="json"))
    payload = json.loads(result)

    assert fake.calls == [("GET", "/get_group/0", {})]
    assert payload["group"] == {"id": 77, "name": "Lisbon Trip"}
    assert payload["sign_convention"] == GROUP_SIGN_CONVENTION
    assert payload["debts_source"] == "original_debts"
    assert payload["debts"] == ORIGINAL
    assert [m["id"] for m in payload["members"]] == [1, 2, 3]


@pytest.mark.parametrize("bad", [-1, True, "abc"])
def test_group_balances_rejects_bad_group_id(bad: Any) -> None:
    with pytest.raises(ValidationError):
        GetGroupBalancesInput(group_id=bad)


async def test_group_balances_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(
        monkeypatch,
        {
            "/get_group/5": _status_error(
                403, {"errors": {"base": ["You are not a member of this group"]}}, "/get_group/5"
            )
        },
    )

    result = await splitwise_get_group_balances(GetGroupBalancesInput(group_id=5))

    assert fake.calls == [("GET", "/get_group/5", {})]
    assert result.startswith("Error (403)")
    assert "You are not a member of this group" in result


# -- review findings: edge amounts, unparsed entries, filter + include_zero, caps ---


async def test_get_balances_zero_forms_hidden_and_plus_sign_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        {
            "/get_friends": {
                "friends": [
                    _friend(
                        21,
                        "Zed",
                        "Zero",
                        [
                            {"currency_code": "USD", "amount": "-0.00"},
                            {"currency_code": "PEN", "amount": "0.000"},
                            {"currency_code": "EUR", "amount": "+5.00"},
                        ],
                    )
                ]
            }
        },
    )

    result = await splitwise_get_balances(GetBalancesInput())

    assert "| Zed Zero (id 21) | +5.00 EUR |" in result
    assert "| EUR | 5.00 EUR | 0.00 EUR | +5.00 EUR |" in result
    assert "USD" not in result
    assert "PEN" not in result
    assert "could not be parsed" not in result


UNPARSED_FRIENDS = {
    "friends": [
        _friend(31, "Ann", "Comma", [{"currency_code": "USD", "amount": "1,000.00"}]),
        _friend(
            32,
            "Ben",
            "Nan",
            [{"currency_code": "PEN", "amount": "NaN"}, {"currency_code": "USD", "amount": "10.00"}],
        ),
        _friend(33, "Cat", "Huge", [{"currency_code": "USD", "amount": "1e30"}]),
        _friend(34, "Dan", "Nocode", [{"amount": "5.00"}]),
    ]
}


async def test_get_balances_flags_unparsed_amounts_and_missing_currency(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": UNPARSED_FRIENDS})

    result = await splitwise_get_balances(GetBalancesInput())

    assert not result.startswith("Error"), result
    # Only the one parseable, currency-bearing entry reaches the totals.
    assert "| USD | 10.00 USD | 0.00 USD | +10.00 USD |" in result
    assert "| N/A |" not in result
    assert "| PEN |" not in result
    assert (
        "_4 balance entries could not be parsed (amount or currency) and are not in the totals; "
        "they are marked `(unparsed)` / `(unknown currency)` in the rows._"
    ) in result
    assert "| Ann Comma (id 31) | 1,000.00 USD (unparsed) |" in result
    assert "| Ben Nan (id 32) | NaN PEN (unparsed), +10.00 USD |" in result
    assert "| Cat Huge (id 33) | 1e30 USD (unparsed) |" in result
    assert "| Dan Nocode (id 34) | +5.00 (unknown currency) |" in result
    assert "settled up" not in result


async def test_get_balances_json_lists_unparsed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": UNPARSED_FRIENDS})

    payload = json.loads(await splitwise_get_balances(GetBalancesInput(response_format="json")))

    assert payload["totals"] == {"USD": {"owed_to_you": "10.00", "you_owe": "0.00", "net": "10.00"}}
    assert payload["unparsed"] == [
        {"friend_id": 31, "currency_code": "USD", "amount": "1,000.00", "reason": "amount could not be parsed"},
        {"friend_id": 32, "currency_code": "PEN", "amount": "NaN", "reason": "amount could not be parsed"},
        {"friend_id": 33, "currency_code": "USD", "amount": "1e30", "reason": "amount could not be parsed"},
        {"friend_id": 34, "currency_code": None, "amount": "5.00", "reason": "missing currency_code"},
    ]
    assert payload["friend_count"] == 4


async def test_get_balances_only_unparsed_is_not_called_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        {"/get_friends": {"friends": [_friend(41, "Eve", "Odd", [{"currency_code": "USD", "amount": "abc"}])]}},
    )

    result = await splitwise_get_balances(GetBalancesInput(include_zero=True))

    assert "settled up" not in result
    assert "Totals by currency" not in result
    assert "_1 balance entry could not be parsed (amount or currency) and is not in the totals;" in result
    assert "| Eve Odd (id 41) | abc USD (unparsed) |" in result


async def test_get_balances_currency_filter_with_include_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_get_balances(GetBalancesInput(currency="USD", include_zero=True))

    assert fake.calls == [("GET", "/get_friends", {})]
    assert "## Friends (5)" in result
    # Alan only owes in PEN: not "settled up", just nothing in USD.
    assert "| Alan Kay (id 15) | no USD balance |" in result
    # Margaret (USD 0.0) and Ken (no entries) have nothing open in any currency.
    assert "| Margaret Hamilton (id 13) | settled up |" in result
    assert "| Ken Thompson (id 14) | settled up |" in result
    assert "| Grace Hopper (id 12) | +30.00 USD |" in result
    assert "| USD | 30.00 USD | 12.50 USD | +17.50 USD |" in result


async def test_get_balances_dedupes_friends_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    twice = _friend(51, "Dup", "Licate", [{"currency_code": "USD", "amount": "40.00"}])
    _install(monkeypatch, {"/get_friends": {"friends": [twice, dict(twice)]}})

    result = await splitwise_get_balances(GetBalancesInput())

    assert "| USD | 40.00 USD | 0.00 USD | +40.00 USD |" in result
    assert "## Friends with a balance (1)" in result
    assert result.count("Dup Licate (id 51)") == 1


async def test_group_balances_caps_members_and_debts(monkeypatch: pytest.MonkeyPatch) -> None:
    n = MAX_DISPLAY_ROWS + 5
    members = [_member(200 + i, f"M{i:03d}", None, [{"currency_code": "USD", "amount": "1.00"}]) for i in range(n)]
    debts = [{"from": 200 + i, "to": 1, "amount": "1.00", "currency_code": "USD"} for i in range(n)]
    body = {"group": {"id": 9, "name": "Big", "members": members, "simplified_debts": debts, "original_debts": []}}
    fake = _install(monkeypatch, {"/get_group/9": body})

    result = await splitwise_get_group_balances(GetGroupBalancesInput(group_id=9))

    assert fake.calls == [("GET", "/get_group/9", {})]
    assert f"## Members with a balance ({n})" in result
    assert result.count("| +1.00 USD |") == MAX_DISPLAY_ROWS
    assert f"Showing {MAX_DISPLAY_ROWS} of {n} members; use response_format='json'." in result
    assert result.count("→ user 1 1.00 USD") == MAX_DISPLAY_ROWS
    assert f"Showing {MAX_DISPLAY_ROWS} of {n} debts; use response_format='json'." in result


async def test_group_balances_flags_unparsed_amounts(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _group([{"from": 2, "to": 1, "amount": "Infinity", "currency_code": "USD"}], [])
    body["group"]["members"][0]["balance"] = [{"currency_code": "USD", "amount": "1e30"}]
    _install(monkeypatch, {"/get_group/77": body})

    result = await splitwise_get_group_balances(GetGroupBalancesInput(group_id=77))

    assert not result.startswith("Error"), result
    assert "| Ada Lovelace (id 1) | 1e30 USD (unparsed) |" in result
    assert "- Bob (id 2) → Ada Lovelace (id 1) Infinity USD (unparsed)" in result


def test_group_id_zero_is_documented_as_unverified() -> None:
    doc = " ".join((splitwise_get_group_balances.__doc__ or "").split())
    assert "whether `/get_group/0` answers is unverified until the live smoke" in doc
    assert "on a 404, use `splitwise_get_balances`" in doc
    field_doc = GetGroupBalancesInput.model_fields["group_id"].description or ""
    assert "unverified on /get_group/0 until the live smoke" in field_doc


# -- live smoke (read-only; skipped without SPLITWISE_API_KEY) ---------------------


@pytest.mark.live
async def test_get_balances_live_smoke() -> None:
    get_settings.cache_clear()
    result = await splitwise_get_balances(GetBalancesInput())

    assert not result.startswith("Error"), result
    assert FRIEND_SIGN_CONVENTION in result
