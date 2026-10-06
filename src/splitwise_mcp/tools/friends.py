"""Splitwise `friends` tools: list/get friends with balances, add one or many, delete one.

Endpoints (research/02 §C): `GET /get_friends`, `GET /get_friend/{id}`,
`POST /create_friend`, `POST /create_friends` (⚠ 200 ≠ success), `POST /delete_friend/{id}`
(⚠ 200 ≠ success). The client raises on a failed envelope; these tools only render the
happy-path body.

Sign convention for every balance rendered here: **positive = the friend owes you**,
negative = you owe the friend.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from splitwise_mcp.client import SplitwiseEnvelopeError, get_client
from splitwise_mcp.errors import format_errors, handle_api_error
from splitwise_mcp.formatters import ResponseFormat, clip_response, fmt_money, fmt_person, iso_to_human, to_json
from splitwise_mcp.server import mcp

MAX_DISPLAY_ROWS = 50
MAX_BATCH_FRIENDS = 50

SIGN_CONVENTION = "Sign convention: positive = they owe you; negative = you owe them."

# `/create_friends` can add some entries and reject others in the same 200 response; the
# client then raises on the non-empty `errors`, so the error alone would read as "nobody added".
PARTIAL_ADD_HINT = (
    "Some of the requested friends may already have been added — call splitwise_get_friends before retrying."
)

# Deliberately loose: Splitwise is the authority on e-mail validity; this only catches
# obvious slips (a name pasted into the e-mail field, a missing domain).
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


# -- input models --------------------------------------------------------------


class GetFriendsInput(BaseModel):
    """Input for `splitwise_get_friends`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    only_with_balance: bool = Field(
        default=False,
        description="When true, list only friends with a non-zero balance in at least one currency.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default) for a readable table, or 'json' for the raw friend objects "
        "(including the per-group balance breakdown).",
    )


class GetFriendInput(BaseModel):
    """Input for `splitwise_get_friend`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    friend_id: int = Field(..., ge=1, description="The friend's Splitwise user id (e.g. 4821).")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default) for a readable summary, or 'json' for the raw friend object.",
    )


class CreateFriendInput(BaseModel):
    """Input for `splitwise_create_friend`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_email: str = Field(
        ...,
        min_length=3,
        max_length=254,
        pattern=_EMAIL_PATTERN,
        description="E-mail address of the person to add as a friend (e.g. 'grace@example.com').",
    )
    user_first_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="First name — required by Splitwise only when no account exists yet for that e-mail; "
        "ignored for an existing user.",
    )
    user_last_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Last name — optional; used only when no account exists yet for that e-mail.",
    )


class NewFriendInput(BaseModel):
    """One person to add in `splitwise_create_friends`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    email: str = Field(
        ...,
        min_length=3,
        max_length=254,
        pattern=_EMAIL_PATTERN,
        description="E-mail address of the person to add (e.g. 'grace@example.com').",
    )
    first_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="First name — needed only when no Splitwise account exists yet for that e-mail.",
    )
    last_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Last name — optional; used only when no Splitwise account exists yet for that e-mail.",
    )


class CreateFriendsInput(BaseModel):
    """Input for `splitwise_create_friends`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    friends: list[NewFriendInput] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH_FRIENDS,
        description=f"People to add as friends in one call (1-{MAX_BATCH_FRIENDS}); each needs an e-mail.",
    )

    @model_validator(mode="after")
    def _unique_emails(self) -> CreateFriendsInput:
        seen: set[str] = set()
        duplicates: list[str] = []
        for friend in self.friends:
            key = friend.email.casefold()
            if key in seen and key not in duplicates:
                duplicates.append(key)
            seen.add(key)
        if duplicates:
            raise ValueError(f"each e-mail may appear only once; duplicated: {', '.join(duplicates)}")
        return self


class DeleteFriendInput(BaseModel):
    """Input for `splitwise_delete_friend`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    friend_id: int = Field(..., ge=1, description="The friend's Splitwise user id to unfriend (e.g. 4821).")

    @field_validator("friend_id", mode="before")
    @classmethod
    def _reject_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("friend_id must be an integer user id, not a boolean")
        return value


# -- rendering helpers -----------------------------------------------------------


def _is_nonzero(amount: Any) -> bool:
    """True when a balance amount is non-zero (an unparsable amount counts as non-zero, so it is shown)."""
    try:
        return Decimal(str(amount)) != 0
    except (InvalidOperation, ValueError):
        return amount not in (None, "")


def _nonzero_balances(balances: Any) -> list[dict[str, Any]]:
    """Keep only the `{currency_code, amount}` entries whose amount is non-zero."""
    if not isinstance(balances, list):
        return []
    return [b for b in balances if isinstance(b, dict) and _is_nonzero(b.get("amount"))]


def _fmt_balances(balances: Any) -> str:
    """Render a balance list as `+12.50 USD, -3.00 PEN` (`settled up` if all zero, `N/A` if absent)."""
    if not isinstance(balances, list):
        return "N/A"
    nonzero = _nonzero_balances(balances)
    if not nonzero:
        return "settled up"
    return ", ".join(fmt_money(b.get("amount"), str(b.get("currency_code") or "")) for b in nonzero)


def _cell(value: Any) -> str:
    """Make a value safe for a markdown table cell."""
    text = "N/A" if value in (None, "") else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _friend_row(friend: dict[str, Any]) -> str:
    return (
        f"| {_cell(fmt_person(friend))} | {_cell(friend.get('email'))} | "
        f"{_cell(friend.get('registration_status'))} | {_cell(_fmt_balances(friend.get('balance')))} |"
    )


def _fmt_group_balances(groups: Any) -> list[str]:
    """One line per shared group: `- group 12: +5.00 USD` (group 0 = non-group expenses)."""
    if not isinstance(groups, list) or not groups:
        return ["- none"]
    lines = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = group.get("group_id")
        label = "non-group expenses (group 0)" if group_id == 0 else f"group {group_id}"
        lines.append(f"- {label}: {_fmt_balances(group.get('balance'))}")
    return lines or ["- none"]


def _friend_summary(friend: dict[str, Any], *, heading: str) -> list[str]:
    return [
        f"# {heading}: {fmt_person(friend)}",
        "",
        f"- **email**: {friend.get('email') or 'N/A'}",
        f"- **registration status**: {friend.get('registration_status') or 'N/A'}",
        f"- **balance**: {_fmt_balances(friend.get('balance'))}",
        f"- **updated at**: {iso_to_human(friend.get('updated_at'))}",
    ]


# -- tools -----------------------------------------------------------------------


@mcp.tool(
    name="splitwise_get_friends",
    annotations=ToolAnnotations(
        title="List Splitwise Friends",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_friends(params: GetFriendsInput) -> str:
    """List your Splitwise friends with their balance per currency.

    Calls `GET /get_friends`. One row per friend: `First Last (id N)`, e-mail,
    registration status (confirmed | invited | dummy) and the overall balance per
    currency. Sign convention: positive = they owe you; negative = you owe them.
    JSON mode returns the raw friend objects, including `groups[]` with the balance
    per shared group (group 0 = non-group expenses).

    When to Use:
    - To see who owes you / whom you owe, per friend and currency.
    - To find a friend's user id before building expense shares or adding them to a group.
    - With `only_with_balance=true` to list only the friendships that are not settled up.

    When NOT to Use:
    - To find a friend by a fuzzy name (use `splitwise_resolve_friend`).
    - For totals per currency across all friends (use `splitwise_get_balances`).
    - For one friend's detail incl. per-group balances (use `splitwise_get_friend`).
    - For balances inside one group (use `splitwise_get_group_balances`).

    Returns:
    Markdown: a header with the sign convention and counts, then a table
    (Friend | Email | Status | Balance), capped at 50 rows with a note when more exist.
    JSON: `{"count", "only_with_balance", "friends": [raw friend objects]}`.

    Examples:
    - All friends: params = {}
    - Only open balances: params = {"only_with_balance": true}
    - Raw objects with per-group balances: params = {"response_format": "json"}

    Error Handling:
    401 means the API key is missing or revoked (https://secure.splitwise.com/apps);
    429 means back off and retry later.
    """
    try:
        client = get_client()
        resp = await client.request("GET", "/get_friends")
        body = resp.json()
        friends: list[dict[str, Any]] = [f for f in (body.get("friends") or []) if isinstance(f, dict)]
        total = len(friends)
        if params.only_with_balance:
            friends = [f for f in friends if _nonzero_balances(f.get("balance"))]

        if params.response_format is ResponseFormat.JSON:
            return clip_response(
                to_json({"count": len(friends), "only_with_balance": params.only_with_balance, "friends": friends})
            )

        lines = ["# Splitwise friends", "", SIGN_CONVENTION, ""]
        count = f"**{len(friends)}** friend(s)"
        if params.only_with_balance:
            count += f" with a non-zero balance (of {total} friends)"
        if len(friends) > MAX_DISPLAY_ROWS:
            lines.append(f"{count}; showing the first {MAX_DISPLAY_ROWS}.")
        else:
            lines.append(f"Showing {count}.")
        if not friends:
            lines.extend(["", "_No friends with a non-zero balance._" if params.only_with_balance else "_No friends._"])
            return clip_response("\n".join(lines))

        shown = friends[:MAX_DISPLAY_ROWS]
        lines.extend(["", "| Friend | Email | Status | Balance |", "|---|---|---|---|"])
        lines.extend(_friend_row(f) for f in shown)
        hidden = len(friends) - len(shown)
        if hidden:
            lines.extend(
                [
                    "",
                    f"_{hidden} more friend(s) not shown (display cap {MAX_DISPLAY_ROWS}) — use "
                    "only_with_balance=true, response_format='json', or splitwise_resolve_friend._",
                ]
            )
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_get_friend",
    annotations=ToolAnnotations(
        title="Get Splitwise Friend",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_friend(params: GetFriendInput) -> str:
    """Get one Splitwise friend with their overall and per-group balances.

    Calls `GET /get_friend/{id}`. Renders the friend as `First Last (id N)`, e-mail,
    registration status, the overall balance per currency, the balance in each shared
    group (group 0 = non-group expenses) and when the friendship last changed.
    Sign convention: positive = they owe you; negative = you owe them.

    When to Use:
    - To see how a balance with one friend splits across shared groups.
    - To confirm a user id belongs to a friend before using it in an expense.

    When NOT to Use:
    - To list every friend (use `splitwise_get_friends`).
    - To turn a name into an id (use `splitwise_resolve_friend`).
    - For a user who is not a friend (use `splitwise_get_user`).

    Returns:
    Markdown: a summary block plus a "Shared groups" list. JSON: the raw friend object.

    Examples:
    - params = {"friend_id": 4821}
    - params = {"friend_id": 4821, "response_format": "json"}

    Error Handling:
    404 means the id is not one of your friends (or does not exist); 401 means the API
    key is missing or revoked.
    """
    try:
        client = get_client()
        resp = await client.request("GET", f"/get_friend/{params.friend_id}")
        friend: dict[str, Any] = resp.json().get("friend") or {}

        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json(friend))

        lines = _friend_summary(friend, heading="Splitwise friend")
        lines.extend(["", SIGN_CONVENTION, "", "## Shared groups", ""])
        lines.extend(_fmt_group_balances(friend.get("groups")))
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_create_friend",
    annotations=ToolAnnotations(
        title="Add Splitwise Friend",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def splitwise_create_friend(params: CreateFriendInput) -> str:
    """Add one person as a Splitwise friend by e-mail. 🔒 Needs SPLITWISE_ALLOW_WRITES=1.

    Calls `POST /create_friend` with `user_email` (+ `user_first_name` /
    `user_last_name` when given). If an account already exists for that e-mail the
    names are ignored; if not, Splitwise creates an invited user and `user_first_name`
    is required. If you don't know whether they have an account, pass `user_first_name`;
    it is ignored for existing users. Adding someone without an account probably sends
    them an invitation e-mail (unverified; the live smoke t10 confirms). Refused with
    `Error: … writes are disabled` unless `SPLITWISE_ALLOW_WRITES=1`. There is no undo
    tool; remove a friend with `splitwise_delete_friend`.

    When to Use:
    - To befriend one person so you can share non-group expenses with them.

    When NOT to Use:
    - To add several people at once (use `splitwise_create_friends`).
    - To add someone to a group (use `splitwise_add_user_to_group`).
    - To check whether someone is already a friend (use `splitwise_get_friends` or
      `splitwise_resolve_friend`).

    Returns:
    A confirmation echoing the friend Splitwise returned: `First Last (id N)`, e-mail and
    registration status.

    Examples:
    - Existing user: params = {"user_email": "grace@example.com"}
    - New user: params = {"user_email": "alan@example.com", "user_first_name": "Alan",
      "user_last_name": "Turing"}

    Error Handling:
    `Error: … writes are disabled` → set SPLITWISE_ALLOW_WRITES=1. A 400 usually means an
    invalid e-mail or a missing first name for a new user. On a 5xx/timeout the outcome
    is UNKNOWN — check `splitwise_get_friends` before retrying.
    """
    try:
        data = {
            key: value
            for key, value in {
                "user_email": params.user_email,
                "user_first_name": params.user_first_name,
                "user_last_name": params.user_last_name,
            }.items()
            if value is not None
        }
        client = get_client()
        resp = await client.request("POST", "/create_friend", data=data)
        friend: dict[str, Any] = resp.json().get("friend") or {}
        if not friend:
            return "Splitwise accepted the request but returned no friend object — check `splitwise_get_friends`."
        return clip_response(
            "\n".join(
                [
                    f"Friend added: {fmt_person(friend)}",
                    f"- **email**: {friend.get('email') or 'N/A'}",
                    f"- **registration status**: {friend.get('registration_status') or 'N/A'}",
                ]
            )
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_create_friends",
    annotations=ToolAnnotations(
        title="Add Several Splitwise Friends",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def splitwise_create_friends(params: CreateFriendsInput) -> str:
    """Add several people as Splitwise friends in one call. 🔒 Needs SPLITWISE_ALLOW_WRITES=1.

    Calls `POST /create_friends` with the list flattened to
    `users__{i}__email` / `users__{i}__first_name` / `users__{i}__last_name` (index from
    0). Names matter only for people without a Splitwise account yet. Splitwise answers
    HTTP 200 even when it rejects entries, so success is read from the body's `errors`.
    Refused with `Error: … writes are disabled` unless `SPLITWISE_ALLOW_WRITES=1`. There is
    no undo tool; remove a friend with `splitwise_delete_friend`.

    When to Use:
    - To befriend a list of people (e.g. trip companions) before sharing expenses.

    When NOT to Use:
    - For a single person (use `splitwise_create_friend`).
    - To put people in a group (use `splitwise_create_group` / `splitwise_add_user_to_group`).

    Returns:
    A confirmation listing every user Splitwise returned (`First Last (id N)`, e-mail,
    registration status) and the body's `errors` (none on success).

    Examples:
    - params = {"friends": [{"email": "grace@example.com"},
      {"email": "alan@example.com", "first_name": "Alan", "last_name": "Turing"}]}

    Error Handling:
    Duplicate e-mails in the list are rejected locally before any call. If Splitwise
    reports any error the call is returned as an `Error:` — some entries may still have
    been added, so read back with `splitwise_get_friends` before retrying. On a
    5xx/timeout the outcome is UNKNOWN.
    """
    try:
        data: dict[str, Any] = {}
        for index, friend in enumerate(params.friends):
            data[f"users__{index}__email"] = friend.email
            if friend.first_name is not None:
                data[f"users__{index}__first_name"] = friend.first_name
            if friend.last_name is not None:
                data[f"users__{index}__last_name"] = friend.last_name
        client = get_client()
        resp = await client.request("POST", "/create_friends", data=data)
        body = resp.json()
        users = [u for u in (body.get("users") or []) if isinstance(u, dict)]
        lines = [f"Splitwise returned {len(users)} user(s) for {len(params.friends)} requested friend(s):"]
        if users:
            lines.extend(
                f"- {fmt_person(u)} — {u.get('email') or 'N/A'} ({u.get('registration_status') or 'N/A'})"
                for u in users
            )
        else:
            lines.append("- none — check `splitwise_get_friends`")
        lines.append(f"- **errors**: {format_errors(body.get('errors')) or 'none'}")
        return clip_response("\n".join(lines))
    except Exception as exc:
        message = handle_api_error(exc)
        if isinstance(exc, SplitwiseEnvelopeError | httpx.HTTPStatusError):
            message += f"\n{PARTIAL_ADD_HINT}"
        return message


@mcp.tool(
    name="splitwise_delete_friend",
    annotations=ToolAnnotations(
        title="Delete Splitwise Friend",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_delete_friend(params: DeleteFriendInput) -> str:
    """Remove a friendship (unfriend a user). 🔒 Needs SPLITWISE_ALLOW_WRITES=1. Destructive.

    Calls `POST /delete_friend/{id}` with the friend's user id. Splitwise answers HTTP
    200 even when it refuses, so the outcome is read from the body's `success` /
    `errors`. Refused with `Error: … writes are disabled` unless
    `SPLITWISE_ALLOW_WRITES=1`. There is no undelete for friendships — re-add with
    `splitwise_create_friend`.

    When to Use:
    - The user explicitly asked to remove someone from their friends list.

    When NOT to Use:
    - To remove someone from a group (use `splitwise_remove_user_from_group`).
    - To settle a balance (record the repayment as an expense via `splitwise_create_expense`).
    - When the id is uncertain — confirm it first with `splitwise_resolve_friend` or
      `splitwise_get_friend`.

    Returns:
    A confirmation with the user id and the body's `success` value.

    Examples:
    - params = {"friend_id": 4821}

    Error Handling:
    `Error: … writes are disabled` → set SPLITWISE_ALLOW_WRITES=1. If Splitwise refuses,
    its `errors` message is returned as `Error: Splitwise rejected the request …`. 404
    means the id is not a friend.
    """
    try:
        client = get_client()
        resp = await client.request("POST", f"/delete_friend/{params.friend_id}")
        body = resp.json()
        errors = format_errors(body.get("errors")) or "none"
        if body.get("success") is True:
            return f"Friendship with user {params.friend_id} deleted.\n- **success**: true\n- **errors**: {errors}"
        return (
            f"Splitwise answered the unfriend request for user {params.friend_id} without a `success` flag.\n"
            f"- **success**: not reported\n- **errors**: {errors}\n"
            f"Verify with `splitwise_get_friend` (friend_id {params.friend_id}) before assuming it was removed."
        )
    except Exception as exc:
        return handle_api_error(exc)
