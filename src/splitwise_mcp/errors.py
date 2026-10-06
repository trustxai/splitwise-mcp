"""Uniform error-to-string mapping for tool responses.

Splitwise error bodies come in two shapes: `{"error": "Invalid API request: you are not
logged in"}` (401) and `{"errors": {"base": ["..."]}}` / `{"errors": {"<field>":
["..."]}}` (400/403/404). Both are surfaced, plus a hint for the statuses that have a
known fix.

Every returned string goes to the LLM transcript, so it is scrubbed of configured
credential values (the API key and the HTTP bearer), and transport errors (whose text can
quote a request header, i.e. the API key) are described by type only.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from splitwise_mcp.config import get_settings

# Configured values shorter than this are not redacted: a tiny test value such as
# "key" would otherwise mangle ordinary words in the message.
_MIN_REDACT_LEN = 8

_KEY_HINT = (
    "The SPLITWISE_API_KEY is missing, invalid, or was regenerated — create or copy the key on your app's "
    "page at https://secure.splitwise.com/apps."
)

_EXPENSE_400_HINT = (
    "Check that money is a 2-decimal string, ids exist, and that paid shares and owed shares each sum to the cost."
)
_GENERIC_400_HINT = "Splitwise names the offending field in the message — check the ids and values you sent."


_EXPENSE_WRITE_PATH = re.compile(r"/(create_expense|update_expense/\d+)/?$")


def _hint_for_400(path: str) -> str:
    """The 400 hint depends on the endpoint: the money/shares rules only apply to expense writes.

    Matched on the endpoint segment only, so a `/get_expenses` 400 (a bad date filter) or a
    base URL that happens to contain "expense" does not get the shares hint.
    """
    return _EXPENSE_400_HINT if _EXPENSE_WRITE_PATH.search(path) else _GENERIC_400_HINT


def format_errors(errors: Any) -> str:
    """Flatten Splitwise's `errors` payload (`{"base": [...]}` / `{"field": [...]}` / a list) into one line."""
    if errors in (None, "", {}, []):
        return ""
    if isinstance(errors, dict):
        parts: list[str] = []
        for key, value in errors.items():
            messages = value if isinstance(value, list) else [value]
            text = "; ".join(str(m) for m in messages if m not in (None, ""))
            parts.append(text if key == "base" else f"{key}: {text}")
        return "; ".join(p for p in parts if p)
    if isinstance(errors, list):
        return "; ".join(str(m) for m in errors if m not in (None, ""))
    return str(errors)


def _detail_from_body(resp: httpx.Response) -> str:
    """Extract Splitwise's `error` / `errors` from a response body."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        if body.get("error"):
            return str(body["error"])
        detail = format_errors(body.get("errors"))
        if detail:
            return detail
        if body.get("message"):
            return str(body["message"])
    return resp.text[:300]


def redact_credentials(text: str) -> str:
    """Replace every configured credential value in `text` with ``***``.

    Covers the Splitwise API key and the HTTP bearer, each in its raw and
    whitespace-stripped form plus the repr-escaped form (OSError and h11 quote values via
    repr, so a newline inside one shows up as a literal backslash-n). Values under
    `_MIN_REDACT_LEN` characters (after stripping) are skipped. If settings cannot be
    loaded (e.g. an invalid env var), the text is returned as-is rather than failing the tool.
    """
    try:
        settings = get_settings()
    except (ValueError, OSError):  # pydantic ValidationError / SettingsError are ValueErrors
        return text
    values: set[str] = set()
    for raw in (settings.splitwise_api_key, settings.splitwise_mcp_bearer):
        stripped = raw.strip()
        if len(stripped) >= _MIN_REDACT_LEN:
            values.update((raw, stripped, repr(raw)[1:-1], repr(stripped)[1:-1]))
    # Longest first, so a value that contains another is replaced whole.
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


def handle_api_error(exc: Exception) -> str:
    """Map an exception to a human-readable `Error ...` string for the LLM.

    Tools never raise: every tool body is wrapped in try/except and returns
    this string on failure. The string never carries a configured credential.
    """
    return redact_credentials(_describe(exc))


def _describe(exc: Exception) -> str:
    """Unredacted message for `handle_api_error`."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = _detail_from_body(exc.response)
        retry_after = exc.response.headers.get("retry-after")
        if status == 400:
            return f"Error (400): Bad request – {detail}. {_hint_for_400(exc.request.url.path)}"
        if status == 401:
            return f"Error (401): Unauthorized – {detail}. {_KEY_HINT}"
        if status == 403:
            return (
                f"Error (403): Forbidden – {detail}. You are not a member of that group / party to that expense, "
                "or the record belongs to someone else."
            )
        if status == 404:
            return f"Error (404): Not found – {detail}. The id does not exist, or the record was deleted."
        if status == 429:
            wait = f" Retry-After: {retry_after}s." if retry_after else ""
            return f"Error (429): Rate limited – {detail}.{wait} Back off; do not retry in a tight loop."
        if status >= 500:
            return (
                f"Error ({status}): Splitwise-side failure – {detail}. Execution status is UNKNOWN: for a write, "
                "read the record back before retrying to avoid a duplicate."
            )
        return f"Error ({status}): {detail}"
    if isinstance(exc, httpx.TimeoutException):
        return (
            "Error: the Splitwise API request timed out. Execution status is UNKNOWN for a write — read the "
            "record back before retrying. Raise SPLITWISE_REQUEST_TIMEOUT_SECONDS if this recurs."
        )
    if isinstance(exc, httpx.ConnectError):
        return "Error: could not connect to the Splitwise API. Check network access and SPLITWISE_API_URL."
    # After the timeout/connect branches (all TransportError subclasses). Never include
    # str(exc): h11's "Illegal header value b'...'" quotes the header verbatim, i.e. the API key.
    if isinstance(exc, httpx.LocalProtocolError):
        return (
            "Error: the HTTP client rejected the Splitwise API request (LocalProtocolError). This usually means "
            "stray whitespace or newlines in SPLITWISE_API_KEY — re-check the value. Execution status is UNKNOWN "
            "for a write — read the record back before retrying."
        )
    if isinstance(exc, httpx.UnsupportedProtocol):
        return (
            "Error: SPLITWISE_API_URL is not an http(s) URL — it must start with https:// "
            "(e.g. https://secure.splitwise.com/api/v3.0)."
        )
    if isinstance(exc, httpx.TransportError):
        return (
            f"Error: the Splitwise API request failed at the network layer ({type(exc).__name__}). Check network "
            "access, any HTTPS_PROXY setting, and SPLITWISE_API_URL. Execution status is UNKNOWN for a write "
            "— read the record back before retrying."
        )
    if isinstance(exc, RuntimeError):
        return f"Error: {exc}"
    return f"Error: unexpected failure – {type(exc).__name__}: {exc}"
