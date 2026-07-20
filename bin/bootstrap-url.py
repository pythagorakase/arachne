#!/usr/bin/env python3
"""Create or open a one-time Arachne browser bootstrap URL."""

from __future__ import annotations

import argparse
import os
import re
import webbrowser
from pathlib import Path
from urllib.parse import quote


PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html\Z")
AUTH_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,256}\Z")


def main() -> int:
    state_root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "page",
        nargs="?",
        default=None,
        help="published decision page name (omit to land on the inbox)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ARACHNE_PUBLIC_URL"),
        help="public tailnet URL (default: ARACHNE_PUBLIC_URL)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(
            os.environ.get("ARACHNE_TOKEN_FILE", state_root / "arachne/auth-token")
        ),
        help="owner-only token file",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="open the bootstrap URL without printing its secret",
    )
    arguments = parser.parse_args()

    page = None
    if arguments.page is not None:
        page = Path(arguments.page).name
        if not PAGE_NAME.fullmatch(page):
            parser.error(f"page name is not allowlisted: {page}")
    try:
        token = arguments.token_file.expanduser().read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        parser.error(f"cannot read token file {arguments.token_file}: {exc}")
    if not AUTH_TOKEN.fullmatch(token):
        parser.error("token file does not contain a valid Arachne token")

    if not arguments.base_url:
        parser.error("set ARACHNE_PUBLIC_URL or provide --base-url")
    base_url = arguments.base_url.rstrip("/")
    destination = "" if page is None else f"?next={quote(page, safe='')}"
    url = f"{base_url}/bootstrap{destination}#token={quote(token, safe='')}"
    if arguments.open:
        if not webbrowser.open(url):
            parser.error("the system browser did not accept the bootstrap URL")
        landing = "the inbox" if page is None else page
        print(f"Opened an authenticated Arachne session for {landing}")
    else:
        print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
