# Arachne — Deployment Runbook

How to make Arachne always-on and reachable from the owner's devices, on the
Whatbox seedbox, tailnet-only. Commands are **illustrative examples** — the
supervision mechanism especially is the implementer's choice (see
[`SPEC.md`](./SPEC.md) §5). What's fixed are the invariants (§2 there):
loopback-only bind, `tailscale serve` not `funnel`, rootless `tailscaled`,
survives reboot.

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
| Tailscale | **not installed yet** (clean slate); `~/bin` absent, not on `PATH` |
| Tailnet | `tail342046` (owner's; existing nodes: `echo` = Mac, `edi-base` = offline, phone, tablet) |

---

## Prerequisites (tailnet admin, one-time)

- **MagicDNS** enabled (gives `arachne.tail342046.ts.net`).
- **HTTPS certificates** enabled (Zero Trust → Settings → Features) — required
  for `tailscale serve` to terminate TLS at the MagicDNS name.

---

## Steps (example commands)

### 1. Lay down the app
```bash
ssh seedbox 'mkdir -p ~/arachne/pages ~/arachne/rulings ~/bin'
rsync -az server.py bin/ keepalive.sh seedbox:arachne/
# publish decision pages with the relative endpoint applied (SPEC §3):
rsync -az /Users/pythagor/nexus/temp/decision_*.html seedbox:arachne/pages/
```

### 2. Install Tailscale, rootless (userspace)
No root, no TUN device — `tailscaled` runs in userspace-networking mode and still
accepts inbound tailnet connections, proxying them to the loopback port.
```bash
ssh seedbox '
  cd /tmp
  VER=1.86.2                                   # check pkgs.tailscale.com/stable/ for current
  curl -fsSLO "https://pkgs.tailscale.com/stable/tailscale_${VER}_amd64.tgz"
  tar -xzf "tailscale_${VER}_amd64.tgz"
  install -m755 tailscale_${VER}_amd64/tailscale tailscale_${VER}_amd64/tailscaled ~/bin/
'
```

### 3. Start the daemon and enroll the node — **the human step**
```bash
# start tailscaled (example supervision: screen; latitude per SPEC §5)
ssh seedbox 'screen -dmS tsd ~/bin/tailscaled \
    --tun=userspace-networking \
    --statedir=$HOME/.tailscale \
    --socket=$HOME/.tailscale/tailscaled.sock \
    --port=0'

# bring the node up — prints a LOGIN URL; the owner opens it to authorize
# "arachne" into tail342046:
ssh seedbox '~/bin/tailscale --socket=$HOME/.tailscale/tailscaled.sock up \
    --hostname=arachne'
```
Capture the printed URL and hand it to the owner. After they authorize:
- In the admin console, **disable key expiry** for the `arachne` node so it
  never silently drops off (this is exactly what happened to `edi-base`).

### 4. Expose it — tailnet-only, TLS
```bash
# serve the loopback app at https://arachne.tail342046.ts.net/  (443).
# NEVER `funnel` — that would make it public and break the invariant + AUP.
ssh seedbox '~/bin/tailscale --socket=$HOME/.tailscale/tailscaled.sock \
    serve --bg 8788'
ssh seedbox '~/bin/tailscale --socket=$HOME/.tailscale/tailscaled.sock serve status'
```
`tailscale serve` works in userspace mode — the proxying happens inside
`tailscaled`, no kernel routing needed.

### 5. Run the server (example: screen)
```bash
ssh seedbox 'cd ~/arachne && screen -dmS arachne \
    env BEAN_DIR=$HOME/arachne BEAN_PORT=8788 /usr/bin/python3 server.py'
ssh seedbox 'curl -s localhost:8788/health'      # {"ok": true, ...}
```

### 6. Survive reboot + self-heal (example: cron; mechanism is latitude)
A tiny `keepalive.sh` that (re)starts `tailscaled`, `serve`, and the app if any
is down, driven by cron:
```cron
@reboot          /home/sylvanmaestro/arachne/keepalive.sh
*/10 * * * *     /home/sylvanmaestro/arachne/keepalive.sh
```
`keepalive.sh` sketch (idempotent; absolute paths since cron's PATH is minimal):
```bash
#!/usr/bin/env bash
TS=$HOME/bin/tailscale; TSD=$HOME/bin/tailscaled
SOCK=$HOME/.tailscale/tailscaled.sock
pgrep -u "$USER" -f tailscaled >/dev/null || \
  screen -dmS tsd "$TSD" --tun=userspace-networking \
    --statedir=$HOME/.tailscale --socket="$SOCK" --port=0
"$TS" --socket="$SOCK" serve status >/dev/null 2>&1 || \
  "$TS" --socket="$SOCK" serve --bg 8788
curl -sf localhost:8788/health >/dev/null || \
  ( cd "$HOME/arachne" && screen -dmS arachne \
    env BEAN_DIR=$HOME/arachne BEAN_PORT=8788 /usr/bin/python3 server.py )
```
*(user-systemd with `Restart=always` + a `.timer` is an equally valid choice.)*

---

## Verify

Run the [`SPEC.md`](./SPEC.md) §7 acceptance criteria against the live service —
especially #4 (push-wake), #5 (missed-wake race), and #8 (tailnet-only negative:
with the Mac's Tailscale off, `arachne.tail342046.ts.net` must not answer and
`nmap -p443,8788 proteus.whatbox.ca` must show no new open public port).

## Host-policy compliance (for any support conversation)

Tailnet-only + device-authenticated (not a "public directory service with no
authentication"); no public port; stdlib footprint (not resource-intensive);
rootless; none of the prohibited categories (LLM/mining/P2P/Tor). Squarely
within the Whatbox software rules and AUP.

## Teardown

```bash
ssh seedbox '~/bin/tailscale --socket=$HOME/.tailscale/tailscaled.sock serve reset'
ssh seedbox '~/bin/tailscale --socket=$HOME/.tailscale/tailscaled.sock down'
ssh seedbox 'screen -S arachne -X quit; screen -S tsd -X quit'
ssh seedbox 'crontab -l | grep -v arachne/keepalive.sh | crontab -'
# then remove the arachne node in the admin console.
```
