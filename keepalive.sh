#!/usr/bin/env bash
# Rootless watchdog for the seedbox deployment. Safe to run repeatedly from cron.

set -uo pipefail

arachne_root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
arachne_runtime=${ARACHNE_RUNTIME_DIR:-/home/sylvanmaestro/.local/state/arachne-runtime}
arachne_data=${ARACHNE_DATA_DIR:-/home/sylvanmaestro/.local/state/arachne}
arachne_pages=${ARACHNE_PAGES_DIR:-${arachne_root}/pages}
arachne_port=${ARACHNE_PORT:-8788}
arachne_python=${ARACHNE_PYTHON:-/usr/bin/python3}
arachne_tailscale=${TAILSCALE_BIN:-/home/sylvanmaestro/bin/tailscale}
arachne_tailscaled=${TAILSCALED_BIN:-/home/sylvanmaestro/bin/tailscaled}
arachne_ts_state=${TAILSCALE_STATE_DIR:-/home/sylvanmaestro/.tailscale}
arachne_ts_socket=${TAILSCALE_SOCKET:-${arachne_ts_state}/tailscaled.sock}

mkdir -p "$arachne_runtime" "$arachne_data" "$arachne_pages" "$arachne_ts_state"
chmod 700 "$arachne_runtime" "$arachne_data" "$arachne_ts_state"

process_matches() {
  local pid_file=$1
  local expected=$2
  local pid_value command_line
  [[ -r "$pid_file" ]] || return 1
  IFS= read -r pid_value < "$pid_file"
  [[ "$pid_value" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid_value" 2>/dev/null || return 1
  command_line=$(ps -p "$pid_value" -o args= 2>/dev/null) || return 1
  [[ "$command_line" == *"$expected"* ]]
}

find_matching_process() {
  local expected=$1
  local process_name pid_value command_line
  process_name=${expected##*/}
  while IFS= read -r pid_value; do
    [[ "$pid_value" =~ ^[0-9]+$ ]] || continue
    command_line=$(ps -p "$pid_value" -o args= 2>/dev/null) || continue
    if [[ "$command_line" == "$expected" || "$command_line" == "$expected "* ]]; then
      printf '%s\n' "$pid_value"
      return 0
    fi
  done < <(pgrep -u "$(id -u)" -x "$process_name" 2>/dev/null)
  return 1
}

if ! process_matches "$arachne_runtime/tailscaled.pid" "$arachne_tailscaled"; then
  if arachne_existing_pid=$(find_matching_process "$arachne_tailscaled"); then
    # Recover a missing or stale PID file instead of launching a second daemon.
    printf '%s\n' "$arachne_existing_pid" > "$arachne_runtime/tailscaled.pid"
  else
    nohup "$arachne_tailscaled" \
      --tun=userspace-networking \
      --statedir="$arachne_ts_state" \
      --socket="$arachne_ts_socket" \
      --port=0 \
      >>"$arachne_runtime/tailscaled.log" 2>&1 </dev/null &
    printf '%s\n' "$!" > "$arachne_runtime/tailscaled.pid"
  fi
fi

arachne_tailscale_ready=0
for _arachne_attempt in {1..100}; do
  if [[ -S "$arachne_ts_socket" ]] && \
    "$arachne_tailscale" --socket="$arachne_ts_socket" status >/dev/null 2>&1; then
    arachne_tailscale_ready=1
    break
  fi
  sleep 0.1
done
if (( ! arachne_tailscale_ready )); then
  printf 'Arachne: tailscaled did not become ready and enrolled at %s\n' \
    "$arachne_ts_socket" >&2
  exit 1
fi

if ! curl --silent --fail --max-time 3 "http://127.0.0.1:${arachne_port}/health" >/dev/null; then
  if process_matches "$arachne_runtime/server.pid" "${arachne_root}/server.py"; then
    arachne_stale_pid=$(<"$arachne_runtime/server.pid")
    kill "$arachne_stale_pid" 2>/dev/null || true
    for _arachne_attempt in {1..50}; do
      kill -0 "$arachne_stale_pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$arachne_stale_pid" 2>/dev/null; then
      printf 'Arachne: unhealthy server PID %s did not stop\n' \
        "$arachne_stale_pid" >&2
      exit 1
    fi
  fi
  nohup env \
    ARACHNE_DATA_DIR="$arachne_data" \
    ARACHNE_PAGES_DIR="$arachne_pages" \
    ARACHNE_PORT="$arachne_port" \
    "$arachne_python" "${arachne_root}/server.py" \
    >>"$arachne_runtime/server.log" 2>&1 </dev/null &
  printf '%s\n' "$!" > "$arachne_runtime/server.pid"
fi

for _arachne_attempt in {1..50}; do
  curl --silent --fail --max-time 1 \
    "http://127.0.0.1:${arachne_port}/health" >/dev/null && break
  sleep 0.1
done
if ! curl --silent --fail --max-time 3 \
  "http://127.0.0.1:${arachne_port}/health" >/dev/null; then
  printf 'Arachne: server failed its loopback health check\n' >&2
  exit 1
fi

if ! "$arachne_tailscale" --socket="$arachne_ts_socket" \
  serve --bg "$arachne_port" >/dev/null; then
  printf 'Arachne: tailscale serve configuration failed\n' >&2
  exit 1
fi
