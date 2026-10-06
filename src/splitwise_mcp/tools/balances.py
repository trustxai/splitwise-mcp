"""Splitwise `balances` tools: derived balance summaries (no endpoints of their own).

Endpoints (research/02 §Derived tools): `splitwise_get_balances` reads `GET /get_friends`
(C1: each friend carries `balance: [{currency_code, amount}]`) and
`splitwise_get_group_balances` reads `GET /get_group/{id}` (B2: `members[].balance`,
`simplified_debts`, `original_debts`). Both are read-only; every figure is computed
client-side from what those two endpoints return.

Sign conventions (stated again in each docstring):
- friend balances: **positive = the friend owes you** (owed to you), negative = you owe them;
- group member balances: **positive = the member is owed by the group**, negative = they owe.

Amounts are parsed defensively: anything that is not a finite decimal below 1e15
(`"1,000.00"`, `"NaN"`, `"1e30"`), and any entry without a `currency_code`, is shown raw
and flagged, and is kept OUT of the totals — never silently counted as zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

from splitwise_mcp.client import get_client
from splitwise_mcp.errors import handle_api_error
from splitwise_mcp.formatters import ResponseFormat, clip_response, fmt_money, fmt_person, to_json
from splitwise_mcp.server import mcp
from splitwise_mcp.validators import currency_code

MAX_DISPLAY_ROWS = 50

FRIEND_SIGN_CONVENTION = "Sign convention: positive = the friend owes you; negative = you owe the friend."
GROUP_SIGN_CONVENTION = (
    "Sign convention: positive = the member is owed by the group; negative = the member owes the group."
)

_TWO_PLACES = Decimal("0.01")
# Above this an amount is not a plausible balance, and `quantize` would overflow the
# default 28-digit context (`"1e30"` raised InvalidOperation and failed the whole tool).
_MAX_MAGNITUDE = Decimal("1e15")


# -- input models --------------------------------------------------------------


class GetBalancesInput(BaseModel):
    """Input for `splitwise_get_balances`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    currency: str | None = Field(
        default=None,
        description="Only consider balances in this 3-letter currency code (e.g. 'USD', 'PEN'); "
        "omit for every currency.",
    )
    include_zero: bool = Field(
        default=False,
        description="When true, also list friends with no non-zero balance in scope (settled up, or — with "
        "`currency` — no balance in that currency).",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default) for the summary + table, or 'json' for the computed totals plus "
        "the matching raw friend objects.",
    )

    @field_validator("currency", mode="before")
    @classmethod
    def _normalise_currency(cls, value: Any) -> Any:
        if value is None:
            return None
        return currency_code(value, field="currency")


class GetGroupBalancesInput(BaseModel):
    """Input for `splitwise_get_group_balances`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    group_id: int = Field(
        ...,
        ge=0,
        description="The group id (from `splitwise_get_groups` / `splitwise_resolve_group`). "
        "0 = the non-group-expenses pseudo-group (unverified on /get_group/0 until the live smoke; "
        "on a 404 use `splitwise_get_balances`).",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default) for members + debts, or 'json' for the raw members and debts "
        "with the debts source named.",
    )

    @field_validator("group_id", mode="before")
    @classmethod
    def _reject_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("group_id must be an integer group id, not a boolean")
        return value


# -- helpers ---------------------------------------------------------------------


def _parse_amount(amount: Any) -> Decimal | None:
    """A finite decimal with magnitude below 1e15, or None (unparsable, NaN/Infinity, absurd)."""
    try:
        dec = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError):
        return None
    if not dec.is_finite() or abs(dec) >= _MAX_MAGNITUDE:
        return None
    return dec


def _wire(amount: Decimal) -> str:
    """Two-decimal string, as Splitwise writes money."""
    return f"{amount.quantize(_TWO_PLACES):f}"


@dataclass(frozen=True)
class _Entry:
    """One non-zero (or unparsable) `{currency_code, amount}` balance entry."""

    code: str  # upper-cased; "" when the API sent none
    amount: Decimal | None  # None when the raw amount could not be parsed
    raw: str

    @property
    def countable(self) -> bool:
        """Whether the entry can go into the per-currency totals."""
        return self.amount is not None and bool(self.code)

    @property
    def reason(self) -> str:
        return "amount could not be parsed" if self.amount is None else "missing currency_code"

    def render(self, *, signed: bool = True) -> str:
        if self.amount is None:
            return f"{self.raw} {self.code or 'unknown currency'} (unparsed)"
        if not self.code:
            return f"{fmt_money(self.amount, '', signed=signed)} (unknown currency)"
        return fmt_money(self.amount, self.code, signed=signed)


def _entries(balances: Any, currency: str | None = None) -> list[_Entry]:
    """The non-zero and the unparsable entries of a `balance` list, optionally for one currency.

    Zero amounts (`"0.0"`, `"-0.00"`, `"0.000"`) are dropped; missing amounts are not balances.
    """
    if not isinstance(balances, list):
        return []
    out: list[_Entry] = []
    for item in balances:
        if not isinstance(item, dict):
            continue
        code = str(item.get("currency_code") or "").strip().upper()
        if currency is not None and code != currency:
            continue
        raw = item.get("amount")
        if raw in (None, ""):
            continue
        dec = _parse_amount(raw)
        if dec is not None and dec == 0:
            continue
        out.append(_Entry(code=code, amount=dec, raw=str(raw)))
    return out


def _fmt_entries(entries: list[_Entry], *, empty: str = "settled up") -> str:
    if not entries:
        return empty
    return ", ".join(entry.render() for entry in entries)


def _cell(value: Any) -> str:
    """Make a value safe for a markdown table cell."""
    text = "N/A" if value in (None, "") else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _sort_name(user: dict[str, Any]) -> str:
    return " ".join(str(user.get(k) or "") for k in ("first_name", "last_name")).casefold()


def _dedupe_by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first object per `id` (objects without an id are all kept)."""
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = item.get("id")
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(item)
    return out


def _unparsed_note(count: int) -> str:
    noun, verb = ("entry", "is") if count == 1 else ("entries", "are")
    return (
        f"_{count} balance {noun} could not be parsed (amount or currency) and {verb} not in the totals; "
        "they are marked `(unparsed)` / `(unknown currency)` in the rows._"
    )


# -- splitwise_get_balances -------------------------------------------------------


@mcp.tool(
    name="splitwise_get_balances",
    annotations=ToolAnnotations(
        title="Splitwise Balances (all friends)",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_balances(params: GetBalancesInput) -> str:
    """Summarise who owes you and whom you owe, per friend and per currency.

    Calls `GET /get_friends` once and, from each friend's `balance` list, renders every
    non-zero balance per currency, plus per-currency totals: how much you are owed, how
    much you owe, and the net. Friend balances already combine group and non-group
    expenses. Sign convention: **positive = the friend owes you (owed to you); negative =
    you owe the friend.** Zero balances are hidden unless `include_zero` is true; the
    optional `currency` filter keeps a single currency (applied locally — the endpoint
    takes no filter). An amount Splitwise sends that is not a plain decimal, or an entry
    with no currency, is shown raw and flagged, and left out of the totals.

    When to Use:
    - "How much am I owed / do I owe overall?", "who owes me money?", "my USD balance".
    - Before settling up, to see every open balance in one call.

    When NOT to Use:
    - For balances inside ONE group and who pays whom there (use `splitwise_get_group_balances`).
    - For friend profiles, e-mails or per-group breakdowns (use `splitwise_get_friends` / `splitwise_get_friend`).
    - To find a friend's id from a name (use `splitwise_resolve_friend`).

    Returns:
    Markdown: the sign convention, a totals table (`Currency | You are owed | You owe | Net`)
    and a table of friends (`First Last (id N)` with e.g. `+12.50 USD, -3.00 PEN`; with
    `include_zero`, `settled up` or `no USD balance`), capped at 50 rows (totals cover every
    friend except the flagged entries). JSON: `{sign_convention, currency_filter, totals:
    {CUR: {owed_to_you, you_owe, net}}, unparsed: [{friend_id, currency_code, amount, reason}],
    friend_count, friends: [raw friend objects]}` with `you_owe` as a positive magnitude and
    money as 2-decimal strings.

    Examples:
    - params = {}
    - params = {"currency": "USD"}
    - params = {"include_zero": true, "response_format": "json"}

    Error Handling:
    A bad `currency` (not 3 letters) is rejected before any call. 401 means the API key is
    missing or was regenerated; 429 means back off (Retry-After). Errors come back as an
    `Error ...` string.
    """
    try:
        client = get_client()
        resp = await client.request("GET", "/get_friends")
        friends = _dedupe_by_id([f for f in (resp.json().get("friends") or []) if isinstance(f, dict)])

        owed: dict[str, Decimal] = {}
        owe: dict[str, Decimal] = {}
        unparsed: list[dict[str, Any]] = []
        rows: list[tuple[dict[str, Any], list[_Entry]]] = []
        for friend in friends:
            entries = _entries(friend.get("balance"), params.currency)
            for entry in entries:
                if entry.countable and entry.amount is not None:
                    bucket = owed if entry.amount > 0 else owe
                    bucket[entry.code] = bucket.get(entry.code, Decimal(0)) + entry.amount
                else:
                    unparsed.append(
                        {
                            "friend_id": friend.get("id"),
                            "currency_code": entry.code or None,
                            "amount": entry.raw,
                            "reason": entry.reason,
                        }
                    )
            if entries or params.include_zero:
                rows.append((friend, entries))
        rows.sort(key=lambda row: (_sort_name(row[0]), str(row[0].get("id"))))
        currencies = sorted(set(owed) | set(owe))
        zero = Decimal(0)

        if params.response_format is ResponseFormat.JSON:
            totals = {
                code: {
                    "owed_to_you": _wire(owed.get(code, zero)),
                    "you_owe": _wire(-owe.get(code, zero)),
                    "net": _wire(owed.get(code, zero) + owe.get(code, zero)),
                }
                for code in currencies
            }
            return clip_response(
                to_json(
                    {
                        "sign_convention": FRIEND_SIGN_CONVENTION,
                        "currency_filter": params.currency,
                        "totals": totals,
                        "unparsed": unparsed,
                        "friend_count": len(rows),
                        "friends": [friend for friend, _ in rows],
                    }
                )
            )

        scope = f" in {params.currency}" if params.currency else ""
        lines = ["# Splitwise balances", "", FRIEND_SIGN_CONVENTION]
        if params.currency:
            lines.append(f"Currency filter: {params.currency}.")
        lines.append("")
        if currencies:
            lines.extend(
                [
                    "## Totals by currency",
                    "",
                    "| Currency | You are owed | You owe | Net |",
                    "|---|---|---|---|",
                ]
            )
            for code in currencies:
                lines.append(
                    f"| {_cell(code)} | {fmt_money(owed.get(code, zero), code, signed=False)} | "
                    f"{fmt_money(-owe.get(code, zero), code, signed=False)} | "
                    f"{fmt_money(owed.get(code, zero) + owe.get(code, zero), code)} |"
                )
        elif not unparsed:
            lines.append(f"_You are settled up with every friend{scope}._")
        if unparsed:
            lines.extend(["", _unparsed_note(len(unparsed))])
        if rows:
            title = "Friends" if params.include_zero else "Friends with a balance"
            lines.extend(["", f"## {title} ({len(rows)})", "", "| Friend | Balance |", "|---|---|"])
            for friend, entries in rows[:MAX_DISPLAY_ROWS]:
                empty = "settled up"
                if params.currency and _entries(friend.get("balance")):
                    empty = f"no {params.currency} balance"
                lines.append(f"| {_cell(fmt_person(friend))} | {_cell(_fmt_entries(entries, empty=empty))} |")
            if len(rows) > MAX_DISPLAY_ROWS:
                lines.append(
                    f"\n_Showing {MAX_DISPLAY_ROWS} of {len(rows)} friends (totals cover all of them); "
                    "narrow with `currency` or use response_format='json'._"
                )
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


# -- splitwise_get_group_balances ------------------------------------------------


def _debt_line(debt: dict[str, Any], names: dict[Any, str]) -> str:
    frm, to = debt.get("from"), debt.get("to")
    who_from = names.get(frm) or fmt_person(None, fallback_id=frm)
    who_to = names.get(to) or fmt_person(None, fallback_id=to)
    code = str(debt.get("currency_code") or "").strip().upper()
    raw = debt.get("amount")
    amount = _Entry(code=code, amount=_parse_amount(raw), raw=str(raw)).render(signed=False)
    return f"- {who_from} → {who_to} {amount}"


@mcp.tool(
    name="splitwise_get_group_balances",
    annotations=ToolAnnotations(
        title="Splitwise Group Balances",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_group_balances(params: GetGroupBalancesInput) -> str:
    """Show each member's balance in one group and who should pay whom to settle it.

    Calls `GET /get_group/{group_id}` and renders the members' non-zero balances per
    currency, then the group's debts as `From → To amount CUR` (From owes To), with names
    resolved from the group's own `members` (an id not among them is shown as `user N`).
    Uses `simplified_debts`; when Splitwise returns none, falls back to `original_debts`
    and says so. Sign convention: **positive = the member is owed by the group; negative =
    the member owes the group.** Settled-up members are listed by name only. An amount
    that is not a plain decimal is shown raw and flagged `(unparsed)`.

    When to Use:
    - "Who owes whom in the Trip group?", "how do we settle up the apartment?".
    - Before recording a settle-up payment, to get the exact amounts and user ids.

    When NOT to Use:
    - For your balances across all friends (use `splitwise_get_balances`).
    - For non-group expenses: group_id 0 is the non-group pseudo-group in `get_groups`, but
      whether `/get_group/0` answers is unverified until the live smoke — on a 404, use
      `splitwise_get_balances`.
    - For the group's details, invite link or membership changes (use `splitwise_get_group`,
      `splitwise_add_user_to_group`, `splitwise_remove_user_from_group`).
    - To find a group id from its name (use `splitwise_resolve_group` / `splitwise_get_groups`).

    Returns:
    Markdown: a member table (`First Last (id N) | +20.00 USD`), the settled-up members,
    and the debts list with its source (`simplified_debts` or `original_debts`), capped at
    50 rows each. JSON: `{group: {id, name}, sign_convention, members: [raw members],
    debts_source: "simplified_debts" | "original_debts" | null, debts: [raw debts]}`.

    Examples:
    - params = {"group_id": 12345}
    - params = {"group_id": 12345, "response_format": "json"}

    Error Handling:
    403 means you are not a member of that group; 404 means the id is wrong or the group
    was deleted (`splitwise_undelete_group` restores it) — or, for group_id 0, that the
    pseudo-group is not served here (use `splitwise_get_balances`). Errors come back as an
    `Error ...` string.
    """
    try:
        client = get_client()
        resp = await client.request("GET", f"/get_group/{params.group_id}")
        group: dict[str, Any] = resp.json().get("group") or {}
        members: list[dict[str, Any]] = [m for m in (group.get("members") or []) if isinstance(m, dict)]
        simplified = [d for d in (group.get("simplified_debts") or []) if isinstance(d, dict)]
        original = [d for d in (group.get("original_debts") or []) if isinstance(d, dict)]
        if simplified:
            source: str | None = "simplified_debts"
            debts = simplified
        elif original:
            source, debts = "original_debts", original
        else:
            source, debts = None, []

        if params.response_format is ResponseFormat.JSON:
            return clip_response(
                to_json(
                    {
                        "group": {"id": group.get("id", params.group_id), "name": group.get("name")},
                        "sign_convention": GROUP_SIGN_CONVENTION,
                        "members": members,
                        "debts_source": source,
                        "debts": debts,
                    }
                )
            )

        names = {m.get("id"): fmt_person(m) for m in members if m.get("id") is not None}
        label = f"{group.get('name') or '(unnamed)'} (id {group.get('id', params.group_id)})"
        lines = [f"# Balances in {label}", "", GROUP_SIGN_CONVENTION, ""]

        with_balance = [(m, _entries(m.get("balance"))) for m in members]
        owing = [(m, entries) for m, entries in with_balance if entries]
        settled = [m for m, entries in with_balance if not entries]
        if owing:
            lines.extend([f"## Members with a balance ({len(owing)})", "", "| Member | Balance |", "|---|---|"])
            for member, entries in owing[:MAX_DISPLAY_ROWS]:
                lines.append(f"| {_cell(fmt_person(member))} | {_cell(_fmt_entries(entries))} |")
            if len(owing) > MAX_DISPLAY_ROWS:
                lines.append(f"\n_Showing {MAX_DISPLAY_ROWS} of {len(owing)} members; use response_format='json'._")
        else:
            lines.append("_Every member is settled up._")
        if settled:
            shown = ", ".join(fmt_person(m) for m in settled[:MAX_DISPLAY_ROWS])
            more = f" … and {len(settled) - MAX_DISPLAY_ROWS} more" if len(settled) > MAX_DISPLAY_ROWS else ""
            lines.extend(["", f"Settled up ({len(settled)}): {shown}{more}."])

        lines.append("")
        if source == "simplified_debts":
            lines.extend(["## Debts (source: simplified_debts; From → To = From owes To)", ""])
        elif source == "original_debts":
            lines.extend(
                [
                    "## Debts (source: original_debts — Splitwise returned no simplified debts; "
                    "From → To = From owes To)",
                    "",
                ]
            )
        if debts:
            lines.extend(_debt_line(debt, names) for debt in debts[:MAX_DISPLAY_ROWS])
            if len(debts) > MAX_DISPLAY_ROWS:
                lines.append(f"\n_Showing {MAX_DISPLAY_ROWS} of {len(debts)} debts; use response_format='json'._")
        else:
            lines.append("_No debts: neither simplified_debts nor original_debts has an entry._")
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
