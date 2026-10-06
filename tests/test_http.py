"""Tests for the streamable-http transport: bearer gate, SDK wiring, refuse-to-start rules.

Everything runs over `httpx.ASGITransport` (no sockets). Each test that needs the SDK app
builds a FRESH `FastMCP("splitwise_mcp")`: `build_app` mutates the instance's settings and
the SDK's session manager can be started only once per instance, so the module singleton
in `splitwise_mcp.server` is never touched here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import LATEST_PROTOCOL_VERSION

import splitwise_mcp.http as http_module
from splitwise_mcp.config import MIN_BEARER_LENGTH, Settings, get_settings
from splitwise_mcp.http import (
    BearerAuthMiddleware,
    allowed_hosts_for,
    build_app,
    normalize_path,
    serve,
    startup_problem,
)

TOKEN = "x" * 40
WRONG_SAME_LENGTH = "y" * 40
BASE = "http://127.0.0.1:8765"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LATEST_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "test-http", "version": "0"},
    },
}

Message = dict[str, Any]


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"splitwise_mcp_bearer": TOKEN, **overrides}
    return Settings(**values)


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)


@asynccontextmanager
async def _lifespan(app: Any) -> AsyncIterator[None]:
    """Drive the ASGI lifespan protocol THROUGH the middleware (httpx does not run it).

    `lifespan.startup.complete` only arrives if the middleware forwarded the lifespan
    scope and the SDK's session manager started behind it.
    """
    to_app: asyncio.Queue[Message] = asyncio.Queue()
    from_app: asyncio.Queue[Message] = asyncio.Queue()

    async def receive() -> Message:
        return await to_app.get()

    async def send(message: Message) -> None:
        await from_app.put(message)

    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, receive, send))
    await to_app.put({"type": "lifespan.startup"})
    started = await asyncio.wait_for(from_app.get(), timeout=5)
    assert started["type"] == "lifespan.startup.complete", started
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        stopped = await asyncio.wait_for(from_app.get(), timeout=5)
        assert stopped["type"] == "lifespan.shutdown.complete", stopped
        await asyncio.wait_for(task, timeout=5)


def _sse_json(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data:"):
            payload: dict[str, Any] = json.loads(line[len("data:") :].strip())
            return payload
    raise AssertionError(f"no SSE data line in the response body: {text!r}")


class _Recorder:
    """Inner ASGI app that records what reached it and answers 204 to http."""

    def __init__(self) -> None:
        self.scopes: list[dict[str, Any]] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        self.scopes.append(scope)
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})


def _assert_challenge(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="splitwise-mcp"'
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"error": "unauthorized"}


# --- the bearer gate ---------------------------------------------------------------


async def test_missing_header_is_401_with_challenge_and_no_echo() -> None:
    app = build_app(FastMCP("splitwise_mcp"), _settings())
    async with _client(app) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    _assert_challenge(response)
    assert TOKEN not in response.text
    assert all(TOKEN not in value for value in response.headers.values())


async def test_wrong_token_of_equal_length_is_401_via_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    real_compare = http_module.hmac.compare_digest
    calls: list[tuple[object, object]] = []

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(http_module.hmac, "compare_digest", spy)
    app = build_app(FastMCP("splitwise_mcp"), _settings())
    async with _client(app) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=_auth(WRONG_SAME_LENGTH))
    _assert_challenge(response)
    # The comparison ran (equal lengths, so a naive `==` would also have run) and it was
    # the constant-time one, over bytes.
    assert calls == [(WRONG_SAME_LENGTH.encode(), TOKEN.encode())]
    assert WRONG_SAME_LENGTH not in response.text
    assert TOKEN not in response.text


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param([("Authorization", f"Basic {TOKEN}")], id="basic-scheme"),
        pytest.param([("Authorization", TOKEN)], id="no-scheme"),
        pytest.param([("Authorization", "Bearer")], id="scheme-only"),
        pytest.param([("Authorization", "Bearer    ")], id="empty-token"),
        pytest.param([("Authorization", f"Bearer {TOKEN}x")], id="token-plus-suffix"),
        pytest.param([("Authorization", f"Bearer {TOKEN[:-1]}")], id="token-prefix"),
        pytest.param([("Authorization", f"Bearer {TOKEN} {TOKEN}")], id="two-tokens"),
        pytest.param(
            [("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
            id="duplicate-header",
        ),
    ],
)
async def test_malformed_authorization_is_401(headers: list[tuple[str, str]]) -> None:
    inner = _Recorder()
    app = BearerAuthMiddleware(inner, TOKEN)
    async with _client(app) as client:
        response = await client.get("/mcp", headers=headers)
    _assert_challenge(response)
    assert inner.scopes == []


@pytest.mark.parametrize("path", ["/", "/mcp", "/mcp/", "/does-not-exist", "/mcp/../admin"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "OPTIONS"])
async def test_every_unauthenticated_http_request_is_401_before_routing(method: str, path: str) -> None:
    # A 404/405/307 here would mean the request reached the SDK's router unauthenticated.
    app = build_app(FastMCP("splitwise_mcp"), _settings())
    async with _client(app) as client:
        response = await client.request(method, path, headers=MCP_HEADERS)
    _assert_challenge(response)


async def test_rejections_log_nothing_about_the_token(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    app = build_app(FastMCP("splitwise_mcp"), _settings())
    async with _client(app) as client:
        await client.post("/mcp", json=INITIALIZE, headers=_auth(WRONG_SAME_LENGTH))
        await client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert WRONG_SAME_LENGTH not in caplog.text
    assert TOKEN not in caplog.text


async def test_scheme_is_case_insensitive() -> None:
    inner = _Recorder()
    app = BearerAuthMiddleware(inner, TOKEN)
    async with _client(app) as client:
        response = await client.get("/mcp", headers={"Authorization": f"bearer {TOKEN}"})
    assert response.status_code == 204
    assert len(inner.scopes) == 1


def test_middleware_refuses_a_short_expected_token() -> None:
    short = "s" * (MIN_BEARER_LENGTH - 1)
    with pytest.raises(ValueError, match=str(MIN_BEARER_LENGTH)) as excinfo:
        BearerAuthMiddleware(_Recorder(), short)
    assert short not in str(excinfo.value)
    with pytest.raises(ValueError):
        BearerAuthMiddleware(_Recorder(), "")


# --- non-http scopes -----------------------------------------------------------------


async def test_lifespan_scope_passes_through_untouched() -> None:
    inner = _Recorder()
    app = BearerAuthMiddleware(inner, TOKEN)
    scope: dict[str, Any] = {"type": "lifespan", "asgi": {"version": "3.0"}}

    async def receive() -> Message:
        raise AssertionError("the middleware must not consume lifespan messages")

    async def send(message: Message) -> None:
        raise AssertionError("the middleware must not answer lifespan messages")

    await app(scope, receive, send)
    assert inner.scopes == [scope]
    assert inner.scopes[0] is scope


async def test_websocket_without_token_is_closed_with_policy_violation() -> None:
    inner = _Recorder()
    app = BearerAuthMiddleware(inner, TOKEN)
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "websocket", "path": "/mcp", "headers": []}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1008}]
    assert inner.scopes == []


async def test_websocket_with_token_passes_through() -> None:
    inner = _Recorder()
    app = BearerAuthMiddleware(inner, TOKEN)
    scope = {"type": "websocket", "path": "/mcp", "headers": [(b"authorization", f"Bearer {TOKEN}".encode())]}

    async def receive() -> Message:
        return {"type": "websocket.connect"}

    async def send(message: Message) -> None:
        raise AssertionError(f"unexpected message {message}")

    await app(scope, receive, send)
    assert inner.scopes == [scope]


async def test_unknown_scope_type_is_never_forwarded_unauthenticated() -> None:
    inner = _Recorder()
    app = BearerAuthMiddleware(inner, TOKEN)
    sent: list[Message] = []

    async def receive() -> Message:
        return {}

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "telepathy", "headers": []}, receive, send)
    assert inner.scopes == []
    assert sent == []


# --- reaching the SDK ----------------------------------------------------------------


async def test_right_token_reaches_the_mcp_app() -> None:
    app = build_app(FastMCP("splitwise_mcp"), _settings())
    async with _lifespan(app), _client(app) as client:
        response = await client.post("/mcp", json=INITIALIZE, headers=_auth())
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "splitwise_mcp" in response.text
    message = _sse_json(response.text)
    assert message["id"] == 1
    assert message["result"]["serverInfo"]["name"] == "splitwise_mcp"


async def test_trailing_slash_is_served_not_redirected() -> None:
    # Funnel `--set-path=/splitwise http://127.0.0.1:8765/mcp` delivers `…/splitwise/` as
    # `/mcp/`; Starlette alone would 307 to `http://<Host>/mcp` (wrong scheme, lost prefix).
    app = build_app(FastMCP("splitwise_mcp"), _settings())
    async with _lifespan(app), _client(app) as client:
        response = await client.post("/mcp/", json=INITIALIZE, headers=_auth())
    assert response.status_code == 200, response.text
    assert _sse_json(response.text)["result"]["serverInfo"]["name"] == "splitwise_mcp"


async def test_custom_path_is_honoured() -> None:
    app = build_app(FastMCP("splitwise_mcp"), _settings(splitwise_mcp_path="splitwise/"))
    async with _lifespan(app), _client(app) as client:
        ok = await client.post("/splitwise", json=INITIALIZE, headers=_auth())
        missing = await client.post("/mcp", json=INITIALIZE, headers=_auth())
    assert ok.status_code == 200, ok.text
    assert missing.status_code == 404


async def test_dns_rebinding_pins_the_host_header_when_hosts_are_configured() -> None:
    settings = _settings(splitwise_mcp_allowed_hosts="ax42.tail8f6c35.ts.net")
    app = build_app(FastMCP("splitwise_mcp"), settings)
    async with _lifespan(app), _client(app) as client:
        public = await client.post("https://ax42.tail8f6c35.ts.net/mcp", json=INITIALIZE, headers=_auth())
        local = await client.post(f"{BASE}/mcp", json=INITIALIZE, headers=_auth())
        foreign = await client.post("http://evil.example/mcp", json=INITIALIZE, headers=_auth())
        explicit_port = await client.post("https://ax42.tail8f6c35.ts.net:443/mcp", json=INITIALIZE, headers=_auth())
        same_origin = await client.post(
            f"{BASE}/mcp", json=INITIALIZE, headers={**_auth(), "Origin": "https://ax42.tail8f6c35.ts.net"}
        )
        foreign_origin = await client.post(
            f"{BASE}/mcp", json=INITIALIZE, headers={**_auth(), "Origin": "https://evil.example"}
        )
        unauthenticated_foreign = await client.post("http://evil.example/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert public.status_code == 200, public.text
    assert local.status_code == 200, local.text
    assert foreign.status_code == 421
    # httpx drops the default port from the Host header, so this is the bare name again.
    assert explicit_port.status_code == 200
    assert same_origin.status_code == 200
    assert foreign_origin.status_code == 403
    # The bearer gate runs first: an unauthenticated request never learns the host rules.
    _assert_challenge(unauthenticated_foreign)


async def test_host_with_unlisted_port_is_rejected_when_pinned() -> None:
    settings = _settings(splitwise_mcp_allowed_hosts="ax42.tail8f6c35.ts.net")
    app = build_app(FastMCP("splitwise_mcp"), settings)
    async with _lifespan(app), _client(app) as client:
        response = await client.post(
            f"{BASE}/mcp", json=INITIALIZE, headers={**_auth(), "Host": "ax42.tail8f6c35.ts.net:8443"}
        )
    assert response.status_code == 421


# --- SDK settings + parsing ------------------------------------------------------------


def test_build_app_configures_the_sdk_settings() -> None:
    mcp = FastMCP("splitwise_mcp")
    settings = _settings(
        splitwise_mcp_host="0.0.0.0",
        splitwise_mcp_port=9000,
        splitwise_mcp_path="/custom/",
        splitwise_mcp_stateless=False,
        splitwise_mcp_allowed_hosts="api.example.com",
    )
    app = build_app(mcp, settings)
    assert isinstance(app, BearerAuthMiddleware)
    assert mcp.settings.host == "0.0.0.0"
    assert mcp.settings.port == 9000
    assert mcp.settings.streamable_http_path == "/custom"
    assert mcp.settings.stateless_http is False
    assert mcp.settings.json_response is False
    security = mcp.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["api.example.com", "127.0.0.1:9000", "localhost:9000"]
    assert "https://api.example.com" in security.allowed_origins
    assert "http://127.0.0.1:9000" in security.allowed_origins


def test_dns_rebinding_protection_is_off_without_configured_hosts() -> None:
    mcp = FastMCP("splitwise_mcp")
    build_app(mcp, _settings())
    security = mcp.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is False
    # The local pair is still listed, ready for the day a host is configured.
    assert security.allowed_hosts == ["127.0.0.1:8765", "localhost:8765"]
    assert mcp.settings.stateless_http is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ["127.0.0.1:8765", "localhost:8765"]),
        (" , ,", ["127.0.0.1:8765", "localhost:8765"]),
        (
            " a.example , ,b.example:8443,a.example ",
            ["a.example", "b.example:8443", "127.0.0.1:8765", "localhost:8765"],
        ),
        ("localhost:8765,ax42.tail8f6c35.ts.net", ["localhost:8765", "ax42.tail8f6c35.ts.net", "127.0.0.1:8765"]),
    ],
)
def test_allowed_hosts_parsing(raw: str, expected: list[str]) -> None:
    assert allowed_hosts_for(_settings(splitwise_mcp_allowed_hosts=raw)) == expected


def test_allowed_hosts_follow_the_port() -> None:
    hosts = allowed_hosts_for(_settings(splitwise_mcp_port=9123, splitwise_mcp_allowed_hosts="x.example"))
    assert hosts == ["x.example", "127.0.0.1:9123", "localhost:9123"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("/mcp", "/mcp"), ("mcp", "/mcp"), ("/mcp/", "/mcp"), (" /a/b/ ", "/a/b"), ("", "/")],
)
def test_normalize_path(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


# --- serve(): refuse-to-start rules ----------------------------------------------------


class _RunSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


@pytest.fixture
def run_spy(monkeypatch: pytest.MonkeyPatch) -> _RunSpy:
    spy = _RunSpy()
    monkeypatch.setattr("uvicorn.run", spy)
    return spy


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_serve_refuses_a_31_char_bearer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], run_spy: _RunSpy
) -> None:
    short = "q" * (MIN_BEARER_LENGTH - 1)
    _env(monkeypatch, SPLITWISE_MCP_BEARER=short)
    with pytest.raises(SystemExit) as excinfo:
        serve(FastMCP("splitwise_mcp"))
    assert excinfo.value.code == 2
    assert run_spy.calls == []
    captured = capsys.readouterr()
    assert "SPLITWISE_MCP_BEARER" in captured.err
    assert short not in captured.err
    assert captured.out == ""


def test_serve_refuses_a_missing_bearer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], run_spy: _RunSpy
) -> None:
    get_settings.cache_clear()
    with pytest.raises(SystemExit) as excinfo:
        serve(FastMCP("splitwise_mcp"))
    assert excinfo.value.code == 2
    assert run_spy.calls == []
    assert "SPLITWISE_MCP_BEARER" in capsys.readouterr().err


def test_serve_refuses_non_loopback_without_allowed_hosts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], run_spy: _RunSpy
) -> None:
    _env(monkeypatch, SPLITWISE_MCP_BEARER=TOKEN, SPLITWISE_MCP_HOST="0.0.0.0")
    with pytest.raises(SystemExit) as excinfo:
        serve(FastMCP("splitwise_mcp"))
    assert excinfo.value.code == 2
    assert run_spy.calls == []
    err = capsys.readouterr().err
    assert "SPLITWISE_MCP_ALLOWED_HOSTS" in err
    assert "0.0.0.0" in err
    assert TOKEN not in err


def test_serve_runs_uvicorn_without_access_log_on_loopback(run_spy: _RunSpy, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, SPLITWISE_MCP_BEARER=TOKEN, SPLITWISE_MCP_PORT="8899")
    mcp = FastMCP("splitwise_mcp")
    serve(mcp)
    assert len(run_spy.calls) == 1
    args, kwargs = run_spy.calls[0]
    assert len(args) == 1
    assert isinstance(args[0], BearerAuthMiddleware)
    assert kwargs == {"host": "127.0.0.1", "port": 8899, "log_level": "warning", "access_log": False}
    assert mcp.settings.port == 8899


def test_serve_allows_non_loopback_with_allowed_hosts(run_spy: _RunSpy, monkeypatch: pytest.MonkeyPatch) -> None:
    _env(
        monkeypatch,
        SPLITWISE_MCP_BEARER=TOKEN,
        SPLITWISE_MCP_HOST="0.0.0.0",
        SPLITWISE_MCP_ALLOWED_HOSTS="splitwise.example.com",
    )
    mcp = FastMCP("splitwise_mcp")
    serve(mcp)
    assert len(run_spy.calls) == 1
    assert run_spy.calls[0][1]["host"] == "0.0.0.0"
    security = mcp.settings.transport_security
    assert security is not None
    assert security.enable_dns_rebinding_protection is True


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_startup_problem_accepts_loopback_without_allowed_hosts(host: str) -> None:
    assert startup_problem(_settings(splitwise_mcp_host=host)) is None


def test_startup_problem_accepts_exactly_min_length_bearer() -> None:
    assert startup_problem(_settings(splitwise_mcp_bearer="b" * MIN_BEARER_LENGTH)) is None
