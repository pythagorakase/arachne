# Arachne — Deployment Runbook

How to make Arachne always-on and reachable from the owner's devices, on the
Whatbox seedbox, tailnet-only. Commands are **illustrative examples** — the
supervision mechanism especially is the implementer's choice (see
[`SPEC.md`](./SPEC.md) §5). What's fixed are the invariants (§2 there):
loopback-only bind, `tailscale serve` not `funnel`, rootless `tailscaled`,
application authentication on shared-host loopback, survives reboot.

> **One human step, flagged.** Enrolling the node into the tailnet requires a
> browser login (`tailscale up` prints a URL the owner must visit). This is the
> only step that can't be scripted — do not try to automate it away.

---

## Host facts (recon, 2026-07-18)

| Fact | Value |
|------|-------|
| Host / alias | `proteus.whatbox.ca` — `ssh seedbox` (key `~/.ssh/seedbox_key`, BatchMode OK) |
| OS / arch | Linux (Gentoo, kernel 6.18), **x86_64**, AMD EPYC |
| Python | `/usr/bin/python3` → 3.12.13 (stdlib is enough) |
| `$HOME` | `/home/sylvanmaestro` |
| Free space | ~3.1 TB |
| Session keepers | `screen`, `tmux` both present |
| Cron | `crontab` present (vixie cron — supports `@reboot`) |
| curl / rsync | both present |
| Tailscale | Static amd64 binaries in `~/bin`; rootless userspace daemon |
| Tailnet | `tail342046` (owner's; existing nodes: `echo` = Mac, `edi-base` = offline, phone, tablet) |
| Isolation | Shared host/network namespace; loopback is **not** a per-user trust boundary |

---

## Prerequisites (tailnet admin, one-time)

- **MagicDNS** enabled (gives `arachne.tail342046.ts.net`).
- **HTTPS certificates** enabled (Zero Trust → Settings → Features) — required
  for `tailscale serve` to terminate TLS at the MagicDNS name.

---

## Steps (example commands)

### 1. Lay down the app
```bash
ssh seedbox 'mkdir -p ~/arachne/pages ~/.local/state/arachne \
  ~/.local/state/arachne-runtime ~/bin'
rsync -az server.py bin keepalive.sh seedbox:arachne/
# Enforce the relative endpoint + localStorage contract before upload:
bin/publish-page.py /Users/pythagor/nexus/temp/decision_*.html
rsync -az pages/ seedbox:arachne/pages/
```

The durable rulings directory is `~/.local/state/arachne/rulings`, deliberately
outside the source checkout. On first boot the server also creates
`~/.local/state/arachne/auth-token` with mode `0600`; never put that file in the
repository or decision HTML.

### 2. Install Tailscale, rootless (userspace)
No root, no TUN device — `tailscaled` runs in userspace-networking mode and still
accepts inbound tailnet connections, proxying them to the loopback port.
```bash
ssh seedbox '
  cd /tmp
  VER=1.98.9                                   # check pkgs.tailscale.com/stable/ for current
  curl -fsSLO "https://pkgs.tailscale.com/stable/tailscale_${VER}_amd64.tgz"
  tar -xzf "tailscale_${VER}_amd64.tgz"
  install -m755 tailscale_${VER}_amd64/tailscale tailscale_${VER}_amd64/tailscaled ~/bin/
'
```

### 3. Start the daemon and enroll the node — **the human step**
```bash
# start tailscaled once; keepalive.sh takes over after deployment
ssh seedbox 'nohup ~/bin/tailscaled \
    --tun=userspace-networking \
    --statedir=/home/sylvanmaestro/.tailscale \
    --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock \
    --port=0 >~/.local/state/arachne-runtime/tailscaled.log 2>&1 </dev/null & \
    printf "%s\n" "$!" >~/.local/state/arachne-runtime/tailscaled.pid'

# bring the node up — prints a LOGIN URL; the owner opens it to authorize
# "arachne" into tail342046:
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock up \
    --hostname=arachne'
```
Capture the printed URL and hand it to the owner. After they authorize:
- In the admin console, **disable key expiry** for the `arachne` node so it
  never silently drops off (this is exactly what happened to `edi-base`).

### 4. Expose it — tailnet-only, TLS
```bash
# serve the loopback app at https://arachne.tail342046.ts.net/  (443).
# NEVER `funnel` — that would make it public and break the invariant + AUP.
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock \
    serve --bg 8788'
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock serve status'
```
`tailscale serve` works in userspace mode — the proxying happens inside
`tailscaled`, no kernel routing needed.

### 5. Run the server
```bash
ssh seedbox 'cd ~/arachne && ./keepalive.sh'
ssh seedbox 'curl -s localhost:8788/health'      # {"ok": true, ...}
```

Copy the generated token into the same owner-only state path on the machine
that runs `arm-wake.sh`:

```bash
mkdir -p ~/.local/state/arachne && chmod 700 ~/.local/state/arachne
scp seedbox:.local/state/arachne/auth-token ~/.local/state/arachne/auth-token
chmod 600 ~/.local/state/arachne/auth-token
```

Do not paste the token into chat or a PR. To establish a browser session, open a
bootstrap link directly (the secret stays in the URL fragment and is removed
before the decision page loads):

```bash
bin/bootstrap-url.py --open decision_476_relationship_drift.html
```

The resulting cookie lasts two days. Run the helper once per browser/device.

### 6. Survive reboot + self-heal (example: cron; mechanism is latitude)
The checked-in `keepalive.sh` idempotently (re)starts `tailscaled`, configures
`serve`, and restarts the app if its loopback health check fails. Drive it from
cron:
```cron
@reboot          /home/sylvanmaestro/arachne/keepalive.sh
*/10 * * * *     /home/sylvanmaestro/arachne/keepalive.sh
```

Install those entries without disturbing the user's other cron jobs:

```bash
ssh seedbox 'cd ~/arachne && ./bin/install-cron.sh'
```

The script uses PID files and logs under `~/.local/state/arachne-runtime/`; it
does not depend on cron's minimal `PATH`.

---

## Verify

Run the [`SPEC.md`](./SPEC.md) §7 acceptance criteria against the live service —
especially #4 (push-wake), #5 (missed-wake race), and #8 (tailnet-only negative:
with the Mac's Tailscale off, `arachne.tail342046.ts.net` must not answer and
`nmap -p443,8788 proteus.whatbox.ca` must show no new open public port).

Also verify the shared-host boundary: an unauthenticated direct request to
`http://127.0.0.1:8788/<decision>.html`, `/ruling`, or `/wait` must return `401`;
`/health` deliberately remains a non-sensitive unauthenticated liveness signal.

## Host-policy compliance (for any support conversation)

Tailnet-only + device-authenticated remotely, application-authenticated on
shared-host loopback (not a "public directory service with no authentication");
no public port; stdlib footprint (not resource-intensive); rootless; none of the
prohibited categories (LLM/mining/P2P/Tor). Squarely within the Whatbox software
rules and AUP.

## Moving to `edi-base`

The authentication design is host-agnostic. Copy the existing token with mode
`0600` if existing browser bootstrap links should remain valid, or let the new
host generate a fresh token and bootstrap each browser again. After live
verification, remove the cron entries, Serve configuration, and node from the
temporary seedbox.

## Teardown

```bash
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock serve reset'
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock down'
ssh seedbox 'kill "$(cat ~/.local/state/arachne-runtime/server.pid)" \
  "$(cat ~/.local/state/arachne-runtime/tailscaled.pid)"'
ssh seedbox 'cd ~/arachne && ./bin/install-cron.sh --remove'
# then remove the arachne node in the admin console.
```
