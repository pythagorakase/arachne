#!/usr/bin/env python3
"""Publish decision HTML with Arachne's same-origin page contract enforced."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path


PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html\Z")
LOOPBACK_RULING = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost)(?::[0-9]+)?/ruling",
    re.IGNORECASE,
)
LOOPBACK_LABEL = re.compile(r"(?:127\.0\.0\.1|localhost):8788", re.IGNORECASE)


def publish(source: Path, pages_dir: Path) -> Path:
    if not source.is_file():
        raise ValueError(f"source page does not exist: {source}")
    if not PAGE_NAME.fullmatch(source.name):
        raise ValueError(f"page name is not allowlisted: {source.name}")
    try:
        html = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"page is not UTF-8: {source}") from exc
    html, endpoint_replacements = LOOPBACK_RULING.subn("/ruling", html)
    html, label_replacements = LOOPBACK_LABEL.subn("same-origin /ruling", html)
    if LOOPBACK_RULING.search(html):
        raise ValueError(f"page still contains a loopback ruling endpoint: {source}")
    if "/ruling" not in html:
        raise ValueError(f"page does not submit to the relative /ruling endpoint: {source}")
    if "localStorage" not in html:
        raise ValueError(f"page does not persist in-progress state to localStorage: {source}")

    pages_dir.mkdir(parents=True, exist_ok=True)
    destination = pages_dir / source.name
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.", suffix=".tmp", dir=pages_dir
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(html)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
    replacements = endpoint_replacements + label_replacements
    action = "rewrote loopback references and published" if replacements else "published"
    print(f"{action}: {source} -> {destination}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="+", type=Path, help="decision HTML page(s)")
    parser.add_argument(
        "--pages-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "ARACHNE_PAGES_DIR", Path(__file__).resolve().parents[1] / "pages"
            )
        ),
        help="publish directory (default: ARACHNE_PAGES_DIR or repo/pages)",
    )
    arguments = parser.parse_args()
    try:
        for source in arguments.source:
            publish(source.resolve(), arguments.pages_dir.expanduser().resolve())
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Arachne publish failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
