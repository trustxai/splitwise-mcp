"""Async HTTP client for the Splitwise API v3.0.

One auth scheme: every request carries ``Authorization: Bearer <SPLITWISE_API_KEY>`` (a
personal API key from https://secure.splitwise.com/apps). GET parameters travel in the
query string; POST bodies travel as JSON, **flattened** the way Splitwise wants nested
user lists: ``users=[{"user_id": 1, "paid_share": "10.00"}]`` becomes
``{"users__0__user_id": 1, "users__0__paid_share": "10.00"}``.

Three safety rails live HERE, not in the tools, so no wave module can forget them:

- **Write kill-switch.** Every non-GET on this API changes account state (there is no
  POST that only reads), so any non-GET is refused with ``WritesDisabledError`` unless
  ``SPLITWISE_ALLOW_WRITES=1``.
- **Forbidden fields.** ``password`` is never sent anywhere, and ``email`` is never sent
  to ``/update_user/{id}`` — an LLM must not be able to change the account's login
  (``ForbiddenFieldError``). ``email`` stays legal where it names *another* person
  (``add_user_to_group``, ``users__i__email`` shares, ``user_email`` in ``create_friend``).
- **Envelope check.** Splitwise answers ``200 OK`` for failed writes and signals the
  failure in the body (``success: false`` or a non-empty ``errors``). The client raises
  ``SplitwiseEnvelopeError`` so tools never have to remember which endpoint lies.

Tools call ``client.request(method, path, params=..., data=...)`` and nothing else.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from splitwise_mcp.config import Settings, get_settings
from splitwise_mcp.errors import format_errors

# Fields that may never reach Splitwise, by path prefix ("" = every path).
FORBIDDEN_FIELDS: dict[str, frozenset[str]] = {
    "": frozenset({"password"}),
    "/update_user": frozenset({"email", "password"}),
}


class SplitwiseEnvelopeError(RuntimeError):
    """A 200 response whose body says the write failed (`success: false` / non-empty `errors`)."""

    def __init__(self, errors: Any, *, path: str = "") -> None:
        self.errors = errors
        self.path = path
        detail = format_errors(errors) or "the response reported success=false with no error message"
        super().__init__(f"Splitwise rejected the request{f' to {path}' if path else ''}: {detail}")


class WritesDisabledError(RuntimeError):
    """Raised for a non-GET request while SPLITWISE_ALLOW_WRITES is off."""


class ForbiddenFieldError(RuntimeError):
    """Raised when a body carries a field this server refuses to send under any configuration."""


def _encode_value(value: Any) -> Any:
    """Render one value the way Splitwise expects it (bools as `true`/`false`, Decimal as str)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return f"{value:f}"
    return value


def build_query(params: Mapping[str, Any] | None) -> str:
    """Build the query string for a GET (None values dropped, bools lower-cased)."""
    if not params:
        return ""
    pairs = [(key, str(_encode_value(value))) for key, value in params.items() if value is not None]
    return urlencode(pairs)


def flatten_data(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """Flatten a POST body into the flat JSON object Splitwise expects.

    A list of mappings under key `k` becomes `k__{i}__{prop}` entries (index from 0);
    `None` values are dropped at both levels; bools become `true`/`false`; Decimals
    become strings. Everything else is passed through as-is.
    """
    flat: dict[str, Any] = {}
    if not data:
        return flat
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, list) and value and all(isinstance(item, Mapping) for item in value):
            for index, item in enumerate(value):
                for prop, prop_value in item.items():
                    if prop_value is None:
                        continue
                    flat[f"{key}__{index}__{prop}"] = _encode_value(prop_value)
            continue
        flat[key] = _encode_value(value)
    return flat


def forbidden_fields_for(path: str) -> frozenset[str]:
    """Union of the forbidden-field sets whose prefix matches `path`."""
    fields: set[str] = set()
    for prefix, names in FORBIDDEN_FIELDS.items():
        if not prefix or path == prefix or path.startswith(prefix + "/"):
            fields |= names
    return frozenset(fields)


def is_write(method: str) -> bool:
    """Every non-GET on the Splitwise API changes account state."""
    return method.upper() != "GET"


class SplitwiseClient:
    """Thin async wrapper around httpx for the Splitwise REST API."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        # Injectable transport so tests can use httpx.MockTransport.
        self._transport = transport
        # Diagnostics captured from the last response (the health tool reports them).
        self.last_retry_after: str | None = None

    # -- guards --------------------------------------------------------------

    def _require_key(self) -> None:
        if not self._settings.has_api_key:
            raise RuntimeError(
                "No Splitwise API key configured. Set SPLITWISE_API_KEY in the environment or .env "
                "(https://secure.splitwise.com/apps → your app → API key)."
            )

    def _guard(self, method: str, path: str, data: Mapping[str, Any] | None) -> None:
        if data:
            forbidden = forbidden_fields_for(path) & set(data)
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ForbiddenFieldError(
                    f"{method.upper()} {path} would send {names}, which this server never sends: changing the "
                    "account's login (email/password) is not possible through the MCP, by design."
                )
        if is_write(method) and not self._settings.splitwise_allow_writes:
            raise WritesDisabledError(
                f"{method.upper()} {path} would change your Splitwise account, but writes are disabled. "
                "Set SPLITWISE_ALLOW_WRITES=1 to allow creating, updating and deleting expenses, groups, "
                "friends and comments. Reads work without it."
            )

    # -- requests ------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Perform a request and return the response.

        Raises `httpx.HTTPStatusError` on non-2xx (tools route it through
        `splitwise_mcp.errors.handle_api_error`), `RuntimeError` when the key is
        missing, `WritesDisabledError` / `ForbiddenFieldError` / `SplitwiseEnvelopeError`
        per the rails above.
        """
        settings = self._settings
        self._guard(method, path, data)
        self._require_key()

        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {settings.splitwise_api_key}",
        }
        url = f"{settings.base_url}/{path.lstrip('/')}"
        query = build_query(params)
        if query:
            url = f"{url}?{query}"
        body = flatten_data(data) if data is not None else None
        effective_timeout = timeout if timeout is not None else settings.splitwise_request_timeout_seconds

        async with httpx.AsyncClient(timeout=effective_timeout, transport=self._transport) as client:
            resp = await client.request(method.upper(), url, headers=headers, json=body)

        self.last_retry_after = resp.headers.get("retry-after")
        resp.raise_for_status()
        self._check_envelope(resp, path)
        return resp

    @staticmethod
    def _check_envelope(resp: httpx.Response, path: str) -> None:
        """A 200 whose body says `success: false` or carries a non-empty `errors` is a failure."""
        if not resp.headers.get("content-type", "").startswith("application/json"):
            return
        try:
            body = resp.json()
        except ValueError:
            return
        if not isinstance(body, dict):
            return
        if body.get("success") is False:
            raise SplitwiseEnvelopeError(body.get("errors"), path=path)
        errors = body.get("errors")
        if errors and format_errors(errors):
            raise SplitwiseEnvelopeError(errors, path=path)


_client: SplitwiseClient | None = None


def get_client() -> SplitwiseClient:
    """Lazy module-level singleton (tools monkeypatch this in unit tests)."""
    global _client
    if _client is None:
        _client = SplitwiseClient()
    return _client
