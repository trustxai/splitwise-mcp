"""Splitwise activity feed: the current user's notifications (inventory F1).

`GET /get_notifications` returns the newest notifications first. Each one carries an
integer `type` (mapped to a name below; Splitwise says the list is incomplete, so an
unmapped code renders as `unknown (N)`), a `source` (`{type, id, url}` — the expense,
group or user it is about) and `content` as limited HTML (`<strong> <strike> <small>
<br> <font>`), flattened to text in markdown and kept raw in JSON.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, field_validator

from splitwise_mcp.client import get_client
from splitwise_mcp.errors import handle_api_error
from splitwise_mcp.formatters import ResponseFormat, clip_response, fmt_person, iso_to_human, strip_html, to_json
from splitwise_mcp.server import mcp
from splitwise_mcp.validators import date_iso

MAX_DISPLAY_ROWS = 50

# research/02-endpoint-inventory.md §F — Splitwise documents this list as incomplete.
NOTIFICATION_TYPES: dict[int, str] = {
    0: "Expense added",
    1: "Expense updated",
    2: "Expense deleted",
    3: "Comment added",
    4: "Added to group",
    5: "Removed from group",
    6: "Group deleted",
    7: "Group settings changed",
    8: "Added as friend",
    9: "Removed as friend",
    10: "News",
    11: "Debt simplification",
    12: "Group undeleted",
    13: "Expense undeleted",
    14: "Group currency conversion",
    15: "Friend currency conversion",
}

# `<strike>old</strike>` marks a replaced value; keep that meaning as ~~old~~ before the tags are stripped.
_STRIKE_RE = re.compile(r"<strike\b[^>]*>(.*?)</strike\s*>", re.IGNORECASE | re.DOTALL)


def notification_type_name(code: Any) -> str:
    """Map a notification `type` code to its name, or `unknown (N)` for an unmapped code."""
    if isinstance(code, int) and not isinstance(code, bool) and code in NOTIFICATION_TYPES:
        return NOTIFICATION_TYPES[code]
    return f"unknown ({code})"


def notification_text(content: Any) -> str:
    """Flatten notification HTML to plain text, keeping struck-through values as `~~old~~`."""
    return strip_html(_STRIKE_RE.sub(r"~~\1~~", str(content if content is not None else "")))


def _cell(text: str) -> str:
    """Make a value safe inside a markdown table cell (escape pipes, keep line breaks as `<br>`)."""
    return text.replace("\r\n", "\n").strip().replace("|", "\\|").replace("\n", "<br>")


def _source(source: dict[str, Any] | None) -> str:
    if not source:
        return "—"
    kind = source.get("type") or "?"
    text = f"{kind} {source['id']}" if source.get("id") is not None else str(kind)
    if source.get("url"):
        text += f" <{source['url']}>"
    return _cell(text)


def _notification_row(item: dict[str, Any]) -> str:
    created_by = item.get("created_by")
    by = fmt_person(None, fallback_id=created_by) if created_by is not None else "—"
    return (
        f"| {iso_to_human(item.get('created_at'))} | {notification_type_name(item.get('type'))} "
        f"| {_cell(notification_text(item.get('content')))} | {_source(item.get('source'))} | {by} "
        f"| {item.get('id', 'N/A')} |"
    )


class GetNotificationsInput(BaseModel):
    """Input for `splitwise_get_notifications`."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    updated_after: str | None = Field(
        default=None,
        description=(
            "Only notifications updated after this ISO-8601 date or datetime (e.g. '2026-10-01' or "
            "'2026-10-01T13:00:00Z'; a bare date means midnight UTC). Omit for the most recent ones."
        ),
    )
    limit: int = Field(
        default=20,
        ge=0,
        le=100,
        description=(
            "Maximum notifications to fetch (1–100, default 20). 0 = the server's maximum, which can be "
            "large — prefer a number or `updated_after`."
        ),
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' (a readable table with the HTML flattened, default) or 'json' (raw objects).",
    )

    @field_validator("updated_after", mode="before")
    @classmethod
    def _normalise_updated_after(cls, value: object) -> object:
        if value is None:
            return None
        return date_iso(value, field="updated_after")


@mcp.tool(
    name="splitwise_get_notifications",
    annotations=ToolAnnotations(
        title="Get Splitwise Notifications",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_get_notifications(params: GetNotificationsInput) -> str:
    """List your recent Splitwise activity (notifications), newest first.

    Calls `GET /get_notifications` and renders one row per notification: when it
    happened, what kind it is (expense added/updated/deleted, comment added, group and
    friend changes, …), its text with the HTML flattened (a struck-through old value
    shows as `~~old~~`), what it is about (source type, id and URL) and who triggered it
    (`user N`). Codes Splitwise has not documented show as `unknown (N)`.

    When to Use:
    - "What changed in my Splitwise lately?" / "Did anyone add an expense this week?"
    - To find the id of a recently added or edited expense or group (the source column).

    When NOT to Use:
    - To list expenses with filters (group, friend, dates) — use `splitwise_get_expenses`.
    - To read the comments on one expense — use `splitwise_get_comments`.
    - To see who owes whom — use `splitwise_get_balances`.

    Returns:
    Markdown: `| When | Type | Content | Source | By | Id |`, at most 50 rows (the rest
    are counted). JSON: `{"count", "limit", "updated_after", "type_names", "notifications":
    [raw objects]}` — `content` stays raw HTML there, and `type_names` maps the codes
    present to their names.

    Pagination:
    There is no offset. `limit` returns the newest N (0 = the server's maximum, which
    can be large); `updated_after` returns only what changed since a point in time (e.g.
    your last check). Combine them to keep the response small.

    Examples:
    - `params = {}` (the 20 most recent)
    - `params = {"updated_after": "2026-10-01", "limit": 50}`
    - `params = {"limit": 5, "response_format": "json"}`

    Error Handling:
    A malformed `updated_after` or a `limit` outside 0–100 is rejected before any call.
    401 means the API key is missing or was regenerated. Errors come back as an
    `Error ...` string, never raised.
    """
    try:
        query: dict[str, Any] = {"updated_after": params.updated_after, "limit": params.limit}
        query = {key: value for key, value in query.items() if value is not None}
        client = get_client()
        resp = await client.request("GET", "/get_notifications", params=query)
        items: list[dict[str, Any]] = resp.json().get("notifications") or []

        if params.response_format is ResponseFormat.JSON:
            codes: list[int] = sorted({code for item in items if isinstance(code := item.get("type"), int)})
            return clip_response(
                to_json(
                    {
                        "count": len(items),
                        "limit": params.limit,
                        "updated_after": params.updated_after,
                        "type_names": {str(code): notification_type_name(code) for code in codes},
                        "notifications": items,
                    }
                )
            )

        lines = ["# Splitwise notifications", ""]
        scope = f"updated after {params.updated_after}" if params.updated_after else "most recent"
        limit_text = "server maximum" if params.limit == 0 else f"limit {params.limit}"
        if not items:
            lines.append(f"_No notifications ({scope}, {limit_text})._")
            return clip_response("\n".join(lines))
        shown = items[:MAX_DISPLAY_ROWS]
        lines.append(f"{len(items)} notification(s), newest first ({scope}, {limit_text}).")
        lines.extend(["", "| When | Type | Content | Source | By | Id |", "|---|---|---|---|---|---|"])
        lines.extend(_notification_row(item) for item in shown)
        if len(items) > len(shown):
            lines.extend(
                [
                    "",
                    f"_Showing the newest {len(shown)} of {len(items)} — narrow with `updated_after`, or use "
                    "`response_format='json'` for all of them._",
                ]
            )
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
