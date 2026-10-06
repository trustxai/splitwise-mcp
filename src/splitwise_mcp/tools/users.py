"""Splitwise `users` tools: who you are, another user's profile, and your profile update.

Inventory group A (research/02): `GET /get_current_user`, `GET /get_user/{id}` and
`POST /update_user/{id}`. The update is a write, so the client refuses it unless
`SPLITWISE_ALLOW_WRITES=1`, and the client also refuses `email`/`password` in its body
under any configuration — this module's input model does not even have those fields.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from splitwise_mcp.client import get_client
from splitwise_mcp.errors import handle_api_error
from splitwise_mcp.formatters import ResponseFormat, clip_response, fmt_person, iso_to_human, to_json
from splitwise_mcp.server import mcp
from splitwise_mcp.validators import currency_code

# House context-window guard. This module renders single users only (no lists), so the
# cap never binds today; it is kept so every module carries the same contract.
MAX_DISPLAY_ROWS = 50

# The profile fields `splitwise_update_user` may change, in rendering order.
UPDATABLE_FIELDS = ("first_name", "last_name", "locale", "default_currency")

_LOCALE_PATTERN = r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$"


# -- input models ------------------------------------------------------------


class GetCurrentUserInput(BaseModel):
    """Input for `splitwise_get_current_user`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default) for a readable summary, 'json' for the raw Splitwise user object.",
    )


class GetUserInput(BaseModel):
    """Input for `splitwise_get_user`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_id: int = Field(..., ge=1, description="Splitwise user id (e.g. 491923). Must be a friend or share a group.")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (default) for a readable summary, 'json' for the raw Splitwise user object.",
    )


class UpdateUserInput(BaseModel):
    """Input for `splitwise_update_user`. Deliberately has NO `email` or `password` field."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    user_id: int = Field(
        ...,
        ge=1,
        description="Your own Splitwise user id (from splitwise_get_current_user). Other users' ids are refused (403).",
    )
    first_name: str | None = Field(
        default=None, min_length=1, max_length=255, description="New first name. Omit to leave unchanged."
    )
    last_name: str | None = Field(
        default=None, min_length=1, max_length=255, description="New last name. Omit to leave unchanged."
    )
    locale: str | None = Field(
        default=None,
        pattern=_LOCALE_PATTERN,
        description="New interface locale code such as 'en', 'es' or 'pt-BR'. Omit to leave unchanged.",
    )
    default_currency: str | None = Field(
        default=None,
        description="New default currency, a 3-letter code such as 'USD' or 'PEN' (upper-cased). Omit to leave unchanged.",
    )

    @field_validator("default_currency", mode="before")
    @classmethod
    def _normalise_currency(cls, value: object) -> object:
        if value is None:
            return None
        return currency_code(value, field="default_currency")

    @model_validator(mode="after")
    def _require_one_change(self) -> UpdateUserInput:
        if all(getattr(self, name) is None for name in UPDATABLE_FIELDS):
            raise ValueError(
                "at least one of first_name, last_name, locale, default_currency must be given "
                "(email and password cannot be changed through this server)"
            )
        return self

    def changes(self) -> dict[str, str]:
        """The fields to send, `None` dropped (the exact POST body)."""
        changes: dict[str, str] = {}
        for name in UPDATABLE_FIELDS:
            value = getattr(self, name)
            if value is not None:
                changes[name] = value
        return changes


# -- rendering helpers -------------------------------------------------------


def _format_notification_settings(settings: dict[str, Any] | None) -> str:
    """Render the `notifications` settings block as `expense_added=on, …` (or N/A)."""
    if not settings:
        return "N/A"
    return ", ".join(f"{key}={'on' if value else 'off'}" for key, value in sorted(settings.items()))


def _account_line(user: dict[str, Any]) -> str:
    """`First Last (id N) <email>` — the email only when Splitwise returned one."""
    return fmt_person(user) + (f" <{user['email']}>" if user.get("email") else "")


def _render_current_user(user: dict[str, Any]) -> str:
    lines = [
        "# Current Splitwise user",
        "",
        f"- **account**: {_account_line(user)}",
        f"- **registration status**: {user.get('registration_status') or 'N/A'}",
        f"- **default currency**: {user.get('default_currency') or 'N/A'}",
        f"- **locale**: {user.get('locale') or 'N/A'}",
        f"- **unread notifications**: {user.get('notifications_count', 'N/A')}",
        f"- **notifications last read**: {iso_to_human(user.get('notifications_read'))}",
        f"- **notification settings**: {_format_notification_settings(user.get('notifications'))}",
    ]
    return "\n".join(lines)


def _render_user(user: dict[str, Any]) -> str:
    lines = [
        f"# Splitwise user {fmt_person(user)}",
        "",
        f"- **account**: {_account_line(user)}",
        f"- **registration status**: {user.get('registration_status') or 'N/A'}",
    ]
    return "\n".join(lines)


def _extract_user(body: Any) -> dict[str, Any]:
    """Return the user object from `{"user": {...}}` or a flat user body (update_user)."""
    if not isinstance(body, dict):
        return {}
    wrapped = body.get("user")
    if isinstance(wrapped, dict):
        return wrapped
    return body if "id" in body else {}


# -- tools -------------------------------------------------------------------


@mcp.tool(
    name="splitwise_get_current_user",
    annotations=ToolAnnotations(
        title="Get Current Splitwise User",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_current_user(params: GetCurrentUserInput) -> str:
    """Get the Splitwise user the API key belongs to: name, id, email, currency, locale, notifications.

    Calls `GET /get_current_user` and renders your name and id (as `First Last (id N)`, so
    the id can be chained into other tools), email, registration status, default
    currency, locale, the unread notifications count, when notifications were last read,
    and your email-notification settings (`expense_added=on, …`).

    When to Use:
    - To learn your own user id before building expense shares or updating your profile.
    - To check your default currency or locale before creating expenses.
    - To see how many unread notifications you have (read them with `splitwise_get_notifications`).

    When NOT to Use:
    - To check the API key / kill-switch state (use `splitwise_health_check`).
    - To look up someone else (use `splitwise_get_user`, or `splitwise_resolve_friend` for a name → id).
    - To read balances (use `splitwise_get_balances` / `splitwise_get_friends`).
    - To change your name, locale or default currency (use `splitwise_update_user`).

    Returns:
    Markdown (default): a bullet list with the fields above. JSON: the raw Splitwise
    `current_user` object (incl. `picture`, `notifications_read`, `notifications`).

    Examples:
    - `params = {}`
    - `params = {"response_format": "json"}`

    Error Handling:
    401 means SPLITWISE_API_KEY is missing, mistyped or was regenerated at
    https://secure.splitwise.com/apps. Every failure comes back as an `Error ...` string.
    """
    try:
        client = get_client()
        resp = await client.request("GET", "/get_current_user")
        user = _extract_user(resp.json())
        if not user:
            return "Error: Splitwise returned no user object for GET /get_current_user."
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json(user))
        return clip_response(_render_current_user(user))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_get_user",
    annotations=ToolAnnotations(
        title="Get Splitwise User",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_user(params: GetUserInput) -> str:
    """Get another Splitwise user's public profile by id: name, email, registration status.

    Calls `GET /get_user/{id}`. Splitwise only answers for users you are connected to —
    a friend, or someone who shares a group with you — and for yourself.

    When to Use:
    - To turn a user id seen in an expense, debt or notification into a name and email.
    - To check whether someone is a `confirmed` user or a `dummy`/`invited` placeholder.

    When NOT to Use:
    - For yourself with notification settings, currency and locale (use `splitwise_get_current_user`).
    - To find a user id from a name (use `splitwise_resolve_friend`).
    - For balances with that person (use `splitwise_get_friend` / `splitwise_get_balances`).

    Returns:
    Markdown (default): `First Last (id N) <email>` and the registration status
    (`confirmed | dummy | invited`). JSON: the raw Splitwise `user` object (incl. `picture`).

    Examples:
    - `params = {"user_id": 491923}`
    - `params = {"user_id": 491923, "response_format": "json"}`

    Error Handling:
    403 when the user is neither your friend nor in a group with you; 404 when the id
    does not exist; 401 when the API key is missing or revoked. Every failure comes back
    as an `Error ...` string.
    """
    try:
        client = get_client()
        resp = await client.request("GET", f"/get_user/{params.user_id}")
        user = _extract_user(resp.json())
        if not user:
            return f"Error: Splitwise returned no user object for GET /get_user/{params.user_id}."
        if params.response_format is ResponseFormat.JSON:
            return clip_response(to_json(user))
        return clip_response(_render_user(user))
    except Exception as exc:
        return handle_api_error(exc)


@mcp.tool(
    name="splitwise_update_user",
    annotations=ToolAnnotations(
        title="Update Splitwise Profile",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_update_user(params: UpdateUserInput) -> str:
    """Update your Splitwise profile: first name, last name, locale and/or default currency.

    Calls `POST /update_user/{id}` with only the fields you pass (at least one). This is
    a write: it is refused with `Error: … writes are disabled` unless
    `SPLITWISE_ALLOW_WRITES=1`. Email and password are NOT updatable on purpose — the
    input has no such fields and the server's client refuses to send them under any
    configuration, so an LLM can never change the account's login. There is no undo tool:
    to revert, read the current values first with `splitwise_get_current_user` and call
    this tool again with them.

    When to Use:
    - To fix a typo in your name, or switch the default currency / interface locale.

    When NOT to Use:
    - To change email or password (impossible here by design — do it at https://secure.splitwise.com).
    - To read your profile (use `splitwise_get_current_user`).
    - To rename a friend or group member (Splitwise does not allow editing other users).

    Returns:
    A confirmation naming the user Splitwise returned and, for each field you sent, the
    value as it appears in the response (flagged when the response does not match).

    Examples:
    - `params = {"user_id": 491923, "default_currency": "PEN"}`
    - `params = {"user_id": 491923, "first_name": "Ada", "last_name": "Lovelace", "locale": "es"}`

    Error Handling:
    Validation rejects a call with no field to change, an unknown field (including
    `email`/`password`) or a currency that is not a 3-letter code, before any request.
    403 when `user_id` is not your own id; 400 with Splitwise's field messages for a value
    it rejects. A timeout or 5xx leaves the outcome UNKNOWN — read the profile back with
    `splitwise_get_current_user` before retrying.
    """
    try:
        changes = params.changes()
        client = get_client()
        resp = await client.request("POST", f"/update_user/{params.user_id}", data=changes)
        user = _extract_user(resp.json())
        if not user:
            return (
                f"Splitwise accepted POST /update_user/{params.user_id} but returned no user object; "
                "read the profile back with splitwise_get_current_user to confirm the change."
            )
        lines = [
            f"# Profile updated: {fmt_person(user)}",
            "",
            "Fields sent, as Splitwise returned them:",
        ]
        for name, sent in changes.items():
            if name not in user:
                lines.append(f"- **{name}**: sent `{sent}` — not present in the response")
                continue
            returned = user.get(name)
            flag = "" if str(returned) == sent else f" (sent `{sent}` — the response differs)"
            lines.append(f"- **{name}**: `{returned}`{flag}")
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
