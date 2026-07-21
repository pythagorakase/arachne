#!/usr/bin/env python3
"""Publish decision HTML briefs with Arachne's v2 page contract enforced."""

from __future__ import annotations

import argparse
import os
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from page_contract import normalize_issue, publish_html  # noqa: E402


class BriefIssueParser(HTMLParser):
    """Collect data-issue declarations from the document root or body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.declarations: list[tuple[str, str | None]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag not in {"html", "body"}:
            return
        for name, value in attrs:
            if name == "data-issue":
                self.declarations.append((tag, value))


def brief_issue(html: str, source: Path) -> str:
    parser = BriefIssueParser()
    parser.feed(html)
    parser.close()
    if not parser.declarations:
        raise ValueError(
            f"brief has no data-issue on <html> or <body>: {source}"
        )
    issues = {
        normalize_issue(value)
        for _tag, value in parser.declarations
    }
    if None in issues:
        raise ValueError(
            f"brief has an empty data-issue on <html> or <body>: {source}"
        )
    if len(issues) != 1:
        raise ValueError(
            f"brief has conflicting data-issue values on <html>/<body>: {source}"
        )
    return next(iter(issues))


def publish(
    source: Path,
    pages_dir: Path,
    issue: str | None,
) -> Path:
    if not source.is_file():
        raise ValueError(f"source page does not exist: {source}")
    try:
        html = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"page is not UTF-8: {source}") from exc
    publication = publish_html(
        source.name,
        html,
        pages_dir,
        issue=issue if issue is not None else brief_issue(html, source),
    )
    print(
        f"published: {source} -> {publication.destination} "
        f"(issue {publication.issue})"
    )
    return publication.destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source", nargs="+", type=Path, help="one or more decision HTML pages"
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
        help="single-source issue override (default: read the brief's data-issue)",
    )
    arguments = parser.parse_args()
    if arguments.issue is not None and len(arguments.source) != 1:
        parser.error("--issue is valid only with a single source")
    try:
        pages_dir = arguments.pages_dir.expanduser().resolve()
        for source in arguments.source:
            publish(source.resolve(), pages_dir, arguments.issue)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Arachne publish failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
