#!/usr/bin/env bash
# Serialized watchdog for rootless seedboxes and system-daemon Linux hosts.

set -euo pipefail
umask 077

: "${HOME:?HOME must be set}"
arachne_root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
arachne_deploy_env=${ARACHNE_DEPLOY_ENV:-${XDG_CONFIG_HOME:-${HOME}/.config}/arachne/deployment.env}

file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

if [[ -e "$arachne_deploy_env" || -L "$arachne_deploy_env" ]]; then
  if [[ ! -f "$arachne_deploy_env" || -L "$arachne_deploy_env" || \
        ! -O "$arachne_deploy_env" ]]; then
    printf 'Arachne: deployment environment is not an owner-controlled file: %s\n' \
      "$arachne_deploy_env" >&2
    exit 1
  fi
  arachne_deploy_mode=$(file_mode "$arachne_deploy_env")
  if (( (8#$arachne_deploy_mode & 077) != 0 )); then
    printf 'Arachne: deployment environment must deny group/other access: %s (%s)\n' \
      "$arachne_deploy_env" "$arachne_deploy_mode" >&2
    exit 1
  fi
  # shellcheck source=/dev/null
  source "$arachne_deploy_env"
else
  printf 'Arachne: configured deployment environment is missing: %s\n' \
    "$arachne_deploy_env" >&2
  exit 1
fi

arachne_state_root=${XDG_STATE_HOME:-${HOME}/.local/state}
arachne_runtime=${ARACHNE_RUNTIME_DIR:-${arachne_state_root}/arachne-runtime}
arachne_data=${ARACHNE_DATA_DIR:-${arachne_state_root}/arachne}
arachne_pages=${ARACHNE_PAGES_DIR:-${arachne_root}/pages}
arachne_port=${ARACHNE_PORT:-8788}
arachne_python_configured=${ARACHNE_PYTHON:-$(command -v python3)}
arachne_manage_tailscaled=${ARACHNE_MANAGE_TAILSCALED:-true}
arachne_tailscale=${TAILSCALE_BIN:-${HOME}/bin/tailscale}
arachne_tailscaled=${TAILSCALED_BIN:-${HOME}/bin/tailscaled}
arachne_ts_state=${TAILSCALE_STATE_DIR:-${HOME}/.tailscale}
arachne_ts_socket_explicit=${TAILSCALE_SOCKET+x}
arachne_ts_socket=${TAILSCALE_SOCKET:-${arachne_ts_state}/tailscaled.sock}
arachne_tls_dir=${ARACHNE_TLS_DIR:-${arachne_state_root}/arachne-tls}
arachne_system_bundle=${ARACHNE_SYSTEM_CA_BUNDLE:-}
arachne_openssl=${OPENSSL_BIN:-openssl}
arachne_ca_cert=${arachne_tls_dir}/ca-cert.pem
arachne_server_cert=${arachne_tls_dir}/server-cert.pem
arachne_server_key=${arachne_tls_dir}/server-key.pem
arachne_trust_bundle=${arachne_tls_dir}/trust-bundle.pem
arachne_quiesce_file=${ARACHNE_QUIESCE_FILE:-${arachne_runtime}/QUIESCED}

if [[ -e "$arachne_quiesce_file" || -L "$arachne_quiesce_file" ]]; then
  exit 0
fi

case ${arachne_manage_tailscaled,,} in
  true|1|yes|on) arachne_manage_tailscaled=true ;;
  false|0|no|off) arachne_manage_tailscaled=false ;;
  *)
    printf 'Arachne: ARACHNE_MANAGE_TAILSCALED must be true or false\n' >&2
    exit 2
    ;;
esac
if [[ ! "$arachne_port" =~ ^[0-9]+$ ]] || \
    (( arachne_port < 1 || arachne_port > 65535 )); then
  printf 'Arachne: ARACHNE_PORT must be between 1 and 65535\n' >&2
  exit 2
fi
if [[ ! -x "$arachne_python_configured" ]]; then
  printf 'Arachne: Python executable is unavailable: %s\n' \
    "$arachne_python_configured" >&2
  exit 1
fi
# Some shared hosts expose Python through an exec-wrapper.  Resolve the
# interpreter-reported executable before building the exact process identity;
# otherwise a healthy child can have a different argv[0] than the configured
# wrapper and the watchdog will reject its own process.
if ! arachne_python=$("$arachne_python_configured" -c \
    'import sys; print(sys.executable)' 2>/dev/null) || \
    [[ "$arachne_python" != /* || "$arachne_python" == *$'\n'* || \
       ! -x "$arachne_python" ]]; then
  printf 'Arachne: configured Python did not resolve to an executable: %s\n' \
    "$arachne_python_configured" >&2
  exit 1
fi
if [[ ! -x "$arachne_tailscale" ]]; then
  printf 'Arachne: tailscale executable is unavailable: %s\n' "$arachne_tailscale" >&2
  exit 1
fi
if [[ "$arachne_manage_tailscaled" == true && ! -x "$arachne_tailscaled" ]]; then
  printf 'Arachne: tailscaled executable is unavailable: %s\n' "$arachne_tailscaled" >&2
  exit 1
fi

mkdir -p "$arachne_runtime" "$arachne_data" "$arachne_pages"
chmod 700 "$arachne_runtime" "$arachne_data"
if [[ -e "$arachne_quiesce_file" || -L "$arachne_quiesce_file" ]]; then
  exit 0
fi
if [[ "$arachne_manage_tailscaled" == true ]]; then
  mkdir -p "$arachne_ts_state"
  chmod 700 "$arachne_ts_state"
fi

arachne_lock=${arachne_runtime}/keepalive.lock
arachne_lock_owned=0
cleanup_lock() {
  local arachne_owner=
  if (( arachne_lock_owned )) && [[ -r "${arachne_lock}/pid" ]]; then
    IFS= read -r arachne_owner <"${arachne_lock}/pid" || true
    if [[ "$arachne_owner" == "$$" ]]; then
      rm -f "${arachne_lock}/pid"
      rmdir "$arachne_lock" 2>/dev/null || true
    fi
  fi
}
trap cleanup_lock EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for _arachne_lock_attempt in 1 2; do
  if mkdir "$arachne_lock" 2>/dev/null; then
    printf '%s\n' "$$" >"${arachne_lock}/pid"
    arachne_lock_owned=1
    break
  fi
  arachne_lock_pid=
  if [[ -r "${arachne_lock}/pid" ]]; then
    IFS= read -r arachne_lock_pid <"${arachne_lock}/pid" || true
  fi
  if [[ "$arachne_lock_pid" =~ ^[0-9]+$ ]] && \
      kill -0 "$arachne_lock_pid" 2>/dev/null; then
    exit 0
  fi
  rm -f "${arachne_lock}/pid"
  rmdir "$arachne_lock" 2>/dev/null || true
done
if (( ! arachne_lock_owned )); then
  printf 'Arachne: could not acquire watchdog lock: %s\n' "$arachne_lock" >&2
  exit 1
fi
if [[ -e "$arachne_quiesce_file" || -L "$arachne_quiesce_file" ]]; then
  exit 0
fi

ARACHNE_DATA_DIR="$arachne_data" \
ARACHNE_TLS_DIR="$arachne_tls_dir" \
ARACHNE_SYSTEM_CA_BUNDLE="$arachne_system_bundle" \
OPENSSL_BIN="$arachne_openssl" \
  bash "${arachne_root}/bin/init-backend-tls.sh"
arachne_trust_digest=$("$arachne_openssl" dgst -sha256 "$arachne_trust_bundle" | \
  awk '{print $NF}')
if [[ ! "$arachne_trust_digest" =~ ^[A-Fa-f0-9]{64}$ ]]; then
  printf 'Arachne: could not fingerprint the TLS trust bundle\n' >&2
  exit 1
fi

process_matches() {
  local arachne_pid_file=$1 arachne_expected=$2 arachne_pid arachne_command
  [[ -r "$arachne_pid_file" ]] || return 1
  IFS= read -r arachne_pid <"$arachne_pid_file" || return 1
  [[ "$arachne_pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$arachne_pid" 2>/dev/null || return 1
  arachne_command=$(ps -p "$arachne_pid" -o args= 2>/dev/null) || return 1
  [[ "$arachne_command" == "$arachne_expected" ]]
}

find_exact_process() {
  local arachne_expected=$1 arachne_pid arachne_command arachne_found=
  while read -r arachne_pid arachne_command; do
    [[ "$arachne_pid" =~ ^[0-9]+$ ]] || continue
    if [[ "$arachne_command" == "$arachne_expected" ]]; then
      if [[ -n "$arachne_found" ]]; then
        return 2
      fi
      arachne_found=$arachne_pid
    fi
  done < <(ps -U "$(id -u)" -o pid= -o args= 2>/dev/null)
  [[ -n "$arachne_found" ]] || return 1
  printf '%s\n' "$arachne_found"
}

recover_exact_pid() {
  local arachne_pid_file=$1 arachne_expected=$2 arachne_label=$3 arachne_pid
  if process_matches "$arachne_pid_file" "$arachne_expected"; then
    return 0
  fi
  if arachne_pid=$(find_exact_process "$arachne_expected"); then
    printf '%s\n' "$arachne_pid" >"$arachne_pid_file"
    return 0
  elif [[ $? -eq 2 ]]; then
    printf 'Arachne: multiple exact %s processes found; refusing to guess\n' \
      "$arachne_label" >&2
    return 2
  fi
  return 1
}

stop_exact_process() {
  local arachne_pid_file=$1 arachne_expected=$2 arachne_label=$3 arachne_pid
  process_matches "$arachne_pid_file" "$arachne_expected" || return 0
  IFS= read -r arachne_pid <"$arachne_pid_file"
  kill -TERM "$arachne_pid" 2>/dev/null || true
  for _arachne_stop_attempt in {1..50}; do
    kill -0 "$arachne_pid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$arachne_pid" 2>/dev/null; then
    printf 'Arachne: %s PID %s did not stop within 5 seconds\n' \
      "$arachne_label" "$arachne_pid" >&2
    return 1
  fi
  rm -f "$arachne_pid_file"
}

server_health() {
  curl --silent --show-error --fail \
    --proto '=https' --tlsv1.2 \
    --cacert "$arachne_ca_cert" \
    --connect-timeout 2 --max-time 3 \
    "https://127.0.0.1:${arachne_port}/health" >/dev/null
}

arachne_ts_args=()
if [[ "$arachne_manage_tailscaled" == true || -n "$arachne_ts_socket_explicit" ]]; then
  arachne_ts_args+=("--socket=${arachne_ts_socket}")
fi

arachne_tailscale_ready=0
if [[ "$arachne_manage_tailscaled" == true ]]; then
  arachne_tailscaled_expected="${arachne_tailscaled} --tun=userspace-networking --statedir=${arachne_ts_state} --socket=${arachne_ts_socket} --port=0"
  arachne_tailscaled_pid_file=${arachne_runtime}/tailscaled.pid
  arachne_tailscaled_trust_file=${arachne_runtime}/tailscaled.trust
  arachne_tailscaled_trust_ok() {
    local arachne_trust_pid arachne_trust_path arachne_trust_hash arachne_live_pid
    [[ -r "$arachne_tailscaled_trust_file" ]] || return 1
    {
      IFS= read -r arachne_trust_pid
      IFS= read -r arachne_trust_path
      IFS= read -r arachne_trust_hash
    } <"$arachne_tailscaled_trust_file" || return 1
    IFS= read -r arachne_live_pid <"$arachne_tailscaled_pid_file" || return 1
    [[ "$arachne_trust_pid" == "$arachne_live_pid" && \
       "$arachne_trust_path" == "$arachne_trust_bundle" && \
       "$arachne_trust_hash" == "$arachne_trust_digest" ]]
  }

  if recover_exact_pid "${arachne_runtime}/tailscaled.pid" \
      "$arachne_tailscaled_expected" tailscaled; then
    for _arachne_attempt in {1..20}; do
      if "$arachne_tailscale" "${arachne_ts_args[@]}" status >/dev/null 2>&1; then
        arachne_tailscale_ready=1
        break
      fi
      sleep 0.1
    done
    if (( arachne_tailscale_ready )); then
      # Replace any persisted pre-TLS mapping before the backend is touched.
      # The old plaintext backend will fail this verified route closed until it
      # is restarted with its private localhost certificate.
      if ! "$arachne_tailscale" "${arachne_ts_args[@]}" \
          serve --bg "https://localhost:${arachne_port}" >/dev/null; then
        printf 'Arachne: could not replace the existing Serve mapping safely\n' >&2
        exit 1
      fi
    else
      # An unresponsive daemon cannot be proved safe; stop the exact process
      # before changing which process owns the backend port.
      stop_exact_process "$arachne_tailscaled_pid_file" \
        "$arachne_tailscaled_expected" tailscaled
      rm -f "$arachne_tailscaled_trust_file"
    fi
    if ! arachne_tailscaled_trust_ok; then
      stop_exact_process "$arachne_tailscaled_pid_file" \
        "$arachne_tailscaled_expected" tailscaled
      rm -f "$arachne_tailscaled_trust_file"
      arachne_tailscale_ready=0
    fi
  else
    arachne_recover_status=$?
    (( arachne_recover_status == 1 )) || exit "$arachne_recover_status"
  fi
else
  for _arachne_attempt in {1..100}; do
    if "$arachne_tailscale" "${arachne_ts_args[@]}" status >/dev/null 2>&1; then
      arachne_tailscale_ready=1
      break
    fi
    sleep 0.1
  done
  if (( ! arachne_tailscale_ready )); then
    printf 'Arachne: system tailscaled is not ready and enrolled\n' >&2
    exit 1
  fi
  if ! "$arachne_tailscale" "${arachne_ts_args[@]}" \
      serve --bg "https://localhost:${arachne_port}" >/dev/null; then
    printf 'Arachne: could not establish the verified Serve mapping\n' >&2
    exit 1
  fi
fi

# A verified mapping is now installed on every already-running daemon.  If the
# managed daemon was absent or stopped, establish the authenticated TLS backend
# before starting it so a persisted legacy mapping never points at a free port.
arachne_server_expected="${arachne_python} ${arachne_root}/server.py"
arachne_server_pid_file=${arachne_runtime}/server.pid
arachne_server_ready=0
if recover_exact_pid "$arachne_server_pid_file" "$arachne_server_expected" server; then
  if server_health; then
    arachne_server_ready=1
  else
    stop_exact_process "$arachne_server_pid_file" "$arachne_server_expected" server
  fi
else
  arachne_recover_status=$?
  (( arachne_recover_status == 1 )) || exit "$arachne_recover_status"
fi

if (( ! arachne_server_ready )); then
  nohup env \
    ARACHNE_DATA_DIR="$arachne_data" \
    ARACHNE_PAGES_DIR="$arachne_pages" \
    ARACHNE_PORT="$arachne_port" \
    ARACHNE_TLS_CERT_FILE="$arachne_server_cert" \
    ARACHNE_TLS_KEY_FILE="$arachne_server_key" \
    "$arachne_python" "${arachne_root}/server.py" \
    >>"${arachne_runtime}/server.log" 2>&1 </dev/null &
  arachne_server_pid=$!
  printf '%s\n' "$arachne_server_pid" >"$arachne_server_pid_file"
  for _arachne_attempt in {1..50}; do
    if process_matches "$arachne_server_pid_file" "$arachne_server_expected" && \
        server_health; then
      arachne_server_ready=1
      break
    fi
    kill -0 "$arachne_server_pid" 2>/dev/null || break
    sleep 0.1
  done
fi
if (( ! arachne_server_ready )) || \
    ! process_matches "$arachne_server_pid_file" "$arachne_server_expected" || \
    ! server_health; then
  printf 'Arachne: exact server process failed its verified HTTPS health check\n' >&2
  exit 1
fi

if [[ "$arachne_manage_tailscaled" == true ]] && \
    ! process_matches "$arachne_tailscaled_pid_file" \
      "$arachne_tailscaled_expected"; then
  nohup env SSL_CERT_FILE="$arachne_trust_bundle" \
    "$arachne_tailscaled" \
    --tun=userspace-networking \
    --statedir="$arachne_ts_state" \
    --socket="$arachne_ts_socket" \
    --port=0 \
    >>"${arachne_runtime}/tailscaled.log" 2>&1 </dev/null &
  arachne_tailscaled_pid=$!
  printf '%s\n' "$arachne_tailscaled_pid" >"$arachne_tailscaled_pid_file"
  printf '%s\n%s\n%s\n' "$arachne_tailscaled_pid" "$arachne_trust_bundle" \
    "$arachne_trust_digest" \
    >"$arachne_tailscaled_trust_file"
  arachne_tailscale_ready=0
fi

if (( ! arachne_tailscale_ready )); then
  for _arachne_attempt in {1..100}; do
    if "$arachne_tailscale" "${arachne_ts_args[@]}" status >/dev/null 2>&1; then
      arachne_tailscale_ready=1
      break
    fi
    sleep 0.1
  done
fi
if (( ! arachne_tailscale_ready )); then
  printf 'Arachne: tailscaled did not become ready and enrolled\n' >&2
  exit 1
fi
if [[ "$arachne_manage_tailscaled" == true ]] && \
    { ! process_matches "$arachne_tailscaled_pid_file" \
        "$arachne_tailscaled_expected" || ! arachne_tailscaled_trust_ok; }; then
  printf 'Arachne: managed tailscaled identity or trust configuration is invalid\n' >&2
  exit 1
fi

if ! "$arachne_tailscale" "${arachne_ts_args[@]}" \
    serve --bg "https://localhost:${arachne_port}" >/dev/null; then
  printf 'Arachne: tailscale serve HTTPS-backend configuration failed\n' >&2
  exit 1
fi
