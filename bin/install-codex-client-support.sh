#!/bin/sh
set -eu

# Install the user-level Arachne skill and a secret-free LaunchAgent that
# exports the MCP bearer token at login. MCP endpoint registration remains an
# explicit Codex configuration step because it is deployment-specific.

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
skill_source="$repo_root/plugin/skills/arachne"
skill_root=${CODEX_SKILLS_ROOT:-"$HOME/.agents/skills"}
skill_link="$skill_root/arachne"
agents_dir=${ARACHNE_LAUNCH_AGENTS_DIR:-"$HOME/Library/LaunchAgents"}
label=com.pythagorakase.arachne.codex-env
plist="$agents_dir/$label.plist"
template="$repo_root/deploy/macos/$label.plist.in"
launchctl_bin=${ARACHNE_LAUNCHCTL:-/bin/launchctl}

/bin/mkdir -p "$skill_root" "$agents_dir"
if [ -L "$skill_link" ]; then
  current=$(/usr/bin/readlink "$skill_link")
  if [ "$current" != "$skill_source" ]; then
    echo "Refusing to replace existing Arachne skill link: $skill_link" >&2
    exit 1
  fi
elif [ -e "$skill_link" ]; then
  echo "Refusing to replace existing Arachne skill path: $skill_link" >&2
  exit 1
else
  /bin/ln -s "$skill_source" "$skill_link"
fi

escaped_root=$(printf '%s' "$repo_root" | /usr/bin/sed 's/[|&]/\\&/g')
temporary=$(/usr/bin/mktemp "$plist.tmp.XXXXXX")
trap '/bin/rm -f "$temporary"' EXIT HUP INT TERM
/usr/bin/sed "s|@@ARACHNE_ROOT@@|$escaped_root|g" "$template" > "$temporary"
/bin/chmod 600 "$temporary"
/bin/mv "$temporary" "$plist"
trap - EXIT HUP INT TERM

if [ "${ARACHNE_INSTALL_NO_BOOTSTRAP:-0}" != "1" ]; then
  domain="gui/$(/usr/bin/id -u)"
  "$launchctl_bin" bootout "$domain/$label" >/dev/null 2>&1 || true
  "$launchctl_bin" bootstrap "$domain" "$plist"
fi

echo "Arachne Codex skill: $skill_link"
echo "Arachne Codex token LaunchAgent: $plist"
echo "Restart Codex once so the GUI process inherits ARACHNE_MCP_TOKEN."

