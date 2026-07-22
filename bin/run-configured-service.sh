#!/usr/bin/env bash
# Launch one Arachne service from the same owner-only environment on any host.

set -euo pipefail
umask 077

: "${HOME:?HOME must be set}"
arachne_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
arachne_deploy_env=${ARACHNE_DEPLOY_ENV:-${XDG_CONFIG_HOME:-${HOME}/.config}/arachne/deployment.env}

file_mode() {
  stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

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

set -a
# shellcheck source=/dev/null
source "$arachne_deploy_env"
set +a

case ${1:-} in
  server)
    arachne_python=${ARACHNE_PYTHON:-$(command -v python3)}
    exec "$arachne_python" "$arachne_root/server.py"
    ;;
  mcp)
    arachne_mcp_python=${ARACHNE_MCP_PYTHON:-${arachne_root}/.venv/bin/python}
    exec "$arachne_mcp_python" "$arachne_root/mcp_server.py"
    ;;
  share)
    arachne_python=${ARACHNE_PYTHON:-$(command -v python3)}
    exec "$arachne_python" "$arachne_root/share_server.py"
    ;;
  *)
    printf 'usage: %s {server|mcp|share}\n' "$0" >&2
    exit 2
    ;;
esac
