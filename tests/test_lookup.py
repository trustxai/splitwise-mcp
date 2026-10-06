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


async def test_resolve_friend_exact_first_name_ranks_over_longer_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_friends": FRIENDS})

    result = await splitwise_resolve_friend(ResolveFriendInput(query="jon", limit=2))

    assert fake.calls == [("GET", "/get_friends", {})]
    assert "**Resolved**: Jon Harris (id 11) — exact match on first name." in result
    assert _table_rows(result, "| # | Friend | Email | Score | Matched on |") == [
        "| 1 | Jon Harris (id 11) | jon.harris@example.com | 1.000 | first name |",
        "| 2 | Jonathan Doe (id 12) | jdoe@example.com | 0.571 | email |",
    ]
    assert "_2 of 6 friend(s) shown, best first._" in result


async def test_resolve_friend_unique_strong_and_accent_folding(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    typo = await splitwise_resolve_friend(ResolveFriendInput(query="Jonh"))
    prefix = await splitwise_resolve_friend(ResolveFriendInput(query="mari", limit=2))
    accents = await splitwise_resolve_friend(ResolveFriendInput(query="  MARIA garcia "))

    assert "**Resolved**: Jon Harris (id 11) — the only candidate scoring ≥ 0.85 (0.857, on first name)." in typo
    # María 0.889 is the only one ≥ 0.85; Marian (0.800) is close but below the bar.
    assert "**Resolved**: María García (id 13) — the only candidate scoring ≥ 0.85 (0.889, on first name)." in prefix
    assert _table_rows(prefix, "| # | Friend | Email | Score | Matched on |") == [
        "| 1 | María García (id 13) | maria@example.com | 0.889 | first name |",
        "| 2 | Marian López (id 14) | mlopez@example.com | 0.800 | first name |",
    ]
    assert "**Resolved**: María García (id 13) — exact match on full name." in accents


async def test_resolve_friend_ambiguous_and_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    ambiguous = await splitwise_resolve_friend(ResolveFriendInput(query="ana", limit=2))
    nothing = await splitwise_resolve_friend(ResolveFriendInput(query="zzz"))

    assert "**Not resolved**: 2 candidates match exactly — ask the user which one" in ambiguous
    assert _table_rows(ambiguous, "| # | Friend | Email | Score | Matched on |") == [
        "| 1 | Ana Ruiz (id 16) | aruiz@example.com | 1.000 | first name |",
        "| 2 | Ana Torres (id 15) | ana.t@example.com | 1.000 | first name |",
    ]
    assert "**Not resolved**: no candidate scores ≥ 0.85" in nothing


async def test_resolve_friend_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_friends": FRIENDS})

    result = json.loads(
        await splitwise_resolve_friend(ResolveFriendInput(query="jon", limit=1, response_format="json"))
    )

    assert result == {
        "query": "jon",
        "resolution": "exact",
        "ambiguous_count": 0,
        "resolved": {
            "id": 11,
            "first_name": "Jon",
            "last_name": "Harris",
            "email": "jon.harris@example.com",
            "score": 1.0,
            "matched_on": "first name",
        },
        "candidates": [
            {
                "id": 11,
                "first_name": "Jon",
                "last_name": "Harris",
                "email": "jon.harris@example.com",
                "score": 1.0,
                "matched_on": "first name",
            }
        ],
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


async def test_resolve_group_exact_beats_longer_name(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_groups": GROUPS})

    result = await splitwise_resolve_group(ResolveGroupInput(query="Peru Trip", limit=2))

    assert fake.calls == [("GET", "/get_groups", {})]
    assert "**Resolved**: Peru trip (id 102) — exact match on name." in result
    assert _table_rows(result, "| # | Group | Type | Members | Score |") == [
        "| 1 | Peru trip (id 102) | trip | 3 | 1.000 |",
        "| 2 | Peru trip 2026 (id 101) | trip | 2 | 0.783 |",
    ]


async def test_resolve_group_unique_strong_pipe_escape_and_group_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_groups": GROUPS})

    near = await splitwise_resolve_group(ResolveGroupInput(query="peru trip 2025", limit=1))
    full = await splitwise_resolve_group(ResolveGroupInput(query="non group", limit=4))

    assert "**Resolved**: Peru trip 2026 (id 101) — the only candidate scoring ≥ 0.85 (0.929, on name)." in near
    rows = _table_rows(full, "| # | Group | Type | Members | Score |")
    assert rows[0].startswith("| 1 | Non-group expenses (id 0) — pseudo-group: group_id 0 = no group | — | 0 |")
    assert any("Home \\| flat (id 103)" in row for row in rows)


async def test_resolve_group_several_strong_matches_are_ambiguous(monkeypatch: pytest.MonkeyPatch) -> None:
    trips = {
        "groups": [
            {"id": 201, "name": "Peru trip 2025", "group_type": "trip", "members": []},
            {"id": 202, "name": "Peru trip 2026", "group_type": "trip", "members": []},
        ]
    }
    _install(monkeypatch, {"/get_groups": trips})

    result = await splitwise_resolve_group(ResolveGroupInput(query="peru trip 202"))

    assert "**Not resolved**: 2 candidates score ≥ 0.85 — ask the user which one" in result
    assert [row.split(" | ")[1] for row in _table_rows(result, "| # | Group | Type | Members | Score |")] == [
        "Peru trip 2025 (id 201)",
        "Peru trip 2026 (id 202)",
    ]


async def test_resolve_group_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_groups": GROUPS})

    result = json.loads(await splitwise_resolve_group(ResolveGroupInput(query="trip", limit=2, response_format="json")))

    assert result["resolution"] == "none"
    assert result["resolved"] is None
    assert [c["id"] for c in result["candidates"]] == [102, 101]
    assert result["candidates"][0] == {
        "id": 102,
        "name": "Peru trip",
        "group_type": "trip",
        "members_count": 3,
        "score": 0.6154,
    }


# ---------------------------------------------------------------------------
# Categories resolver
# ---------------------------------------------------------------------------


async def test_resolve_category_exact_and_unique_strong(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_categories": CATEGORIES})

    exact = await splitwise_resolve_category(ResolveCategoryInput(query="Groceries"))
    near = await splitwise_resolve_category(ResolveCategoryInput(query="tax", limit=2))

    assert "**Resolved**: Groceries (id 12) under Food and drink — exact match on name." in exact
    assert (
        "**Resolved**: Taxi (id 32) under Transportation — the only candidate scoring ≥ 0.85 (0.857, on name)." in near
    )
    assert _table_rows(near, "| # | Subcategory | Parent | Score | Matched on |") == [
        "| 1 | Taxi (id 32) | Transportation | 0.857 | name |",
        "| 2 | Taxes (id 43) | Life | 0.750 | name |",
    ]
    # The category list is cached across tools: one request for both calls.
    assert fake.calls == [("GET", "/get_categories", {})]


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
    assert _table_rows(qualified, "| # | Subcategory | Parent | Score | Matched on |") == [
        "| 1 | Other (id 35) | Transportation | 1.000 | parent + name |"
    ]


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
