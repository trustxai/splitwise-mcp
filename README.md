# Splitwise MCP Server (amazing-splitwise-mcp)

A [Model Context Protocol](https://modelcontextprotocol.io) server for the
[Splitwise API v3.0](https://dev.splitwise.com/), built on the official
[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (FastMCP). It
authenticates with a personal **API key** — no OAuth — so an LLM can read your groups,
friends, balances, expenses, comments and notifications, and — only when you flip the
kill-switch — create, update, delete and restore expenses, groups, friends and comments.
It runs over **stdio** by default and ships a second entry point that serves
**streamable-http behind a bearer token** for clients that can only reach a public URL.

> The PyPI distribution and console script are both **`amazing-splitwise-mcp`** (the bare
> `splitwise-mcp` and `splitwise-mcp-server` names on PyPI belong to unrelated packages).
> Always run `uvx amazing-splitwise-mcp`.

_This README is a Stage-0 stub; the full version lands with the `t9-readme-docs` task._

## Available Tools

<!-- TOOL TABLE START -->
All **1 tools**, grouped by module. 🔒 = refused unless `SPLITWISE_ALLOW_WRITES=1`.

| Tool | Description |
|---|---|
| **Health** | |
| `splitwise_health_check` | Verify connectivity and the API key against Splitwise, and report the write kill-switch. |
<!-- TOOL TABLE END -->

## License

Apache-2.0 — see [LICENSE](LICENSE).
