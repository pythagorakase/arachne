#!/usr/bin/env python3
"""Publish decision HTML with Arachne's same-origin page contract enforced."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from page_contract import publish_file  # noqa: E402


def publish(
    source: Path,
    pages_dir: Path,
    axes_path: Path,
    issue: str | None,
) -> Path:
    try:
        axes = json.loads(axes_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"axis manifest is not valid JSON: {axes_path}: {exc}") from exc
    publication = publish_file(source, pages_dir, axes, issue=issue)
    print(
        f"published: {source} -> {publication.destination} "
        f"(issue {publication.issue})"
    )
    return publication.destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="+", type=Path, help="decision HTML page(s)")
    parser.add_argument(
        "--axes",
        action="append",
        required=True,
        type=Path,
        metavar="MANIFEST",
        help=(
            "v2 axis-manifest JSON file; repeat once per source in source order"
        ),
    )
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
        help="optional consistency check against the manifest's issue token",
    )
    arguments = parser.parse_args()
    if len(arguments.axes) != len(arguments.source):
        parser.error("provide exactly one --axes manifest per source page")
    if arguments.issue is not None and len(arguments.source) > 1:
        parser.error("--issue applies to exactly one source page")
    try:
        for source, axes_path in zip(arguments.source, arguments.axes, strict=True):
            publish(
                source.resolve(),
                arguments.pages_dir.expanduser().resolve(),
                axes_path.expanduser().resolve(),
                arguments.issue,
            )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Arachne publish failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
