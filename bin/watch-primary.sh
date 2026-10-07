#!/usr/bin/env bash
# One primary health check, with durable transition-only alerts.
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

exec "${ARACHNE_PYTHON:-${arachne_root}/.venv/bin/python}" - <<'PY'
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit


def interrupted(signum, frame):
    raise RuntimeError("interrupted")


for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, interrupted)


def acquire(lock):
    """Take the directory lock, reclaiming one left by a killed run."""
    for _ in range(2):
        try:
            lock.mkdir(mode=0o700)
            return True
        except FileExistsError:
            pass
        try:
            owner = int((lock / "pid").read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            owner = None
        if owner is None:
            # A holder writes its PID immediately; only an old PID-less lock
            # is abandoned rather than mid-acquisition.
            try:
                if time.time() - lock.stat().st_mtime < 60:
                    return False
            except FileNotFoundError:
                continue
        else:
            try:
                os.kill(owner, 0)
                return False
            except PermissionError:
                return False
            except ProcessLookupError:
                pass
        shutil.rmtree(lock, ignore_errors=True)
    return False


def check_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or any(ord(char) < 32 for char in url)):
        raise ValueError("health URLs must use HTTPS without credentials")


def healthy(url):
    try:
        result = subprocess.run(
            ["curl", "--silent", "--proto", "=https", "--connect-timeout", "15",
             "--max-time", "15", "--write-out", "\n%{http_code}", "--url", url],
            capture_output=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return False
    if result.returncode:
        return False
    try:
        body, status = result.stdout.rsplit(b"\n", 1)
        document = json.loads(body)
        return status == b"200" and isinstance(document, dict) and document.get("ok") is True
    except (ValueError, UnicodeError):
        return False


def alert(title, message):
    command = os.environ.get("ARACHNE_ALERT_COMMAND")
    if command:
        arguments = [command, title, message]
    else:
        # Pass data as argv, never interpolate URL/message text into AppleScript.
        arguments = ["osascript", "-e", "on run argv\n"
                     "display notification (item 2 of argv) with title (item 1 of argv)\n"
                     "end run", title, message]
    subprocess.run(arguments, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=30)


def watch():
    primary = os.environ["ARACHNE_PRIMARY_HEALTH_URL"]
    control = os.environ.get("ARACHNE_WATCH_CONTROL_URL")
    check_url(primary)
    if control:
        check_url(control)
    threshold_text = os.environ.get("ARACHNE_WATCH_FAILURES", "2")
    if not re.fullmatch(r"[0-9]+", threshold_text) or int(threshold_text) < 1:
        raise ValueError("invalid failure threshold")
    threshold = int(threshold_text)
    runtime = Path(os.environ["ARACHNE_RUNTIME_DIR"])
    if not runtime.is_absolute() or runtime.is_symlink():
        raise ValueError("runtime directory must be absolute and not a symlink")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    if runtime.stat().st_uid != os.getuid():
        raise ValueError("runtime directory must be owner-controlled")
    runtime.chmod(0o700)
    lock = runtime / "watch-primary.lock"
    if not acquire(lock):
        print("Arachne watch: skipped (locked)")
        return
    temporary = lock / "state.json"
    try:
        (lock / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
        state_file = runtime / "watch-primary.json"
        state = {"failures": 0, "down": False}
        if os.path.lexists(state_file):
            if (state_file.is_symlink() or not state_file.is_file()
                    or state_file.stat().st_uid != os.getuid()
                    or state_file.stat().st_mode & 0o077):
                raise ValueError("state file must be owner-only")
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if (type(state["failures"]) is not int or state["failures"] < 0
                    or type(state["down"]) is not bool):
                raise ValueError("invalid state")
        was_down = state["down"]
        if healthy(primary):
            state = {"failures": 0, "down": False}
        elif control and not healthy(control):
            print("Arachne watch: local network unavailable")
            return
        else:
            state["failures"] = min(state["failures"] + 1, threshold)
            state["down"] = was_down or state["failures"] >= threshold

        # Persist the transition before delivery: subsequent invocations never
        # repeat an alert, even if the notification command fails after delivery.
        temporary.write_text(json.dumps(state) + "\n", encoding="utf-8")
        os.replace(temporary, state_file)
        transition = was_down != state["down"]
        status = "DOWN" if state["down"] else "RECOVERED" if transition else "healthy" if not state["failures"] else "pending"
        if transition:
            message = f"Primary {primary} is {status}."
            # On the standby host its own public URL is the standby inbox;
            # ARACHNE_STANDBY_URL there would be the server's offline-link setting.
            standby = (os.environ.get("ARACHNE_WATCH_STANDBY_URL")
                       or os.environ.get("ARACHNE_PUBLIC_URL"))
            if standby:
                message += f" Standby inbox: {standby}"
            alert(f"Arachne {status}", message)
        print(f"Arachne watch: {status} (failures={state['failures']})")
    finally:
        temporary.unlink(missing_ok=True)
        (lock / "pid").unlink(missing_ok=True)
        lock.rmdir()


try:
    watch()
except Exception:
    print("Arachne watch: failed (check configuration, state, and alert command)", file=sys.stderr)
    sys.exit(1)
PY
