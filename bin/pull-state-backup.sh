#!/usr/bin/env bash
# Pull a private, verified off-host snapshot; launchd supplies the schedule.
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

exec "${ARACHNE_PYTHON:-${arachne_root}/.venv/bin/python}" - "$arachne_root" <<'PY'
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, sys.argv[1])
from server import RulingStore


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


def complete(path):
    return (
        re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", path.name)
        and not path.is_symlink() and path.is_dir()
        and (path / "MANIFEST.json").is_file()
        and not (path / "MANIFEST.json").is_symlink()
        and all((path / name).is_dir() and not (path / name).is_symlink()
                for name in ("state", "pages"))
    )


def backup():
    sources = {name: os.environ[f"ARACHNE_BACKUP_{name.upper()}_SRC"]
               for name in ("state", "pages")}
    if any(not value or value.startswith("-") for value in sources.values()):
        raise ValueError("invalid source")
    directory = Path(os.environ["ARACHNE_BACKUP_DIR"])
    keep_text = os.environ.get("ARACHNE_BACKUP_KEEP", "30")
    if not re.fullmatch(r"[0-9]+", keep_text) or int(keep_text) < 1:
        raise ValueError("invalid retention")
    keep = int(keep_text)
    if not directory.is_absolute() or directory.is_symlink():
        raise ValueError("backup directory must be absolute and not a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid():
        raise ValueError("backup directory must be owner-controlled")
    directory.chmod(0o700)
    latest = directory / "latest"
    if os.path.lexists(latest) and not latest.is_symlink():
        raise ValueError("latest must be a symlink")
    lock = directory / ".backup.lock"
    if not acquire(lock):
        print("Arachne backup: skipped (locked)")
        return

    partial = None
    published = None
    latest_temp = lock / "latest"
    try:
        (lock / "pid").write_text(f"{os.getpid()}\n", encoding="ascii")
        snapshots = sorted(path for path in directory.iterdir() if complete(path))
        previous = snapshots[-1] if snapshots else None
        # A manual second run in the same second must not overwrite a snapshot.
        while True:
            now = datetime.now(timezone.utc)
            name = now.strftime("%Y%m%dT%H%M%SZ")
            destination = directory / name
            candidate = directory / f".{name}.partial"
            if not os.path.lexists(destination) and not os.path.lexists(candidate):
                break
            time.sleep(1)
        candidate.mkdir(mode=0o700)
        partial = candidate
        for component, source in sources.items():
            arguments = ["rsync", "-a", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=15"]
            if previous:
                arguments += ["--link-dest", str(previous / component)]
            # A trailing slash always means the source directory's contents.
            arguments += ["--", source.rstrip("/") + "/", str(partial / component) + "/"]
            subprocess.run(arguments, check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)

        store = RulingStore(partial / "state")
        manifest = {
            "latest_sequence": store.latest_sequence,
            "ruling_count": store.count,
            "file_count": sum(len(files) for _, _, files in os.walk(partial)),
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "sources": sources,
        }
        (partial / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        partial.chmod(0o700)
        partial.rename(destination)
        partial = None
        published = destination
        latest_temp.symlink_to(name)
        # os.replace replaces a symlink itself, unlike platform-specific mv behavior.
        os.replace(latest_temp, latest)
        published = None
        snapshots.append(destination)
        for obsolete in sorted(snapshots)[:-keep]:
            # Only recognized complete snapshots are eligible, never other entries.
            shutil.rmtree(obsolete)
        print(f"Arachne backup: {name} complete (sequence={store.latest_sequence}, rulings={store.count})")
    finally:
        if partial is not None:
            shutil.rmtree(partial)
        if published is not None:
            shutil.rmtree(published)
        latest_temp.unlink(missing_ok=True)
        (lock / "pid").unlink(missing_ok=True)
        lock.rmdir()


try:
    backup()
except Exception:
    # Never echo subprocess output or persisted ruling contents (which may be private).
    print("Arachne backup: failed (check configuration, sources, and ruling integrity)", file=sys.stderr)
    sys.exit(1)
PY
