#!/usr/bin/env bash
# headersHelper for the Arachne MCP server: emit the Authorization header as a
# JSON object on stdout. Runs fresh on every connect; must never log the token.
#
# Token resolution order (mirrors bin/arm-wake.sh and the server's Settings):
#   1. ARACHNE_TOKEN_FILE from the launch environment
#   2. ARACHNE_TOKEN_FILE from ~/.config/arachne/env (or $ARACHNE_CONFIG)
#   3. ${XDG_STATE_HOME:-$HOME/.local/state}/arachne/auth-token
#
# The sourced config and the token file must both satisfy the owner-only
# contract enforced elsewhere in the repo (regular file, not a symlink, owned
# by the current user, no group/other access): the config because it is
# executed, the token because it is a credential. The token must match the
# server's grammar, which also guarantees the emitted JSON needs no escaping.
set -euo pipefail

fail() {
    echo "arachne auth-headers: $*" >&2
    exit 1
}

owner_only() {
    local path="$1" uid mode
    [[ -f "$path" && ! -L "$path" ]] || return 1
    if uid=$(stat -c '%u' "$path" 2>/dev/null); then
        mode=$(stat -c '%a' "$path" 2>/dev/null) || return 1
    else
        uid=$(stat -f '%u' "$path" 2>/dev/null) || return 1
        mode=$(stat -f '%Lp' "$path" 2>/dev/null) || return 1
    fi
    [[ "$uid" == "$(id -u)" ]] || return 1
    (( (8#$mode & 8#077) == 0 )) || return 1
}

config="${ARACHNE_CONFIG:-$HOME/.config/arachne/env}"
if [[ -z "${ARACHNE_TOKEN_FILE:-}" && -e "$config" ]]; then
    owner_only "$config" || fail "config must be an owner-only regular file before it can be sourced: $config"
    # shellcheck disable=SC1090
    source "$config"
fi

state_root="${XDG_STATE_HOME:-$HOME/.local/state}"
token_file="${ARACHNE_TOKEN_FILE:-$state_root/arachne/auth-token}"

owner_only "$token_file" || fail "token must be an owner-only regular file: $token_file
copy it once from the server: scp <host>:~/.local/state/arachne/auth-token \"$token_file\" && chmod 600 \"$token_file\""

# Length is checked arithmetically because macOS ERE caps interval bounds at
# RE_DUP_MAX (255), so a {32,256} quantifier fails regcomp there.
token="$(tr -d '[:space:]' < "$token_file")"
if (( ${#token} < 32 || ${#token} > 256 )) || [[ ! "$token" =~ ^[A-Za-z0-9_-]+$ ]]; then
    fail "token file does not contain a valid Arachne token: $token_file"
fi

printf '{"Authorization":"Bearer %s"}\n' "$token"
