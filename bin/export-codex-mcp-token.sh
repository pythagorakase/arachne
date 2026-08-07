#!/bin/sh
set -eu

# Make the owner-only Arachne token available to GUI apps launched by the
# current macOS user session. The secret stays out of launchd plists and repo
# configuration; Codex references only the ARACHNE_MCP_TOKEN variable name.

state_home=${XDG_STATE_HOME:-"$HOME/.local/state"}
token_file=${ARACHNE_TOKEN_FILE:-"$state_home/arachne/auth-token"}
launchctl_bin=${ARACHNE_LAUNCHCTL:-/bin/launchctl}

if [ ! -f "$token_file" ] || [ -L "$token_file" ]; then
  echo "Arachne token must be an owner-only regular file: $token_file" >&2
  exit 1
fi

owner=$(/usr/bin/stat -f '%u' "$token_file")
mode=$(/usr/bin/stat -f '%Lp' "$token_file")
if [ "$owner" != "$(/usr/bin/id -u)" ]; then
  echo "Arachne token is not owned by the current user: $token_file" >&2
  exit 1
fi
case "$mode" in
  ?00) ;;
  *)
    echo "Arachne token must not grant group or other access: $token_file" >&2
    exit 1
    ;;
esac

token=$(/usr/bin/tr -d '\r\n' < "$token_file")
length=${#token}
case "$token" in
  ''|*[!A-Za-z0-9_-]*)
    echo "Arachne token has invalid syntax" >&2
    exit 1
    ;;
esac
if [ "$length" -lt 32 ] || [ "$length" -gt 256 ]; then
  echo "Arachne token has invalid length" >&2
  exit 1
fi

"$launchctl_bin" setenv ARACHNE_MCP_TOKEN "$token"
unset token

