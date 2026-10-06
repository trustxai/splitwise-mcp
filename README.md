# Splitwise MCP Server (amazing-splitwise-mcp)

A [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Splitwise API v3.0](https://dev.splitwise.com/), built on the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP). It
authenticates with a personal **API key** — no OAuth — so an LLM can read your groups,
friends, balances, expenses, comments and notifications, resolve people and categories
by name, and — only when you flip the kill-switch — create, update, delete and restore
expenses, groups, friends and comments. It runs over **stdio** by default and ships a
second entry point that serves **streamable-http behind a bearer token** for clients
that can only reach a public URL (Grok Bot, for one).

> The PyPI distribution and console script are both **`amazing-splitwise-mcp`** (the bare
> `splitwise-mcp` and `splitwise-mcp-server` names on PyPI belong to unrelated packages).
> Always run `uvx amazing-splitwise-mcp`.

## Safety model

Four rails, all enforced in the HTTP client — not in the tools — so no tool can forget them:

- **Writes are off by default.** Every `POST` on this API changes your account (there is
  no POST that only reads), so anything that creates, updates, deletes or restores an
  expense, group, friend or comment, or edits your profile, returns
  `Error: … writes are disabled` until `SPLITWISE_ALLOW_WRITES=1`. Those tools are marked
  🔒 below. Reads need nothing.
- **Your login can never be changed.** `password` is never sent anywhere and `email` is
  never sent to `update_user` — the profile tool only edits name, locale and default
  currency, whatever an LLM asks for.
- **"200 OK" is not taken as success.** Splitwise answers `200` to failed writes and puts
  the failure in the body (`success: false` or a non-empty `errors`). The client raises
  on that, so a tool can only claim what the body confirms; a partial batch (some
  friends added, some rejected) lists who did land.
- **Confirmations never claim more than Splitwise said.** A create echoes the expense
  Splitwise returned; an unreadable `200` or a `5xx`/timeout on a write is reported as
  **outcome UNKNOWN** with the tool to read back before retrying — the server never
  retries a write on its own (a retried `create_expense` is a duplicate expense).

Everything an LLM sees is scrubbed of the API key and the HTTP bearer. One API key is an
access token for your **whole** account: keep it like a password, and regenerate it on
the apps page if it ever leaks. Found a way around one of these rails? Report it
privately — see [SECURITY.md](SECURITY.md).

One thing no rail can fix: expense descriptions, notes, comments and notification text
are **written by the other members of your groups** and reach the LLM verbatim (tags
stripped, table cells escaped). Treat them as data, not instructions — especially for a
client that has writes enabled.

## Features

- **Balances the way you ask for them.** Who owes you and whom you owe, per friend and
  per currency with totals; per group with the simplified debts rendered as
  `A → B 12.50 USD`.
- **Expenses with three split modes.** Equal split inside a group, explicit paid/owed
  shares, or "split equally between these people" — the shares are computed and checked
  locally (they must sum to the cost, to the cent) **before** anything is sent, so a bad
  split never reaches Splitwise.
- **Names, not ids.** `splitwise_resolve_friend` / `_group` / `_category` turn "Ana",
  "Ski trip" or "groceries" into the id the write tools need, with a score and an
  explicit *ambiguous* verdict when two people match.
- **Groups, friends, comments, notifications, categories and currencies** — the whole
  v3.0 surface (27 endpoints) minus the account's login.
- **Dual-format responses** — markdown for humans and LLMs (default) or JSON for
  programmatic use, per call via `response_format`.
- **Strict inputs, uniform errors.** One pydantic model per tool (`extra="forbid"`), money
  as 2-decimal strings, ISO-8601 dates, and Splitwise's `{"error"}` / `{"errors": {...}}`
  bodies normalised into readable strings with a hint for the statuses that have a
  known fix.
- **Two transports.** stdio (every MCP client launches it as a subprocess) and a
  streamable-http server behind a shared bearer for clients that need a URL.

## Available Tools

<!-- TOOL TABLE START -->
All **33 tools**, grouped by module. 🔒 = refused unless `SPLITWISE_ALLOW_WRITES=1`.

| Tool | Description |
|---|---|
| **Health** | |
| `splitwise_health_check` | Verify connectivity and the API key against Splitwise, and report the write kill-switch. |
| **Users** | |
| `splitwise_get_current_user` | Get the Splitwise user the API key belongs to: name, id, email, currency, locale, notifications. |
| `splitwise_get_user` | Get another Splitwise user's public profile by id: name, email, registration status. |
| `splitwise_update_user` 🔒 | Update your Splitwise profile: first name, last name, locale and/or default currency. |
| **Groups** | |
| `splitwise_add_user_to_group` 🔒 | Add a person to a group, by user id or by inviting a name + email. 🔒 write. |
| `splitwise_create_group` 🔒 | Create a group, optionally with members (you are added automatically). 🔒 write. |
| `splitwise_delete_group` 🔒 | Delete a group and all its expenses — for every member, not just you. 🔒 destructive write. |
| `splitwise_get_group` | Read one group: its members with balances, who owes whom, and the invite link. |
| `splitwise_get_groups` | List every group you belong to, with members count and balances per currency. |
| `splitwise_remove_user_from_group` 🔒 | Remove a member from a group. 🔒 destructive write. |
| `splitwise_undelete_group` 🔒 | Restore a deleted group (and its expenses). 🔒 write. |
| **Friends** | |
| `splitwise_create_friend` 🔒 | Add one person as a Splitwise friend by e-mail. 🔒 Needs SPLITWISE_ALLOW_WRITES=1. |
| `splitwise_create_friends` 🔒 | Add several people as Splitwise friends in one call. 🔒 Needs SPLITWISE_ALLOW_WRITES=1. |
| `splitwise_delete_friend` 🔒 | Remove a friendship (unfriend a user). 🔒 Needs SPLITWISE_ALLOW_WRITES=1. Destructive. |
| `splitwise_get_friend` | Get one Splitwise friend with their overall and per-group balances. |
| `splitwise_get_friends` | List your Splitwise friends with their balance per currency. |
| **Expenses** | |
| `splitwise_create_expense` 🔒 | Create an expense with exactly one split mode; the shares are checked locally before sending. |
| `splitwise_delete_expense` 🔒 | Delete an expense (Splitwise keeps it as deleted; restore with `splitwise_undelete_expense`). |
| `splitwise_get_expense` | Read one expense in full: cost, group, category, every share, repayments and comments. |
| `splitwise_get_expenses` | List expenses, newest first, optionally filtered by group, friend and date range. |
| `splitwise_undelete_expense` 🔒 | Restore a deleted expense. |
| `splitwise_update_expense` 🔒 | Change an existing expense — only the fields given are sent. |
| **Balances** | |
| `splitwise_get_balances` | Summarise who owes you and whom you owe, per friend and per currency. |
| `splitwise_get_group_balances` | Show each member's balance in one group and who should pay whom to settle it. |
| **Lookup — categories, currencies, name resolution** | |
| `splitwise_get_categories` | List Splitwise's expense categories: each parent with its subcategories and their ids. |
| `splitwise_get_currencies` | List the currency codes Splitwise accepts, optionally filtered by a substring. |
| `splitwise_resolve_category` | Turn a category name into the subcategory id an expense needs, with ranked fuzzy candidates. |
| `splitwise_resolve_friend` | Turn a friend's name (or email) into their Splitwise user id, with ranked fuzzy candidates. |
| `splitwise_resolve_group` | Turn a group's name into its Splitwise group id, with ranked fuzzy candidates. |
| **Comments** | |
| `splitwise_create_comment` 🔒 | Add a comment to a Splitwise expense. |
| `splitwise_delete_comment` 🔒 | Delete a comment from a Splitwise expense (destructive; comments have no undelete). |
| `splitwise_get_comments` | List the comments on one Splitwise expense. |
| **Notifications** | |
| `splitwise_get_notifications` | List your recent Splitwise activity (notifications), newest first. |
<!-- TOOL TABLE END -->

Read tools return markdown (default) or JSON; mutating tools return a confirmation that
echoes exactly what Splitwise returned. Tool arguments are nested under a single
`params` object (`{"params": {"group_id": 123}}`).

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — for the zero-install `uvx` path and for local
  development (Python 3.13+ is only needed for the clone path; `uvx` brings its own).
- **A Splitwise API key — which needs Splitwise Pro.** Splitwise only lets a **Pro**
  subscriber register an application (the apps page shows "Get Splitwise Pro to
  register an application" otherwise), and the key comes from the registered app. With
  Pro active: sign in at <https://secure.splitwise.com/apps> → **Register your
  application** (any name, the URLs do not matter) → on the app's page click
  **API key**. It is a personal access token for your whole account — read and write.
  Regenerate it on the same page to rotate; if Pro lapses, the key stops working (401)
  until you resubscribe. The self-serve API is for personal use; commercial use needs a
  licence from Splitwise (see their terms on dev.splitwise.com).
- **(Optional) Docker** if you prefer the container path.

## Quickstart

```bash
git clone https://github.com/trustxai/splitwise-mcp.git
cd splitwise-mcp
uv sync --group dev
cp .env.example .env            # set SPLITWISE_API_KEY
uv run amazing-splitwise-mcp    # starts the stdio server
```

The server speaks MCP over stdio, so it is normally launched by an MCP client (see
[Client Configuration](#client-configuration)) rather than run by hand. Call
`splitwise_health_check` first: it reports connectivity, who you are logged in as (with
your user id, which the expense shares need), and whether writes are enabled.

## Run with uvx (zero install)

```bash
SPLITWISE_API_KEY=your_key uvx amazing-splitwise-mcp
```

> Use `amazing-splitwise-mcp`, not `splitwise-mcp`. The bare name on PyPI is an unrelated
> package and will **not** run this server.

## Client Configuration

Every client launches the server as a subprocess and passes the key through `env`.
Add `"SPLITWISE_ALLOW_WRITES": "1"` only for a client you want to be able to write.

### Cursor

Add to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per project):

```json
{
  "mcpServers": {
    "splitwise": {
      "command": "uvx",
      "args": ["amazing-splitwise-mcp"],
      "env": {
        "SPLITWISE_API_KEY": "your_key"
      }
    }
  }
}
```

### Claude Desktop

Edit `claude_desktop_config.json` (Settings → Developer → Edit Config; on macOS it lives
at `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "splitwise": {
      "command": "uvx",
      "args": ["amazing-splitwise-mcp"],
      "env": {
        "SPLITWISE_API_KEY": "your_key"
      }
    }
  }
}
```

Restart Claude Desktop after saving.

### Claude Code

```bash
claude mcp add splitwise --env SPLITWISE_API_KEY=your_key -- uvx amazing-splitwise-mcp
```

Or add the equivalent block to `~/.claude.json` under `mcpServers` (same shape as the
Cursor example above).

### MCP Inspector

```bash
SPLITWISE_API_KEY=your_key npx @modelcontextprotocol/inspector uvx amazing-splitwise-mcp
```

If you cloned the repo, `uv run mcp dev src/splitwise_mcp/server.py` does the same (the
`mcp` dev CLI ships in the dev dependency group).

### Docker

Build the image from the repo `Dockerfile`, then point any client at `docker run`:

```bash
docker build -t amazing-splitwise-mcp .
```

```json
{
  "mcpServers": {
    "splitwise": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-e", "SPLITWISE_API_KEY", "amazing-splitwise-mcp"],
      "env": {
        "SPLITWISE_API_KEY": "your_key"
      }
    }
  }
}
```

The `-i` flag is required — the server communicates over stdin/stdout.

## Remote: streamable-http behind a bearer

Some hosts cannot spawn a local process and only accept an MCP server at a **public
URL** (Grok Bot's connectors, for example). For those the same package ships a second
console script:

```bash
SPLITWISE_API_KEY=your_key \
SPLITWISE_MCP_BEARER=$(openssl rand -hex 32) \
uvx --from amazing-splitwise-mcp amazing-splitwise-mcp-http
```

It serves the MCP endpoint at `http://127.0.0.1:8765/mcp` and answers **401** to any
request that does not carry `Authorization: Bearer <SPLITWISE_MCP_BEARER>` — before any
routing, in constant time, without logging the presented token. Rules it enforces:

- It **refuses to start** without a bearer of at least 32 characters, or when bound
  beyond loopback without `SPLITWISE_MCP_ALLOWED_HOSTS`.
- It is **loopback by default**: put TLS in front of it — a reverse proxy or
  [Tailscale Funnel](https://tailscale.com/kb/1223/funnel). With Funnel, the path prefix
  is stripped before proxying, so the backend URL must carry the MCP path:

  ```bash
  tailscale funnel --bg --https=443 --set-path=/splitwise http://127.0.0.1:8765/mcp
  # public URL: https://<node>.<tailnet>.ts.net/splitwise/
  ```

- **Host pinning** (DNS-rebinding protection) is on as soon as
  `SPLITWISE_MCP_ALLOWED_HOSTS` is set — list the public hostname your proxy forwards;
  the server adds its own loopback addresses. A request with a `Host` not on the list
  gets **421**. With pinning on, a request carrying an `Origin` that is not one of those
  hosts gets **403**; a cloud client that sends its own Origin goes in
  `SPLITWISE_MCP_ALLOWED_ORIGINS`.
- It runs **stateless** (no server-side session ids) and keeps **no access log**, so no
  URL or header ever lands in your logs.

### Connecting Grok Bot (or any URL-only client)

Add a connector with the public URL (keep the trailing slash:
`https://<node>.<tailnet>.ts.net/splitwise/`) and a custom header
`Authorization: Bearer <SPLITWISE_MCP_BEARER>`. Set `SPLITWISE_ALLOW_WRITES=1` in the
server's environment only if that client should be able to create expenses. One bearer
is the whole account: rotate it (and the connector) if it ever leaks.

## Environment Variables

| Variable | Default | Meaning |
|---|---|---|
| `SPLITWISE_API_KEY` | — | Personal API key, sent as `Authorization: Bearer`. Required. |
| `SPLITWISE_ALLOW_WRITES` | `false` | Kill-switch. `1` allows every 🔒 tool. |
| `SPLITWISE_API_URL` | `https://secure.splitwise.com/api/v3.0` | REST base; override only for proxies/tests. |
| `SPLITWISE_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout. |
| `SPLITWISE_MCP_BEARER` | — | HTTP only. Shared secret the client must present (≥ 32 chars). |
| `SPLITWISE_MCP_HOST` | `127.0.0.1` | HTTP only. Bind address. |
| `SPLITWISE_MCP_PORT` | `8765` | HTTP only. Bind port. |
| `SPLITWISE_MCP_PATH` | `/mcp` | HTTP only. Endpoint path. |
| `SPLITWISE_MCP_ALLOWED_HOSTS` | — | HTTP only. Comma list of accepted `Host` values; turns pinning on. |
| `SPLITWISE_MCP_ALLOWED_ORIGINS` | — | HTTP only. Extra `Origin` values accepted when pinning is on. |
| `SPLITWISE_MCP_STATELESS` | `true` | HTTP only. Stateless streamable-http. |

The stdio server reads only the first four. Values are whitespace-stripped on the way
in (a pasted key with a trailing newline would otherwise be an illegal header).

## Running Manually

```bash
uv run amazing-splitwise-mcp          # stdio
uv run python -m splitwise_mcp        # same
uv run amazing-splitwise-mcp-http     # streamable-http (needs SPLITWISE_MCP_BEARER)
```

The stdio server reads MCP JSON-RPC on stdin and writes it on stdout — nothing else is
ever written to stdout.

## Troubleshooting

- **`Error (401): Unauthorized – Invalid API request: you are not logged in`** — the key
  is missing, mistyped, or was regenerated. Copy it again from your app's page at
  <https://secure.splitwise.com/apps>.
- **`Error: POST /create_expense would change your Splitwise account, but writes are
  disabled`** — by design. Set `SPLITWISE_ALLOW_WRITES=1` for that client.
- **`Error: Splitwise rejected the request to /…: <message>`** — Splitwise answered
  `200` with `success: false` or a non-empty `errors`; the message is theirs. For a
  batch (`create_friends`) the people that *were* added are listed under it.
- **`… outcome UNKNOWN; read back with …`** — a write got a `5xx`, a timeout or an
  unreadable body. Read the record back before retrying; a blind retry can duplicate it.
- **`cost must have at most 2 decimal places` / `paid shares … must equal the cost`** —
  Splitwise takes money as 2-decimal strings and the shares must sum to the cost on both
  sides; the tool checks locally and nothing was sent.
- **`Update accepted by Splitwise — N of M field(s) not confirmed`** — `update_user`'s
  response does not echo `locale`/`default_currency`; read the profile back with
  `splitwise_get_current_user` to confirm.
- **I ran `uvx splitwise-mcp` and got something else** — that is an unrelated PyPI
  package. The script is `amazing-splitwise-mcp`.
- **HTTP: 401 on every request** — the `Authorization: Bearer …` header is missing or
  wrong (exact match, one header). **421** — the `Host` is not in
  `SPLITWISE_MCP_ALLOWED_HOSTS` (matching is exact: `example.com` ≠ `example.com:443`).
  **403 "Invalid Origin header"** — add the client's Origin to
  `SPLITWISE_MCP_ALLOWED_ORIGINS`. **404 behind a proxy** — the proxy strips the path
  prefix; point it at `http://127.0.0.1:8765/mcp`, not at the bare port.
- **Balances look inverted** — the sign convention is stated in every balance tool's
  output: positive = owed **to you**.

## Contributing

```bash
uv sync --group dev
uv run pytest -m "not live" && uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/ && uv run mypy src/
uv run python scripts/gen_tool_table.py --check   # README tool table is generated, never edited by hand
```

Live tests (`-m live`) run read-only against your own account when `SPLITWISE_API_KEY` is
in `.env`; the mutating `live_write` tests create and delete a throwaway group and only
run with `SPLITWISE_TEST_ALLOW_WRITES=1`. Commits follow
[Conventional Commits](https://www.conventionalcommits.org/); releases are cut by
release-please and published to PyPI through trusted publishing.

## License

Apache-2.0 — see [LICENSE](LICENSE).
