#!/usr/bin/env python3
"""Publish decision HTML with Arachne's same-origin page contract enforced."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from page_contract import publish_file  # noqa: E402


def publish(source: Path, pages_dir: Path) -> Path:
    publication = publish_file(source, pages_dir)
    action = (
        "rewrote loopback references and published"
        if publication.replacements
        else "published"
    )
    print(f"{action}: {source} -> {publication.destination}")
    return publication.destination


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
