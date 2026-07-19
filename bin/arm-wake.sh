#!/usr/bin/env bash
# Block on Arachne's server-side waiter and exit only when a real ruling arrives.

set -uo pipefail
umask 077

: "${ARACHNE_URL:?ARACHNE_URL must name the intended Arachne endpoint}"
arachne_url=$ARACHNE_URL
arachne_url=${arachne_url%/}
arachne_state_root=${XDG_STATE_HOME:-${HOME}/.local/state}
arachne_cursor_file=${ARACHNE_CURSOR_FILE:-${arachne_state_root}/arachne/cursor}
arachne_token_file=${ARACHNE_TOKEN_FILE:-${arachne_state_root}/arachne/auth-token}
arachne_request_timeout=${ARACHNE_REQUEST_TIMEOUT:-570}

mkdir -p "$(dirname "$arachne_cursor_file")"

arachne_cursor=0
if [[ -s "$arachne_cursor_file" ]]; then
  IFS= read -r arachne_cursor < "$arachne_cursor_file"
fi
if [[ ! "$arachne_cursor" =~ ^[0-9]+$ ]]; then
  printf 'Arachne: invalid cursor in %s: %s\n' "$arachne_cursor_file" "$arachne_cursor" >&2
  exit 2
fi
if [[ ! -r "$arachne_token_file" ]]; then
  printf 'Arachne: authentication token is not readable: %s\n' "$arachne_token_file" >&2
  exit 2
fi
IFS= read -r arachne_token < "$arachne_token_file"
if (( ${#arachne_token} < 32 || ${#arachne_token} > 256 )) || \
  [[ "$arachne_token" == *[!A-Za-z0-9_-]* ]]; then
  printf 'Arachne: invalid authentication token in %s\n' "$arachne_token_file" >&2
  exit 2
fi

arachne_tmp=$(mktemp -d "${TMPDIR:-/tmp}/arachne-wake.XXXXXX")
arachne_child_pid=
arachne_cleanup() {
  if [[ "$arachne_child_pid" =~ ^[0-9]+$ ]]; then
    kill "$arachne_child_pid" 2>/dev/null || true
    wait "$arachne_child_pid" 2>/dev/null || true
  fi
  rm -rf "$arachne_tmp"
}
trap arachne_cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
arachne_response="$arachne_tmp/response.json"
arachne_curl_config="$arachne_tmp/curl.conf"
arachne_http_code_file="$arachne_tmp/http-code"
printf 'header = "Authorization: Bearer %s"\n' "$arachne_token" >"$arachne_curl_config"
unset arachne_token
arachne_backoff=1

while true; do
  arachne_http_code=000
  : >"$arachne_response"
  : >"$arachne_http_code_file"
  curl \
      --config "$arachne_curl_config" \
      --silent \
      --show-error \
      --connect-timeout 10 \
      --max-time "$arachne_request_timeout" \
      --output "$arachne_response" \
      --write-out '%{http_code}' \
      "${arachne_url}/wait?since=${arachne_cursor}" \
      >"$arachne_http_code_file" &
  arachne_child_pid=$!
  if wait "$arachne_child_pid"; then
    IFS= read -r arachne_http_code <"$arachne_http_code_file"
  else
    arachne_http_code=000
  fi
  arachne_child_pid=

  case "$arachne_http_code" in
    200)
      if ! arachne_sequence=$(python3 - "$arachne_response" "$arachne_cursor" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
sequence = payload.get("sequence")
cursor = int(sys.argv[2])
if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= cursor:
    raise SystemExit("Arachne: server returned a ruling without a valid sequence")
print(sequence)
PY
      ); then
        printf 'Arachne: invalid 200 response from %s\n' "$arachne_url" >&2
        sed -n '1,40p' "$arachne_response" >&2
        exit 1
      fi
      # Put the ruling in the harness pipe before advancing the cursor. If the
      # process dies between these operations, replay is possible but loss is not.
      if ! cat "$arachne_response" || ! printf '\n'; then
        printf 'Arachne: could not emit ruling payload\n' >&2
        exit 1
      fi
      if ! python3 - "$arachne_cursor_file" "$arachne_sequence" <<'PY'
import os
import pathlib
import sys
import tempfile

cursor_path = pathlib.Path(sys.argv[1])
sequence = int(sys.argv[2])
cursor_path.parent.mkdir(parents=True, exist_ok=True)
descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{cursor_path.name}.", dir=cursor_path.parent
)
try:
    with os.fdopen(descriptor, "w", encoding="ascii") as stream:
        stream.write(f"{sequence}\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_name, cursor_path)
    directory = os.open(cursor_path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
PY
      then
        printf 'Arachne: ruling emitted but cursor persistence failed\n' >&2
        exit 1
      fi
      exit 0
      ;;
    204)
      # A normal server-side timeout. Re-arm immediately; this is not polling.
      arachne_backoff=1
      ;;
    4??)
      printf 'Arachne: waiter request failed with HTTP %s\n' "$arachne_http_code" >&2
      sed -n '1,40p' "$arachne_response" >&2
      exit 1
      ;;
    *)
      printf 'Arachne: transient waiter failure (HTTP %s); retrying in %ss\n' \
        "$arachne_http_code" "$arachne_backoff" >&2
      if [[ -s "$arachne_response" ]]; then
        sed -n '1,20p' "$arachne_response" >&2
      fi
      sleep "$arachne_backoff"
      if (( arachne_backoff < 30 )); then
        arachne_backoff=$((arachne_backoff * 2))
      fi
      ;;
  esac
done
