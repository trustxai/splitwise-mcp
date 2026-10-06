"""Unit tests for the expense tools (D1–D6) against a fake client — no network, no live writes."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from splitwise_mcp.client import SplitwiseClient, SplitwiseEnvelopeError
from splitwise_mcp.config import Settings, get_settings
from splitwise_mcp.server import mcp
from splitwise_mcp.tools import expenses as mod
from splitwise_mcp.tools.expenses import (
    CreateExpenseInput,
    ExpenseIdInput,
    GetExpenseInput,
    GetExpensesInput,
    ShareInput,
    UpdateExpenseInput,
    equal_split,
    flatten_shares,
    shares_problem,
    splitwise_create_expense,
    splitwise_delete_expense,
    splitwise_get_expense,
    splitwise_get_expenses,
    splitwise_undelete_expense,
    splitwise_update_expense,
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


def _install(monkeypatch: pytest.MonkeyPatch, routes: dict[str, Any] | None = None) -> _FakeClient:
    fake = _FakeClient(routes)
    monkeypatch.setattr("splitwise_mcp.tools.expenses.get_client", lambda: fake)
    return fake


def _status_error(status: int, body: dict[str, Any], path: str = "/get_expense/1") -> httpx.HTTPStatusError:
    request = httpx.Request("GET", f"https://secure.splitwise.com/api/v3.0{path}")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


ADA = {"id": 1, "first_name": "Ada", "last_name": "Lovelace"}
BOB = {"id": 2, "first_name": "Bob", "last_name": "Byte"}
CYD = {"id": 3, "first_name": "Cyd", "last_name": None}


def _share(user: dict[str, Any], paid: str, owed: str) -> dict[str, Any]:
    net = f"{float(paid) - float(owed):.2f}"
    return {"user": user, "user_id": user["id"], "paid_share": paid, "owed_share": owed, "net_balance": net}


DINNER = {
    "id": 101,
    "group_id": 55,
    "description": "Dinner | Central",
    "details": "incl. tip",
    "cost": "30.0",
    "currency_code": "USD",
    "date": "2026-10-01T19:00:00Z",
    "created_at": "2026-10-01T20:00:00Z",
    "created_by": ADA,
    "updated_at": "2026-10-02T08:00:00Z",
    "updated_by": BOB,
    "deleted_at": None,
    "payment": False,
    "repeat_interval": "never",
    "category": {"id": 13, "name": "Dining out"},
    "comments_count": 1,
    "users": [_share(ADA, "30.00", "10.00"), _share(BOB, "0.00", "10.00"), _share(CYD, "0.00", "10.00")],
    "repayments": [{"from": 2, "to": 1, "amount": "10.0"}, {"from": 3, "to": 1, "amount": "10.0"}],
    "comments": [
        {
            "id": 9,
            "content": "thanks!",
            "comment_type": "User",
            "created_at": "2026-10-01T21:00:00Z",
            "user": BOB,
        }
    ],
}
TAXI_DELETED = {
    "id": 102,
    "group_id": None,
    "description": "Taxi",
    "cost": "12.00",
    "currency_code": "PEN",
    "date": "2026-09-30T10:00:00Z",
    "deleted_at": "2026-09-30T11:00:00Z",
    "payment": False,
    "users": [_share(BOB, "12.00", "6.00"), _share(ADA, "0.00", "6.00")],
}
SETTLE_UP = {
    "id": 103,
    "group_id": 0,
    "description": "Payment",
    "cost": "10.00",
    "currency_code": "USD",
    "date": "2026-09-29T10:00:00Z",
    "deleted_at": None,
    "payment": True,
    "users": [_share(BOB, "10.00", "0.00"), _share(ADA, "0.00", "10.00")],
}


# ---------------------------------------------------------------------------
# splitwise_get_expenses (D1)
# ---------------------------------------------------------------------------


async def test_get_expenses_hides_deleted_counts_them_and_renders_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_expenses": {"expenses": [DINNER, TAXI_DELETED, SETTLE_UP]}})

    result = await splitwise_get_expenses(
        GetExpensesInput(group_id=55, dated_after="2026-09-01", limit=3, current_user_id=1)
    )

    assert fake.calls == [
        (
            "GET",
            "/get_expenses",
            {"params": {"group_id": 55, "dated_after": "2026-09-01T00:00:00Z", "limit": 3, "offset": 0}},
        )
    ]
    assert "Splitwise returned **3** expense(s) at offset 0 (limit 3)." in result
    assert "Deleted expenses hidden: **1**" in result
    assert "More available — next offset → **3**." in result
    assert "positive = owed to you" in result
    dinner_row = (
        "| 101 | 2026-10-01 19:00 UTC | Dinner \\| Central | 30.00 USD | group 55 | Ada Lovelace (id 1) | +20.00 USD |"
    )
    settle_row = (
        "| 103 | 2026-09-29 10:00 UTC | [settle-up] Payment | 10.00 USD | no group | Bob Byte (id 2) | -10.00 USD |"
    )
    assert dinner_row in result
    assert settle_row in result
    assert "Taxi" not in result


async def test_get_expenses_include_deleted_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_expenses": {"expenses": [DINNER, TAXI_DELETED]}})

    result = await splitwise_get_expenses(
        GetExpensesInput(friend_id=2, include_deleted=True, offset=40, response_format="json")
    )

    assert fake.calls == [("GET", "/get_expenses", {"params": {"friend_id": 2, "limit": 20, "offset": 40}})]
    payload = json.loads(result)
    assert payload["count"] == 2
    assert payload["returned_by_api"] == 2
    assert payload["hidden_deleted"] == 0
    assert payload["has_more"] is False
    assert payload["next_offset"] is None
    assert [e["id"] for e in payload["expenses"]] == [101, 102]


async def test_get_expenses_json_counts_hidden_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_expenses": {"expenses": [TAXI_DELETED, DINNER]}})

    payload = json.loads(await splitwise_get_expenses(GetExpensesInput(limit=2, response_format="json")))

    assert payload["hidden_deleted"] == 1
    assert [e["id"] for e in payload["expenses"]] == [101]
    # The page was full, so the next offset counts the hidden row too.
    assert payload["has_more"] is True
    assert payload["next_offset"] == 2


async def test_get_expenses_markdown_row_cap_gives_raw_offset(monkeypatch: pytest.MonkeyPatch) -> None:
    page = [TAXI_DELETED] + [{**DINNER, "id": 1000 + i} for i in range(60)]
    _install(monkeypatch, {"/get_expenses": {"expenses": page}})

    result = await splitwise_get_expenses(GetExpensesInput(limit=100, offset=200))

    assert result.count("| Dinner \\| Central |") == mod.MAX_DISPLAY_ROWS
    # 50 visible rows shown; the 51st visible row is raw index 51 (one hidden deleted row before it).
    assert "_10 more expense(s) on this page not shown (markdown cap 50) — continue with offset=251" in result
    assert "Pass current_user_id" in result
    assert "More available" not in result  # 61 < limit 100


async def test_get_expenses_empty_page(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_expenses": {"expenses": []}})

    result = await splitwise_get_expenses(GetExpensesInput())

    assert "_No expenses._" in result
    assert "Deleted expenses hidden: **0**" in result


def test_get_expenses_rejects_group_and_friend_together() -> None:
    with pytest.raises(ValidationError, match="group_id OR friend_id"):
        GetExpensesInput(group_id=1, friend_id=2)


def test_get_expenses_rejects_bad_date_and_limit() -> None:
    with pytest.raises(ValidationError, match="dated_before must be ISO-8601"):
        GetExpensesInput(dated_before="last tuesday")
    with pytest.raises(ValidationError):
        GetExpensesInput(limit=101)


# ---------------------------------------------------------------------------
# splitwise_get_expense (D2)
# ---------------------------------------------------------------------------


async def test_get_expense_renders_shares_repayments_comments(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_expense/101": {"expense": DINNER}})

    result = await splitwise_get_expense(GetExpenseInput(expense_id=101))

    assert fake.calls == [("GET", "/get_expense/101", {})]
    assert result.startswith("# Expense 101: Dinner \\| Central")
    assert "- **cost**: 30.00 USD" in result
    assert "- **group**: group 55" in result
    assert "- **category**: Dining out (id 13)" in result
    assert "- **created**: 2026-10-01 20:00 UTC by Ada Lovelace (id 1)" in result
    assert "- **updated**: 2026-10-02 08:00 UTC by Bob Byte (id 2)" in result
    assert "| Ada Lovelace (id 1) | 30.00 USD | 10.00 USD | +20.00 USD |" in result
    assert "| Cyd (id 3) | 0.00 USD | 10.00 USD | -10.00 USD |" in result
    assert "- Bob Byte (id 2) → Ada Lovelace (id 1): 10.00 USD" in result
    assert "## Comments (1)" in result
    assert "| 2026-10-01 21:00 UTC | Bob Byte (id 2) | User | thanks! |" in result
    assert "DELETED" not in result


async def test_get_expense_flags_deleted_and_json_is_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_expense/102": {"expense": TAXI_DELETED}})

    markdown = await splitwise_get_expense(GetExpenseInput(expense_id=102))
    raw = await splitwise_get_expense(GetExpenseInput(expense_id=102, response_format="json"))

    assert "- **DELETED**: 2026-09-30 11:00 UTC" in markdown
    assert "splitwise_undelete_expense" in markdown
    assert json.loads(raw) == TAXI_DELETED


async def test_get_expense_not_found_is_routed_through_handle_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/get_expense/9": _status_error(404, {"errors": {"base": ["Expense not found"]}})})

    result = await splitwise_get_expense(GetExpenseInput(expense_id=9))

    assert result.startswith("Error (404): Not found – Expense not found.")


def test_expense_id_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        ExpenseIdInput(expense_id=0)


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------


def test_equal_split_puts_remainder_cents_on_payer() -> None:
    shares = equal_split("10.00", [1, 2, 3], payer_id=1)

    assert [(s.user_id, s.paid_share, s.owed_share) for s in shares] == [
        (1, "10.00", "3.34"),
        (2, "0.00", "3.33"),
        (3, "0.00", "3.33"),
    ]
    assert shares_problem("10.00", shares) is None


@pytest.mark.parametrize(
    ("cost", "count"),
    [("0.05", 3), ("100.00", 7), ("0.01", 2), ("33.33", 4), ("1234.57", 9)],
)
def test_equal_split_always_closes(cost: str, count: int) -> None:
    ids = list(range(10, 10 + count))
    shares = equal_split(cost, ids, payer_id=ids[-1])

    assert shares_problem(cost, shares) is None
    owed = [s.owed_share for s in shares]
    # Everyone but the payer owes the same; the payer owes at most n-1 cents more.
    assert len(set(owed[:-1])) == 1


def test_shares_problem_names_the_mismatch() -> None:
    shares = [
        ShareInput(user_id=1, paid_share="9.00", owed_share="5.00"),
        ShareInput(user_id=2, paid_share="0.00", owed_share="5.00"),
    ]

    problem = shares_problem("10.00", shares)

    assert problem is not None
    assert "paid shares sum to 9.00 but cost is 10.00 (off by -1.00)" in problem
    assert "owed shares" not in problem  # the owed side closes, so only the paid side is named

    both = shares_problem("12.00", shares)
    assert both is not None
    assert "paid shares sum to 9.00 but cost is 12.00 (off by -3.00)" in both
    assert "owed shares sum to 10.00 but cost is 12.00 (off by -2.00)" in both


def test_flatten_shares_builds_users_keys() -> None:
    shares = [
        ShareInput(user_id=1, paid_share="25.5", owed_share=10),
        ShareInput(email="eve@example.com", first_name="Eve", last_name="Ng", paid_share="0", owed_share="15.50"),
    ]

    assert flatten_shares(shares) == {
        "users__0__user_id": 1,
        "users__0__paid_share": "25.50",
        "users__0__owed_share": "10.00",
        "users__1__email": "eve@example.com",
        "users__1__first_name": "Eve",
        "users__1__last_name": "Ng",
        "users__1__paid_share": "0.00",
        "users__1__owed_share": "15.50",
    }


# ---------------------------------------------------------------------------
# splitwise_create_expense (D3)
# ---------------------------------------------------------------------------

CREATED = {"expenses": [{**DINNER, "id": 777}], "errors": {}}


async def test_create_split_equally_in_group(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/create_expense": CREATED})

    result = await splitwise_create_expense(
        CreateExpenseInput(
            cost="30",
            description="  Dinner  ",
            group_id=55,
            split_equally=True,
            currency_code="usd",
            date="2026-10-01",
            category_id=13,
        )
    )

    assert fake.calls == [
        (
            "POST",
            "/create_expense",
            {
                "data": {
                    "cost": "30.00",
                    "description": "Dinner",
                    "date": "2026-10-01T00:00:00Z",
                    "currency_code": "USD",
                    "category_id": 13,
                    "group_id": 55,
                    "split_equally": True,
                }
            },
        )
    ]
    assert "- **errors**: `{}`" in result
    assert "## Expense 777: Dinner \\| Central" in result
    assert "- **cost**: 30.00 USD" in result
    assert "| Bob Byte (id 2) | 0.00 USD | 10.00 USD | -10.00 USD |" in result


async def test_create_explicit_shares_flattened(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/create_expense": CREATED})

    await splitwise_create_expense(
        CreateExpenseInput(
            cost="50.00",
            description="Groceries",
            details="market",
            repeat_interval="monthly",
            shares=[
                ShareInput(user_id=2, paid_share="50.00", owed_share="30.00"),
                ShareInput(email="eve@example.com", first_name="Eve", last_name="Ng", paid_share="0", owed_share="20"),
            ],
        )
    )

    assert fake.calls == [
        (
            "POST",
            "/create_expense",
            {
                "data": {
                    "cost": "50.00",
                    "description": "Groceries",
                    "details": "market",
                    "repeat_interval": "monthly",
                    "group_id": 0,
                    "users__0__user_id": 2,
                    "users__0__paid_share": "50.00",
                    "users__0__owed_share": "30.00",
                    "users__1__email": "eve@example.com",
                    "users__1__first_name": "Eve",
                    "users__1__last_name": "Ng",
                    "users__1__paid_share": "0.00",
                    "users__1__owed_share": "20.00",
                }
            },
        )
    ]


async def test_create_equal_split_between_learns_me_and_assigns_remainder(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(
        monkeypatch,
        {"/get_current_user": {"user": {"id": 2, "first_name": "Bob"}}, "/create_expense": CREATED},
    )

    result = await splitwise_create_expense(
        CreateExpenseInput(cost="10.00", description="Taxi", equal_split_between=[1, 2, 3])
    )

    assert fake.calls == [
        ("GET", "/get_current_user", {}),
        (
            "POST",
            "/create_expense",
            {
                "data": {
                    "cost": "10.00",
                    "description": "Taxi",
                    "group_id": 0,
                    "users__0__user_id": 1,
                    "users__0__paid_share": "0.00",
                    "users__0__owed_share": "3.33",
                    "users__1__user_id": 2,
                    "users__1__paid_share": "10.00",
                    "users__1__owed_share": "3.34",
                    "users__2__user_id": 3,
                    "users__2__paid_share": "0.00",
                    "users__2__owed_share": "3.33",
                }
            },
        ),
    ]
    assert "- **errors**: `{}`" in result


async def test_create_equal_split_between_with_explicit_payer_skips_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/create_expense": CREATED})

    await splitwise_create_expense(
        CreateExpenseInput(
            cost="7.00", description="Coffee", group_id=55, equal_split_between=[4, 5], paid_by_user_id=5
        )
    )

    assert [call[:2] for call in fake.calls] == [("POST", "/create_expense")]
    data = fake.calls[0][2]["data"]
    assert data["users__1__paid_share"] == "7.00"
    assert data["users__1__owed_share"] == "3.50"
    assert data["users__0__owed_share"] == "3.50"
    assert data["group_id"] == 55


async def test_create_equal_split_between_refuses_when_i_am_not_in_the_list(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_current_user": {"user": {"id": 9}}})

    result = await splitwise_create_expense(
        CreateExpenseInput(cost="10.00", description="Taxi", equal_split_between=[1, 2])
    )

    assert result.startswith("Error: the payer (user id 9, you) is not in equal_split_between [1, 2]")
    assert fake.calls == [("GET", "/get_current_user", {})]  # no POST


async def test_create_sum_mismatch_is_refused_before_any_call(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    arguments = {
        "params": {
            "cost": "10.00",
            "description": "Lunch",
            "shares": [
                {"user_id": 1, "paid_share": "9.00", "owed_share": "5.00"},
                {"user_id": 2, "paid_share": "0.00", "owed_share": "5.00"},
            ],
        }
    }

    with pytest.raises(ToolError, match=r"paid shares sum to 9\.00 but cost is 10\.00"):
        await mcp.call_tool("splitwise_create_expense", arguments)
    assert fake.calls == []


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"split_equally": True}, "split_equally=true needs group_id > 0"),
        ({}, "choose exactly one split mode .* got: none"),
        (
            {"split_equally": True, "group_id": 5, "equal_split_between": [1, 2]},
            "got: split_equally, equal_split_between",
        ),
        ({"equal_split_between": [1]}, "at least 2 user ids"),
        ({"equal_split_between": [1, 2, 1]}, r"user id\(s\) \[1\] more than once"),
        ({"equal_split_between": [1, 2], "paid_by_user_id": 3}, "paid_by_user_id 3 is not in equal_split_between"),
        (
            {"paid_by_user_id": 1, "shares": [{"user_id": 1, "paid_share": "10", "owed_share": "5"}]},
            "paid_by_user_id only applies to equal_split_between",
        ),
        ({"shares": [{"user_id": 1, "paid_share": "10", "owed_share": "10"}]}, "at least 2 participants"),
        (
            {
                "shares": [
                    {"user_id": 1, "paid_share": "10", "owed_share": "5"},
                    {"user_id": 1, "paid_share": "0", "owed_share": "5"},
                ]
            },
            "user 1 appears more than once",
        ),
        (
            {
                "shares": [
                    {"user_id": 1, "email": "a@b.co", "paid_share": "10", "owed_share": "5"},
                    {"user_id": 2, "paid_share": "0", "owed_share": "5"},
                ]
            },
            "user_id OR email",
        ),
        (
            {
                "shares": [
                    {"email": "a@b.co", "first_name": "A", "paid_share": "10", "owed_share": "5"},
                    {"user_id": 2, "paid_share": "0", "owed_share": "5"},
                ]
            },
            "all three of email, first_name and last_name",
        ),
        (
            {
                "shares": [
                    {"user_id": 1, "paid_share": 10.0, "owed_share": "5"},
                    {"user_id": 2, "paid_share": "0", "owed_share": "5"},
                ]
            },
            "paid_share must be a decimal string",
        ),
        ({"split_equally": True, "group_id": 5, "currency_code": "dollars"}, "3-letter code"),
        ({"split_equally": True, "group_id": 5, "unknown": 1}, "Extra inputs are not permitted"),
    ],
)
def test_create_local_validation_rejections(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        CreateExpenseInput.model_validate({"cost": "10.00", "description": "x", **kwargs})


def test_create_rejects_bad_cost() -> None:
    with pytest.raises(ValidationError, match="cost must have at most 2 decimal places"):
        CreateExpenseInput(cost="10.001", description="x", split_equally=True, group_id=5)
    with pytest.raises(ValidationError, match="cost must be positive"):
        CreateExpenseInput(cost="0", description="x", split_equally=True, group_id=5)


async def test_create_envelope_error_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = SplitwiseEnvelopeError({"base": ["Cost must equal the sum of shares"]}, path="/create_expense")
    _install(monkeypatch, {"/create_expense": rejected})

    result = await splitwise_create_expense(
        CreateExpenseInput(cost="10.00", description="x", split_equally=True, group_id=5)
    )

    assert result == "Error: Splitwise rejected the request to /create_expense: Cost must equal the sum of shares"


async def test_create_reports_empty_expenses_without_claiming_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/create_expense": {"expenses": [], "errors": {}}})

    result = await splitwise_create_expense(
        CreateExpenseInput(cost="10.00", description="x", split_equally=True, group_id=5)
    )

    assert "Splitwise returned no expense object" in result
    assert "## Expense" not in result


async def test_create_is_refused_by_the_client_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool does not gate — the real client does, before any byte leaves the process."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"expenses": [], "errors": {}})

    client = SplitwiseClient(settings=Settings(splitwise_api_key="k" * 20), transport=httpx.MockTransport(handler))
    monkeypatch.setattr("splitwise_mcp.tools.expenses.get_client", lambda: client)

    result = await splitwise_create_expense(
        CreateExpenseInput(cost="10.00", description="x", equal_split_between=[1, 2], paid_by_user_id=1)
    )

    assert result.startswith("Error: POST /create_expense would change your Splitwise account")
    assert "SPLITWISE_ALLOW_WRITES=1" in result
    assert sent == []


# ---------------------------------------------------------------------------
# splitwise_update_expense (D4)
# ---------------------------------------------------------------------------

UPDATED = {"expenses": [{**DINNER, "description": "Dinner (tip)"}], "errors": {}}


def test_update_docstring_warns_that_shares_replace_all() -> None:
    doc = splitwise_update_expense.__doc__ or ""
    assert "WARNING: `shares` REPLACES ALL existing shares" in doc
    assert "REPLACES ALL existing shares" in (UpdateExpenseInput.model_fields["shares"].description or "")


async def test_update_single_field_sends_only_that_field(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/update_expense/101": UPDATED})

    result = await splitwise_update_expense(UpdateExpenseInput(expense_id=101, description="Dinner (tip)"))

    assert fake.calls == [("POST", "/update_expense/101", {"data": {"description": "Dinner (tip)"}})]
    assert "# Expense updated" in result
    assert "- **errors**: `{}`" in result
    assert "## Expense 101: Dinner (tip)" in result


async def test_update_shares_without_cost_checks_against_current_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_expense/101": {"expense": DINNER}, "/update_expense/101": UPDATED})

    await splitwise_update_expense(
        UpdateExpenseInput(
            expense_id=101,
            shares=[
                ShareInput(user_id=1, paid_share="30.00", owed_share="15.00"),
                ShareInput(user_id=2, paid_share="0.00", owed_share="15.00"),
            ],
        )
    )

    assert fake.calls == [
        ("GET", "/get_expense/101", {}),
        (
            "POST",
            "/update_expense/101",
            {
                "data": {
                    "users__0__user_id": 1,
                    "users__0__paid_share": "30.00",
                    "users__0__owed_share": "15.00",
                    "users__1__user_id": 2,
                    "users__1__paid_share": "0.00",
                    "users__1__owed_share": "15.00",
                }
            },
        ),
    ]


async def test_update_shares_mismatch_against_current_cost_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_expense/101": {"expense": DINNER}})

    result = await splitwise_update_expense(
        UpdateExpenseInput(
            expense_id=101,
            shares=[
                ShareInput(user_id=1, paid_share="40.00", owed_share="20.00"),
                ShareInput(user_id=2, paid_share="0.00", owed_share="20.00"),
            ],
        )
    )

    assert result.startswith("Error: shares do not add up: paid shares sum to 40.00 but cost is 30.00")
    assert "checked against the expense's current cost 30.00" in result
    assert fake.calls == [("GET", "/get_expense/101", {})]  # no POST


async def test_update_cost_with_shares_validates_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/update_expense/101": UPDATED})

    await splitwise_update_expense(
        UpdateExpenseInput(
            expense_id=101,
            cost="60",
            group_id=0,
            shares=[
                ShareInput(user_id=1, paid_share="60", owed_share="30"),
                ShareInput(user_id=2, paid_share="0", owed_share="30"),
            ],
        )
    )

    assert [call[:2] for call in fake.calls] == [("POST", "/update_expense/101")]
    assert fake.calls[0][2]["data"]["cost"] == "60.00"
    assert fake.calls[0][2]["data"]["group_id"] == 0

    with pytest.raises(ValidationError, match="owed shares sum to 50.00 but cost is 60.00"):
        UpdateExpenseInput(
            expense_id=101,
            cost="60",
            shares=[
                ShareInput(user_id=1, paid_share="60", owed_share="30"),
                ShareInput(user_id=2, paid_share="0", owed_share="20"),
            ],
        )


def test_update_requires_something_to_change() -> None:
    with pytest.raises(ValidationError, match="nothing to update"):
        UpdateExpenseInput(expense_id=101)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        UpdateExpenseInput.model_validate({"expense_id": 101, "split_equally": True})


async def test_update_unreadable_current_cost_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/get_expense/101": {"expense": {"id": 101}}})

    result = await splitwise_update_expense(
        UpdateExpenseInput(
            expense_id=101,
            shares=[
                ShareInput(user_id=1, paid_share="1", owed_share="0.5"),
                ShareInput(user_id=2, paid_share="0", owed_share="0.5"),
            ],
        )
    )

    assert result.startswith("Error: could not read the current cost of expense 101")
    assert fake.calls == [("GET", "/get_expense/101", {})]


# ---------------------------------------------------------------------------
# splitwise_delete_expense (D5) / splitwise_undelete_expense (D6)
# ---------------------------------------------------------------------------


async def test_delete_expense_echoes_success_and_undo(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/delete_expense/101": {"success": True, "errors": {}}})

    result = await splitwise_delete_expense(ExpenseIdInput(expense_id=101))

    assert fake.calls == [("POST", "/delete_expense/101", {})]
    assert result.startswith("Expense 101 deleted — Splitwise answered `success: true`.")
    assert "splitwise_undelete_expense (expense_id=101)" in result


async def test_delete_expense_without_success_field_does_not_claim_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"/delete_expense/101": {}})

    result = await splitwise_delete_expense(ExpenseIdInput(expense_id=101))

    assert result.startswith("Splitwise answered `success: null` for deleting expense 101")
    assert "deleted —" not in result


async def test_undelete_expense(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, {"/undelete_expense/101": {"success": True}})

    result = await splitwise_undelete_expense(ExpenseIdInput(expense_id=101))

    assert fake.calls == [("POST", "/undelete_expense/101", {})]
    assert result == "Expense 101 restored — Splitwise answered `success: true`."


async def test_undelete_expense_rejected_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = SplitwiseEnvelopeError(None, path="/undelete_expense/101")
    _install(monkeypatch, {"/undelete_expense/101": rejected})

    result = await splitwise_undelete_expense(ExpenseIdInput(expense_id=101))

    assert result.startswith("Error: Splitwise rejected the request to /undelete_expense/101")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

EXPECTED_HINTS = {
    "splitwise_get_expenses": (True, False, True),
    "splitwise_get_expense": (True, False, True),
    "splitwise_create_expense": (False, False, False),
    "splitwise_update_expense": (False, False, True),
    "splitwise_delete_expense": (False, True, True),
    "splitwise_undelete_expense": (False, False, True),
}


async def test_tools_registered_with_annotations_for_their_verb() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name, (read_only, destructive, idempotent) in EXPECTED_HINTS.items():
        annotations = tools[name].annotations
        assert annotations is not None, name
        assert (annotations.readOnlyHint, annotations.destructiveHint, annotations.idempotentHint) == (
            read_only,
            destructive,
            idempotent,
        ), name
        assert annotations.openWorldHint is True, name


async def test_mutating_docstrings_name_the_kill_switch() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    mutating = (
        "splitwise_create_expense",
        "splitwise_update_expense",
        "splitwise_delete_expense",
        "splitwise_undelete_expense",
    )
    for name in mutating:
        description = " ".join((tools[name].description or "").split())
        assert "Refused with `Error: … writes are disabled` unless `SPLITWISE_ALLOW_WRITES=1`" in description, name
    assert "Restore with `splitwise_undelete_expense`" in (tools["splitwise_delete_expense"].description or "")


# ---------------------------------------------------------------------------
# Live smoke — READ tools only (skipped without SPLITWISE_API_KEY)
# ---------------------------------------------------------------------------


@pytest.mark.live
async def test_get_expenses_live_smoke() -> None:
    get_settings.cache_clear()
    result = await splitwise_get_expenses(GetExpensesInput(limit=2, response_format="json"))

    assert not result.startswith("Error"), result
    payload = json.loads(result)
    assert "expenses" in payload
    if payload["expenses"]:
        detail = await splitwise_get_expense(GetExpenseInput(expense_id=payload["expenses"][0]["id"]))
        assert detail.startswith("# Expense"), detail
