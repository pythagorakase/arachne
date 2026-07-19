#!/usr/bin/env bash
# headersHelper for the Arachne MCP server: emit the Authorization header as a
# JSON object on stdout. Runs fresh on every connect; must never log the token.
#
# Token resolution order:
#   1. ARACHNE_TOKEN_FILE, if set in the environment Claude Code launched with
#   2. ARACHNE_TOKEN_FILE from ~/.config/arachne/env (or $ARACHNE_CONFIG)
#   3. the standard state path ~/.local/state/arachne/auth-token
set -euo pipefail

config="${ARACHNE_CONFIG:-$HOME/.config/arachne/env}"
if [[ -z "${ARACHNE_TOKEN_FILE:-}" && -r "$config" ]]; then
    # shellcheck disable=SC1090
    source "$config"
fi
token_file="${ARACHNE_TOKEN_FILE:-$HOME/.local/state/arachne/auth-token}"

if [[ ! -r "$token_file" ]]; then
    echo "arachne auth-headers: token file not readable: $token_file" >&2
    echo "copy it once from the server: scp <host>:~/.local/state/arachne/auth-token $token_file && chmod 600 $token_file" >&2
    exit 1
fi

token="$(tr -d '[:space:]' < "$token_file")"
if [[ -z "$token" ]]; then
    echo "arachne auth-headers: token file is empty: $token_file" >&2
    exit 1
fi

# Arachne tokens are URL-safe base64 ([A-Za-z0-9_-]); no JSON escaping needed.
printf '{"Authorization":"Bearer %s"}\n' "$token"
