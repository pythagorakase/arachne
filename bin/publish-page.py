#!/usr/bin/env python3
"""Publish decision HTML with Arachne's same-origin page contract enforced."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from page_contract import publish_file  # noqa: E402


def publish(source: Path, pages_dir: Path, issue: str | None) -> Path:
    publication = publish_file(source, pages_dir, issue=issue)
    action = (
        "rewrote loopback references and published"
        if publication.replacements
        else "published"
    )
    recorded = f" (issue {publication.issue})" if publication.issue else ""
    print(f"{action}: {source} -> {publication.destination}{recorded}")
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
    parser.add_argument(
        "--issue",
        default=None,
        help="issue token the page files; recorded so the inbox can pair the ruling",
    )
    arguments = parser.parse_args()
    if arguments.issue is not None and len(arguments.source) > 1:
        parser.error("--issue applies to exactly one source page")
    try:
        for source in arguments.source:
            publish(
                source.resolve(),
                arguments.pages_dir.expanduser().resolve(),
                arguments.issue,
            )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Arachne publish failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
