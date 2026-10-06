"""Streamable-HTTP transport behind a shared bearer token (`amazing-splitwise-mcp-http`).

stdio stays the default transport; this module exists because Grok Bot only accepts an
MCP server at a public URL (research/03). The shape:

- `build_app(mcp, settings)` configures the SDK's streamable-http settings on the FastMCP
  instance (host, port, path, stateless, SSE responses, DNS-rebinding host pinning) and
  wraps `mcp.streamable_http_app()` in `BearerAuthMiddleware`.
- `BearerAuthMiddleware` is pure ASGI: every `http` (and `websocket`) scope must carry
  `Authorization: Bearer <SPLITWISE_MCP_BEARER>`, compared in constant time; anything
  else gets a 401 before it reaches the SDK. `lifespan` passes through untouched — the
  SDK's session manager is started by it. The presented token is never logged or echoed.
- `serve(mcp)` refuses to start (exit code 2, secret-free stderr message) with a bearer
  shorter than `MIN_BEARER_LENGTH`, or when binding beyond loopback without
  `SPLITWISE_MCP_ALLOWED_HOSTS`; then runs uvicorn with no access log, so no URL or
  header ever lands in journald.
"""

from __future__ import annotations

import hmac
import sys
from typing import TYPE_CHECKING

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings

from splitwise_mcp.config import MIN_BEARER_LENGTH, get_settings

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP
    from starlette.types import ASGIApp, Receive, Scope, Send

    from splitwise_mcp.config import Settings

REALM = "splitwise-mcp"
UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'

_UNAUTHORIZED_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-type", b"application/json"),
    (b"content-length", str(len(UNAUTHORIZED_BODY)).encode("ascii")),
    (b"www-authenticate", f'Bearer realm="{REALM}"'.encode("ascii")),
    (b"cache-control", b"no-store"),
]

# RFC 6455 §7.4.1: 1008 = policy violation. Sent before `websocket.accept`, the server
# rejects the handshake (uvicorn answers it with HTTP 403).
_WS_POLICY_VIOLATION = 1008


class BearerAuthMiddleware:
    """Pure-ASGI gate: only requests presenting the shared bearer reach the wrapped app.

    The comparison is `hmac.compare_digest` over bytes (no early exit on the first
    differing byte, and no decoding of attacker-controlled header bytes). A request is
    rejected when it has no `Authorization` header, more than one, a scheme other than
    `Bearer` (case-insensitive, RFC 7235), an empty token, or the wrong token. Rejections
    are silent: nothing is logged, and the 401 body is a constant that never echoes
    the request.
    """

    def __init__(self, app: ASGIApp, token: str) -> None:
        # Defence in depth on top of serve()'s refuse-to-start rule: an empty expected
        # token would make `Authorization: Bearer ` (empty) a match.
        if len(token) < MIN_BEARER_LENGTH:
            raise ValueError(f"the bearer token must be at least {MIN_BEARER_LENGTH} characters")
        self.app = app
        self._expected = token.encode("utf-8")

    def _is_authorized(self, scope: Scope) -> bool:
        values = [value for name, value in scope.get("headers", ()) if name.lower() == b"authorization"]
        if len(values) != 1:
            return False
        scheme, _, presented = values[0].strip().partition(b" ")
        presented = presented.strip()
        if scheme.lower() != b"bearer" or not presented:
            return False
        return hmac.compare_digest(presented, self._expected)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "lifespan":
            await self.app(scope, receive, send)
            return
        if self._is_authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope_type == "http":
            await send({"type": "http.response.start", "status": 401, "headers": _UNAUTHORIZED_HEADERS})
            await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})
        elif scope_type == "websocket":
            await send({"type": "websocket.close", "code": _WS_POLICY_VIOLATION})
        # Any other (unknown) scope type is dropped: never forwarded unauthenticated.


class _TrailingSlashAlias:
    """Serve `<path>/` as `<path>` instead of letting Starlette redirect it.

    The SDK mounts the endpoint at exactly `<path>`; Starlette answers `<path>/` with a 307
    to `http://<Host><path>`. Behind Tailscale Funnel (`--set-path=/splitwise
    http://127.0.0.1:8765/mcp`, research/03) the public URL `…/splitwise/` arrives here as
    `/mcp/`, and that redirect would drop both the `https` scheme and the `/splitwise`
    prefix. Rewriting the path in place keeps one canonical endpoint reachable under both
    spellings. Sits INSIDE the bearer middleware, so it only ever sees authorized requests.
    """

    def __init__(self, app: ASGIApp, path: str) -> None:
        self.app = app
        self._path = path
        self._alias = path + "/" if path != "/" else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._alias is not None and scope["type"] == "http" and scope.get("path") == self._alias:
            scope = {**scope, "path": self._path, "raw_path": self._path.encode("utf-8")}
        await self.app(scope, receive, send)


def normalize_path(raw: str) -> str:
    """`SPLITWISE_MCP_PATH` as the SDK route wants it: one leading slash, no trailing one."""
    return "/" + raw.strip().strip("/")


def allowed_hosts_for(settings: Settings) -> list[str]:
    """Host header values accepted when DNS-rebinding protection is on.

    `SPLITWISE_MCP_ALLOWED_HOSTS` (comma-separated, whitespace and blanks dropped,
    de-duplicated, order kept) plus — always — `127.0.0.1:<port>` and `localhost:<port>`
    so a local health probe keeps working. Matching in the SDK is exact (or `name:*` for
    any port): `example.com` does not match `example.com:443`.
    """
    hosts = list(settings.allowed_hosts)
    port = settings.splitwise_mcp_port
    for local in (f"127.0.0.1:{port}", f"localhost:{port}"):
        if local not in hosts:
            hosts.append(local)
    return hosts


def build_app(mcp: FastMCP, settings: Settings) -> ASGIApp:
    """Configure `mcp` for streamable-http and return the bearer-protected ASGI app.

    FastMCP reads its HTTP settings when `streamable_http_app()` first builds the session
    manager, so they are set here, before that call. DNS-rebinding protection (Host /
    Origin pinning) is enabled only when `SPLITWISE_MCP_ALLOWED_HOSTS` is non-empty; the
    accepted Origins mirror the accepted hosts (`http://` and `https://`), so a
    same-origin client passes and a request carrying any foreign `Origin` gets 403. A
    request without an `Origin` header (server-to-server, curl) is unaffected.
    """
    path = normalize_path(settings.splitwise_mcp_path)
    configured = settings.allowed_hosts
    hosts = allowed_hosts_for(settings)

    mcp.settings.host = settings.splitwise_mcp_host
    mcp.settings.port = settings.splitwise_mcp_port
    mcp.settings.streamable_http_path = path
    mcp.settings.stateless_http = settings.splitwise_mcp_stateless
    mcp.settings.json_response = False
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(configured),
        allowed_hosts=hosts,
        allowed_origins=[f"{scheme}://{host}" for host in hosts for scheme in ("https", "http")],
    )

    inner: ASGIApp = mcp.streamable_http_app()
    return BearerAuthMiddleware(_TrailingSlashAlias(inner, path), settings.splitwise_mcp_bearer)


def startup_problem(settings: Settings) -> str | None:
    """Why the HTTP server must not start, or None. The message never contains a secret."""
    if len(settings.splitwise_mcp_bearer) < MIN_BEARER_LENGTH:
        return (
            f"SPLITWISE_MCP_BEARER is missing or shorter than {MIN_BEARER_LENGTH} characters; "
            "generate one with `openssl rand -hex 32`."
        )
    if not settings.host_is_loopback and not settings.allowed_hosts:
        return (
            f"SPLITWISE_MCP_HOST={settings.splitwise_mcp_host} is not a loopback address and "
            "SPLITWISE_MCP_ALLOWED_HOSTS is empty; set it to the host name(s) clients connect "
            "with, or bind to 127.0.0.1 behind a TLS proxy."
        )
    return None


def serve(mcp: FastMCP) -> None:
    """Run the HTTP server from `get_settings()`; exit with code 2 instead of starting unsafely."""
    settings = get_settings()
    problem = startup_problem(settings)
    if problem is not None:
        print(f"amazing-splitwise-mcp-http: refusing to start: {problem}", file=sys.stderr)
        raise SystemExit(2)
    app = build_app(mcp, settings)
    uvicorn.run(
        app,
        host=settings.splitwise_mcp_host,
        port=settings.splitwise_mcp_port,
        log_level="warning",
        access_log=False,
    )
