# Arachne — Claude Code Plugin

The thin Claude Code layer over the
[Arachne decision loom](https://github.com/pythagorakase/arachne): it
registers the shared MCP adapter (with your bearer token read locally at
connect time — no secret ever ships in config) and installs the client skill
that teaches the agent the publish → inbox → wait → wake workflow.

The plugin is the *client* half only. Arachne is self-hosted: you run your own
server on your own tailnet. Deploy that first
([`DEPLOY.md`](https://github.com/pythagorakase/arachne/blob/main/DEPLOY.md),
[`MCP.md`](https://github.com/pythagorakase/arachne/blob/main/MCP.md));
install this after.

## Prerequisites

- A running Arachne deployment reachable over your tailnet, with the MCP
  adapter served on its own HTTPS port (see `MCP.md` in the repo).
- Tailscale connected on the machine running Claude Code.
- The owner token copied once to this machine (owner-only mode is enforced,
  as is the server's token grammar; the default path honors
  `XDG_STATE_HOME`):

  ```bash
  scp <host>:~/.local/state/arachne/auth-token ~/.local/state/arachne/auth-token
  chmod 600 ~/.local/state/arachne/auth-token
  ```

- Claude Code ≥ 2.1.195 (`headersHelper` path substitution).

## Install

```
/plugin marketplace add pythagorakase/arachne
/plugin install arachne@arachne
```

or non-interactively:

```bash
claude plugin marketplace add pythagorakase/arachne
claude plugin install arachne@arachne
```

## Point It at Your Server

The bundled registration defaults to the author's deployment. Override the
endpoint by exporting `ARACHNE_MCP_URL` in the environment that **launches
Claude Code** (a per-shell export inside a session does nothing):

```bash
export ARACHNE_MCP_URL="https://<your-host>.<your-tailnet>.ts.net:8443/mcp"
```

Token location can likewise be overridden with `ARACHNE_TOKEN_FILE`, either in
the launch environment or in `~/.config/arachne/env` (the helper script
sources that file when the variable is not already set — and refuses to source
it unless it is an owner-only regular file, since sourcing executes it).

## Permissions

Plugins cannot grant their own permissions. To let the tools run unprompted in
normal auto mode, add the plugin-namespaced allowlist entry to
`~/.claude/settings.json` (note the `mcp__plugin_...` prefix — plugin-bundled
servers are namespaced differently from `claude mcp add` registrations):

```json
{
  "permissions": {
    "allow": [
      "mcp__plugin_arachne_arachne__*"
    ]
  }
}
```

## Verify

`/mcp` should list the `arachne` server as connected with five tools:
`status`, `get_ruling`, `wait_for_ruling`, `publish_decision`,
`bootstrap_url`. Quickest live check: ask the agent to call `status` with
`since: 0` and report the health block.

## Update

```
/plugin marketplace update arachne
/plugin update arachne
```

The plugin is versioned by git commit; every merged change to `plugin/` is an
update.
