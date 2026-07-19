#!/usr/bin/env bash
# Install Arachne's idempotent watchdog schedule without disturbing other jobs.

set -euo pipefail

arachne_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
: "${HOME:?HOME must be set}"
arachne_runtime=${ARACHNE_RUNTIME_DIR:-${XDG_STATE_HOME:-${HOME}/.local/state}/arachne-runtime}
arachne_begin='# BEGIN ARACHNE (managed by bin/install-cron.sh)'
arachne_end='# END ARACHNE'
arachne_tmp=$(mktemp "${TMPDIR:-/tmp}/arachne-cron.XXXXXX")
trap 'rm -f "$arachne_tmp" "${arachne_tmp}.current" "${arachne_tmp}.error"' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if crontab -l >"${arachne_tmp}.current" 2>"${arachne_tmp}.error"; then
  :
elif grep -Eiq 'no crontab([[:space:]]|$)' "${arachne_tmp}.error"; then
  : >"${arachne_tmp}.current"
else
  printf 'Arachne: could not read the existing crontab; refusing to replace it\n' >&2
  sed -n '1,20p' "${arachne_tmp}.error" >&2
  exit 1
fi

if ! awk -v begin="$arachne_begin" -v end="$arachne_end" \
    -v error_file="${arachne_tmp}.error" '
  $0 == begin {
    begin_count++
    if (inside || begin_count > 1) invalid = 1
    inside = 1
    next
  }
  $0 == end {
    end_count++
    if (!inside || end_count > 1) invalid = 1
    inside = 0
    next
  }
  END {
    if (inside || begin_count != end_count) invalid = 1
    if (invalid) {
      print "unbalanced or duplicate Arachne managed markers" > error_file
      exit 1
    }
  }
' "${arachne_tmp}.current"; then
  printf 'Arachne: refusing to edit an ambiguous crontab: ' >&2
  cat "${arachne_tmp}.error" >&2
  exit 1
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
chmod 700 "$arachne_runtime"
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
