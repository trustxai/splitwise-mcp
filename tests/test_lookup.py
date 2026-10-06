"""Unit tests for the lookup tools (categories, currencies, resolvers) against a fake client."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from splitwise_mcp.server import mcp
from splitwise_mcp.tools import lookup
from splitwise_mcp.tools.lookup import (
    CACHE_TTL_SECONDS,
    GetCategoriesInput,
    GetCurrenciesInput,
    ResolveCategoryInput,
    ResolveFriendInput,
    ResolveGroupInput,
    splitwise_get_categories,
    splitwise_get_currencies,
    splitwise_resolve_category,
    splitwise_resolve_friend,
    splitwise_resolve_group,
)


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    """Routes by path; records every call (method, path, kwargs). A route may be a list = one payload per call."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self._routes = routes or {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.last_retry_after: str | None = None

    async def request(self, method: str, path: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((method, path, kwargs))
        payload = self._routes.get(path)
        if isinstance(payload, list):
            payload = payload.pop(0)
        if isinstance(payload, Exception):
            raise payload
        return _FakeResponse(payload if payload is not None else {})


def _status_error(status: int, body: dict[str, Any], path: str = "/get_friends") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://secure.splitwise.com/api/v3.0{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.fixture(autouse=True)
def _reset_lookup_cache() -> Iterator[None]:
    """Every test starts (and leaves) with an empty category/currency cache."""
    lookup.clear_cache()
    yield
    lookup.clear_cache()


def _install(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any]) -> _FakeClient:
    fake = _FakeClient(routes)
    monkeypatch.setattr("splitwise_mcp.tools.lookup.get_client", lambda: fake)
    return fake


CATEGORIES = {
    "categories": [
        {
            "id": 1,
            "name": "Utilities",
            "icon": "https://example.com/utilities.png",
            "subcategories": [
                {"id": 48, "name": "Cleaning"},
                {"id": 5, "name": "Electricity"},
                {"id": 8, "name": "Other"},
            ],
        },
        {
            "id": 25,
            "name": "Food and drink",
            "subcategories": [
                {"id": 13, "name": "Dining out"},
                {"id": 12, "name": "Groceries"},
                {"id": 26, "name": "Other"},
            ],
        },
        {
            "id": 31,
            "name": "Transportation",
            "subcategories": [{"id": 32, "name": "Taxi"}, {"id": 35, "name": "Other"}],
        },
        {"id": 19, "name": "Life", "subcategories": [{"id": 43, "name": "Taxes"}, {"id": 41, "name": "Other"}]},
    ]
}

CURRENCIES = {
    "currencies": [
        {"currency_code": "USD", "unit": "$"},
        {"currency_code": "PEN", "unit": "S/"},
        {"currency_code": "EUR", "unit": "€"},
        {"currency_code": "BTC", "unit": "฿"},
    ]
}

FRIENDS = {
    "friends": [
        {"id": 11, "first_name": "Jon", "last_name": "Harris", "email": "jon.harris@example.com", "balance": []},
        {"id": 12, "first_name": "Jonathan", "last_name": "Doe", "email": "jdoe@example.com", "balance": []},
        {"id": 13, "first_name": "María", "last_name": "García", "email": "maria@example.com", "balance": []},
        {"id": 14, "first_name": "Marian", "last_name": "López", "email": "mlopez@example.com", "balance": []},
        {"id": 15, "first_name": "Ana", "last_name": "Torres", "email": "ana.t@example.com", "balance": []},
        {"id": 16, "first_name": "Ana", "last_name": "Ruiz", "email": "aruiz@example.com", "balance": []},
    ]
}

GROUPS = {
    "groups": [
        {"id": 0, "name": "Non-group expenses", "group_type": None, "members": []},
        {"id": 101, "name": "Peru trip 2026", "group_type": "trip", "members": [{"id": 1}, {"id": 2}]},
        {"id": 102, "name": "Peru trip", "group_type": "trip", "members": [{"id": 1}, {"id": 2}, {"id": 3}]},
        {"id": 103, "name": "Home | flat", "group_type": "home", "members": [{"id": 1}, {"id": 4}]},
    ]
}


def _table_rows(result: str, header: str) -> list[str]:
    """The data rows of the markdown table that starts with `header`."""
    lines = result.splitlines()
    start = lines.index(header) + 2
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append(line)
    return rows


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


async def test_get_categories_renders_parents_with_subcategories(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_categories": CATEGORIES})

    result = await splitwise_get_categories(GetCategoriesInput())

    assert fake.calls == [("GET", "/get_categories", {})]
    assert "Use a **subcategory** id as `category_id`" in result
    assert "| Food and drink (id 25) | Dining out (id 13), Groceries (id 12), Other (id 26) |" in result.splitlines()
    assert "_4 parent(s), 10 subcategories._" in result
    assert "Source: Splitwise API — cached in-process for 24 h." in result


async def test_get_categories_second_call_uses_cache_and_refresh_bypasses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_categories": CATEGORIES})

    await splitwise_get_categories(GetCategoriesInput())
    cached = await splitwise_get_categories(GetCategoriesInput())
    assert fake.calls == [("GET", "/get_categories", {})]
    assert "Source: in-process cache" in cached

    refreshed = await splitwise_get_categories(GetCategoriesInput(refresh=True))
    assert fake.calls == [("GET", "/get_categories", {}), ("GET", "/get_categories", {})]
    assert "Source: Splitwise API" in refreshed


async def test_categories_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_categories": CATEGORIES})
    clock = {"now": 1000.0}
    monkeypatch.setattr("splitwise_mcp.tools.lookup._now", lambda: clock["now"])

    await splitwise_get_categories(GetCategoriesInput())
    clock["now"] += CACHE_TTL_SECONDS - 1
    await splitwise_get_categories(GetCategoriesInput())
    assert len(fake.calls) == 1

    clock["now"] += 2
    await splitwise_get_categories(GetCategoriesInput())
    assert len(fake.calls) == 2


async def test_categories_failure_and_empty_answer_are_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(
        monkeypatch,
        {
            "/get_categories": [
                _status_error(503, {"errors": {"base": ["Down for maintenance"]}}, "/get_categories"),
                {"categories": []},
                CATEGORIES,
            ]
        },
    )

    failed = await splitwise_get_categories(GetCategoriesInput())
    assert failed.startswith("Error (503)")
    assert "Down for maintenance" in failed

    empty = await splitwise_get_categories(GetCategoriesInput())
    assert empty == "Splitwise returned no categories (an empty answer is not cached — try again later)."
    ok = await splitwise_get_categories(GetCategoriesInput())

    assert "Food and drink (id 25)" in ok
    assert fake.calls == [("GET", "/get_categories", {})] * 3


async def test_get_categories_parent_filter_by_name_and_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_categories": CATEGORIES})

    by_name = await splitwise_get_categories(GetCategoriesInput(parent="FOOD"))
    by_id = await splitwise_get_categories(GetCategoriesInput(parent="31"))
    missing = await splitwise_get_categories(GetCategoriesInput(parent="pets"))

    assert _table_rows(by_name, "| Parent (not usable for expenses) | Subcategories (use these ids) |") == [
        "| Food and drink (id 25) | Dining out (id 13), Groceries (id 12), Other (id 26) |"
    ]
    assert "parent matching 'FOOD'" in by_name
    assert _table_rows(by_id, "| Parent (not usable for expenses) | Subcategories (use these ids) |") == [
        "| Transportation (id 31) | Taxi (id 32), Other (id 35) |"
    ]
    assert missing.startswith("No parent category matches 'pets'.")
    assert "Utilities (id 1), Food and drink (id 25), Transportation (id 31), Life (id 19)" in missing


async def test_get_categories_json_returns_raw_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_categories": CATEGORIES})

    result = await splitwise_get_categories(GetCategoriesInput(parent="utilities", response_format="json"))

    assert json.loads(result) == {"categories": [CATEGORIES["categories"][0]]}


def test_get_categories_input_rejects_blank_parent_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        GetCategoriesInput(parent="   ")
    with pytest.raises(ValidationError):
        GetCategoriesInput(category="food")  # type: ignore[call-arg]


async def test_categories_cache_expires_exactly_at_the_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_categories": CATEGORIES})
    clock = {"now": 1000.0}
    monkeypatch.setattr("splitwise_mcp.tools.lookup._now", lambda: clock["now"])

    await splitwise_get_categories(GetCategoriesInput())
    clock["now"] += CACHE_TTL_SECONDS
    result = await splitwise_get_categories(GetCategoriesInput())

    assert "Source: Splitwise API" in result
    assert fake.calls == [("GET", "/get_categories", {})] * 2


async def test_failed_refresh_keeps_the_warm_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(
        monkeypatch,
        {
            "/get_categories": [
                CATEGORIES,
                _status_error(500, {"errors": {"base": ["Internal error"]}}, "/get_categories"),
            ]
        },
    )

    await splitwise_get_categories(GetCategoriesInput())
    failed = await splitwise_get_categories(GetCategoriesInput(refresh=True))
    served = await splitwise_get_categories(GetCategoriesInput())

    assert failed.startswith("Error (500)")
    assert "Internal error" in failed
    assert "Source: in-process cache" in served
    assert "Food and drink (id 25)" in served
    assert fake.calls == [("GET", "/get_categories", {})] * 2


async def test_get_categories_caps_markdown_at_50_parents(monkeypatch: pytest.MonkeyPatch) -> None:
    many = {
        "categories": [
            {"id": 1000 + i, "name": f"Parent {i:02d}", "subcategories": [{"id": 2000 + i, "name": f"Sub {i:02d}"}]}
            for i in range(60)
        ]
    }
    _install(monkeypatch, {"/get_categories": many})

    result = await splitwise_get_categories(GetCategoriesInput())

    rows = _table_rows(result, "| Parent (not usable for expenses) | Subcategories (use these ids) |")
    assert len(rows) == lookup.MAX_DISPLAY_ROWS
    assert rows[-1] == "| Parent 49 (id 1049) | Sub 49 (id 2049) |"
    assert "_50 parent(s), 50 subcategories — showing 50 of 60 parents; narrow with `parent`._" in result


# ---------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------


async def test_get_currencies_lists_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_currencies": CURRENCIES})

    first = await splitwise_get_currencies(GetCurrenciesInput())
    second = await splitwise_get_currencies(GetCurrenciesInput(query="pen"))
    refreshed = await splitwise_get_currencies(GetCurrenciesInput(query="€", refresh=True))

    assert _table_rows(first, "| Code | Unit |") == ["| USD | $ |", "| PEN | S/ |", "| EUR | € |", "| BTC | ฿ |"]
    assert "_4 of 4 currencies._" in first
    assert _table_rows(second, "| Code | Unit |") == ["| PEN | S/ |"]
    assert "Source: in-process cache" in second
    assert _table_rows(refreshed, "| Code | Unit |") == ["| EUR | € |"]
    assert fake.calls == [("GET", "/get_currencies", {}), ("GET", "/get_currencies", {})]


async def test_get_currencies_no_match_and_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_currencies": CURRENCIES})

    none = await splitwise_get_currencies(GetCurrenciesInput(query="xyz"))
    as_json = await splitwise_get_currencies(GetCurrenciesInput(query="b", response_format="json"))

    assert none.startswith("No currency code or unit contains 'xyz' (4 currencies checked).")
    assert json.loads(as_json) == {"currencies": [{"currency_code": "BTC", "unit": "฿"}]}


async def test_get_currencies_caps_markdown_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    many = {"currencies": [{"currency_code": f"C{i:02d}", "unit": "¤"} for i in range(60)]}
    _install(monkeypatch, {"/get_currencies": many})

    result = await splitwise_get_currencies(GetCurrenciesInput())

    assert len(_table_rows(result, "| Code | Unit |")) == lookup.MAX_DISPLAY_ROWS
    assert "60 of 60 currencies — showing the first 50; narrow with `query`" in result


def test_get_currencies_input_rejects_blank_query() -> None:
    with pytest.raises(ValidationError):
        GetCurrenciesInput(query="  ")


# ---------------------------------------------------------------------------
# Friends
# ---------------------------------------------------------------------------

# The review's probe cases: SequenceMatcher alone picked the wrong person for most of these.
PROBE_FRIENDS = {
    "friends": [
        {"id": 21, "first_name": "Alejandro", "last_name": "Latorre", "email": "alej669@gmail.com"},
        {"id": 22, "first_name": "Alex", "last_name": "Kim", "email": "alex.k@example.com"},
        {"id": 23, "first_name": "Daniel", "last_name": "Ortiz", "email": "dortiz@example.com"},
        {"id": 24, "first_name": "Gabriel", "last_name": "Ruiz", "email": "gruiz@example.com"},
        {"id": 25, "first_name": "José", "last_name": "Pérez", "email": "jperez@example.com"},
        {"id": 26, "first_name": "Alexandra", "last_name": "Lopez", "email": "alexandra@example.com"},
        {"id": 27, "first_name": "Martín", "last_name": "Gómez", "email": "mgomez@example.com"},
        {"id": 28, "first_name": "Ana", "last_name": "Martin", "email": "ana.martin@example.com"},
        {"id": 29, "first_name": "John", "last_name": "Smith", "email": "jsmith@example.com"},
        {"id": 30, "first_name": "Jonathan", "last_name": "Reyes", "email": "jreyes@example.com"},
    ]
}

FRIEND_TABLE = "| # | Friend | Email | Score | Matched on |"


async def test_resolve_friend_exact_with_a_prefix_rival_is_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_resolve_friend(ResolveFriendInput(query="jon", limit=2))

    assert fake.calls == [("GET", "/get_friends", {})]
    # "Jon" is an exact first name, but "Jonathan" also starts with it: the user may mean either.
    assert '**Not resolved**: 2 candidates could be meant: "jon" is also how another name starts' in result
    assert _table_rows(result, FRIEND_TABLE) == [
        "| 1 | Jon Harris (id 11) | jon.harris@example.com | 1.000 | first name |",
        "| 2 | Jonathan Doe (id 12) | jdoe@example.com | 0.571 | email local-part |",
    ]
    assert "_2 of 6 friend(s) shown, best first._" in result


async def test_resolve_friend_exact_full_name_resolves_with_accent_folding(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    full = await splitwise_resolve_friend(ResolveFriendInput(query="Jon Harris"))
    accents = await splitwise_resolve_friend(ResolveFriendInput(query="  MARIA garcia "))

    assert "**Resolved**: Jon Harris (id 11) — exact match on full name." in full
    assert "**Resolved**: María García (id 13) — exact match on full name." in accents


async def test_resolve_friend_fuzzy_match_is_only_probable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    typo = await splitwise_resolve_friend(ResolveFriendInput(query="Jonh"))
    prefix = await splitwise_resolve_friend(ResolveFriendInput(query="mari", limit=2))

    assert (
        "**Not resolved.** Probable match — confirm with the user before using this id: Jon Harris (id 11) "
        "(score 0.857 on first name; a friend resolves only on an exact match)."
    ) in typo
    # María scores 0.889, but "Marian" starts with "mari": ambiguous, not probable.
    assert '**Not resolved**: 2 candidates could be meant: "mari" is also how another name starts' in prefix
    assert _table_rows(prefix, FRIEND_TABLE) == [
        "| 1 | María García (id 13) | maria@example.com | 0.889 | first name |",
        "| 2 | Marian López (id 14) | mlopez@example.com | 0.800 | first name |",
    ]


@pytest.mark.parametrize(
    ("query", "expected_id", "score"),
    [
        ("Daniela", 23, 0.9231),
        ("Gabriela", 24, 0.9333),
        ("Alejandra Latorre", 21, 0.9412),
    ],
)
async def test_resolve_friend_near_name_of_a_different_person_is_never_resolved(
    monkeypatch: pytest.MonkeyPatch, query: str, expected_id: int, score: float
) -> None:
    _install(monkeypatch, {"/get_friends": PROBE_FRIENDS})

    result = json.loads(await splitwise_resolve_friend(ResolveFriendInput(query=query, response_format="json")))

    assert result["resolution"] == "probable"
    assert result["resolved"] is None
    assert result["probable"]["id"] == expected_id
    assert result["probable"]["score"] == score
    assert result["ambiguous_count"] == 0


@pytest.mark.parametrize(
    ("query", "count"),
    [
        ("ale", 3),  # Alex 0.857 — but Alejandro and Alexandra start with "ale"
        ("jon", 2),  # John 0.857 — but Jonathan starts with "jon"
    ],
)
async def test_resolve_friend_prefix_rivals_make_a_strong_match_ambiguous(
    monkeypatch: pytest.MonkeyPatch, query: str, count: int
) -> None:
    _install(monkeypatch, {"/get_friends": PROBE_FRIENDS})

    result = json.loads(await splitwise_resolve_friend(ResolveFriendInput(query=query, response_format="json")))

    assert result["resolution"] == "ambiguous"
    assert result["ambiguous_count"] == count
    assert result["resolved"] is None
    assert result["probable"] is None


async def test_resolve_friend_short_query_gets_no_fuzzy_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": PROBE_FRIENDS})

    result = await splitwise_resolve_friend(ResolveFriendInput(query="jhn", limit=1))

    assert '**Not resolved**: "jhn" is under 4 characters, too short to accept a fuzzy match' in result
    assert _table_rows(result, FRIEND_TABLE) == ["| 1 | John Smith (id 29) | jsmith@example.com | 0.857 | first name |"]


async def test_resolve_friend_last_name_and_full_email(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": PROBE_FRIENDS})

    latorre = await splitwise_resolve_friend(ResolveFriendInput(query="Latorre"))
    lopez = await splitwise_resolve_friend(ResolveFriendInput(query="Lopez"))
    martin = await splitwise_resolve_friend(ResolveFriendInput(query="Martin", limit=2))
    email = await splitwise_resolve_friend(ResolveFriendInput(query="alej669@gmail.com"))

    assert "**Resolved**: Alejandro Latorre (id 21) — exact match on last name." in latorre
    assert "**Resolved**: Alexandra Lopez (id 26) — exact match on last name." in lopez
    assert _table_rows(lopez, FRIEND_TABLE)[0] == (
        "| 1 | Alexandra Lopez (id 26) | alexandra@example.com | 1.000 | last name |"
    )
    # Martín's first name and Ana's last name are both exactly "martin" once folded.
    assert "**Not resolved**: 2 candidates match exactly — ask the user which one" in martin
    assert _table_rows(martin, FRIEND_TABLE) == [
        "| 1 | Ana Martin (id 28) | ana.martin@example.com | 1.000 | last name |",
        "| 2 | Martín Gómez (id 27) | mgomez@example.com | 1.000 | first name |",
    ]
    assert "**Resolved**: Alejandro Latorre (id 21) — exact match on email." in email


async def test_resolve_friend_ambiguous_and_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    ambiguous = await splitwise_resolve_friend(ResolveFriendInput(query="ana", limit=2))
    nothing = await splitwise_resolve_friend(ResolveFriendInput(query="zzz"))

    assert "**Not resolved**: 2 candidates match exactly — ask the user which one" in ambiguous
    assert _table_rows(ambiguous, FRIEND_TABLE) == [
        "| 1 | Ana Ruiz (id 16) | aruiz@example.com | 1.000 | first name |",
        "| 2 | Ana Torres (id 15) | ana.t@example.com | 1.000 | first name |",
    ]
    assert "**Not resolved**: no candidate scores ≥ 0.85" in nothing


async def test_resolve_friend_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    result = json.loads(
        await splitwise_resolve_friend(ResolveFriendInput(query="jon harris", limit=1, response_format="json"))
    )

    brief = {
        "id": 11,
        "first_name": "Jon",
        "last_name": "Harris",
        "email": "jon.harris@example.com",
        "score": 1.0,
        "matched_on": "full name",
    }
    assert result == {
        "query": "jon harris",
        "resolution": "exact",
        "ambiguous_count": 0,
        "reason": "exact match on full name",
        "resolved": brief,
        "probable": None,
        "candidates": [brief],
    }


async def test_resolve_friend_without_friends(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": {"friends": []}})

    result = await splitwise_resolve_friend(ResolveFriendInput(query="jon"))

    assert result == 'No friends on this Splitwise account to match "jon" against.'


async def test_resolve_friend_unauthorized_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": _status_error(401, {"error": "Invalid API request: you are not logged in"})})

    result = await splitwise_resolve_friend(ResolveFriendInput(query="jon"))

    assert result.startswith("Error (401): Unauthorized – Invalid API request: you are not logged in.")
    assert "secure.splitwise.com/apps" in result


def test_resolve_inputs_reject_blank_query_bad_limit_and_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ResolveFriendInput(query="   ")
    with pytest.raises(ValidationError):
        ResolveFriendInput(query="jon", limit=0)
    with pytest.raises(ValidationError):
        ResolveGroupInput(query="trip", limit=lookup.MAX_RESOLVE_LIMIT + 1)
    with pytest.raises(ValidationError):
        ResolveCategoryInput(query="taxi", name="taxi")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------

GROUP_TABLE = "| # | Group | Type | Members | Score |"


async def test_resolve_group_exact_name_with_a_longer_sibling_is_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_groups": GROUPS})

    result = await splitwise_resolve_group(ResolveGroupInput(query="Peru Trip", limit=2))

    assert fake.calls == [("GET", "/get_groups", {})]
    assert '**Not resolved**: 2 candidates could be meant: "peru trip" is also how another name starts' in result
    assert _table_rows(result, GROUP_TABLE) == [
        "| 1 | Peru trip (id 102) | trip | 3 | 1.000 |",
        "| 2 | Peru trip 2026 (id 101) | trip | 2 | 0.783 |",
    ]


async def test_resolve_group_exact_resolves_and_marks_group_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_groups": GROUPS})

    flat = await splitwise_resolve_group(ResolveGroupInput(query="home | flat"))
    zero = await splitwise_resolve_group(ResolveGroupInput(query="Non-group expenses"))
    zero_json = json.loads(
        await splitwise_resolve_group(ResolveGroupInput(query="non-group expenses", response_format="json"))
    )

    assert "**Resolved**: Home | flat (id 103) — exact match on name." in flat
    assert any("Home \\| flat (id 103)" in row for row in _table_rows(flat, GROUP_TABLE))
    assert (
        "**Resolved**: Non-group expenses (id 0) — pseudo-group: group_id 0 = no group — exact match on name." in zero
    )
    assert _table_rows(zero, GROUP_TABLE)[0].startswith(
        "| 1 | Non-group expenses (id 0) — pseudo-group: group_id 0 = no group | — | 0 |"
    )
    assert zero_json["resolved"]["pseudo_group"] is True
    assert zero_json["resolved"]["id"] == 0


async def test_resolve_group_fuzzy_match_is_only_probable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_groups": GROUPS})

    result = await splitwise_resolve_group(ResolveGroupInput(query="peru trp", limit=1))

    assert (
        "**Not resolved.** Probable match — confirm with the user before using this id: Peru trip (id 102) "
        "(score 0.941 on name; a group resolves only on an exact match)."
    ) in result


@pytest.mark.parametrize(
    ("groups", "query", "closest"),
    [
        (GROUPS, "peru trip 2025", "| 1 | Peru trip 2026 (id 101) | trip | 2 | 0.929 |"),
        (
            {"groups": [{"id": 301, "name": "Cusco 2024", "group_type": "trip", "members": []}]},
            "cusco 2025",
            "| 1 | Cusco 2024 (id 301) | trip | 0 | 0.900 |",
        ),
    ],
)
async def test_resolve_group_different_numbers_never_match(
    monkeypatch: pytest.MonkeyPatch, groups: dict[str, Any], query: str, closest: str
) -> None:
    _install(monkeypatch, {"/get_groups": groups})

    result = await splitwise_resolve_group(ResolveGroupInput(query=query, limit=1))

    assert (
        "**Not resolved**: the closest candidates carry different numbers than the query (e.g. another year)" in result
    )
    assert _table_rows(result, GROUP_TABLE) == [closest]


async def test_resolve_group_several_strong_matches_are_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    flats = {
        "groups": [
            {"id": 401, "name": "Depa Miraflores", "group_type": "home", "members": []},
            {"id": 402, "name": "Depa Miraflore", "group_type": "home", "members": []},
        ]
    }
    _install(monkeypatch, {"/get_groups": flats})

    result = await splitwise_resolve_group(ResolveGroupInput(query="depa miraflors"))

    assert "**Not resolved**: 2 candidates score ≥ 0.85 — ask the user which one" in result
    assert _table_rows(result, GROUP_TABLE) == [
        "| 1 | Depa Miraflores (id 401) | home | 0 | 0.966 |",
        "| 2 | Depa Miraflore (id 402) | home | 0 | 0.929 |",
    ]


async def test_resolve_group_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_groups": GROUPS})

    result = json.loads(await splitwise_resolve_group(ResolveGroupInput(query="trip", limit=2, response_format="json")))

    assert result["resolution"] == "none"
    assert result["ambiguous_count"] == 0
    assert result["resolved"] is None
    assert result["probable"] is None
    assert [c["id"] for c in result["candidates"]] == [102, 101]
    assert result["candidates"][0] == {
        "id": 102,
        "name": "Peru trip",
        "group_type": "trip",
        "members_count": 3,
        "pseudo_group": False,
        "score": 0.6154,
    }


# ---------------------------------------------------------------------------
# Categories resolver
# ---------------------------------------------------------------------------

SUB_TABLE = "| # | Subcategory | Parent | Score | Matched on |"

# Real Splitwise subcategory names that carry several words in one name.
PIECE_CATEGORIES = {
    "categories": [
        {
            "id": 1,
            "name": "Utilities",
            "subcategories": [
                {"id": 48, "name": "Cleaning"},
                {"id": 5, "name": "Electricity"},
                {"id": 6, "name": "Heat/gas"},
                {"id": 8, "name": "TV/Phone/Internet"},
                {"id": 11, "name": "Other"},
            ],
        },
        {
            "id": 31,
            "name": "Transportation",
            "subcategories": [
                {"id": 32, "name": "Bus/train"},
                {"id": 33, "name": "Gas/fuel"},
                {"id": 36, "name": "Taxi"},
                {"id": 35, "name": "Other"},
            ],
        },
        {
            "id": 25,
            "name": "Food and drink",
            "subcategories": [
                {"id": 13, "name": "Dining out"},
                {"id": 12, "name": "Groceries"},
                {"id": 38, "name": "Liquor"},
                {"id": 26, "name": "Other"},
            ],
        },
        {
            "id": 19,
            "name": "Life",
            "subcategories": [
                {"id": 43, "name": "Medical expenses"},
                {"id": 44, "name": "Taxes"},
                {"id": 41, "name": "Other"},
            ],
        },
    ]
}


async def test_resolve_category_exact_and_unique_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_categories": CATEGORIES})

    exact = await splitwise_resolve_category(ResolveCategoryInput(query="Groceries"))
    near = await splitwise_resolve_category(ResolveCategoryInput(query="groceris"))

    assert "**Resolved**: Groceries (id 12) under Food and drink — exact match on name." in exact
    assert (
        "**Resolved**: Groceries (id 12) under Food and drink — the only candidate scoring ≥ 0.85 (0.941, on name)."
        in near
    )
    # The category list is cached across calls: one request for both.
    assert fake.calls == [("GET", "/get_categories", {})]


async def test_resolve_category_prefix_rival_makes_a_strong_match_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_categories": CATEGORIES})

    result = await splitwise_resolve_category(ResolveCategoryInput(query="tax", limit=2))

    # Taxi scores 0.857, but "Taxes" starts with "tax".
    assert '**Not resolved**: 2 candidates could be meant: "tax" is also how another name starts' in result
    assert _table_rows(result, SUB_TABLE) == [
        "| 1 | Taxi (id 32) | Transportation | 0.857 | name |",
        "| 2 | Taxes (id 43) | Life | 0.750 | name |",
    ]


@pytest.mark.parametrize(
    ("query", "resolved_line"),
    [
        ("train", "**Resolved**: Bus/train (id 32) under Transportation — exact match on name piece."),
        ("phone", "**Resolved**: TV/Phone/Internet (id 8) under Utilities — exact match on name piece."),
        ("Internet", "**Resolved**: TV/Phone/Internet (id 8) under Utilities — exact match on name piece."),
        ("medical", "**Resolved**: Medical expenses (id 43) under Life — exact match on name piece."),
    ],
)
async def test_resolve_category_matches_a_piece_of_the_name(
    monkeypatch: pytest.MonkeyPatch, query: str, resolved_line: str
) -> None:
    _install(monkeypatch, {"/get_categories": PIECE_CATEGORIES})

    result = await splitwise_resolve_category(ResolveCategoryInput(query=query))

    assert resolved_line in result


async def test_resolve_category_pieces_gas_is_ambiguous_and_dinner_ranks_dining_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, {"/get_categories": PIECE_CATEGORIES})

    gas = await splitwise_resolve_category(ResolveCategoryInput(query="gas", limit=2))
    dinner = await splitwise_resolve_category(ResolveCategoryInput(query="dinner", limit=1))

    assert "**Not resolved**: 2 candidates match exactly — ask the user which one" in gas
    assert _table_rows(gas, SUB_TABLE) == [
        "| 1 | Gas/fuel (id 33) | Transportation | 1.000 | name piece |",
        "| 2 | Heat/gas (id 6) | Utilities | 1.000 | name piece |",
    ]
    assert "**Not resolved**: no candidate scores ≥ 0.85" in dinner
    assert _table_rows(dinner, SUB_TABLE) == ["| 1 | Dining out (id 13) | Food and drink | 0.667 | name piece |"]


async def test_resolve_category_shares_cache_with_get_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_categories": CATEGORIES})

    await splitwise_get_categories(GetCategoriesInput())
    cached = await splitwise_resolve_category(ResolveCategoryInput(query="taxi"))
    await splitwise_resolve_category(ResolveCategoryInput(query="taxi", refresh=True))

    assert "Source: in-process cache" in cached
    assert fake.calls == [("GET", "/get_categories", {}), ("GET", "/get_categories", {})]


async def test_resolve_category_other_is_ambiguous_until_qualified(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_categories": CATEGORIES})

    bare = await splitwise_resolve_category(ResolveCategoryInput(query="other"))
    qualified = await splitwise_resolve_category(ResolveCategoryInput(query="transportation other", limit=1))

    assert "**Not resolved**: 4 candidates match exactly" in bare
    assert "**Resolved**: Other (id 35) under Transportation — exact match on parent + name." in qualified
    assert _table_rows(qualified, SUB_TABLE) == ["| 1 | Other (id 35) | Transportation | 1.000 | parent + name |"]


async def test_resolve_category_parent_name_is_flagged_not_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_categories": CATEGORIES})

    result = await splitwise_resolve_category(ResolveCategoryInput(query="food and drink"))

    assert "**Not resolved**: no candidate scores ≥ 0.85" in result
    assert result.index("## Subcategories") < result.index("## Closest parent categories")
    parents = _table_rows(result, "| Parent | Score | Matched on | Its subcategories |")
    assert parents[0] == (
        "| Food and drink (id 25) — parent, not usable for expenses | 1.000 | name | "
        "Dining out (id 13), Groceries (id 12), Other (id 26) |"
    )
    assert len(parents) == lookup.PARENT_ROWS


async def test_resolve_category_parent_ranked_by_best_subcategory(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_categories": CATEGORIES})

    result = await splitwise_resolve_category(ResolveCategoryInput(query="tax", limit=2))

    assert _table_rows(result, "| Parent | Score | Matched on | Its subcategories |") == [
        "| Transportation (id 31) — parent, not usable for expenses | 0.857 | subcategory Taxi | "
        "Taxi (id 32), Other (id 35) |",
        "| Life (id 19) — parent, not usable for expenses | 0.750 | subcategory Taxes | Taxes (id 43), Other (id 41) |",
    ]


async def test_resolve_category_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_categories": CATEGORIES})

    result = json.loads(
        await splitwise_resolve_category(ResolveCategoryInput(query="groceries", limit=1, response_format="json"))
    )

    assert result["resolution"] == "exact"
    assert result["ambiguous_count"] == 0
    assert result["reason"] == "exact match on name"
    assert result["resolved"] == {
        "id": 12,
        "name": "Groceries",
        "score": 1.0,
        "usable_for_expenses": True,
        "parent_id": 25,
        "parent_name": "Food and drink",
        "matched_on": "name",
    }
    assert [s["id"] for s in result["subcategories"]] == [12]
    assert result["parents"][0]["id"] == 25
    assert result["parents"][0]["usable_for_expenses"] is False
    assert result["parents"][0]["matched_on"] == "subcategory Groceries"
    assert result["parents"][0]["subcategories"][0] == {"id": 13, "name": "Dining out"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_lookup_tools_registered_read_only() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in (
        "splitwise_get_categories",
        "splitwise_get_currencies",
        "splitwise_resolve_friend",
        "splitwise_resolve_group",
        "splitwise_resolve_category",
    ):
        annotations = tools[name].annotations
        assert annotations is not None, name
        assert annotations.readOnlyHint is True, name
        assert annotations.destructiveHint is False, name
        assert annotations.idempotentHint is True, name
        assert annotations.openWorldHint is True, name


# ---------------------------------------------------------------------------
# Live (read-only, global catalogues only)
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_get_categories_live_smoke() -> None:
    result = await splitwise_get_categories(GetCategoriesInput())

    assert "Subcategories (use these ids)" in result


@pytest.mark.live
async def test_get_currencies_live_smoke() -> None:
    result = await splitwise_get_currencies(GetCurrenciesInput(query="usd"))

    assert "| USD |" in result


@pytest.mark.live
async def test_resolve_category_live_smoke() -> None:
    result = await splitwise_resolve_category(ResolveCategoryInput(query="Groceries"))

    assert "**Resolved**: Groceries (id" in result
