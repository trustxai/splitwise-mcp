"""Unit tests for Settings and the cached singleton."""

from __future__ import annotations

import pytest

from splitwise_mcp.config import DEFAULT_API_URL, MIN_BEARER_LENGTH, Settings, get_settings


def test_defaults() -> None:
    settings = Settings()
    assert settings.splitwise_api_key == ""
    assert settings.splitwise_api_url == DEFAULT_API_URL
    assert settings.splitwise_request_timeout_seconds == 30.0
    assert settings.splitwise_allow_writes is False
    assert settings.splitwise_mcp_bearer == ""
    assert settings.splitwise_mcp_host == "127.0.0.1"
    assert settings.splitwise_mcp_port == 8765
    assert settings.splitwise_mcp_path == "/mcp"
    assert settings.splitwise_mcp_allowed_hosts == ""
    assert settings.splitwise_mcp_allowed_origins == ""
    assert settings.splitwise_mcp_stateless is True
    assert settings.has_api_key is False
    assert settings.base_url == DEFAULT_API_URL
    assert settings.allowed_hosts == []
    assert settings.host_is_loopback is True
    assert MIN_BEARER_LENGTH == 32


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPLITWISE_API_KEY", "key123")
    monkeypatch.setenv("SPLITWISE_REQUEST_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("SPLITWISE_ALLOW_WRITES", "1")
    monkeypatch.setenv("SPLITWISE_MCP_BEARER", "b" * 40)
    monkeypatch.setenv("SPLITWISE_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("SPLITWISE_MCP_PORT", "9000")
    monkeypatch.setenv("SPLITWISE_MCP_PATH", "/splitwise")
    monkeypatch.setenv("SPLITWISE_MCP_STATELESS", "false")
    settings = Settings()
    assert settings.splitwise_api_key == "key123"
    assert settings.has_api_key is True
    assert settings.splitwise_request_timeout_seconds == 5.5
    assert settings.splitwise_allow_writes is True
    assert settings.splitwise_mcp_bearer == "b" * 40
    assert settings.splitwise_mcp_host == "0.0.0.0"
    assert settings.host_is_loopback is False
    assert settings.splitwise_mcp_port == 9000
    assert settings.splitwise_mcp_path == "/splitwise"
    assert settings.splitwise_mcp_stateless is False


@pytest.mark.parametrize("pad", [" ", "\t", "\n", " \t\n"])
def test_string_settings_are_stripped(monkeypatch: pytest.MonkeyPatch, pad: str) -> None:
    # A pasted key with a trailing space/newline is an illegal header value, and the
    # resulting error echoes the key back to the model.
    monkeypatch.setenv("SPLITWISE_API_KEY", f"{pad}FAKEKEY123{pad}")
    monkeypatch.setenv("SPLITWISE_MCP_BEARER", f"{pad}fakebearer456{pad}")
    monkeypatch.setenv("SPLITWISE_API_URL", f"{pad}https://example.test/api/v3.0/{pad}")
    monkeypatch.setenv("SPLITWISE_MCP_HOST", f"{pad}127.0.0.1{pad}")
    settings = Settings()
    assert settings.splitwise_api_key == "FAKEKEY123"
    assert settings.splitwise_mcp_bearer == "fakebearer456"
    assert settings.splitwise_api_url == "https://example.test/api/v3.0/"
    assert settings.base_url == "https://example.test/api/v3.0"
    assert settings.host_is_loopback is True


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_blank_key_is_empty(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SPLITWISE_API_KEY", value)
    settings = Settings()
    assert settings.splitwise_api_key == ""
    assert settings.has_api_key is False


def test_allowed_hosts_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "SPLITWISE_MCP_ALLOWED_HOSTS", " ax42.tail8f6c35.ts.net, 127.0.0.1:8765 ,,localhost:8765, 127.0.0.1:8765 "
    )
    assert Settings().allowed_hosts == ["ax42.tail8f6c35.ts.net", "127.0.0.1:8765", "localhost:8765"]


def test_allowed_origins_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().allowed_origins == []
    monkeypatch.setenv("SPLITWISE_MCP_ALLOWED_ORIGINS", " https://grok.com ,https://grok.com, ,https://x.ai ")
    settings = Settings()
    assert settings.splitwise_mcp_allowed_origins == "https://grok.com ,https://grok.com, ,https://x.ai"
    assert settings.allowed_origins == ["https://grok.com", "https://x.ai"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_hosts(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setenv("SPLITWISE_MCP_HOST", host)
    assert Settings().host_is_loopback is True


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
