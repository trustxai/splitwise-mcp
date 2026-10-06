"""Settings for the Splitwise MCP server, loaded from env vars / .env."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_API_URL = "https://secure.splitwise.com/api/v3.0"

# The HTTP transport refuses to start with a bearer shorter than this (hex of 16 random
# bytes). `openssl rand -hex 32` gives 64.
MIN_BEARER_LENGTH = 32

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class Settings(BaseSettings):
    """All configuration, derived from `SPLITWISE_*` environment variables.

    Every field has a default so importing the package never fails; a missing API key
    surfaces as a descriptive error on the first request. The `SPLITWISE_MCP_*` fields
    are read ONLY by the streamable-http entry point (`amazing-splitwise-mcp-http`); the
    stdio server ignores them.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Personal API key from https://secure.splitwise.com/apps (the app's details page).
    # Sent as `Authorization: Bearer <key>` on every request. It is an access token for
    # the whole account — treat it like a password; regenerate on the same page to rotate.
    splitwise_api_key: str = ""

    # REST base URL. Override only for proxies/testing.
    splitwise_api_url: str = DEFAULT_API_URL

    splitwise_request_timeout_seconds: float = 30.0

    # Kill-switch for anything that changes account state. Every POST on this API is a
    # write (create/update/delete/undelete expenses, groups, friends, comments; update
    # the user). Off by default: those tools return an error until SPLITWISE_ALLOW_WRITES=1.
    splitwise_allow_writes: bool = False

    # --- streamable-http transport (amazing-splitwise-mcp-http) --------------------
    # Shared secret the HTTP client must present as `Authorization: Bearer <value>`.
    # The server refuses to start when it is shorter than MIN_BEARER_LENGTH.
    splitwise_mcp_bearer: str = ""
    # Bind address. Loopback by default: put a TLS reverse proxy / Tailscale Funnel in
    # front. Binding beyond loopback requires SPLITWISE_MCP_ALLOWED_HOSTS to be set.
    splitwise_mcp_host: str = "127.0.0.1"
    splitwise_mcp_port: int = 8765
    # Path the MCP endpoint is served at (the SDK default is /mcp).
    splitwise_mcp_path: str = "/mcp"
    # Comma-separated Host header values accepted (DNS-rebinding protection), e.g.
    # `ax42.tail8f6c35.ts.net,127.0.0.1:8765`. Empty = protection off (loopback only).
    splitwise_mcp_allowed_hosts: str = ""
    # Comma-separated extra `Origin` values accepted when host pinning is on (the SDK
    # rejects any Origin it does not know with 403). The transport already mirrors the
    # allowed hosts as origins; this is the escape hatch for a cloud client that sends
    # its own Origin (e.g. `https://grok.com`). Ignored when SPLITWISE_MCP_ALLOWED_HOSTS is empty.
    splitwise_mcp_allowed_origins: str = ""
    # Stateless streamable-http (no server-side session ids) — the mode that survives
    # cloud clients and reverse proxies best.
    splitwise_mcp_stateless: bool = True

    # MCP clients pass these via a JSON `env` block, where a pasted value often keeps a
    # trailing space/newline: the key then fails as a header value (and that error echoes
    # it back). Strip every string setting on the way in.
    @field_validator(
        "splitwise_api_key",
        "splitwise_api_url",
        "splitwise_mcp_bearer",
        "splitwise_mcp_host",
        "splitwise_mcp_path",
        "splitwise_mcp_allowed_hosts",
        "splitwise_mcp_allowed_origins",
        mode="before",
    )
    @classmethod
    def _strip_whitespace(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @property
    def has_api_key(self) -> bool:
        return bool(self.splitwise_api_key)

    @property
    def base_url(self) -> str:
        """Effective REST base without a trailing slash."""
        return self.splitwise_api_url.rstrip("/")

    @staticmethod
    def _split_csv(value: str) -> list[str]:
        seen: list[str] = []
        for raw in value.split(","):
            item = raw.strip()
            if item and item not in seen:
                seen.append(item)
        return seen

    @property
    def allowed_hosts(self) -> list[str]:
        """`SPLITWISE_MCP_ALLOWED_HOSTS` as a de-duplicated list (order kept, blanks dropped)."""
        return self._split_csv(self.splitwise_mcp_allowed_hosts)

    @property
    def allowed_origins(self) -> list[str]:
        """`SPLITWISE_MCP_ALLOWED_ORIGINS` as a de-duplicated list (order kept, blanks dropped)."""
        return self._split_csv(self.splitwise_mcp_allowed_origins)

    @property
    def host_is_loopback(self) -> bool:
        return self.splitwise_mcp_host in _LOOPBACK_HOSTS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton (tests call `get_settings.cache_clear()`)."""
    return Settings()
