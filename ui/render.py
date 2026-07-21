"""Render Arachne's browser surfaces from the assets beside this module."""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# The inbox is server-rendered, then its application-owned inline script drives
# iframe selection, brief-mediated capture, draft persistence, and filing.
INBOX_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline'; "
    "style-src 'unsafe-inline'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)
BOOTSTRAP_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
)

_ASSET_DIR = Path(__file__).resolve().parent
_UI_MARKER = re.compile(r"@@ARACHNE_[A-Z0-9_]+@@")
_PAGE_TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_PAGE_TITLE_SCAN_BYTES = 16_384


def _load_asset(name: str) -> str:
    try:
        return (_ASSET_DIR / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"could not load Arachne UI asset {name!r}") from exc


_INBOX_TEMPLATE = _load_asset("inbox.html")
_INBOX_CONTENT_TEMPLATE = _load_asset("inbox-content.html")
_BRIEF_TEMPLATE = _load_asset("brief.html")
_EMPTY_TEMPLATE = _load_asset("empty.html")
_LOCKED_TEMPLATE = _load_asset("locked.html")
_BOOTSTRAP_TEMPLATE = _load_asset("bootstrap.html")
_INBOX_STYLE = _load_asset("inbox.css").strip()
_INBOX_SCRIPT = _load_asset("inbox.js").strip()
_FONT_ASSETS = frozenset(
    {
        "Megrim.ttf",
        "Cinzel-wght.ttf",
        "Spectral-Regular.ttf",
        "Spectral-SemiBold.ttf",
    }
)


def _fill_template(
    name: str, template: str, replacements: Mapping[str, str]
) -> str:
    """Fill a UI template while failing loudly if its slot contract drifts."""

    counts = Counter(_UI_MARKER.findall(template))
    expected = set(replacements)
    if set(counts) != expected or any(count != 1 for count in counts.values()):
        found = ", ".join(
            f"{marker}={count}" for marker, count in sorted(counts.items())
        )
        wanted = ", ".join(sorted(expected))
        raise RuntimeError(
            f"Arachne UI template {name!r} has invalid slots "
            f"(expected {wanted}; found {found or 'none'})"
        )
    # Substitute from the original template in one pass. A page title that
    # happens to resemble another slot must remain ordinary display text.
    return _UI_MARKER.sub(lambda match: replacements[match.group(0)], template)


def page_title(path: Path) -> str | None:
    """Read a compact display title from the beginning of a decision page."""

    try:
        with path.open("rb") as stream:
            head = stream.read(_PAGE_TITLE_SCAN_BYTES)
    except OSError:
        return None
    match = _PAGE_TITLE.search(head)
    if match is None:
        return None
    text = html.unescape(match.group(1).decode("utf-8", "replace"))
    collapsed = " ".join(text.split())
    return collapsed[:120] or None


def fallback_title(name: str, issue: str) -> str:
    """Make a readable inbox title when a decision page has no title tag."""

    stem = name[: -len(".html")]
    remainder = stem.removeprefix("decision_").removeprefix(issue).strip("_-")
    prettified = remainder.replace("_", " ").replace("-", " ").strip()
    return prettified or stem


def _format_moment(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def _render_brief(entry: Mapping[str, Any], *, ruled: bool) -> str:
    if ruled:
        timestamp = f"ruled {_format_moment(entry['ruled_at'])}"
        status = "archived"
        ruling_sequence = str(entry["ruling_sequence"])
        ruling_suffix = (
            '<span class="brief-ruling-suffix">ruling '
            f"{html.escape(ruling_sequence)}</span>"
        )
    else:
        timestamp = f"published {_format_moment(entry['published_at'])}"
        status = "awaiting"
        ruling_sequence = ""
        ruling_suffix = ""
    return _fill_template(
        "brief.html",
        _BRIEF_TEMPLATE,
        {
            "@@ARACHNE_BRIEF_NAME@@": html.escape(entry["name"], quote=True),
            "@@ARACHNE_BRIEF_ISSUE_ATTR@@": html.escape(
                entry["issue"], quote=True
            ),
            "@@ARACHNE_BRIEF_TITLE_ATTR@@": html.escape(
                entry["title"], quote=True
            ),
            "@@ARACHNE_BRIEF_STATUS@@": status,
            "@@ARACHNE_BRIEF_RULING_SEQUENCE@@": html.escape(
                ruling_sequence, quote=True
            ),
            "@@ARACHNE_BRIEF_ISSUE@@": html.escape(entry["issue"]),
            "@@ARACHNE_BRIEF_TITLE@@": html.escape(entry["title"]),
            "@@ARACHNE_BRIEF_TIMESTAMP@@": html.escape(timestamp),
            "@@ARACHNE_BRIEF_RULING_SUFFIX@@": ruling_suffix,
        },
    ).strip()


def _render_empty(message: str) -> str:
    return _fill_template(
        "empty.html",
        _EMPTY_TEMPLATE,
        {"@@ARACHNE_EMPTY_MESSAGE@@": html.escape(message)},
    ).strip()


def _inbox_document(main_html: str, script: str = "") -> bytes:
    document = _fill_template(
        "inbox.html",
        _INBOX_TEMPLATE,
        {
            "@@ARACHNE_INBOX_STYLE@@": _INBOX_STYLE,
            "@@ARACHNE_INBOX_MAIN@@": main_html,
            "@@ARACHNE_INBOX_SCRIPT@@": script,
        },
    )
    return document.encode("utf-8")


def render_inbox(
    pending: Sequence[Mapping[str, Any]],
    archived: Sequence[Mapping[str, Any]],
) -> bytes:
    """Render the authenticated decision inbox."""

    pending_items = "\n".join(
        _render_brief(entry, ruled=False) for entry in pending
    ) or _render_empty("The loom is quiet — no briefs await your ruling.")
    archived_items = "\n".join(
        _render_brief(entry, ruled=True) for entry in archived
    ) or _render_empty("Nothing has been ruled yet.")
    main = _fill_template(
        "inbox-content.html",
        _INBOX_CONTENT_TEMPLATE,
        {
            "@@ARACHNE_PENDING_COUNT@@": str(len(pending)),
            "@@ARACHNE_PENDING_ITEMS@@": pending_items,
            "@@ARACHNE_ARCHIVED_COUNT@@": str(len(archived)),
            "@@ARACHNE_ARCHIVED_ITEMS@@": archived_items,
        },
    ).strip()
    return _inbox_document(main, _INBOX_SCRIPT)


def render_locked_inbox() -> bytes:
    """Render the non-disclosing shell shown without a live session."""

    return _inbox_document(_LOCKED_TEMPLATE.strip())


def font_asset(name: str) -> bytes | None:
    """Return one allowlisted bundled display font, or None for unknown names."""

    if name not in _FONT_ASSETS:
        return None
    path = _ASSET_DIR / "fonts" / name
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Arachne UI font asset is missing or unsafe: {name!r}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"could not load Arachne UI font asset {name!r}") from exc


def render_bootstrap(binding: str, destination: str) -> bytes:
    """Render the fragment-consuming browser session bootstrap page."""

    document = _fill_template(
        "bootstrap.html",
        _BOOTSTRAP_TEMPLATE,
        {
            "@@ARACHNE_BOOTSTRAP_BINDING@@": json.dumps(binding),
            "@@ARACHNE_BOOTSTRAP_DESTINATION@@": json.dumps(destination),
        },
    )
    return document.encode("utf-8")
