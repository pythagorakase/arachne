# Arachne MCP adapter

`mcp_server.py` exposes Arachne as one authenticated Streamable HTTP MCP
server. The wire server is harness-neutral: Codex and Claude Code can use the
same endpoint and tool contract. Harness-specific plugin packaging is
deliberately deferred; it is not required to use the server directly.

## Tools

| Tool | Effect |
|------|--------|
| `status(since=0)` | Read core health and non-destructive ruling summaries. |
| `get_ruling(sequence)` | Read one complete persisted ruling. |
| `wait_for_ruling(since)` | Wait for the first ruling after an explicit cursor. |
| `publish_decision(name, html)` | Validate and atomically publish trusted HTML. |
| `bootstrap_url(page)` | Mint a five-minute, single-use browser URL. |

`wait_for_ruling` never owns hidden cursor state. Callers pass the last
observed sequence in `since`, receive the advancing sequence in `cursor`, and
decide when to retain it. Repeating the same call is replay-safe. While the
core long-poll is blocked, the MCP request emits progress notifications (every
30 seconds by default) so a harness can distinguish a live wait from a wedged
tool call.

Publication is a real server capability, not an `rsync` wrapper. The MCP
adapter authenticates to the core server, which applies the same page-name,
same-origin `/ruling`, `localStorage`, UTF-8, and atomic-replacement contract as
`bin/publish-page.py`. Browser session cookies cannot publish pages or mint
bootstrap tickets. Neither tool returns the durable application token.

## Install and configure

Install the pinned Python environment once:

```bash
uv sync --frozen
```

Both processes load one owner-only environment file through
`bin/run-configured-service.sh`. A portable baseline is:

```dotenv
ARACHNE_RUNTIME_DIR=/absolute/path/to/runtime
ARACHNE_DATA_DIR=/absolute/path/to/state
ARACHNE_PAGES_DIR=/absolute/path/to/arachne/pages
ARACHNE_TOKEN_FILE=/absolute/path/to/state/auth-token
ARACHNE_PORT=8788
ARACHNE_PYTHON=/absolute/path/to/arachne/.venv/bin/python
ARACHNE_URL=http://127.0.0.1:8788
ARACHNE_PUBLIC_URL=https://host.example-tailnet.ts.net
ARACHNE_MCP_HOST=127.0.0.1
ARACHNE_MCP_PORT=8790
ARACHNE_MCP_ALLOWED_HOSTS=127.0.0.1:8790,localhost:8790,host.example-tailnet.ts.net:8443
ARACHNE_MCP_HEARTBEAT_SECONDS=30
ARACHNE_REQUEST_TIMEOUT=570
ARACHNE_MCP_PYTHON=/absolute/path/to/arachne/.venv/bin/python
```

The file must be a regular file owned by the service user with no group or
other permissions (`chmod 600`). The token file has the same ownership and
mode requirement. Keep the process bound to loopback. For remote harnesses,
proxy that listener through Tailscale Serve on a separate HTTPS port; never
bind it to a LAN/public interface and never use Funnel:

```bash
tailscale serve --bg --yes --https=8443 http://127.0.0.1:8790
```

Include the public `host:port` in `ARACHNE_MCP_ALLOWED_HOSTS`. Tailscale device
identity and the MCP bearer credential then protect separate network and
application boundaries.

For a private-CA HTTPS core, also set `ARACHNE_CA_FILE` to its trust bundle.
The temporary personal-Mac topology may use loopback HTTP between two processes
owned by the same user while Tailscale terminates HTTPS for browsers. The
Ubuntu deployment should restore verified HTTPS on the Serve-to-core hop as
described in `DEPLOY.md`.

## Supervision

macOS LaunchAgent templates live under `deploy/macos/`. Replace the three
`@@...@@` placeholders with absolute paths, install the resulting files in
`~/Library/LaunchAgents/`, and bootstrap both labels. Equivalent user-systemd
units live under `deploy/systemd/` for the later Ubuntu migration.

Only the core state directory and published pages are durable deployment
state. The MCP process has no cursor database, so migrating it does not create
a second cutover boundary. Stop both source services, transfer the core state
and pages with modes preserved, install a destination-specific environment,
and start the core before the adapter.

## Codex client

Expose the bearer credential to the process that launches Codex, then register
the tailnet endpoint:

```bash
launchctl setenv ARACHNE_MCP_TOKEN "$(tr -d '\n' < ~/.local/state/arachne/auth-token)"
codex mcp add arachne \
  --url https://host.example-tailnet.ts.net:8443/mcp \
  --bearer-token-env-var ARACHNE_MCP_TOKEN
```

Give `wait_for_ruling` a tool timeout longer than the intended human wait. For
a least-privilege profile, approve only these five named tools rather than
granting general shell access. A running Codex desktop process may need to be
restarted before it sees a newly registered MCP server or launch environment
variable.

## Claude Code client

The same Streamable HTTP endpoint and bearer credential are sufficient at the
protocol layer. A minimal direct registration is:

```bash
claude mcp add --transport http arachne \
  https://host.example-tailnet.ts.net:8443/mcp \
  --header "Authorization: Bearer <owner-token>"
```

Prefer a local `headersHelper` or equivalent secret store over placing the
literal credential in a committed `.mcp.json` or shell history. Allowlist the
five `mcp__arachne__...` tools and set the MCP idle timeout above the configured
heartbeat interval. Any later Claude Code plugin should be a thin
harness-specific layer for installation, permissions, and prompting; it should
not reimplement publication, authentication, cursor handling, or wait
semantics.
