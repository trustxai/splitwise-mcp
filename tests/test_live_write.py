"""Opt-in live WRITE smoke against the real account (`-m live_write`).

Runs only with `SPLITWISE_API_KEY` set AND `SPLITWISE_TEST_ALLOW_WRITES=1` (conftest skips
it otherwise). It touches the account on purpose, in a throwaway group that holds nobody
but you:

    create group (you only) → create a 1.00 expense in it (split equally = you pay you)
    → read it back → delete the expense → delete the group,

and cleans up in `finally` even when an assertion fails midway, so a red run never
leaves a stray group behind. Nothing here adds a friend or edits the profile.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from splitwise_mcp.client import SplitwiseClient
from splitwise_mcp.config import Settings
from splitwise_mcp.formatters import ResponseFormat
from splitwise_mcp.tools.expenses import (
    CreateExpenseInput,
    ExpenseIdInput,
    GetExpenseInput,
    splitwise_create_expense,
    splitwise_delete_expense,
    splitwise_get_expense,
)
from splitwise_mcp.tools.groups import (
    CreateGroupInput,
    GetGroupInput,
    GroupIdInput,
    splitwise_create_group,
    splitwise_delete_group,
    splitwise_get_group,
)

_ID_IN_LABEL = re.compile(r"\(id (\d+)\)")
_EXPENSE_HEADING = re.compile(r"^## Expense (\d+):", re.MULTILINE)


@pytest.mark.live_write
async def test_group_and_expense_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    # Writes are enabled on THIS client only — the environment's SPLITWISE_ALLOW_WRITES stays off.
    client = SplitwiseClient(settings=Settings(splitwise_allow_writes=True))
    monkeypatch.setattr("splitwise_mcp.tools.groups.get_client", lambda: client)
    monkeypatch.setattr("splitwise_mcp.tools.expenses.get_client", lambda: client)

    today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    group_name = f"mcp-smoke-{today}"
    group_id: int | None = None
    expense_id: int | None = None
    try:
        created = await splitwise_create_group(CreateGroupInput(name=group_name, group_type="other"))
        assert created.startswith("Created group **"), created[:300]
        assert "members (1)" in created, created[:300]  # nobody but you
        group_id = int(_ID_IN_LABEL.search(created).group(1))  # type: ignore[union-attr]

        # Open item 3 (research/02): a bare date on create — the validator sends T00:00:00Z.
        saved = await splitwise_create_expense(
            CreateExpenseInput(
                cost="1.00", description="mcp smoke (safe to delete)", group_id=group_id, split_equally=True, date=today
            )
        )
        assert saved.startswith("# Expense created"), saved[:300]
        assert "- **errors**: `{}`" in saved, saved[:300]
        expense_id = int(_EXPENSE_HEADING.search(saved).group(1))  # type: ignore[union-attr]

        read_back = json.loads(
            await splitwise_get_expense(GetExpenseInput(expense_id=expense_id, response_format=ResponseFormat.JSON))
        )
        expense = read_back.get("expense") or read_back
        assert expense["group_id"] == group_id
        assert expense["cost"] in ("1.0", "1.00")
        assert expense["date"].startswith(today), expense["date"]
        assert expense.get("deleted_at") is None
        shares = expense.get("users") or []
        assert (
            len(shares) == 1
            and shares[0]["paid_share"] in ("1.0", "1.00")
            and shares[0]["owed_share"] in ("1.0", "1.00")
        )

        deleted = await splitwise_delete_expense(ExpenseIdInput(expense_id=expense_id))
        assert "not confirmed" not in deleted and "UNKNOWN" not in deleted, deleted[:300]
        after = json.loads(
            await splitwise_get_expense(GetExpenseInput(expense_id=expense_id, response_format=ResponseFormat.JSON))
        )
        after_expense = after.get("expense") or after
        assert after_expense.get("deleted_at"), "the expense should read back with deleted_at set"
        expense_id = None  # nothing left to clean

        gone = await splitwise_delete_group(GroupIdInput(group_id=group_id))
        assert gone.startswith("Deleted group id"), gone[:300]
        group_id = None
    finally:
        if expense_id is not None:
            await splitwise_delete_expense(ExpenseIdInput(expense_id=expense_id))
        if group_id is not None:
            await splitwise_delete_group(GroupIdInput(group_id=group_id))

    # The group is gone: reading it must not succeed as a live group.
    listing = await splitwise_get_group(GetGroupInput(group_id=int(_ID_IN_LABEL.search(created).group(1))))  # type: ignore[union-attr]
    assert listing.startswith("Error") or "deleted" in listing.lower(), listing[:300]
