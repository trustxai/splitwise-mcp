"""Splitwise `groups` tools — list, read, create, delete/undelete groups and change membership.

Inventory B1–B7 (research/02). Reads render members as `First Last (id N)` and balances
as `+12.50 USD`; group id 0 is Splitwise's pseudo-group for non-group expenses. Every
write is gated by the client's kill-switch (SPLITWISE_ALLOW_WRITES) and its envelope
check (Splitwise answers 200 OK for failed writes); the tools render the happy path from
the body and never claim more than it says.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from splitwise_mcp.client import get_client
from splitwise_mcp.errors import format_errors, handle_api_error
from splitwise_mcp.formatters import (
    ResponseFormat,
    clip_response,
    fmt_money,
    fmt_person,
    iso_to_human,
    to_json,
)
from splitwise_mcp.server import mcp

# Context-window guard on top of the API (get_groups has no limit/offset).
MAX_DISPLAY_ROWS = 50

NON_GROUP_ID = 0
NON_GROUP_NOTE = (
    "Group id 0 is Splitwise's pseudo-group for **non-group expenses** (expenses with friends outside any group); "
    "it cannot be deleted or have members added."
)
SIGN_NOTE = "Sign convention: a positive balance means that member is owed money; negative means they owe."

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

GroupType = Literal["home", "trip", "couple", "other"]
# Splitwise documents apartment/house as legacy aliases and says to use `home` instead.
_GROUP_TYPE_ALIASES = {"apartment": "home", "house": "home"}


# -- input models --------------------------------------------------------------


def _email(value: object) -> str | None:
    """Validate a person's email (another member — never the account's own login)."""
    if value is None:
        return None
    text = str(value).strip()
    if not _EMAIL_RE.match(text):
        raise ValueError(f"email must look like name@example.com (got {value!s})")
    return text


class GetGroupsInput(BaseModel):
    """Input for `splitwise_get_groups`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    current_user_id: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Your own Splitwise user id (from `splitwise_health_check`). When given, the balance column shows "
            "only YOUR balance in each group; when omitted, every member's non-zero balance is listed."
        ),
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default, a table) or 'json' (the raw group objects).",
    )


class GetGroupInput(BaseModel):
    """Input for `splitwise_get_group`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    group_id: int = Field(
        ...,
        ge=0,
        description="The group id (from `splitwise_get_groups`). 0 = the non-group-expenses pseudo-group.",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default) or 'json' (the raw group object).",
    )


class MemberInput(BaseModel):
    """One member for `splitwise_create_group`: an existing `user_id` OR an `email` + `first_name`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_id: int | None = Field(default=None, ge=1, description="An existing Splitwise user id (a friend).")
    email: str | None = Field(
        default=None, description="Email of a person to invite (use instead of user_id). Needs first_name."
    )
    first_name: str | None = Field(
        default=None, min_length=1, description="First name of the invited person (required with email)."
    )
    last_name: str | None = Field(
        default=None, min_length=1, description="Last name of the invited person (optional, only with email)."
    )

    @field_validator("email", mode="before")
    @classmethod
    def _check_email(cls, value: object) -> str | None:
        return _email(value)

    @model_validator(mode="after")
    def _exactly_one_identity(self) -> MemberInput:
        if self.user_id is not None:
            if any(v is not None for v in (self.email, self.first_name, self.last_name)):
                raise ValueError("a member is EITHER user_id OR email + first_name (+ last_name), not both")
        elif self.email is None:
            raise ValueError("each member needs a user_id, or an email + first_name")
        elif self.first_name is None:
            raise ValueError("a member identified by email also needs first_name")
        return self


class CreateGroupInput(BaseModel):
    """Input for `splitwise_create_group`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=200, description="The group's name, e.g. 'Lima trip 2026'.")
    group_type: GroupType | None = Field(
        default=None,
        description="One of home, trip, couple, other (apartment/house are accepted and sent as home).",
    )
    simplify_by_default: bool | None = Field(
        default=None, description="Turn on Splitwise's debt simplification for the group."
    )
    members: list[MemberInput] = Field(
        default_factory=list,
        max_length=MAX_DISPLAY_ROWS,
        description=(
            "Members besides you (you are added automatically). Each is {user_id} or {email, first_name, last_name?}."
        ),
    )

    @field_validator("group_type", mode="before")
    @classmethod
    def _normalise_group_type(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip().lower()
            return _GROUP_TYPE_ALIASES.get(text, text)
        return value


class GroupIdInput(BaseModel):
    """Input for the tools that act on one real group by id (delete / undelete)."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    group_id: int = Field(..., ge=1, description="The group id (from `splitwise_get_groups`); 0 is not a real group.")


class AddUserToGroupInput(BaseModel):
    """Input for `splitwise_add_user_to_group`: `user_id` OR the `first_name` + `last_name` + `email` trio."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    group_id: int = Field(..., ge=1, description="The group to add the person to.")
    user_id: int | None = Field(default=None, ge=1, description="An existing Splitwise user id (a friend).")
    first_name: str | None = Field(
        default=None, min_length=1, description="First name of a person to invite (with last_name + email)."
    )
    last_name: str | None = Field(
        default=None, min_length=1, description="Last name of a person to invite (with first_name + email)."
    )
    email: str | None = Field(default=None, description="Email of a person to invite (with first_name + last_name).")

    @field_validator("email", mode="before")
    @classmethod
    def _check_email(cls, value: object) -> str | None:
        return _email(value)

    @model_validator(mode="after")
    def _one_of(self) -> AddUserToGroupInput:
        trio = (self.first_name, self.last_name, self.email)
        given = [v is not None for v in trio]
        if self.user_id is not None:
            if any(given):
                raise ValueError("give EITHER user_id OR first_name + last_name + email, not both")
        elif not all(given):
            missing = [name for name, v in zip(("first_name", "last_name", "email"), trio, strict=True) if v is None]
            raise ValueError(
                "give either user_id, or all of first_name + last_name + email (missing: " + ", ".join(missing) + ")"
            )
        return self


class RemoveUserFromGroupInput(BaseModel):
    """Input for `splitwise_remove_user_from_group`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    group_id: int = Field(..., ge=1, description="The group to remove the person from.")
    user_id: int = Field(..., ge=1, description="The member's user id (from `splitwise_get_group`).")


# -- rendering helpers ---------------------------------------------------------


def _is_nonzero(amount: Any) -> bool:
    try:
        return Decimal(str(amount)) != 0
    except (InvalidOperation, ValueError):
        return False


def _balances(member: dict[str, Any]) -> list[str]:
    """Non-zero balances of one member as `+12.50 USD` strings."""
    return [
        fmt_money(b.get("amount"), b.get("currency_code") or "")
        for b in member.get("balance") or []
        if _is_nonzero(b.get("amount"))
    ]


def _balance_text(member: dict[str, Any]) -> str:
    parts = _balances(member)
    return ", ".join(parts) if parts else "settled up"


def _group_label(group: dict[str, Any]) -> str:
    name = group.get("name") or "(unnamed)"
    return f"{name} (id {group.get('id')})"


def _groups_balance_cell(group: dict[str, Any], current_user_id: int | None) -> str:
    members: list[dict[str, Any]] = group.get("members") or []
    if current_user_id is not None:
        me = next((m for m in members if m.get("id") == current_user_id), None)
        if me is None:
            return "you are not listed"
        return _balance_text(me)
    owing = [f"{fmt_person(m)} {', '.join(_balances(m))}" for m in members if _balances(m)]
    return "; ".join(owing) if owing else "everyone settled up"


def _debt_lines(debts: list[dict[str, Any]], names: dict[Any, str]) -> list[str]:
    lines = []
    for debt in debts:
        frm, to = debt.get("from"), debt.get("to")
        lines.append(
            f"- {names.get(frm, fmt_person(None, fallback_id=frm))} → {names.get(to, fmt_person(None, fallback_id=to))}"
            f" {fmt_money(debt.get('amount'), debt.get('currency_code') or '', signed=False)}"
        )
    return lines


def _success_text(body: dict[str, Any]) -> str:
    """Render the `success` / `errors` outcome exactly as the body reported it."""
    success = body.get("success")
    text = f"success: {str(success).lower()}" if isinstance(success, bool) else "success: not reported"
    if "errors" in body:
        detail = format_errors(body.get("errors"))
        text += f"; errors: {detail}" if detail else "; errors: none"
    return text


# -- read tools ----------------------------------------------------------------


@mcp.tool(
    name="splitwise_get_groups",
    annotations=ToolAnnotations(
        title="List Splitwise Groups",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_groups(params: GetGroupsInput) -> str:
    """List every group you belong to, with members count and balances per currency.

    Calls `GET /get_groups` and renders one row per group: name, id, type, number of
    members and the balance column. With `current_user_id` the column is YOUR balance in
    that group; without it, every member's non-zero balance is listed compactly.
    Group id 0 is Splitwise's pseudo-group for non-group expenses.
    Sign convention: positive = owed to that member (with current_user_id: owed to you);
    negative = they (you) owe.

    When to Use:
    - To find a group's id before reading, adding an expense to it, or changing its members.
    - For an overview of where you owe or are owed money, group by group.

    When NOT to Use:
    - To match a group by a fuzzy name (use `splitwise_resolve_group`).
    - For one group's members, debts and invite link (use `splitwise_get_group`).
    - For balances per friend across all groups (use `splitwise_get_balances` / `splitwise_get_friends`).

    Returns:
    A markdown table (at most 50 rows; the rest is counted) or, with response_format=json,
    `{"count", "shown", "groups": [raw group objects]}`.

    Examples:
    - params = {}
    - params = {"current_user_id": 491923}
    - params = {"response_format": "json"}

    Error Handling:
    401 → the API key is missing or was regenerated; network errors are reported as `Error: ...`.
    """
    try:
        client = get_client()
        resp = await client.request("GET", "/get_groups")
        groups: list[dict[str, Any]] = resp.json().get("groups") or []
        shown = groups[:MAX_DISPLAY_ROWS]

        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json({"count": len(groups), "shown": len(shown), "groups": shown}))

        lines = [f"# Splitwise groups ({len(groups)})", ""]
        if not groups:
            lines.append("_You are not in any group._")
            return "\n".join(lines)
        balance_header = "Your balance" if params.current_user_id is not None else "Members' balances"
        lines.extend(
            [
                f"| Group | Type | Members | {balance_header} |",
                "|---|---|---|---|",
            ]
        )
        for group in shown:
            label = _group_label(group)
            if group.get("id") == NON_GROUP_ID:
                label += " — non-group expenses"
            lines.append(
                f"| {label} | {group.get('group_type') or 'N/A'} | {len(group.get('members') or [])} | "
                f"{_groups_balance_cell(group, params.current_user_id)} |"
            )
        if len(groups) > len(shown):
            lines.append("")
            lines.append(f"_Showing the first {len(shown)} of {len(groups)} groups._")
        lines.extend(["", SIGN_NOTE])
        if any(g.get("id") == NON_GROUP_ID for g in shown):
            lines.append(NON_GROUP_NOTE)
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_get_group",
    annotations=ToolAnnotations(
        title="Get Splitwise Group",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_group(params: GetGroupInput) -> str:
    """Read one group: its members with balances, who owes whom, and the invite link.

    Calls `GET /get_group/{id}`. Members are rendered `First Last (id N)` with every
    non-zero balance per currency. Debts are rendered `A → B 12.50 USD` (A owes B), names
    resolved from the group's own members; the simplified debts are shown, or the
    original debts (labelled as such) when Splitwise returned no simplified ones.
    Sign convention for member balances: positive = that member is owed money; negative =
    they owe. Group id 0 is the pseudo-group for non-group expenses.

    When to Use:
    - To see who owes whom inside a group before settling up.
    - To get a member's user id before removing them or building expense shares.
    - To fetch the group's invite link.

    When NOT to Use:
    - To list all your groups (use `splitwise_get_groups`), or find one by name (`splitwise_resolve_group`).
    - For the group's expenses (use `splitwise_get_expenses` with group_id).

    Returns:
    A markdown block (name, type, settings, members, debts, invite link) or, with
    response_format=json, the raw group object.

    Examples:
    - params = {"group_id": 12345}
    - params = {"group_id": 12345, "response_format": "json"}

    Error Handling:
    403 → you are not a member of that group; 404 → wrong id or the group was deleted
    (see `splitwise_undelete_group`).
    """
    try:
        client = get_client()
        resp = await client.request("GET", f"/get_group/{params.group_id}")
        group: dict[str, Any] = resp.json().get("group") or {}

        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json(group))

        members: list[dict[str, Any]] = group.get("members") or []
        names = {m.get("id"): fmt_person(m) for m in members}
        lines = [
            f"# {_group_label(group)}",
            "",
            f"- **type**: {group.get('group_type') or 'N/A'}",
            f"- **simplify debts by default**: {group.get('simplify_by_default', 'N/A')}",
            f"- **updated**: {iso_to_human(group.get('updated_at'))}",
            f"- **invite link**: {group.get('invite_link') or 'N/A'}",
        ]
        if group.get("id") == NON_GROUP_ID:
            lines.append(f"- {NON_GROUP_NOTE}")

        lines.extend(["", f"## Members ({len(members)})", ""])
        if members:
            lines.extend(["| Member | Balance |", "|---|---|"])
            lines.extend(f"| {fmt_person(m)} | {_balance_text(m)} |" for m in members[:MAX_DISPLAY_ROWS])
            if len(members) > MAX_DISPLAY_ROWS:
                lines.append(f"\n_Showing the first {MAX_DISPLAY_ROWS} of {len(members)} members._")
            lines.extend(["", SIGN_NOTE])
        else:
            lines.append("_No members listed._")

        simplified: list[dict[str, Any]] = group.get("simplified_debts") or []
        original: list[dict[str, Any]] = group.get("original_debts") or []
        if simplified:
            lines.extend(["", "## Simplified debts (A → B = A owes B)", ""])
            lines.extend(_debt_lines(simplified[:MAX_DISPLAY_ROWS], names))
        elif original:
            lines.extend(["", "## Original debts (no simplified debts returned; A → B = A owes B)", ""])
            lines.extend(_debt_lines(original[:MAX_DISPLAY_ROWS], names))
        else:
            lines.extend(["", "## Debts", "", "_No outstanding debts — everyone is settled up._"])
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


# -- write tools -----------------------------------------------------------------


@mcp.tool(
    name="splitwise_create_group",
    annotations=ToolAnnotations(
        title="Create Splitwise Group",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def splitwise_create_group(params: CreateGroupInput) -> str:
    """Create a group, optionally with members (you are added automatically). 🔒 write.

    Calls `POST /create_group` with `name`, `group_type`, `simplify_by_default` and the
    members flattened to `users__{i}__user_id` or `users__{i}__email` /
    `users__{i}__first_name` / `users__{i}__last_name`. Each member is EITHER an existing
    `user_id` OR an `email` + `first_name` (+ `last_name`) to invite — checked locally
    before any call. Refused with `Error: … writes are disabled` unless
    `SPLITWISE_ALLOW_WRITES=1`. Undo: `splitwise_delete_group` with the returned id.

    When to Use:
    - To start a new shared-expenses group (a trip, a flat, a couple).

    When NOT to Use:
    - To add someone to an existing group (use `splitwise_add_user_to_group`).
    - To bring back a deleted group (use `splitwise_undelete_group`).
    - To find a friend's user id by name first (use `splitwise_resolve_friend`).

    Returns:
    A confirmation echoing what Splitwise returned: the new group's name, id, type and members.

    Examples:
    - params = {"name": "Lima trip", "group_type": "trip", "members": [{"user_id": 5823}]}
    - params = {"name": "Flat", "group_type": "home", "simplify_by_default": true,
                "members": [{"email": "ana@example.com", "first_name": "Ana", "last_name": "Diaz"}]}

    Error Handling:
    A member with both or neither identity is rejected before the call. Splitwise's 400
    field errors (e.g. a blank name) are surfaced as `Error (400): ...`.
    """
    try:
        data: dict[str, Any] = {"name": params.name}
        if params.group_type is not None:
            data["group_type"] = params.group_type
        if params.simplify_by_default is not None:
            data["simplify_by_default"] = params.simplify_by_default
        for index, member in enumerate(params.members):
            for prop, value in member.model_dump(exclude_none=True).items():
                data[f"users__{index}__{prop}"] = value

        client = get_client()
        resp = await client.request("POST", "/create_group", data=data)
        group: dict[str, Any] = resp.json().get("group") or {}
        if not group:
            return "Splitwise answered without a group object — check `splitwise_get_groups` before retrying."
        members: list[dict[str, Any]] = group.get("members") or []
        lines = [
            f"Created group **{_group_label(group)}**.",
            f"- **type**: {group.get('group_type') or 'N/A'}",
            f"- **simplify debts by default**: {group.get('simplify_by_default', 'N/A')}",
            f"- **members ({len(members)})**: " + (", ".join(fmt_person(m) for m in members) or "none listed"),
        ]
        if group.get("invite_link"):
            lines.append(f"- **invite link**: {group['invite_link']}")
        lines.append(f"Undo with `splitwise_delete_group` (group_id={group.get('id')}).")
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_delete_group",
    annotations=ToolAnnotations(
        title="Delete Splitwise Group",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_delete_group(params: GroupIdInput) -> str:
    """Delete a group AND every expense in it. 🔒 destructive write.

    Calls `POST /delete_group/{id}`. Splitwise destroys the group's associated records —
    all of its expenses go with it. Refused with `Error: … writes are disabled` unless
    `SPLITWISE_ALLOW_WRITES=1`. Undo: `splitwise_undelete_group` with the same id restores
    the group and its expenses.

    When to Use:
    - Only when the user explicitly asked to delete this group (confirm the id first with
      `splitwise_get_group`).

    When NOT to Use:
    - To leave a group or drop one member (use `splitwise_remove_user_from_group`).
    - To delete a single expense (use `splitwise_delete_expense`).

    Returns:
    A confirmation with the `success` value Splitwise returned and how to undo it.

    Examples:
    - params = {"group_id": 12345}

    Error Handling:
    A refusal that comes back as HTTP 200 with `success: false` is reported as
    `Error: Splitwise rejected the request ...`; 403 → not a member; 404 → wrong id or already deleted.
    """
    try:
        client = get_client()
        resp = await client.request("POST", f"/delete_group/{params.group_id}")
        body: dict[str, Any] = resp.json() or {}
        return (
            f"Deleted group id {params.group_id} ({_success_text(body)}). Its expenses were deleted with it; "
            f"restore both with `splitwise_undelete_group` (group_id={params.group_id})."
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_undelete_group",
    annotations=ToolAnnotations(
        title="Restore Deleted Splitwise Group",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_undelete_group(params: GroupIdInput) -> str:
    """Restore a deleted group (and its expenses). 🔒 write.

    Calls `POST /undelete_group/{id}`. Splitwise answers HTTP 200 even when this fails and
    puts the outcome in `success` / `errors`; the client turns a failure into an error,
    and this tool renders the outcome the body reported. Refused with
    `Error: … writes are disabled` unless `SPLITWISE_ALLOW_WRITES=1`. Undo:
    `splitwise_delete_group`.

    When to Use:
    - To bring back a group deleted by mistake (`splitwise_delete_group`).

    When NOT to Use:
    - To restore one expense (use `splitwise_undelete_expense`).
    - To re-add a removed member (use `splitwise_add_user_to_group`).

    Returns:
    A confirmation with the `success` / `errors` values Splitwise returned.

    Examples:
    - params = {"group_id": 12345}

    Error Handling:
    `success: false` / non-empty `errors` come back as `Error: Splitwise rejected the request ...`.
    """
    try:
        client = get_client()
        resp = await client.request("POST", f"/undelete_group/{params.group_id}")
        body: dict[str, Any] = resp.json() or {}
        return (
            f"Restored group id {params.group_id} ({_success_text(body)}). "
            f"Check it with `splitwise_get_group` (group_id={params.group_id})."
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_add_user_to_group",
    annotations=ToolAnnotations(
        title="Add User to Splitwise Group",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
async def splitwise_add_user_to_group(params: AddUserToGroupInput) -> str:
    """Add a person to a group, by user id or by inviting a name + email. 🔒 write.

    Calls `POST /add_user_to_group` with `group_id` plus EITHER `user_id` OR all of
    `first_name`, `last_name`, `email` (a oneOf, checked locally before any call; an
    invited email may receive a Splitwise invitation). Splitwise answers HTTP 200 even on
    failure; the client turns `success: false` / non-empty `errors` into an error.
    Refused with `Error: … writes are disabled` unless `SPLITWISE_ALLOW_WRITES=1`. Undo:
    `splitwise_remove_user_from_group` with the returned user id.

    When to Use:
    - To add a friend (user id) or a new person (name + email) to an existing group.

    When NOT to Use:
    - To create a group with its members in one call (use `splitwise_create_group`).
    - To find a friend's user id by name (use `splitwise_resolve_friend` first).

    Returns:
    A confirmation with the user Splitwise returned (`First Last (id N)`) and its `success` value.

    Examples:
    - params = {"group_id": 12345, "user_id": 5823}
    - params = {"group_id": 12345, "first_name": "Ana", "last_name": "Diaz", "email": "ana@example.com"}

    Error Handling:
    Both or neither identity → rejected before the call; `success: false` → `Error: Splitwise
    rejected the request ...`; 403 → you are not a member of the group.
    """
    try:
        data: dict[str, Any] = params.model_dump(exclude_none=True)
        client = get_client()
        resp = await client.request("POST", "/add_user_to_group", data=data)
        body: dict[str, Any] = resp.json() or {}
        user = body.get("user")
        who = fmt_person(user) if user else "the user (no user object returned)"
        return (
            f"Added {who} to group id {params.group_id} ({_success_text(body)}). "
            "Undo with `splitwise_remove_user_from_group`"
            + (f" (group_id={params.group_id}, user_id={user['id']})." if user and user.get("id") else ".")
        )
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_remove_user_from_group",
    annotations=ToolAnnotations(
        title="Remove User from Splitwise Group",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_remove_user_from_group(params: RemoveUserFromGroupInput) -> str:
    """Remove a member from a group. 🔒 destructive write.

    Calls `POST /remove_user_from_group` with `group_id` and `user_id`. Splitwise refuses
    when the member has a non-zero balance in the group (settle up first) — and it does so
    with HTTP 200 + `success: false`, which the client turns into an error. Refused with
    `Error: … writes are disabled` unless `SPLITWISE_ALLOW_WRITES=1`. Undo:
    `splitwise_add_user_to_group` with the same ids.

    When to Use:
    - To drop someone (or yourself) from a group whose balances are settled.

    When NOT to Use:
    - To delete the whole group (use `splitwise_delete_group`).
    - To check balances first (use `splitwise_get_group`).

    Returns:
    A confirmation with the `success` / `errors` values Splitwise returned.

    Examples:
    - params = {"group_id": 12345, "user_id": 5823}

    Error Handling:
    A non-zero balance (or any refusal) → `Error: Splitwise rejected the request ...` with
    Splitwise's message; 403 → you are not a member of the group.
    """
    try:
        data: dict[str, Any] = {"group_id": params.group_id, "user_id": params.user_id}
        client = get_client()
        resp = await client.request("POST", "/remove_user_from_group", data=data)
        body: dict[str, Any] = resp.json() or {}
        return (
            f"Removed user id {params.user_id} from group id {params.group_id} ({_success_text(body)}). "
            f"Undo with `splitwise_add_user_to_group` (group_id={params.group_id}, user_id={params.user_id})."
        )
    except Exception as exc:
        return handle_api_error(exc)
