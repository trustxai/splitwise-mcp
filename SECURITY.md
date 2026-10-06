# Security Policy

This server holds a personal API key that can read and (when enabled) change a live
Splitwise account, and it can be exposed over HTTP behind a bearer token. This page
covers how to report a vulnerability, what counts as one, and how to run the server safely.

## Reporting a vulnerability

**Do not open a public issue, pull request or discussion.** Report privately through
GitHub:

1. Open the repository's **Security** tab → **Report a vulnerability**
   (direct link: <https://github.com/trustxai/splitwise-mcp/security/advisories/new>).
2. Include:
   - the version you ran (the PyPI release, e.g. `0.1.0`, or the commit SHA);
   - the settings involved — variable **names** only, e.g. whether
     `SPLITWISE_ALLOW_WRITES` was set or which transport was running;
   - the tool calls and steps to reproduce, and what an attacker gains.

**Never include real credentials** — no API keys, bearer tokens or account identifiers.
Build proofs of concept with mocked responses (the test suite uses `httpx.MockTransport`
and `httpx.ASGITransport`). If a live key ends up in a report anyway, regenerate it at
<https://secure.splitwise.com/apps> right away: a key that has been shared is compromised.

### What to expect

The project has a single maintainer, so these are targets, not guarantees:

| Step | Target |
|---|---|
| Acknowledge the report | 3 business days |
| Confirm or rule out the issue, with a severity assessment | 10 business days |
| Release a fix or mitigation | 90 days from the report — sooner for anything that can change account data |

Once the fix is on PyPI, a GitHub Security Advisory is published (with a CVE when
warranted) and the CHANGELOG entry links to it. Reporters are credited in the advisory
unless they ask not to be. Please keep the details private until the advisory is out.

## Supported versions

Only the latest release on PyPI receives fixes.

## What counts as a vulnerability

- A way to change account state while `SPLITWISE_ALLOW_WRITES` is off.
- A way to change the account's email or password through any tool.
- A request reaching the HTTP transport without the bearer, or the bearer being
  recoverable from a response, a log line or an error message.
- The API key or the bearer appearing in any tool output, log or error.
- Anything that makes the server send a write Splitwise did not confirm as successful
  while reporting it as done.

## Running it safely

- Keep `SPLITWISE_ALLOW_WRITES` unset unless the client genuinely needs to write.
- Never expose `amazing-splitwise-mcp-http` without TLS in front of it and a bearer of at
  least 32 random characters (`openssl rand -hex 32`); keep it bound to loopback and set
  `SPLITWISE_MCP_ALLOWED_HOSTS` to the hostname your proxy forwards.
- Rotate the API key on the Splitwise apps page and the bearer wherever it is configured
  (environment file, the client's connector) if either is ever exposed.
