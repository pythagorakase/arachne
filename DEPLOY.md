# Arachne — Deployment Runbook

> **Current deployment: `cairn`** (home Ubuntu box, system `tailscaled`,
> user-systemd supervision), cut over 2026-07-19 as a deliberate fresh-state
> migration; the seedbox's final rulings are archived client-side. The
> MacBook-bridge and seedbox sections below are retained as the historical
> runbook and for teardown reference.

How to make Arachne always-on and reachable from the owner's devices,
tailnet-only. The durable deployment is the Ubuntu host `cairn`; the MacBook
bridge is retained only as rollback state. The older Whatbox procedure remains
below as a shared-host reference, but is not part of the current deployment.

Commands are **illustrative examples** — the supervision mechanism especially
is the implementer's choice (see [`SPEC.md`](./SPEC.md) §5). What's fixed are
the invariants (§2 there): loopback-only bind, `tailscale serve` not `funnel`,
application authentication, and supervised restart. A shared host additionally
requires verified HTTPS from Serve to Arachne using a private localhost CA.
The personal MacBook may temporarily use same-user loopback HTTP behind
Tailscale TLS; Ubuntu should restore the verified private-CA backend.

> **One human step, flagged.** Enrolling the node into the tailnet requires a
> browser login (`tailscale up` prints a URL the owner must visit). This is the
> only step that can't be scripted — do not try to automate it away.

---

## Historical: MacBook bridge (retired)

Use one owner-only `~/.config/arachne/deployment.env` for both the core and MCP
adapter. Avoid ports already occupied by an older local decision server. For
the current checkout, a representative configuration is:

```dotenv
ARACHNE_RUNTIME_DIR=/Users/OWNER/.local/state/arachne-runtime
ARACHNE_DATA_DIR=/Users/OWNER/.local/state/arachne
ARACHNE_PAGES_DIR=/Users/OWNER/arachne/pages
ARACHNE_TOKEN_FILE=/Users/OWNER/.local/state/arachne/auth-token
ARACHNE_PORT=8878
ARACHNE_PYTHON=/Users/OWNER/arachne/.venv/bin/python
ARACHNE_SECURE_COOKIE=true
ARACHNE_WAIT_SECONDS=540
ARACHNE_URL=http://127.0.0.1:8878
ARACHNE_PUBLIC_URL=https://MAC.tailnet-name.ts.net
ARACHNE_MCP_HOST=127.0.0.1
ARACHNE_MCP_PORT=8879
ARACHNE_MCP_ALLOWED_HOSTS=127.0.0.1:8879,localhost:8879,MAC.tailnet-name.ts.net:8443
ARACHNE_MCP_HEARTBEAT_SECONDS=30
ARACHNE_REQUEST_TIMEOUT=570
ARACHNE_MCP_PYTHON=/Users/OWNER/arachne/.venv/bin/python
```

Run `uv sync --frozen`, create the runtime directory, and restrict both the
environment and token to mode `0600`. Render the templates under
`deploy/macos/` into `~/Library/LaunchAgents/`, replacing their three absolute
path placeholders, then bootstrap both labels with `launchctl`. Expose the core
and MCP listeners through distinct Tailscale HTTPS ports:

```bash
tailscale serve --bg --yes http://127.0.0.1:8878
tailscale serve --bg --yes --https=8443 http://127.0.0.1:8879
```

Do not use Funnel or expose either process on a LAN/public bind. Agent harnesses
use the tailnet HTTPS MCP endpoint with its additional bearer credential; see
[`MCP.md`](./MCP.md). Before migration, stop both LaunchAgents and record the
core's latest sequence. The sidecar has no durable state of its own.

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
| Tailnet | `tail342046` (owner's; `echo` = Mac, `cairn` = Ubuntu, plus phone and tablet) |
| Isolation | Shared host/network namespace; loopback is **not** a per-user trust boundary |

---

## Prerequisites (tailnet admin, one-time)

- **MagicDNS** enabled (gives `arachne.tail342046.ts.net`).
- **HTTPS certificates** enabled (Zero Trust → Settings → Features) — required
  for `tailscale serve` to terminate TLS at the MagicDNS name.
- OpenSSL, used by the checked-in TLS initializer to create a private localhost
  CA and a leaf with `DNS:localhost` and `IP:127.0.0.1` subject alternatives.

### iPhone and iPad convenience

After enrolling an iPhone or iPad in the tailnet, enable Tailscale **VPN On
Demand** and use MagicDNS hostname matching for `*.ts.net`. The Arachne Home
Screen app can then bring up the tunnel when its private URL is opened. Only
one on-demand VPN can be active at a time on iOS/iPadOS, so confirm that another
VPN profile is not taking precedence. See Tailscale's
[iOS and macOS VPN On Demand guide](https://tailscale.com/docs/features/client/ios-vpn-on-demand).

Install Arachne from the authenticated inbox in Safari. If the installed app's
sliding session later lapses, mint a no-argument `bootstrap_url()` and paste the
single-use URL into its locked screen; do not save the enrollment URL or owner
token in deployment notes.

---

## Steps (example commands)

### 1. Lay down the app
```bash
ssh seedbox 'mkdir -p ~/arachne/pages ~/.local/state/arachne \
  ~/.local/state/arachne-runtime ~/bin'
rsync -az server.py ui bin keepalive.sh seedbox:arachne/
# Enforce the v2 nav contract and publish every matched brief using its own
# <html>/<body> data-issue before upload:
bin/publish-page.py /Users/pythagor/nexus/temp/decision_*.html --pages-dir pages
rsync -az pages/ seedbox:arachne/pages/
```

The durable rulings directory is `~/.local/state/arachne/rulings`, deliberately
outside the source checkout. On first boot the server also creates
`~/.local/state/arachne/auth-token` with mode `0600`; never put that file in the
repository or decision HTML.

### 2. Install the private localhost TLS material

Create the seedbox-specific `~/.config/arachne/deployment.env` as a regular file
owned by the deployment user with mode `0600`. `keepalive.sh` loads that path by
default; `ARACHNE_DEPLOY_ENV` may name a different file. Use absolute paths:

```dotenv
ARACHNE_RUNTIME_DIR=/home/sylvanmaestro/.local/state/arachne-runtime
ARACHNE_DATA_DIR=/home/sylvanmaestro/.local/state/arachne
ARACHNE_PAGES_DIR=/home/sylvanmaestro/arachne/pages
ARACHNE_PORT=8788
ARACHNE_PYTHON=/usr/bin/python3
ARACHNE_TLS_DIR=/home/sylvanmaestro/.local/state/arachne-tls
ARACHNE_MANAGE_TAILSCALED=true
TAILSCALE_BIN=/home/sylvanmaestro/bin/tailscale
TAILSCALED_BIN=/home/sylvanmaestro/bin/tailscaled
TAILSCALE_STATE_DIR=/home/sylvanmaestro/.tailscale
TAILSCALE_SOCKET=/home/sylvanmaestro/.tailscale/tailscaled.sock
```

`ARACHNE_PYTHON` may name a host-provided wrapper such as `/usr/bin/python3`.
The watchdog asks that command for `sys.executable` and uses the returned
absolute executable for both launch and exact process matching. This preserves
the identity check on shared hosts whose wrapper replaces itself with a
versioned interpreter path.

`ARACHNE_SYSTEM_CA_BUNDLE` may override the system bundle path; the initializer
otherwise detects common Linux/macOS locations. Run it once before enrollment:

```bash
ssh seedbox 'set -a; . ~/.config/arachne/deployment.env; set +a; \
  cd ~/arachne && ./bin/init-backend-tls.sh'
```

The initializer atomically creates `ca-key.pem`, `ca-cert.pem`,
`server-key.pem`, `server-cert.pem`, and `trust-bundle.pem` in an owner-only TLS
directory. It fails loud on partial or invalid identity material, while safely
refreshing the derived trust bundle when the host's system CA bundle changes.
The CA key remains host-local and owner-only; never publish or copy it into the
app state transfer. The watchdog passes the trust bundle to rootless
`tailscaled` and the leaf certificate/key to Arachne. It also fails closed on a
missing, non-regular, wrong-owner, or group/other-accessible deployment file.

### 3. Install Tailscale, rootless (userspace)
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

### 4. Start the daemon and enroll the node — **the human step**
```bash
# start tailscaled once; keepalive.sh takes over after deployment
ssh seedbox 'SSL_CERT_FILE="$HOME/.local/state/arachne-tls/trust-bundle.pem" \
  nohup ~/bin/tailscaled \
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
  never silently drops off while unattended.

### 5. Expose it — tailnet-only, verified TLS on both hops
```bash
# Serve the HTTPS loopback app at https://arachne.tail342046.ts.net/ (443).
# Do not use an insecure-TLS target: backend certificate verification is the
# defense against another seedbox account claiming port 8788.
# NEVER `funnel` — that would make it public and break the invariant + AUP.
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock \
    serve --bg https://localhost:8788'
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock serve status'
```
`tailscale serve` works in userspace mode — the proxying happens inside
`tailscaled`, no kernel routing needed.

### 6. Run the server
```bash
ssh seedbox 'cd ~/arachne && ./keepalive.sh'
ssh seedbox 'curl --fail --cacert ~/.local/state/arachne-tls/ca-cert.pem \
  https://localhost:8788/health'                 # includes "tls": true
```

Copy the generated token into the same owner-only state path on the machine
that runs `arm-wake.sh`:

```bash
mkdir -p ~/.local/state/arachne && chmod 700 ~/.local/state/arachne
scp seedbox:.local/state/arachne/auth-token ~/.local/state/arachne/auth-token
chmod 600 ~/.local/state/arachne/auth-token

# Set both endpoints in the environment that launches the waiter/helpers.
export ARACHNE_URL=https://arachne.tail342046.ts.net
export ARACHNE_PUBLIC_URL=$ARACHNE_URL
```

Do not paste the token into chat or a PR. To establish a browser session, open a
bootstrap link directly (the secret stays in the URL fragment and is removed
before the page loads):

```bash
bin/bootstrap-url.py --open                                      # inbox at /
bin/bootstrap-url.py --open decision_476_relationship_drift.html # deep link
```

The resulting session lasts fifteen days in both the cookie and the
server-validated credential, and slides on active use: any authenticated visit
past the half-life re-issues the full window. Run the helper once per
browser/device; a device that opens the inbox regularly never re-enrolls.

### 7. Survive reboot + self-heal (example: cron; mechanism is latitude)
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

Verify both parts of the shared-host boundary from the seedbox:

```bash
# The real private CA succeeds and health reports "tls": true.
curl --fail --cacert ~/.local/state/arachne-tls/ca-cert.pem \
  https://localhost:8788/health

# The OS trust store alone must reject the private backend certificate.
curl --fail https://localhost:8788/health
```

An unauthenticated request made with `--cacert` to an allowlisted decision page,
`/ruling`, `/rulings?since=0`, `/rulings/1`, or `/wait` must return `401`;
`/health` deliberately remains a non-sensitive unauthenticated liveness signal.
Confirm `tailscale serve status` shows `https://localhost:8788`, never an HTTP
or insecure-TLS target.

## Host-policy compliance (for any support conversation)

Tailnet-only + device-authenticated remotely, application-authenticated on
shared-host loopback (not a "public directory service with no authentication");
no public port; stdlib footprint (not resource-intensive); rootless; none of the
prohibited categories (LLM/mining/P2P/Tor). Squarely within the Whatbox software
rules and AUP.

## Moving the MacBook bridge to `cairn`

The cutover has one durable application boundary: the complete
`ARACHNE_DATA_DIR` (including `auth-token` and `rulings/`) plus every published
file in `ARACHNE_PAGES_DIR`. Preserve modes. The MCP adapter stores neither
rulings nor cursors.

1. Prepare the Ubuntu checkout, virtual environment, destination TLS identity,
   owner-only environment, user-systemd units, and Tailscale Serve config
   without starting Arachne.
2. Stop any active waiter in its agent session without advancing its external
   cursor. Stop both Mac LaunchAgents and confirm ports 8878 and 8879 are no
   longer listening.
3. Record `latest_sequence` from the stopped source, then transfer the complete
   data and pages directories with ownership and modes preserved. Do not copy
   the Mac runtime directory or destination TLS keys.
4. Start `arachne.service`; verify health, the recorded sequence, and one known
   page. Start `arachne-mcp.service`; call `status` with the existing cursor.
5. Point Tailscale Serve at the verified Ubuntu HTTPS backend, update client
   `ARACHNE_PUBLIC_URL`/MCP registration if the hostname changed, and re-arm the
   waiter from the same cursor. Do not accept rulings on both hosts at once.

The detailed continuity and rollback checks below also apply, substituting the
MacBook for the seedbox wherever it is the source.

## Moving from the seedbox to `cairn`

This is a stateful cutover, not a node-name substitution. The server's next
sequence number is reconstructed from `rulings/`, while `arm-wake.sh` retains
its last-consumed cursor on the orchestrating machine. Starting an empty
destination behind an existing cursor can therefore suppress several wakes.

### 1. Prepare the destination without starting Arachne

- Install the checkout and a destination-specific `deployment.env`; do not copy
  the seedbox file unchanged. Point data, runtime, pages, Python, and TLS paths
  at real `cairn` locations.
- Install `uv`, run `uv sync --frozen`, and install the two checked-in user
  units from `deploy/systemd/`. Start the core before the MCP adapter.
  **Run every `uv` command as the service user, never via `sudo`.** uv installs
  by hardlinking from `~/.cache/uv`; one root-umask invocation seeds the cache
  with root-owned mode-600 blobs, and later user-level syncs hardlink those
  same unreadable inodes into the venv. The failure is deferred and oblique:
  imports may still succeed while `importlib.metadata.version()` returns
  `None` from the unreadable `METADATA`, crashing the MCP handshake. If it
  happens: remove `~/.cache/uv` and `.venv` as the service user (directory
  write permission suffices) and re-run `uv sync --frozen`.
- Inventory the currently published HTML under the seedbox's `~/arachne/pages/`.
  Those files are runtime content and deliberately git-ignored, so the checkout
  alone is not a page backup.
- Use Ubuntu's **system** `tailscaled`; do not start a second rootless daemon.
  Give the deployment account narrow Serve authority, then verify it can read
  status without sudo:

  ```bash
  sudo tailscale set --operator="$USER"
  tailscale status
  ```

- With the destination environment loaded, run `bin/init-backend-tls.sh` to
  generate a destination-specific CA/leaf set. Install its CA certificate into
  Ubuntu's trust store so the system `tailscaled` can verify the leaf, then
  restart it:

  ```bash
  set -a; . ~/.config/arachne/deployment.env; set +a
  ~/arachne/bin/init-backend-tls.sh
  sudo install -m 0644 ~/.local/state/arachne-tls/ca-cert.pem \
    /usr/local/share/ca-certificates/arachne-localhost-ca.crt
  sudo update-ca-certificates
  sudo systemctl restart tailscaled
  ```

  The CA certificate is public; its signing key remains owner-only in
  `ARACHNE_TLS_DIR`. Never copy that TLS directory with application state or
  replace verification with an insecure HTTPS target.

For example, install this as the `cairn` deployment user's owner-only
`~/.config/arachne/deployment.env`, replacing `/home/OWNER` with that user's
actual home directory:

```dotenv
ARACHNE_RUNTIME_DIR=/home/OWNER/.local/state/arachne-runtime
ARACHNE_DATA_DIR=/home/OWNER/.local/state/arachne
ARACHNE_PAGES_DIR=/home/OWNER/arachne/pages
ARACHNE_TOKEN_FILE=/home/OWNER/.local/state/arachne/auth-token
ARACHNE_PORT=8788
ARACHNE_PYTHON=/home/OWNER/arachne/.venv/bin/python
ARACHNE_TLS_DIR=/home/OWNER/.local/state/arachne-tls
ARACHNE_TLS_CERT_FILE=/home/OWNER/.local/state/arachne-tls/server-cert.pem
ARACHNE_TLS_KEY_FILE=/home/OWNER/.local/state/arachne-tls/server-key.pem
ARACHNE_SYSTEM_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ARACHNE_MANAGE_TAILSCALED=false
ARACHNE_URL=https://127.0.0.1:8788
ARACHNE_PUBLIC_URL=https://cairn.tail342046.ts.net:8444
ARACHNE_CA_FILE=/home/OWNER/.local/state/arachne-tls/trust-bundle.pem
ARACHNE_MCP_HOST=127.0.0.1
ARACHNE_MCP_PORT=8790
ARACHNE_MCP_ALLOWED_HOSTS=127.0.0.1:8790,localhost:8790,cairn.tail342046.ts.net:8443
ARACHNE_MCP_HEARTBEAT_SECONDS=30
ARACHNE_REQUEST_TIMEOUT=570
ARACHNE_MCP_PYTHON=/home/OWNER/arachne/.venv/bin/python
TAILSCALE_BIN=/usr/bin/tailscale
TAILSCALE_SOCKET=/var/run/tailscale/tailscaled.sock
```

The same `sys.executable` resolution applies in system-daemon mode, including
virtual environments and distribution-provided Python wrappers.

System mode never launches or stops `tailscaled`; Ubuntu owns that lifecycle.
The operator setting permits the unprivileged watchdog to inspect the system
daemon and configure Serve through its socket. The MCP unit keeps no durable
cursor database; `ARACHNE_DATA_DIR`, `ARACHNE_PAGES_DIR`, and the external
client cursor are the continuity boundary.

### 2. Warm-copy, then quiesce the source

An optional warm copy can reduce downtime, but it is not the cutover copy. Copy
the **entire** `~/.local/state/arachne/` tree, including `auth-token` and every
file under `rulings/`, while preserving modes. In the same agent session that
will continue after cutover, terminate and wait for every currently armed
`arm-wake.sh` child that targets the seedbox; do not edit or advance its
persisted cursor.

Because form drafts live in origin-scoped browser `localStorage`, also confirm
there is no unfinished answer at the old seedbox hostname. Finish it before the
cutover, explicitly abandon it, or export it through page-specific tooling. A
hostname change cannot carry a browser draft automatically.

Before the final copy, create the watchdog's quiesce sentinel, remove future
cron starts, and wait for any invocation that was already running to release its
lock. The sentinel is checked both before and after lock acquisition, closing
the fork-before-crontab-edit race:

```bash
ssh seedbox 'install -d -m 700 ~/.local/state/arachne-runtime && \
  install -m 600 /dev/null ~/.local/state/arachne-runtime/QUIESCED'
ssh seedbox 'cd ~/arachne && ./bin/install-cron.sh --remove'
ssh seedbox 'crontab -l 2>/dev/null | grep -F "BEGIN ARACHNE" && exit 1 || exit 0'
ssh seedbox '
  lock=$HOME/.local/state/arachne-runtime/keepalive.lock
  arachne_wait=0
  while [ -d "$lock" ] && [ "$arachne_wait" -lt 300 ]; do
    sleep 0.1
    arachne_wait=$((arachne_wait + 1))
  done
  test ! -d "$lock"
'
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock serve reset'

# This is only a final liveness check. Record latest_sequence after the exact
# server process has stopped so an already-admitted request cannot race it.
ssh seedbox 'curl --fail --silent \
  --cacert ~/.local/state/arachne-tls/ca-cert.pem \
  https://localhost:8788/health'
```

Then stop only the expected Arachne server process. Refuse to signal a missing,
non-numeric, dead, or identity-mismatched PID:

```bash
ssh seedbox '
  pid_file=$HOME/.local/state/arachne-runtime/server.pid
  IFS= read -r pid <"$pid_file" || exit 0
  case $pid in (""|*[!0-9]*) echo "invalid server PID" >&2; exit 1;; esac
  args=$(ps -p "$pid" -o args=) || exit 0
  expected="/usr/bin/python3 $HOME/arachne/server.py"
  if [ "$args" = "$expected" ]; then
    kill "$pid"
    arachne_wait=0
    while kill -0 "$pid" 2>/dev/null && [ "$arachne_wait" -lt 50 ]; do
      sleep 0.1
      arachne_wait=$((arachne_wait + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "server PID $pid did not stop" >&2
      exit 1
    fi
  else
    echo "refusing to stop PID $pid: $args" >&2
    exit 1
  fi
'
```

With the exact server stopped, load the committed store once and record its
final sequence in the cutover notes. This also re-validates every committed
ruling before it is copied:

```bash
ssh seedbox "cd ~/arachne && python3 -c 'from pathlib import Path; from server import RulingStore; print(RulingStore(Path.home() / \".local/state/arachne\").latest_sequence)'"
```

After the source is quiescent, make the mandatory final copy through an
owner-only local staging directory. The destination below must be a new,
dedicated path; stop if it already exists.

```bash
arachne_cutover_dir=$(mktemp -d "${TMPDIR:-/tmp}/arachne-cutover.XXXXXX")
chmod 700 "$arachne_cutover_dir"
rsync --archive seedbox:.local/state/arachne/ "$arachne_cutover_dir/state/"
rsync --archive seedbox:arachne/pages/ "$arachne_cutover_dir/pages/"

ssh cairn 'test ! -e ~/.local/state/arachne-cutover-final && \
  install -d -m 700 ~/.local/state/arachne-cutover-final && \
  install -d -m 755 ~/arachne/pages'
rsync --archive "$arachne_cutover_dir/state/" \
  cairn:.local/state/arachne-cutover-final/
rsync --archive --delete "$arachne_cutover_dir/pages/" cairn:arachne/pages/
rsync --archive --checksum --dry-run "$arachne_cutover_dir/state/" \
  cairn:.local/state/arachne-cutover-final/
rsync --archive --delete --checksum --dry-run "$arachne_cutover_dir/pages/" \
  cairn:arachne/pages/
```

Both dry runs must show no file differences. On `cairn`, move any prior data
directory to a named backup, then rename `arachne-cutover-final` to `arachne`;
do not merge a fresh final copy into an old live state tree. Verify the expected
page names are regular, non-symlink HTML files before start. Keep the protected
local staging directory until validation completes, then dispose of it without
printing or copying its `auth-token` elsewhere.

### 3. Start and prove continuity

Start Arachne on `cairn`, configure Serve port 8444 to the verified target
`https://localhost:8788`, and check all of the following before routing real
decisions there:

- local `/health` succeeds only with the private CA, reports `"tls": true`, and
  has exactly the recorded source `latest_sequence`;
- the destination rulings manifest/checksums match the quiescent source;
- the published-page manifest/checksums match the quiescent source;
- the orchestrator's persisted cursor is not ahead of that sequence;
- the new MagicDNS URL is tailnet-only and rejects unauthenticated sensitive
  requests.

Then switch both agent-side endpoint variables to the exact HTTPS URL reported
by `tailscale serve status`, and re-arm `arm-wake.sh` **from the same agent
session**, using the existing token and cursor files. Do not leave an old
seedbox waiter parked in parallel:

```bash
export ARACHNE_URL=https://cairn.tail342046.ts.net:8444
export ARACHNE_PUBLIC_URL=$ARACHNE_URL
bin/arm-wake.sh &
bin/bootstrap-url.py --open phone-smoke.html  # use the published smoke-page name
```

The synthetic ruling must release that newly armed waiter and receive exactly
the next sequence. Replace the example hostname and page name with the values
shown by Serve and the publisher; both helpers deliberately refuse to guess a
deployment URL.

Preserving `auth-token` preserves the application secret, but browser cookies
are scoped to the old hostname; bootstrap each browser at the new URL. If a
deliberate fresh-state migration is chosen instead, first prove no source ruling
is unconsumed, reset/reconcile the orchestrator cursor to the destination's
sequence, rotate the token, and bootstrap every browser. Never combine an empty
ruling store with the old cursor.

## Custom tailnet-only domain (`arachne.pythagora.net`)

The stable inbox deserves a stable, memorable name. This section fronts the
`cairn` deployment with a Cloudflare-managed hostname **without changing the
exposure invariant**: the record is DNS-only, the TLS terminator binds only the
tailnet interface, and certificates arrive via DNS-01, so no public listener
ever exists. A device still needs Tailscale to reach it; the ts.net name keeps
working in parallel.

Three properties make this shape spec-compliant (SPEC §6):

- **DNS-only record, never proxied.** The A record points at `cairn`'s tailnet
  address (`100.64.0.0/10`), which is unroutable from the public internet. An
  orange-cloud (proxied) record would both fail — Cloudflare's edge cannot
  reach a tailnet address — and constitute the public reverse proxy the spec
  forbids. Publishing the tailnet IP in public DNS discloses tailnet
  membership, not reachability; that trade is accepted.
- **Caddy binds the tailnet address only.** Never `0.0.0.0` — a wildcard bind
  would expose the service to `cairn`'s LAN. The systemd unit's
  `CAP_NET_BIND_SERVICE` covers port 443 on that interface.
- **DNS-01 issuance.** Let's Encrypt validates domain control through a TXT
  record Caddy writes via a scoped Cloudflare API token. No inbound HTTP-01
  challenge, therefore no public port 80/443, is ever required.

### 1. Cloudflare (dashboard, one-time)

1. Find `cairn`'s tailnet IPv4: `tailscale ip -4` on `cairn`.
2. In the `pythagora.net` zone add an **A** record: name `arachne`, content
   `<tailnet IPv4>`, proxy status **DNS only** (grey cloud). Do **not** add an
   AAAA record unless the Caddyfile also binds the tailnet IPv6 address — an
   advertised address with no listener stalls IPv6-capable clients until IPv4
   fallback.
3. Create an API token scoped to exactly this zone: *Zone → DNS → Edit* plus
   *Zone → Zone → Read*, zone resources limited to `pythagora.net`. This token
   can only manage DNS records for the one zone; treat it as a secret anyway.

### 2. Caddy on `cairn` (root-managed; fine on an owned host)

Distro Caddy packages are too old for `caddy add-package` (Ubuntu ships a
2.6-era build without it). Install the apt package for its systemd unit,
user, and directories, then divert the binary and drop in an official
build-service binary that bakes in the Cloudflare DNS module:

```bash
sudo apt install caddy                      # unit + caddy user + /etc/caddy
arch=$(dpkg --print-architecture)           # amd64 / arm64
curl -fsSL "https://caddyserver.com/api/download?os=linux&arch=${arch}&p=github.com/caddy-dns/cloudflare" \
  -o /tmp/caddy-custom && chmod +x /tmp/caddy-custom
/tmp/caddy-custom list-modules | grep dns.providers.cloudflare   # must print
sudo dpkg-divert --divert /usr/bin/caddy.dpkg --rename /usr/bin/caddy
sudo install -m 755 -o root -g root /tmp/caddy-custom /usr/bin/caddy

sudo sh -c 'umask 077; echo CLOUDFLARE_API_TOKEN=<token> > /etc/caddy/cloudflare.env'
sudo chown root:root /etc/caddy/cloudflare.env
sudo mkdir -p /etc/systemd/system/caddy.service.d
printf '[Service]\nEnvironmentFile=/etc/caddy/cloudflare.env\n' | \
  sudo tee /etc/systemd/system/caddy.service.d/cloudflare.conf >/dev/null
sudo systemctl daemon-reload

# The caddy service user cannot read Arachne's owner-only state dir; give it
# a copy of the PUBLIC CA certificate only (never key material):
sudo install -m 644 -o root -g root \
  ~/.local/state/arachne-tls/ca-cert.pem /etc/caddy/arachne-ca.pem
```

Install `deploy/caddy/Caddyfile` at `/etc/caddy/Caddyfile` after replacing the
`@@...@@` placeholders (tailnet IPv4 and `/etc/caddy/arachne-ca.pem`; uncomment
the second site block only if the MCP adapter should also ride the custom
name). The proxy target is the same verified-HTTPS loopback backend Tailscale
Serve uses; Caddy verifies it against the private CA, so a different process
claiming port 8788 cannot impersonate Arachne here either. Validate with the
token exported — the env file is root-owned 0600, so validation must run as
root: `sudo sh -c 'set -a; . /etc/caddy/cloudflare.env; set +a; caddy validate
--config /etc/caddy/Caddyfile --adapter caddyfile'`. An unprivileged shell
cannot even source the file, and a validate without the token fails on an
empty-token provision error even when the config is correct. Because the apt
package started the stock binary before the swap, the first activation must be
a full `sudo systemctl restart caddy` — a reload would hand the
Cloudflare-provider config to the old process, which cannot load it. Reloads
are fine for every subsequent edit. Afterwards confirm with `ss -tlnp` that
:443 listens only on the tailnet address and that **no :80 listener exists at
all** — the Caddyfile disables the automatic HTTP→HTTPS redirect server
(`auto_https disable_redirects`), since DNS-01 needs no HTTP listener and
redirect servers are not guaranteed to inherit bind addresses on every Caddy
version. Re-copy `arachne-ca.pem` if the private CA is ever rotated, and
expect apt to hold the diverted binary (`caddy.dpkg`) while renewals ride the
custom build.

```bash
sudo systemctl restart caddy   # first activation MUST be a restart (see below);
                               # use `reload` only for subsequent config edits
```

If the MCP adapter is fronted too, append `arachne.pythagora.net:8443` to
`ARACHNE_MCP_ALLOWED_HOSTS` and restart `arachne-mcp.service`.

### 3. Switch the canonical URL

Browser sessions are cookie-scoped per hostname, so pick **one** canonical
name for bookmarks — the custom domain — and let ts.net remain a fallback:

```bash
# in the owner-only environment file loaded by both services and helpers
ARACHNE_PUBLIC_URL=https://arachne.pythagora.net
```

Freshly minted bootstrap links and published-page URLs now use the custom
name. Bootstrap each device once at the new hostname (`bin/bootstrap-url.py
--open`), add `/` to the phone home screen, and the enrollment cycle ends
there — the fifteen-day sliding session renews itself on use.

### 4. Verify

```bash
dig +short arachne.pythagora.net            # the 100.x tailnet address, nothing else
curl --fail https://arachne.pythagora.net/health          # on-tailnet: ok
ss -tlnp | grep caddy                       # binds <tailnet IP>:443 only, no 0.0.0.0
```

From an off-tailnet network (phone with Tailscale toggled off), the name must
not connect at all; a public scan of `cairn`'s internet-facing address must
show no new open port. On-tailnet, an unauthenticated `GET /` returns the
friendly locked shell and names nothing.

## Teardown

Only after the destination passes the continuity test:

1. Confirm the seedbox watchdog remains removed. This prevents a restart race.
2. Reset Serve and take down the temporary rootless node.
3. Stop the rootless `tailscaled` only after the PID-file identity check used
   above, matching its exact binary path. Never blindly kill a PID-file value.
4. Remove the `arachne` node in the Tailscale admin console.
5. Retain the quiescent seedbox state as a rollback copy until the home service
   has survived the agreed observation window; deletion is a separate action.

For a rollback, make the destination fully quiescent before copying anything:

1. In the same agent session, terminate and wait for every `arm-wake.sh` child
   targeting the destination. Do not edit or advance its persisted cursor.
2. Finish, explicitly abandon, or export every unfinished browser draft at the
   destination hostname; its origin-scoped `localStorage` cannot follow the
   rollback automatically.
3. Create the destination `QUIESCED` sentinel, remove its Arachne cron entries,
   and wait for the watchdog lock exactly as in the source procedure above.
4. Reset the destination Serve mapping, then stop only the exact destination
   Arachne PID using the same executable-and-arguments identity check shown
   above, adapted to its deployment paths. Do not copy while its listener is
   alive.
5. With the process stopped, load `RulingStore` from the destination data path
   and record the final committed sequence. This is the rollback source of
   truth, not an earlier health response.

If any destination ruling advanced that final sequence—even the smoke ruling—
reverse-sync the destination's complete app state and published pages into a
fresh seedbox staging directory, verify checksums, and atomically replace the
seedbox state before removing its `QUIESCED` sentinel. Only then reinstall
seedbox cron, restore both agent-side endpoint variables, and re-arm the existing
cursor. The narrower alternative (discarding destination state and
lowering/reconciling the cursor) is allowed only after proving every discarded
sequence was synthetic and never consumed as a real decision. Never run both
origins as writable services against one cursor.

```bash
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock serve reset'
ssh seedbox '~/bin/tailscale --socket=/home/sylvanmaestro/.tailscale/tailscaled.sock down'
ssh seedbox '
  pid_file=$HOME/.local/state/arachne-runtime/tailscaled.pid
  IFS= read -r pid <"$pid_file" || exit 0
  case $pid in (""|*[!0-9]*) echo "invalid tailscaled PID" >&2; exit 1;; esac
  args=$(ps -p "$pid" -o args=) || exit 0
  expected=$HOME/bin/tailscaled
  case $args in
    ("$expected"|"$expected "*) kill "$pid" ;;
    (*) echo "refusing to stop PID $pid: $args" >&2; exit 1 ;;
  esac
'
```
