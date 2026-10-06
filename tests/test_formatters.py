"""Unit tests for the shared formatters."""

from __future__ import annotations

import json
from typing import Any

from splitwise_mcp.formatters import (
    MAX_RESPONSE_BYTES,
    ResponseFormat,
    clip_response,
    fmt_money,
    fmt_person,
    iso_to_human,
    paginated_response,
    strip_html,
    to_json,
)


def test_iso_to_human() -> None:
    assert iso_to_human("2012-05-02T13:00:00Z") == "2012-05-02 13:00 UTC"
    assert iso_to_human("2012-05-02T08:00:00-05:00") == "2012-05-02 13:00 UTC"
    assert iso_to_human("2012-05-02") == "2012-05-02"
    assert iso_to_human(None) == "N/A"
    assert iso_to_human("") == "N/A"
    assert iso_to_human("not a date") == "not a date"


def test_fmt_money() -> None:
    assert fmt_money("414.5", "USD") == "+414.50 USD"
    assert fmt_money("-3", "PEN") == "-3.00 PEN"
    assert fmt_money("0", "USD") == "0.00 USD"
    assert fmt_money("25.0", "USD", signed=False) == "25.00 USD"
    assert fmt_money("25.0") == "+25.00"
    assert fmt_money(None, "USD") == "N/A"
    assert fmt_money("abc", "USD") == "abc USD"


def test_fmt_person() -> None:
    assert fmt_person({"id": 7, "first_name": "Ada", "last_name": "Lovelace"}) == "Ada Lovelace (id 7)"
    assert fmt_person({"id": 7, "first_name": "Ada", "last_name": None}) == "Ada (id 7)"
    assert fmt_person({"id": 7, "email": "ada@example.com"}) == "ada@example.com (id 7)"
    assert fmt_person({"id": 7}) == "user 7 (id 7)"
    assert fmt_person({"first_name": "Ada"}) == "Ada"
    assert fmt_person(None, fallback_id=9) == "user 9"
    assert fmt_person({}) == "unknown user"


def test_strip_html() -> None:
    raw = '<strong>You</strong> paid <strong>Jon H.</strong><br/>&amp; got <font color="#FFEE44">$5</font>'
    assert strip_html(raw) == "You paid Jon H.\n& got $5"
    assert strip_html(None) == ""
    assert strip_html("plain") == "plain"


def test_clip_response_passthrough_and_truncate() -> None:
    assert clip_response("short") == "short"
    big = "x" * (MAX_RESPONSE_BYTES + 10)
    clipped = clip_response(big)
    assert clipped.endswith("narrow the query]_")
    body = clipped.split("\n\n_[truncated")[0]
    assert len(body.encode()) == MAX_RESPONSE_BYTES
    assert clip_response(big, max_bytes=10).startswith("x" * 10 + "\n\n_[truncated: response exceeded 10 bytes")


def test_to_json_handles_non_serialisable() -> None:
    assert json.loads(to_json({"a": {1, 2}}))["a"] in ("{1, 2}", "{2, 1}")


def _fmt(item: dict[str, Any]) -> str:
    return f"- {item['name']}"


def test_paginated_response_markdown_has_more_from_full_page() -> None:
    items = [{"name": "a"}, {"name": "b"}]
    text = paginated_response(
        items=items, limit=2, offset=0, fmt=ResponseFormat.MARKDOWN, item_formatter=_fmt, title="T"
    )
    assert text.startswith("# T")
    assert "Showing **2** item(s) (offset 0)." in text
    assert "next offset → **2**" in text
    assert "- a\n- b" in text


def test_paginated_response_markdown_no_more_and_empty() -> None:
    text = paginated_response(
        items=[{"name": "a"}], limit=2, offset=0, fmt=ResponseFormat.MARKDOWN, item_formatter=_fmt, title="T"
    )
    assert "More available" not in text
    empty = paginated_response(items=[], limit=2, offset=4, fmt=ResponseFormat.MARKDOWN, item_formatter=_fmt, title="T")
    assert "_No items._" in empty


def test_paginated_response_json_uses_total() -> None:
    text = paginated_response(
        items=[{"name": "a"}], limit=1, offset=0, fmt=ResponseFormat.JSON, item_formatter=_fmt, title="T", total=5
    )
    payload = json.loads(text)
    assert payload["has_more"] is True
    assert payload["total"] == 5
    assert payload["items"] == [{"name": "a"}]
