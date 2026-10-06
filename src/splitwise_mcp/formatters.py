"""Response formatting shared by every tool (markdown / JSON dual output)."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

# Context-window guard: MCP responses cap at 1 MB; stay well under it.
MAX_RESPONSE_BYTES = 900_000

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


class ResponseFormat(StrEnum):
    """Output format selector present on read tools."""

    MARKDOWN = "markdown"
    JSON = "json"


def to_json(data: Any) -> str:
    """Serialize any payload for the LLM (stable, human-readable)."""
    return json.dumps(data, indent=2, default=str)


def iso_to_human(value: Any) -> str:
    """Render a Splitwise ISO-8601 timestamp as `YYYY-MM-DD HH:MM UTC` (date-only input stays a date)."""
    if value in (None, ""):
        return "N/A"
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if len(text) == 10:  # bare date
        return parsed.strftime("%Y-%m-%d")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def fmt_money(amount: Any, currency: str = "", *, signed: bool = True) -> str:
    """Render a Splitwise decimal string as `+12.50 USD` / `-3.00 PEN` / `0.00 USD`.

    `signed=False` drops the plus sign for amounts that are not balances (costs, shares).
    """
    if amount in (None, ""):
        return "N/A"
    try:
        dec = Decimal(str(amount))
    except InvalidOperation:
        return f"{amount} {currency}".strip()
    text = f"{dec.quantize(Decimal('0.01')):f}"
    if signed and dec > 0:
        text = f"+{text}"
    return f"{text} {currency}".strip()


def fmt_person(user: Mapping[str, Any] | None, *, fallback_id: Any = None) -> str:
    """Render a user object as `First Last (id 123)` so an LLM can chain the id."""
    if not user:
        return f"user {fallback_id}" if fallback_id is not None else "unknown user"
    user_id = user.get("id", fallback_id)
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    if not name:
        name = user.get("email") or f"user {user_id}"
    return f"{name} (id {user_id})" if user_id is not None else name


def strip_html(text: Any) -> str:
    """Flatten the limited HTML Splitwise uses in notifications to plain text."""
    if text in (None, ""):
        return ""
    flat = _BR_RE.sub("\n", str(text))
    flat = _TAG_RE.sub("", flat)
    return html.unescape(flat).strip()


def clip_response(text: str, max_bytes: int = MAX_RESPONSE_BYTES) -> str:
    """Truncate an oversized response with an explicit note instead of failing the call."""
    encoded = text.encode()
    if len(encoded) <= max_bytes:
        return text
    cut = encoded[:max_bytes].decode(errors="ignore")
    return f"{cut}\n\n_[truncated: response exceeded {max_bytes:,} bytes — narrow the query]_"


def paginated_response(
    *,
    items: list[dict[str, Any]],
    limit: int,
    offset: int,
    fmt: ResponseFormat,
    item_formatter: Callable[[dict[str, Any]], str],
    title: str,
    total: int | None = None,
) -> str:
    """Uniform paginated output for list tools.

    `has_more` is computed from `total` when the API provides one, otherwise
    from the page being full (`len(items) == limit`).
    """
    count = len(items)
    if total is not None:
        has_more = total > offset + count
    else:
        has_more = count == limit

    if fmt is ResponseFormat.JSON:
        return clip_response(
            to_json(
                {
                    "title": title,
                    "count": count,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "has_more": has_more,
                    "items": items,
                }
            )
        )

    lines = [f"# {title}", ""]
    if total is not None:
        lines.append(f"Showing **{count:,}** of total **{total:,}** (offset {offset:,}).")
    else:
        lines.append(f"Showing **{count:,}** item(s) (offset {offset:,}).")
    if has_more:
        lines.append(f"More available — next offset → **{offset + count:,}**.")
    lines.append("")
    for item in items:
        lines.append(item_formatter(item))
    if not items:
        lines.append("_No items._")
    return clip_response("\n".join(lines))
