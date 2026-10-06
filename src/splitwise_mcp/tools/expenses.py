"""Splitwise expense tools — list, read, create, update, delete, undelete (inventory D1–D6).

Reads: `splitwise_get_expenses` (filters + offset pagination; deleted expenses hidden by
default and counted) and `splitwise_get_expense` (shares, repayments, comments).

Writes (every one is a POST, so the CLIENT refuses it unless SPLITWISE_ALLOW_WRITES=1 —
these tools never gate): `splitwise_create_expense`, `splitwise_update_expense`,
`splitwise_delete_expense`, `splitwise_undelete_expense`.

Money safety lives here: a create (or an update that sends shares) is checked locally
with `Decimal` arithmetic — Σ paid_share == Σ owed_share == cost — and refused with a
readable error BEFORE any request when the numbers do not close. The flattened
`users__{i}__…` share keys are built in this module so the exact wire body is visible in
the tests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any, Literal, Self

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationInfo, field_validator, model_validator

from splitwise_mcp.client import SplitwiseEnvelopeError, get_client
from splitwise_mcp.errors import handle_api_error
from splitwise_mcp.formatters import ResponseFormat, clip_response, fmt_money, fmt_person, iso_to_human, to_json
from splitwise_mcp.server import mcp
from splitwise_mcp.validators import currency_code, date_iso, money

# Markdown row cap per table, on top of the API `limit` (context-window guard).
MAX_DISPLAY_ROWS = 50

_CENT = Decimal("0.01")
_ZERO = Decimal("0")

RepeatInterval = Literal["never", "weekly", "fortnightly", "monthly", "yearly"]

# Expense fields shared by create and update, in wire order.
_COMMON_FIELDS = (
    "cost",
    "description",
    "details",
    "date",
    "repeat_interval",
    "currency_code",
    "category_id",
    "group_id",
)


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------


class ShareInput(BaseModel):
    """One participant of an expense: who they are, what they paid, what they owe."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_id: PositiveInt | None = Field(
        default=None,
        description=(
            "Splitwise user id of this participant (find it with splitwise_resolve_friend). "
            "Give user_id OR the email+first_name+last_name trio, never both."
        ),
    )
    email: str | None = Field(
        default=None,
        pattern=r"^[^@\s]+@[^@\s]+$",
        description="Email of a participant who has no known user id (Splitwise invites them). Needs first_name and last_name.",
    )
    first_name: str | None = Field(
        default=None, min_length=1, description="First name — only with email, for a participant without a user id."
    )
    last_name: str | None = Field(
        default=None, min_length=1, description="Last name — only with email, for a participant without a user id."
    )
    paid_share: str = Field(
        description="What this person PAID toward the cost, as a 2-decimal string ('30.00'; '0.00' if they paid nothing)."
    )
    owed_share: str = Field(
        description="This person's portion of the cost (what they OWE), as a 2-decimal string ('10.00')."
    )

    @field_validator("paid_share", "owed_share", mode="before")
    @classmethod
    def _share_money(cls, value: object, info: ValidationInfo) -> str:
        return money(value, field=info.field_name or "share", allow_zero=True)

    @model_validator(mode="after")
    def _exactly_one_identity(self) -> Self:
        trio = (self.email, self.first_name, self.last_name)
        if self.user_id is not None:
            if any(part is not None for part in trio):
                raise ValueError("a share takes user_id OR email+first_name+last_name, not both")
        elif not all(trio):
            raise ValueError(
                "a share needs a user_id, or all three of email, first_name and last_name "
                "(for someone without a known user id)"
            )
        return self

    def identity(self) -> str:
        """Human label used in validation messages."""
        return f"user {self.user_id}" if self.user_id is not None else f"<{self.email}>"


class _ExpenseFields(BaseModel):
    """Fields shared by create and update. Create narrows cost/description to required and group_id to default 0."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    cost: str | None = Field(
        default=None, description="Total cost as a 2-decimal string, e.g. '25.50' (never a float)."
    )
    description: str | None = Field(default=None, min_length=1, description="Short title, e.g. 'Dinner at Central'.")
    details: str | None = Field(default=None, description="Free-text notes shown under the expense.")
    date: str | None = Field(
        default=None,
        description="When the expense happened, ISO-8601 ('2026-10-05' or '2026-10-05T13:00:00Z'); default = now.",
    )
    repeat_interval: RepeatInterval | None = Field(
        default=None, description="Recurrence: never, weekly, fortnightly, monthly or yearly."
    )
    currency_code: str | None = Field(
        default=None, description="3-letter currency code (USD, PEN, EUR…); default = your default currency."
    )
    category_id: PositiveInt | None = Field(
        default=None,
        description="Category id — must be a SUBcategory id (see splitwise_get_categories / splitwise_resolve_category).",
    )
    group_id: int | None = Field(
        default=None,
        ge=0,
        description="Group id (splitwise_resolve_group); 0 = a non-group expense between friends.",
    )

    @field_validator("cost", mode="before")
    @classmethod
    def _cost(cls, value: object) -> str | None:
        return None if value is None else money(value, field="cost")

    @field_validator("currency_code", mode="before")
    @classmethod
    def _currency(cls, value: object) -> str | None:
        return None if value is None else currency_code(value)

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, value: object) -> str | None:
        return None if value is None else date_iso(value)

    def common_body(self) -> dict[str, Any]:
        """The expense fields that were given (None dropped), ready for the POST body."""
        body: dict[str, Any] = {}
        for name in _COMMON_FIELDS:
            value = getattr(self, name)
            if value is not None:
                body[name] = value
        return body


class GetExpensesInput(BaseModel):
    """Input for splitwise_get_expenses."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    group_id: PositiveInt | None = Field(
        default=None, description="Only expenses of this group (splitwise_resolve_group). Not with friend_id."
    )
    friend_id: PositiveInt | None = Field(
        default=None,
        description="Only expenses shared with this friend's user id (splitwise_resolve_friend). Not with group_id.",
    )
    dated_after: str | None = Field(default=None, description="Only expenses dated on/after this ISO-8601 date.")
    dated_before: str | None = Field(default=None, description="Only expenses dated on/before this ISO-8601 date.")
    updated_after: str | None = Field(default=None, description="Only expenses updated after this ISO-8601 datetime.")
    updated_before: str | None = Field(default=None, description="Only expenses updated before this ISO-8601 datetime.")
    limit: int = Field(default=20, ge=1, le=100, description="Page size requested from Splitwise (1-100).")
    offset: int = Field(default=0, ge=0, description="How many expenses to skip (use the next offset shown).")
    include_deleted: bool = Field(
        default=False,
        description="Splitwise returns deleted expenses too; they are hidden (and counted) unless this is true.",
    )
    current_user_id: PositiveInt | None = Field(
        default=None,
        description="Your user id (splitwise_health_check) — adds a 'Your net' column (positive = owed to you).",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="markdown (default, a table) or json (the raw expense objects)."
    )

    @field_validator("dated_after", "dated_before", "updated_after", "updated_before", mode="before")
    @classmethod
    def _dates(cls, value: object, info: ValidationInfo) -> str | None:
        return None if value is None else date_iso(value, field=info.field_name or "date")

    @model_validator(mode="after")
    def _group_or_friend(self) -> Self:
        if self.group_id is not None and self.friend_id is not None:
            raise ValueError(
                "pass group_id OR friend_id, not both — Splitwise silently ignores friend_id when group_id is set"
            )
        return self


class ExpenseIdInput(BaseModel):
    """Input for the tools that act on one expense id."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    expense_id: PositiveInt = Field(description="The expense id (from splitwise_get_expenses).")


class GetExpenseInput(ExpenseIdInput):
    """Input for splitwise_get_expense."""

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN, description="markdown (default) or json (the raw expense object)."
    )


class CreateExpenseInput(_ExpenseFields):
    """Input for splitwise_create_expense: the expense fields plus EXACTLY ONE split mode."""

    cost: str = Field(description="Total cost as a 2-decimal string, e.g. '25.50' (never a float).")
    description: str = Field(min_length=1, description="Short title, e.g. 'Dinner at Central'.")
    group_id: int = Field(
        default=0,
        ge=0,
        description="Group id (splitwise_resolve_group); 0 (default) = a non-group expense between friends.",
    )
    split_equally: bool = Field(
        default=False,
        description=(
            "Split mode A: true = YOU paid and the cost is split equally among ALL members of group_id "
            "(requires group_id > 0)."
        ),
    )
    shares: list[ShareInput] | None = Field(
        default=None,
        description=(
            "Split mode B: explicit shares, one per participant (at least 2). "
            "Σ paid_share and Σ owed_share must each equal cost."
        ),
    )
    equal_split_between: list[PositiveInt] | None = Field(
        default=None,
        description=(
            "Split mode C: user ids (at least 2, INCLUDING the payer) that split the cost equally; the payer paid "
            "it all and absorbs the leftover cents (10.00 / 3 → payer owes 3.34, others 3.33)."
        ),
    )
    paid_by_user_id: PositiveInt | None = Field(
        default=None,
        description="Mode C only: who paid (must be in equal_split_between). Default: you (one GET /get_current_user).",
    )

    @model_validator(mode="after")
    def _exactly_one_split_mode(self) -> Self:
        chosen = [
            name
            for name, on in (
                ("split_equally", self.split_equally),
                ("shares", self.shares is not None),
                ("equal_split_between", self.equal_split_between is not None),
            )
            if on
        ]
        if len(chosen) != 1:
            found = ", ".join(chosen) if chosen else "none"
            raise ValueError(
                "choose exactly one split mode — split_equally=true (group expense, you paid), shares=[...] "
                f"(explicit), or equal_split_between=[user ids] — got: {found}"
            )
        if self.paid_by_user_id is not None and self.equal_split_between is None:
            raise ValueError("paid_by_user_id only applies to equal_split_between; with shares, set paid_share instead")
        if self.split_equally and self.group_id == 0:
            raise ValueError(
                "split_equally=true needs group_id > 0 (Splitwise splits among the group's members); "
                "for a non-group expense use equal_split_between or shares"
            )
        if self.shares is not None:
            problem = shares_problem(self.cost, self.shares)
            if problem:
                raise ValueError(problem)
        if self.equal_split_between is not None:
            ids = self.equal_split_between
            if len(ids) < 2:
                raise ValueError("equal_split_between needs at least 2 user ids (including the payer)")
            duplicates = sorted({uid for uid in ids if ids.count(uid) > 1})
            if duplicates:
                raise ValueError(f"equal_split_between lists user id(s) {duplicates} more than once")
            too_small = equal_split_problem(self.cost, len(ids))
            if too_small:
                raise ValueError(too_small)
            if self.paid_by_user_id is not None and self.paid_by_user_id not in ids:
                raise ValueError(
                    f"paid_by_user_id {self.paid_by_user_id} is not in equal_split_between {ids} — include the "
                    "payer, or use explicit shares if the payer owes nothing"
                )
        return self


class UpdateExpenseInput(_ExpenseFields):
    """Input for splitwise_update_expense: only the fields to change."""

    expense_id: PositiveInt = Field(description="The expense to change (from splitwise_get_expenses).")
    shares: list[ShareInput] | None = Field(
        default=None,
        description=(
            "WARNING: REPLACES ALL existing shares — list EVERY participant, not just the one that changed. "
            "Σ paid_share and Σ owed_share must each equal the new cost (or the current cost if cost is omitted)."
        ),
    )

    @model_validator(mode="after")
    def _something_to_change(self) -> Self:
        if not self.common_body() and self.shares is None:
            raise ValueError(
                "nothing to update — pass at least one of cost, description, details, date, repeat_interval, "
                "currency_code, category_id, group_id or shares"
            )
        if self.shares is not None:
            # With a new cost the sums are checked here; without one the tool checks them
            # against the expense's current cost (fetched once) before the POST.
            problem = shares_problem(self.cost, self.shares)
            if problem:
                raise ValueError(problem)
        return self


# ---------------------------------------------------------------------------
# Money helpers (pure — unit-tested directly)
# ---------------------------------------------------------------------------


def shares_problem(cost: str | None, shares: list[ShareInput]) -> str | None:
    """Return why `shares` cannot be sent for `cost`, or None when they are valid.

    Structural checks always run (at least 2 shares, nobody twice); the sum check runs
    when `cost` is known: Σ paid_share == cost and Σ owed_share == cost, in `Decimal`.
    """
    if len(shares) < 2:
        return "shares needs at least 2 participants (the payer and whoever owes them)"
    seen: set[str] = set()
    for share in shares:
        key = f"id:{share.user_id}" if share.user_id is not None else f"email:{(share.email or '').casefold()}"
        if key in seen:
            return f"{share.identity()} appears more than once in shares"
        seen.add(key)
    if cost is None:
        return None
    total = Decimal(cost)
    paid = sum((Decimal(share.paid_share) for share in shares), _ZERO)
    owed = sum((Decimal(share.owed_share) for share in shares), _ZERO)
    mismatches: list[str] = []
    if paid != total:
        mismatches.append(f"paid shares sum to {paid:.2f} but cost is {total:.2f} (off by {paid - total:+.2f})")
    if owed != total:
        mismatches.append(f"owed shares sum to {owed:.2f} but cost is {total:.2f} (off by {owed - total:+.2f})")
    if not mismatches:
        return None
    return (
        "shares do not add up: "
        + "; ".join(mismatches)
        + ". Σ paid_share and Σ owed_share must each equal cost exactly."
    )


def equal_split_problem(cost: str, count: int) -> str | None:
    """Why `cost` cannot be split equally between `count` people, or None (needs ≥ 0.01 each)."""
    if Decimal(cost) < _CENT * count:
        return (
            f"cost too small to split among {count} people — {Decimal(cost):.2f} is less than 0.01 each; "
            "use explicit shares instead"
        )
    return None


def equal_split(cost: str, user_ids: list[int], payer_id: int) -> list[ShareInput]:
    """Split `cost` equally between `user_ids`; the payer paid everything and absorbs the leftover cents.

    Each owed share is cost / n rounded DOWN to the cent; the remainder (0 ≤ r < n cents)
    goes on the payer's owed share so both sums close exactly: 10.00 / 3 → 3.34 (payer),
    3.33, 3.33. Raises `ValueError` when the cost is under one cent per person (the
    non-payers would get 0.00/0.00 shares).
    """
    problem = equal_split_problem(cost, len(user_ids))
    if problem:
        raise ValueError(problem)
    total = Decimal(cost)
    count = len(user_ids)
    base = (total / count).quantize(_CENT, rounding=ROUND_DOWN)
    remainder = total - base * count
    shares: list[ShareInput] = []
    for uid in user_ids:
        is_payer = uid == payer_id
        shares.append(
            ShareInput(
                user_id=uid,
                paid_share=f"{(total if is_payer else _ZERO):.2f}",
                owed_share=f"{(base + remainder if is_payer else base):.2f}",
            )
        )
    return shares


def flatten_shares(shares: list[ShareInput]) -> dict[str, Any]:
    """Build the `users__{i}__…` keys Splitwise expects (index from 0, identity + paid/owed)."""
    flat: dict[str, Any] = {}
    for index, share in enumerate(shares):
        prefix = f"users__{index}__"
        if share.user_id is not None:
            flat[prefix + "user_id"] = share.user_id
        else:
            flat[prefix + "email"] = share.email
            flat[prefix + "first_name"] = share.first_name
            flat[prefix + "last_name"] = share.last_name
        flat[prefix + "paid_share"] = share.paid_share
        flat[prefix + "owed_share"] = share.owed_share
    return flat


def _two_places(value: Any) -> str | None:
    """Normalise an API money string ('10.0') to '10.00'; None when it is not a finite decimal."""
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite():
        return None
    return f"{dec.quantize(_CENT):f}"


def _decimal_or_zero(value: Any) -> Decimal:
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return _ZERO
    return dec if dec.is_finite() else _ZERO


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _cell(value: Any) -> str:
    """Make a value safe inside a markdown table cell."""
    text = "" if value is None else str(value)
    return " ".join(text.replace("|", "\\|").split())


def _person(value: Any) -> str:
    """Render a user object, or a bare user id, as `First Last (id N)`."""
    if isinstance(value, Mapping):
        return fmt_person(value)
    if value in (None, ""):
        return "N/A"
    return f"user {value}"


def _group_label(group_id: Any) -> str:
    return "no group" if group_id in (None, 0, "0") else f"group {group_id}"


def _share_user_id(share: Mapping[str, Any]) -> Any:
    user = share.get("user")
    if share.get("user_id") is not None:
        return share.get("user_id")
    return user.get("id") if isinstance(user, Mapping) else None


def _share_person(share: Mapping[str, Any]) -> str:
    user = share.get("user")
    return fmt_person(user if isinstance(user, Mapping) else None, fallback_id=_share_user_id(share))


def _payers(expense: Mapping[str, Any]) -> str:
    """Who paid: shares with paid_share > 0 (amounts shown only when several people paid)."""
    shares = [s for s in expense.get("users") or [] if isinstance(s, Mapping)]
    paid = [s for s in shares if _decimal_or_zero(s.get("paid_share")) > 0]
    if not paid:
        return "N/A"
    if len(paid) == 1:
        return _share_person(paid[0])
    currency = str(expense.get("currency_code") or "")
    return "; ".join(f"{_share_person(s)} {fmt_money(s.get('paid_share'), currency, signed=False)}" for s in paid)


def _your_net(expense: Mapping[str, Any], user_id: int) -> str:
    for share in expense.get("users") or []:
        if isinstance(share, Mapping) and str(_share_user_id(share)) == str(user_id):
            return fmt_money(share.get("net_balance"), str(expense.get("currency_code") or ""))
    return "not involved"


def _kind_prefix(expense: Mapping[str, Any]) -> str:
    tags = []
    if expense.get("deleted_at"):
        tags.append("[deleted]")
    if expense.get("payment"):
        tags.append("[settle-up]")
    return (" ".join(tags) + " ") if tags else ""


def _shares_table(expense: Mapping[str, Any]) -> list[str]:
    currency = str(expense.get("currency_code") or "")
    shares = [s for s in expense.get("users") or [] if isinstance(s, Mapping)]
    if not shares:
        return ["_No shares returned._"]
    lines = ["| Person | Paid | Owed | Net |", "|---|---|---|---|"]
    for share in shares[:MAX_DISPLAY_ROWS]:
        lines.append(
            f"| {_cell(_share_person(share))} | {fmt_money(share.get('paid_share'), currency, signed=False)} | "
            f"{fmt_money(share.get('owed_share'), currency, signed=False)} | "
            f"{fmt_money(share.get('net_balance'), currency)} |"
        )
    if len(shares) > MAX_DISPLAY_ROWS:
        lines.append(f"_{len(shares) - MAX_DISPLAY_ROWS} more share(s) not shown — use response_format=json._")
    return lines


def _post_body(resp: Any) -> dict[str, Any] | None:
    """The JSON object a write answered with, or None when the body is not a readable JSON object.

    A 200 with an unreadable body must not surface as a generic parse failure: the write
    may well have happened, so the caller reports the outcome as UNKNOWN instead.
    """
    try:
        body = resp.json()
    except ValueError:  # json.JSONDecodeError is a ValueError
        return None
    return body if isinstance(body, dict) else None


def _unknown_outcome(read_back: str) -> str:
    return (
        "Error: Splitwise answered without a readable body — outcome UNKNOWN; read back with "
        f"{read_back} before retrying (a blind retry can duplicate or repeat the change)."
    )


def _with_partial_outcome(message: str, exc: Exception, read_back: str) -> str:
    """Append what Splitwise still returned when a 200 carried errors (a partial outcome).

    The client raises `SplitwiseEnvelopeError` on a non-empty `errors`, but the same body
    can carry `expenses: [...]` — an expense that DID land. Saying only "rejected" would
    make the LLM retry and duplicate it.
    """
    if not isinstance(exc, SplitwiseEnvelopeError):
        return message
    expenses = [e for e in exc.body.get("expenses") or [] if isinstance(e, dict)]
    if not expenses:
        return message
    ids = ", ".join(str(e.get("id")) for e in expenses)
    return (
        f"{message}\n- Splitwise still returned {len(expenses)} expense object(s) (id {ids}) alongside the "
        f"errors — read back with {read_back} before retrying; the change may have landed."
    )


_SAVED_VERBS = {"create": "created", "update": "updated"}
_SAVED_READ_BACK = {"create": "splitwise_get_expenses", "update": "splitwise_get_expense"}


def _render_saved(action: Literal["create", "update"], body: Mapping[str, Any]) -> str:
    """Confirmation for create/update: echo only what Splitwise returned.

    The header claims `created`/`updated` only when Splitwise returned an expense object;
    otherwise the outcome is reported as NOT confirmed.
    """
    errors = body.get("errors")
    errors_text = "absent" if "errors" not in body else f"`{json.dumps(errors)}`"
    expenses = [e for e in body.get("expenses") or [] if isinstance(e, Mapping)]
    if not expenses:
        return "\n".join(
            [
                f"# Expense {action} — outcome NOT confirmed",
                "",
                f"- **errors**: {errors_text}",
                f"- Splitwise returned no expense object. Read it back with {_SAVED_READ_BACK[action]} before "
                "retrying (a blind retry can duplicate the expense).",
            ]
        )
    lines = [f"# Expense {_SAVED_VERBS[action]}", "", f"- **errors**: {errors_text}"]
    lines.append(f"- **expenses returned**: {len(expenses)}")
    for expense in expenses:
        currency = str(expense.get("currency_code") or "")
        lines.extend(
            [
                "",
                f"## Expense {expense.get('id', 'N/A')}: {_cell(expense.get('description') or '(no description)')}",
                f"- **cost**: {fmt_money(expense.get('cost'), currency, signed=False)}",
                f"- **group**: {_group_label(expense.get('group_id'))}",
                f"- **date**: {iso_to_human(expense.get('date'))}",
                "",
                "Shares as returned (Net = paid − owed; positive = that person is owed money):",
                "",
                *_shares_table(expense),
            ]
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    name="splitwise_get_expenses",
    annotations=ToolAnnotations(
        title="List Splitwise Expenses",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_expenses(params: GetExpensesInput) -> str:
    """List expenses, newest first, optionally filtered by group, friend and date range.

    Calls `GET /get_expenses`. Splitwise includes DELETED expenses in this list; they are
    hidden by default and the response says how many were hidden (`include_deleted=true`
    shows them, tagged `[deleted]`). Settle-up payments are tagged `[settle-up]`. Each row
    shows the date, description, full cost, group, who paid (shares with paid_share > 0)
    and — when `current_user_id` is given — your net on that expense.

    Sign convention: "Your net" is your `net_balance` on the expense; positive = owed to
    you (you paid more than your share), negative = you owe.

    When to Use:
    - "What did we spend in the Lima trip group?" (group_id) or "expenses with Ana" (friend_id).
    - Finding an expense id before reading, updating or deleting it.
    - Reviewing recent activity in a date window (dated_after / dated_before).

    When NOT to Use:
    - To see one expense's full split, repayments and comments (use `splitwise_get_expense`).
    - To get balances per friend or per group (use `splitwise_get_balances` / `splitwise_get_group_balances`).
    - To turn a group or friend NAME into an id (use `splitwise_resolve_group` / `splitwise_resolve_friend`).

    Returns:
    Markdown: a table `| id | Date | Description | Cost | Group | Paid by | (Your net) |`
    plus the hidden-deleted count and the next offset. JSON: `{count, returned_by_api,
    hidden_deleted, limit, offset, has_more, next_offset, expenses: [raw expense objects]}`.

    Pagination:
    `limit` (1-100, default 20) is sent to Splitwise; `has_more` is true when Splitwise
    returned a full page, and the next offset is `offset + rows returned by Splitwise`
    (deleted rows count toward it even when hidden). Markdown shows at most 50 rows; the
    note under the table gives the offset to continue from.

    Examples:
    - params = {"group_id": 12345, "dated_after": "2026-09-01"}
    - params = {"friend_id": 678, "limit": 50, "current_user_id": 491923}
    - params = {"limit": 20, "offset": 20, "response_format": "json"}

    Error Handling:
    Passing both group_id and friend_id is refused (Splitwise would ignore friend_id). 401
    → API key missing/regenerated; 403 → you are not a member of that group; 429 → back
    off for Retry-After seconds.
    """
    try:
        query = {
            "group_id": params.group_id,
            "friend_id": params.friend_id,
            "dated_after": params.dated_after,
            "dated_before": params.dated_before,
            "updated_after": params.updated_after,
            "updated_before": params.updated_before,
            "limit": params.limit,
            "offset": params.offset,
        }
        query = {key: value for key, value in query.items() if value is not None}
        resp = await get_client().request("GET", "/get_expenses", params=query)
        raw = resp.json().get("expenses")
        expenses: list[dict[str, Any]] = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
        returned = len(expenses)
        visible = expenses if params.include_deleted else [e for e in expenses if not e.get("deleted_at")]
        hidden = returned - len(visible)
        has_more = returned == params.limit
        next_offset = params.offset + returned

        if params.response_format is ResponseFormat.JSON:
            return clip_response(
                to_json(
                    {
                        "count": len(visible),
                        "returned_by_api": returned,
                        "hidden_deleted": hidden,
                        "include_deleted": params.include_deleted,
                        "limit": params.limit,
                        "offset": params.offset,
                        "has_more": has_more,
                        "next_offset": next_offset if has_more else None,
                        "expenses": visible,
                    }
                )
            )

        lines = [
            "# Splitwise expenses",
            "",
            f"Splitwise returned **{returned}** expense(s) at offset {params.offset} (limit {params.limit}).",
        ]
        if params.include_deleted:
            lines.append("Deleted expenses are included and tagged `[deleted]`.")
        else:
            lines.append(
                f"Deleted expenses hidden: **{hidden}** (Splitwise lists them with `deleted_at` set; "
                "pass include_deleted=true to see them)."
            )
        if has_more:
            lines.append(f"More available — next offset → **{next_offset}**.")
        show_net = params.current_user_id is not None
        if show_net:
            lines.append("Your net: positive = owed to you, negative = you owe.")
        else:
            lines.append("Pass current_user_id (see splitwise_health_check) to add your net per expense.")
        lines.append("")
        if not visible:
            lines.append("_No expenses._")
            return clip_response("\n".join(lines))

        header = "| id | Date | Description | Cost | Group | Paid by |"
        rule = "|---|---|---|---|---|---|"
        if show_net:
            header += " Your net |"
            rule += "---|"
        lines.extend([header, rule])
        shown = 0
        for raw_index, expense in enumerate(expenses):
            if expense.get("deleted_at") and not params.include_deleted:
                continue
            if shown == MAX_DISPLAY_ROWS:
                remaining = len(visible) - shown
                lines.append(
                    f"\n_{remaining} more expense(s) on this page not shown (markdown cap {MAX_DISPLAY_ROWS}) — "
                    f"continue with offset={params.offset + raw_index}, or use response_format=json._"
                )
                break
            currency = str(expense.get("currency_code") or "")
            row = (
                f"| {expense.get('id', 'N/A')} | {iso_to_human(expense.get('date'))} | "
                f"{_cell(_kind_prefix(expense) + str(expense.get('description') or ''))} | "
                f"{fmt_money(expense.get('cost'), currency, signed=False)} | {_group_label(expense.get('group_id'))} | "
                f"{_cell(_payers(expense))} |"
            )
            if params.current_user_id is not None:
                row += f" {_your_net(expense, params.current_user_id)} |"
            lines.append(row)
            shown += 1
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_get_expense",
    annotations=ToolAnnotations(
        title="Get Splitwise Expense",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_expense(params: GetExpenseInput) -> str:
    """Read one expense in full: cost, group, category, every share, repayments and comments.

    Calls `GET /get_expense/{id}`. The shares table shows what each person paid, owed and
    their net on this expense; repayments say who owes whom for it; comments are listed
    with author and time. A deleted expense is still readable and is flagged as deleted.

    Sign convention: Net = paid − owed; positive = that person is owed money on this
    expense, negative = they owe.

    When to Use:
    - Before `splitwise_update_expense`, to see the current shares (an update with shares
      replaces ALL of them).
    - "Who paid for dinner and how was it split?"
    - Checking that a create/update landed the way it was intended.

    When NOT to Use:
    - To list or search expenses (use `splitwise_get_expenses`).
    - To read only the comments thread (use `splitwise_get_comments`).
    - For overall balances (use `splitwise_get_balances`).

    Returns:
    Markdown: header fields, a `| Person | Paid | Owed | Net |` table, repayments
    `From → To amount`, and a comments table. JSON: the raw expense object.

    Examples:
    - params = {"expense_id": 3456789012}
    - params = {"expense_id": 3456789012, "response_format": "json"}

    Error Handling:
    404 → wrong id; 403 → you are not part of that expense; 401 → API key problem.
    """
    try:
        resp = await get_client().request("GET", f"/get_expense/{params.expense_id}")
        raw = resp.json().get("expense")
        expense: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if not expense:
            return f"Error: Splitwise returned no expense object for id {params.expense_id}."
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json(expense))

        currency = str(expense.get("currency_code") or "")
        category = expense.get("category")
        category_text = (
            f"{category.get('name', 'N/A')} (id {category.get('id', 'N/A')})"
            if isinstance(category, Mapping)
            else "N/A"
        )
        lines = [
            f"# Expense {expense.get('id', params.expense_id)}: {_cell(expense.get('description') or '(no description)')}",
            "",
            f"- **cost**: {fmt_money(expense.get('cost'), currency, signed=False)}",
            f"- **date**: {iso_to_human(expense.get('date'))}",
            f"- **group**: {_group_label(expense.get('group_id'))}",
            f"- **category**: {category_text}",
            f"- **type**: {'settle-up payment' if expense.get('payment') else 'expense'}",
            f"- **repeats**: {expense.get('repeat_interval') or 'never'}",
        ]
        if expense.get("details"):
            lines.append(f"- **details**: {_cell(expense.get('details'))}")
        lines.append(
            f"- **created**: {iso_to_human(expense.get('created_at'))} by {_person(expense.get('created_by'))}"
        )
        if expense.get("updated_by"):
            lines.append(
                f"- **updated**: {iso_to_human(expense.get('updated_at'))} by {_person(expense.get('updated_by'))}"
            )
        if expense.get("deleted_at"):
            lines.append(
                f"- **DELETED**: {iso_to_human(expense.get('deleted_at'))} by {_person(expense.get('deleted_by'))} "
                "— restore with splitwise_undelete_expense"
            )

        lines.extend(["", "## Shares", "Net = paid − owed (positive = that person is owed money).", ""])
        lines.extend(_shares_table(expense))

        names = {str(_share_user_id(s)): _share_person(s) for s in expense.get("users") or [] if isinstance(s, Mapping)}
        repayments = [r for r in expense.get("repayments") or [] if isinstance(r, Mapping)]
        lines.extend(["", "## Repayments (who owes whom for this expense)", ""])
        if repayments:
            for repayment in repayments[:MAX_DISPLAY_ROWS]:
                debtor = names.get(str(repayment.get("from")), f"user {repayment.get('from')}")
                creditor = names.get(str(repayment.get("to")), f"user {repayment.get('to')}")
                amount = fmt_money(repayment.get("amount"), currency, signed=False)
                lines.append(f"- {debtor} → {creditor}: {amount}")
        else:
            lines.append("_None._")

        comments = [c for c in expense.get("comments") or [] if isinstance(c, Mapping)]
        lines.extend(["", f"## Comments ({expense.get('comments_count', len(comments))})", ""])
        if comments:
            lines.extend(["| When | Author | Type | Content |", "|---|---|---|---|"])
            for comment in comments[:MAX_DISPLAY_ROWS]:
                lines.append(
                    f"| {iso_to_human(comment.get('created_at'))} | {_cell(_person(comment.get('user')))} | "
                    f"{comment.get('comment_type') or 'N/A'} | {_cell(comment.get('content'))} |"
                )
            if len(comments) > MAX_DISPLAY_ROWS:
                lines.append(f"_{len(comments) - MAX_DISPLAY_ROWS} more comment(s) — use splitwise_get_comments._")
        else:
            lines.append("_None._")
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_create_expense",
    annotations=ToolAnnotations(
        title="Create Splitwise Expense",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def splitwise_create_expense(params: CreateExpenseInput) -> str:
    """Create an expense with exactly one split mode; the shares are checked locally before sending.

    Calls `POST /create_expense`. Refused with `Error: … writes are disabled` unless
    `SPLITWISE_ALLOW_WRITES=1` (the server's client enforces it; this tool never bypasses
    it). Undo with `splitwise_delete_expense`. NOT idempotent: every successful call
    creates a new expense — never retry blindly after a timeout; list first.

    Pick EXACTLY ONE split mode:
    - A `split_equally: true` — a GROUP expense (group_id > 0) that YOU paid, split
      equally among all group members by Splitwise.
    - B `shares: [...]` — explicit shares, one per participant (≥ 2), each with
      `user_id` OR `email`+`first_name`+`last_name`, a `paid_share` and an `owed_share`
      (2-decimal strings). Σ paid_share and Σ owed_share must each equal `cost`.
    - C `equal_split_between: [user ids]` — equal split between these people (≥ 2,
      INCLUDING the payer). The payer is `paid_by_user_id`, or you when omitted (one
      `GET /get_current_user`). Owed shares are cost / n rounded down to the cent; the
      leftover cents go on the payer so the sums close (10.00 / 3 → 3.34 payer, 3.33, 3.33).

    Shares that do not add up are refused with an error naming the mismatch BEFORE any
    request. Shares are sent flattened as `users__{i}__user_id|email|first_name|last_name|
    paid_share|owed_share`. Money is a 2-decimal string, never a float.

    When to Use:
    - "Add 90.00 for dinner in the Cusco group, split equally" (mode A).
    - "I paid 30.00 for a taxi with Ana and Luis, split three ways" (mode C).
    - "Ana paid 50.00; I owe 20.00 of it and she owes 30.00" (mode B).

    When NOT to Use:
    - To change an existing expense (use `splitwise_update_expense`).
    - To find a friend's, group's or category's id by name (use `splitwise_resolve_friend`,
      `splitwise_resolve_group`, `splitwise_resolve_category` first).
    - To comment on an expense (use `splitwise_create_comment`).

    Returns:
    A confirmation echoing Splitwise's response: the literal `errors` value (empty `{}`
    means accepted), and per returned expense its id, description, cost, group, date and
    the shares exactly as Splitwise stored them.

    Examples:
    - params = {"cost": "90.00", "description": "Dinner", "group_id": 12345, "split_equally": true}
    - params = {"cost": "30.00", "description": "Taxi", "equal_split_between": [491923, 678, 910]}
    - params = {"cost": "50.00", "description": "Groceries", "currency_code": "PEN", "shares": [
        {"user_id": 678, "paid_share": "50.00", "owed_share": "30.00"},
        {"user_id": 491923, "paid_share": "0.00", "owed_share": "20.00"}]}

    Error Handling:
    Local refusals (no request made): more or fewer than one split mode, shares that do not
    sum to cost, a share with both or neither identity, duplicate participants, split_equally
    without a group, a payer missing from equal_split_between. Splitwise answers 200 even
    when it rejects an expense — the client turns a non-empty `errors` into
    `Error: Splitwise rejected the request…`. On a timeout or 5xx the outcome is UNKNOWN:
    check `splitwise_get_expenses` before retrying.
    """
    try:
        client = get_client()
        body = params.common_body()
        if params.split_equally:
            body["split_equally"] = True
        else:
            shares = params.shares
            if params.equal_split_between is not None:
                payer = params.paid_by_user_id
                if payer is None:
                    me = await client.request("GET", "/get_current_user")
                    user = me.json().get("user")
                    payer_id = user.get("id") if isinstance(user, dict) else None
                    if not isinstance(payer_id, int) or isinstance(payer_id, bool):
                        return "Error: could not read your user id from /get_current_user — pass paid_by_user_id."
                    payer = payer_id
                if payer not in params.equal_split_between:
                    return (
                        f"Error: the payer (user id {payer}{', you' if params.paid_by_user_id is None else ''}) is not "
                        f"in equal_split_between {params.equal_split_between}. Include the payer, or use explicit "
                        "shares if the payer owes nothing. Nothing was sent."
                    )
                shares = equal_split(params.cost, params.equal_split_between, payer)
            if shares is None:  # unreachable: the model enforces exactly one mode
                return "Error: no split mode given — pass split_equally, shares or equal_split_between."
            problem = shares_problem(params.cost, shares)
            if problem:
                return f"Error: {problem} Nothing was sent."
            body.update(flatten_shares(shares))
        resp = await client.request("POST", "/create_expense", data=body)
        saved = _post_body(resp)
        if saved is None:
            return _unknown_outcome("splitwise_get_expenses")
        return clip_response(_render_saved("create", saved))
    except Exception as exc:
        return _with_partial_outcome(handle_api_error(exc), exc, "splitwise_get_expenses")


@mcp.tool(
    name="splitwise_update_expense",
    annotations=ToolAnnotations(
        title="Update Splitwise Expense",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_update_expense(params: UpdateExpenseInput) -> str:
    """Change an existing expense — only the fields given are sent.

    Calls `POST /update_expense/{id}`. Refused with `Error: … writes are disabled` unless
    `SPLITWISE_ALLOW_WRITES=1` (enforced by the server's client).

    WARNING: `shares` REPLACES ALL existing shares. Splitwise treats any `users__*` key as
    the complete new split, so list EVERY participant with their paid_share and
    owed_share — sending only the person who changed drops everyone else from the
    expense. Read the current split first with `splitwise_get_expense`.

    No undo tool — to revert, re-send the previous values read with
    `splitwise_get_expense` first.

    When `shares` is given, Σ paid_share and Σ owed_share must each equal the cost: the new
    `cost` if you pass one, otherwise the expense's current cost (read once with
    `GET /get_expense/{id}`). A mismatch is refused before the update is sent. Changing
    `cost` WITHOUT `shares` leaves the split to Splitwise, which may reject it if the
    current shares no longer add up — pass both to be explicit.

    When to Use:
    - Fix a description, date, category or currency.
    - Change the amount together with the full new split.
    - Move an expense to another group (group_id; 0 = no group).

    When NOT to Use:
    - To create a new expense (use `splitwise_create_expense`).
    - To delete or restore one (use `splitwise_delete_expense` / `splitwise_undelete_expense`).
    - To look up ids by name (use `splitwise_resolve_friend` / `splitwise_resolve_group` /
      `splitwise_resolve_category`).

    Returns:
    A confirmation echoing Splitwise's response: the literal `errors` value and the expense
    as returned (id, description, cost, group, date, shares).

    Examples:
    - params = {"expense_id": 3456789012, "description": "Dinner (incl. tip)"}
    - params = {"expense_id": 3456789012, "cost": "60.00", "shares": [
        {"user_id": 491923, "paid_share": "60.00", "owed_share": "30.00"},
        {"user_id": 678, "paid_share": "0.00", "owed_share": "30.00"}]}

    Error Handling:
    Refused locally (nothing sent) when no field is given, when shares do not add up, or a
    share has both/neither identity. Splitwise answers 200 even on failure — the client turns
    a non-empty `errors` into `Error: Splitwise rejected the request…`. 404 → wrong id; 403 →
    not your expense.
    """
    try:
        client = get_client()
        body = params.common_body()
        if params.shares is not None:
            cost = params.cost
            if cost is None:
                current = await client.request("GET", f"/get_expense/{params.expense_id}")
                expense = current.json().get("expense")
                cost = _two_places(expense.get("cost")) if isinstance(expense, dict) else None
                if cost is None:
                    return (
                        f"Error: could not read the current cost of expense {params.expense_id} to check the shares — "
                        "pass cost explicitly. Nothing was sent."
                    )
            problem = shares_problem(cost, params.shares)
            if problem:
                source = "the new cost" if params.cost is not None else f"the expense's current cost {cost}"
                return f"Error: {problem} (checked against {source}). Nothing was sent."
            body.update(flatten_shares(params.shares))
        resp = await client.request("POST", f"/update_expense/{params.expense_id}", data=body)
        saved = _post_body(resp)
        if saved is None:
            return _unknown_outcome(f"splitwise_get_expense (expense_id={params.expense_id})")
        return clip_response(_render_saved("update", saved))
    except Exception as exc:
        return _with_partial_outcome(
            handle_api_error(exc), exc, f"splitwise_get_expense (expense_id={params.expense_id})"
        )


@mcp.tool(
    name="splitwise_delete_expense",
    annotations=ToolAnnotations(
        title="Delete Splitwise Expense",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_delete_expense(params: ExpenseIdInput) -> str:
    """Delete an expense (Splitwise keeps it as deleted; restore with `splitwise_undelete_expense`).

    Calls `POST /delete_expense/{id}`. Refused with `Error: … writes are disabled` unless
    `SPLITWISE_ALLOW_WRITES=1` (enforced by the server's client). Deleting changes every
    participant's balances. Restore with `splitwise_undelete_expense` and the same id.

    When to Use:
    - Remove a duplicate or mistaken expense.

    When NOT to Use:
    - To fix an amount or split (use `splitwise_update_expense`).
    - To remove a whole group with its expenses (use `splitwise_delete_group`).

    Returns:
    The `success` value Splitwise returned for the deletion, and the undo hint.

    Examples:
    - params = {"expense_id": 3456789012}

    Error Handling:
    Splitwise answers 200 even when it refuses — the client turns `success: false` / a
    non-empty `errors` into `Error: Splitwise rejected the request…`. 404 → wrong id; 403 →
    not your expense.
    """
    try:
        resp = await get_client().request("POST", f"/delete_expense/{params.expense_id}")
        body = _post_body(resp)
        if body is None:
            return _unknown_outcome(f"splitwise_get_expense (expense_id={params.expense_id})")
        success = body.get("success")
        if success is True:
            return (
                f"Expense {params.expense_id} deleted — Splitwise answered `success: true`. "
                f"Restore it with splitwise_undelete_expense (expense_id={params.expense_id})."
            )
        return (
            f"Splitwise answered `success: {json.dumps(success)}` for deleting expense {params.expense_id}; the outcome "
            "is not confirmed — read it back with splitwise_get_expense before retrying."
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_undelete_expense",
    annotations=ToolAnnotations(
        title="Restore Deleted Splitwise Expense",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_undelete_expense(params: ExpenseIdInput) -> str:
    """Restore a deleted expense.

    Calls `POST /undelete_expense/{id}`. Refused with `Error: … writes are disabled` unless
    `SPLITWISE_ALLOW_WRITES=1` (enforced by the server's client). The expense comes back
    with its original shares, so balances change again.

    When to Use:
    - Undo a `splitwise_delete_expense`.
    - Bring back an expense seen as `[deleted]` in `splitwise_get_expenses` with include_deleted=true.

    When NOT to Use:
    - On an expense that is not deleted (nothing to restore).
    - To restore a deleted group (use `splitwise_undelete_group`).

    Returns:
    The `success` value Splitwise returned.

    Examples:
    - params = {"expense_id": 3456789012}

    Error Handling:
    Splitwise answers 200 even when it refuses — the client turns `success: false` into
    `Error: Splitwise rejected the request…`. 404 → wrong id; 403 → not your expense.
    """
    try:
        resp = await get_client().request("POST", f"/undelete_expense/{params.expense_id}")
        body = _post_body(resp)
        if body is None:
            return _unknown_outcome(f"splitwise_get_expense (expense_id={params.expense_id})")
        success = body.get("success")
        if success is True:
            return f"Expense {params.expense_id} restored — Splitwise answered `success: true`."
        return (
            f"Splitwise answered `success: {json.dumps(success)}` for restoring expense {params.expense_id}; the "
            "outcome is not confirmed — read it back with splitwise_get_expense."
        )
    except Exception as exc:
        return handle_api_error(exc)
