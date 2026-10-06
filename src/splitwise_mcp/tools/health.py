"""Health-check tool — the in-repo exemplar of the house tool style.

Wave workers: copy this module's shape (decorator, annotations, docstring sections,
try/except -> handle_api_error, `-> str` return, `fmt_person` for people). It calls the
one endpoint every account can answer, `GET /get_current_user`, and reports the write
kill-switch so an LLM knows up front whether mutations will be refused.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

from splitwise_mcp.client import get_client
from splitwise_mcp.config import get_settings
from splitwise_mcp.errors import handle_api_error
from splitwise_mcp.formatters import clip_response, fmt_person
from splitwise_mcp.server import mcp


def _format_notification_settings(settings: dict[str, Any] | None) -> str:
    """Render the `notifications` settings block as `expense_added=on, …` (or N/A)."""
    if not settings:
        return "N/A"
    return ", ".join(f"{key}={'on' if value else 'off'}" for key, value in sorted(settings.items()))


@mcp.tool(
    name="splitwise_health_check",
    annotations=ToolAnnotations(
        title="Splitwise Health Check",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
async def splitwise_health_check() -> str:
    """Verify connectivity and the API key against Splitwise, and report the write kill-switch.

    Calls `GET /get_current_user` with the configured personal API key and renders who
    you are logged in as (name, id, email, default currency, locale, unread
    notifications). Also states whether writes are enabled (SPLITWISE_ALLOW_WRITES).

    When to Use:
    - As the first call after configuring the server, to confirm the key works.
    - To learn your own user id before building expense shares.
    - To debug a 401 (key missing / regenerated) before trying other tools.

    When NOT to Use:
    - To read balances (use `splitwise_get_balances` / `splitwise_get_friends`).
    - To change profile settings (use `splitwise_update_user`).

    Returns:
    A markdown block with the base URL, the kill-switch state, connectivity, and the
    current user — or an `Error ...` string describing the failure.

    Error Handling:
    401 means the key is missing, mistyped, or was regenerated at
    https://secure.splitwise.com/apps; a connection error means the API URL or the
    network is wrong. Without a configured key the tool returns the config state and
    says what to set, without calling the API.
    """
    try:
        settings = get_settings()
        lines = [
            "# Splitwise health",
            "",
            f"- **base URL**: {settings.base_url}",
            "- **write kill-switch**: "
            + (
                "ENABLED — creating/updating/deleting is allowed"
                if settings.splitwise_allow_writes
                else "disabled (read-only; set SPLITWISE_ALLOW_WRITES=1 to allow writes)"
            ),
        ]
        if not settings.has_api_key:
            lines.append(
                "- **credentials**: none configured — set SPLITWISE_API_KEY "
                "(https://secure.splitwise.com/apps → your app → API key)."
            )
            return "\n".join(lines)
        client = get_client()
        resp = await client.request("GET", "/get_current_user")
        user: dict[str, Any] = resp.json().get("user") or {}
        lines.extend(
            [
                "- **connectivity**: OK",
                f"- **account**: {fmt_person(user)}" + (f" <{user['email']}>" if user.get("email") else ""),
                f"- **default currency**: {user.get('default_currency') or 'N/A'}",
                f"- **locale**: {user.get('locale') or 'N/A'}",
                f"- **unread notifications**: {user.get('notifications_count', 'N/A')}",
                f"- **notification settings**: {_format_notification_settings(user.get('notifications'))}",
            ]
        )
        if client.last_retry_after:
            lines.append(f"- **retry-after**: {client.last_retry_after}s (Splitwise asked to slow down)")
        return clip_response("\n".join(lines))
    except Exception as exc:
        return handle_api_error(exc)
