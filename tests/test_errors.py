"""Unit tests for error mapping and credential redaction."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from splitwise_mcp.config import Settings
from splitwise_mcp.errors import format_errors, handle_api_error, redact_credentials


def _status_error(
    status: int, body: Any = None, *, text: str | None = None, headers: dict[str, str] | None = None
) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://secure.splitwise.com/api/v3.0/get_current_user")
    if text is not None:
        response = httpx.Response(status, text=text, request=request, headers=headers)
    else:
        response = httpx.Response(status, json=body, request=request, headers=headers)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_format_errors_shapes() -> None:
    assert format_errors(None) == ""
    assert format_errors({}) == ""
    assert format_errors([]) == ""
    assert format_errors({"base": ["Invalid group"]}) == "Invalid group"
    assert format_errors({"base": ["a", "b"], "cost": ["must be > 0"]}) == "a; b; cost: must be > 0"
    assert format_errors(["x", "y"]) == "x; y"
    assert format_errors({"cost": "bad"}) == "cost: bad"
    assert format_errors("plain") == "plain"


def test_400_includes_field_errors_and_a_generic_hint_off_the_expense_paths() -> None:
    message = handle_api_error(_status_error(400, {"errors": {"user_email": ["is invalid"]}}))
    assert message.startswith("Error (400): Bad request – user_email: is invalid.")
    assert "sum to the cost" not in message
    assert "names the offending field" in message


@pytest.mark.parametrize("path", ["/create_expense", "/update_expense/51023"])
def test_400_on_expense_paths_gets_the_money_and_shares_hint(path: str) -> None:
    request = httpx.Request("POST", f"https://secure.splitwise.com/api/v3.0{path}")
    response = httpx.Response(400, json={"errors": {"cost": ["must be a number"]}}, request=request)
    message = handle_api_error(httpx.HTTPStatusError("boom", request=request, response=response))
    assert message.startswith("Error (400): Bad request – cost: must be a number.")
    assert "sum to the cost" in message
    assert "names the offending field" not in message


def test_401_points_at_the_apps_page() -> None:
    message = handle_api_error(_status_error(401, {"error": "Invalid API request: you are not logged in"}))
    assert "Error (401): Unauthorized – Invalid API request: you are not logged in." in message
    assert "secure.splitwise.com/apps" in message


def test_403_and_404() -> None:
    assert "Error (403): Forbidden – Not allowed." in handle_api_error(
        _status_error(403, {"errors": {"base": ["Not allowed."]}})
    )
    assert "Error (404): Not found – Record not found." in handle_api_error(
        _status_error(404, {"errors": {"base": ["Record not found."]}})
    )


def test_429_mentions_retry_after() -> None:
    message = handle_api_error(_status_error(429, {"error": "slow down"}, headers={"retry-after": "30"}))
    assert "Error (429): Rate limited – slow down. Retry-After: 30s." in message


def test_5xx_is_execution_unknown() -> None:
    message = handle_api_error(_status_error(502, text="<html>bad gateway</html>"))
    assert message.startswith("Error (502): Splitwise-side failure – <html>bad gateway</html>.")
    assert "UNKNOWN" in message


def test_other_status_falls_through() -> None:
    assert handle_api_error(_status_error(418, {"message": "teapot"})) == "Error (418): teapot"


def test_transport_errors() -> None:
    assert "timed out" in handle_api_error(httpx.ReadTimeout("t"))
    assert "could not connect" in handle_api_error(httpx.ConnectError("c"))
    local = handle_api_error(httpx.LocalProtocolError("Illegal header value b'FAKE-KEY-VALUE'"))
    assert "FAKE-KEY-VALUE" not in local
    assert "stray whitespace" in local
    assert "must start with https://" in handle_api_error(httpx.UnsupportedProtocol("u"))
    generic = handle_api_error(httpx.ReadError("r"))
    assert "network layer (ReadError)" in generic


def test_runtime_and_unexpected() -> None:
    assert handle_api_error(RuntimeError("writes are disabled")) == "Error: writes are disabled"
    assert handle_api_error(ValueError("nope")) == "Error: unexpected failure – ValueError: nope"


def test_redaction_covers_key_and_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Settings strip outer whitespace, so the stored values are the stripped ones; a value
    # with an INNER newline keeps it, and its repr-escaped form must be covered too.
    settings = Settings(splitwise_api_key="SECRETKEY-abc\ndef123", splitwise_mcp_bearer="BEARERTOKEN-xyz987")
    monkeypatch.setattr("splitwise_mcp.errors.get_settings", lambda: settings)
    text = "key=SECRETKEY-abc\ndef123 raw='SECRETKEY-abc\\ndef123' bearer=BEARERTOKEN-xyz987 short=key"
    assert redact_credentials(text) == "key=*** raw='***' bearer=*** short=key"


def test_redaction_skips_short_values(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(splitwise_api_key="key")
    monkeypatch.setattr("splitwise_mcp.errors.get_settings", lambda: settings)
    assert redact_credentials("the key is key") == "the key is key"


def test_redaction_survives_broken_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Settings:
        raise ValueError("bad env")

    monkeypatch.setattr("splitwise_mcp.errors.get_settings", boom)
    assert redact_credentials("unchanged") == "unchanged"


def test_handle_api_error_redacts(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(splitwise_api_key="SECRETKEY-abcdef123")
    monkeypatch.setattr("splitwise_mcp.errors.get_settings", lambda: settings)
    message = handle_api_error(RuntimeError("leaked SECRETKEY-abcdef123 here"))
    assert message == "Error: leaked *** here"
