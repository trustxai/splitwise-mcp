"""Input normalisers shared by every tool module's pydantic models.

Each function takes the raw value the LLM sent, returns the exact string Splitwise
expects on the wire, and raises `ValueError` with a readable message otherwise — so a
`field_validator` can call it directly and pydantic turns the message into the tool's
`Error:` string. Tool modules must use these instead of re-implementing the rules.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_TWO_PLACES = Decimal("0.01")


def money(value: object, *, field: str = "amount", allow_zero: bool = False) -> str:
    """Validate a money amount and return it as a string with exactly two decimals.

    Splitwise takes money as decimal strings limited to 2 decimal places (`"25.50"`); we
    never send floats. Accepts `str`, `int` or `Decimal`; rejects floats explicitly
    (binary floats are how `0.1 + 0.2` becomes `0.30000000000000004`).
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{field} must be a decimal string like '25.50', not a float/bool")
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field} must be a decimal string like '25.50'") from None
    if not dec.is_finite():
        raise ValueError(f"{field} must be a finite decimal")
    if dec < 0 or (dec == 0 and not allow_zero):
        raise ValueError(f"{field} must be {'zero or ' if allow_zero else ''}positive")
    try:
        quantized = dec.quantize(_TWO_PLACES)
    except InvalidOperation:
        # quantize overflows the default 28-digit context for absurd magnitudes ("1e30").
        raise ValueError(f"{field} is too large to be a money amount (got {value!s})") from None
    if dec != quantized:
        raise ValueError(f"{field} must have at most 2 decimal places (got {value!s})")
    return f"{quantized:f}"


def currency_code(value: object, *, field: str = "currency_code") -> str:
    """Upper-case a 3-letter currency code (Splitwise uses ISO 4217 plus a few colloquial codes like BTC)."""
    code = str(value).strip().upper()
    if not _CURRENCY_RE.match(code):
        raise ValueError(f"{field} must be a 3-letter code like USD or PEN (got {value!s})")
    return code


def date_iso(value: object, *, field: str = "date") -> str:
    """Normalise a date/datetime to the `YYYY-MM-DDTHH:MM:SSZ` form Splitwise returns.

    Accepts a bare date (`2026-10-05` → midnight UTC), a naive datetime (taken as UTC), or
    an aware one (converted to UTC). `Z` is accepted as a suffix.
    """
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} must be an ISO-8601 date or datetime")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be ISO-8601 (e.g. 2026-10-05 or 2026-10-05T13:00:00Z), got {text!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
