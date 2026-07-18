#!/usr/bin/env bash
# Install Arachne's idempotent watchdog schedule without disturbing other jobs.

set -euo pipefail

arachne_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
arachne_runtime=${ARACHNE_RUNTIME_DIR:-/home/sylvanmaestro/.local/state/arachne-runtime}
arachne_begin='# BEGIN ARACHNE (managed by bin/install-cron.sh)'
arachne_end='# END ARACHNE'
arachne_tmp=$(mktemp "${TMPDIR:-/tmp}/arachne-cron.XXXXXX")
trap 'rm -f "$arachne_tmp" "${arachne_tmp}.current"' EXIT HUP INT TERM

if ! crontab -l >"${arachne_tmp}.current" 2>/dev/null; then
  : >"${arachne_tmp}.current"
fi

awk -v begin="$arachne_begin" -v end="$arachne_end" '
  $0 == begin { skipping = 1; next }
  $0 == end { skipping = 0; next }
  !skipping { print }
' "${arachne_tmp}.current" >"$arachne_tmp"

if [[ ${1:-} == "--remove" ]]; then
  crontab "$arachne_tmp"
  printf 'Removed Arachne watchdog entries\n'
  exit 0
fi
if (( $# > 0 )); then
  printf 'Usage: %s [--remove]\n' "$0" >&2
  exit 2
fi

mkdir -p "$arachne_runtime"
{
  printf '%s\n' "$arachne_begin"
  printf '@reboot %s/keepalive.sh >>%s/cron.log 2>&1\n' \
    "$arachne_root" "$arachne_runtime"
  printf '*/10 * * * * %s/keepalive.sh >>%s/cron.log 2>&1\n' \
    "$arachne_root" "$arachne_runtime"
  printf '%s\n' "$arachne_end"
} >>"$arachne_tmp"

crontab "$arachne_tmp"
printf 'Installed Arachne watchdog entries for %s\n' "$arachne_root"
