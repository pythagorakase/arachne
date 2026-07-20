"""Shared validation and atomic publication for trusted decision pages."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html\Z")
# Sidecar publication metadata (currently the page's issue token) lives under
# a dot-directory the page allowlist can never serve: PAGE_NAME requires a
# leading alphanumeric and the router rejects multi-segment paths.
METADATA_DIRECTORY = ".meta"

# Page and sidecar are two files, so a publication is two commits. This lock
# serializes whole publications within a process (the server is the only
# production writer), so concurrent same-name publishes cannot interleave one
# publication's page with another's issue.
_PUBLISH_LOCK = threading.Lock()
LOOPBACK_RULING = re.compile(
    r"https?://(?:127\.0\.0\.1|localhost)(?::[0-9]+)?/ruling",
    re.IGNORECASE,
)
LOOPBACK_LABEL = re.compile(r"(?:127\.0\.0\.1|localhost):8788", re.IGNORECASE)


@dataclass(frozen=True)
class Publication:
    """The safe, committed result of publishing one decision page."""

    name: str
    destination: Path
    endpoint_replacements: int
    label_replacements: int
    issue: str | None = None

    @property
    def replacements(self) -> int:
        return self.endpoint_replacements + self.label_replacements


def normalize_issue(issue: object) -> str | None:
    """Return the canonical issue token, or None when absent.

    Mirrors the ruling endpoint's rule: a non-empty string or integer,
    stringified and stripped, at most 200 characters.
    """

    if issue is None:
        return None
    if isinstance(issue, bool) or not isinstance(issue, (str, int)):
        raise ValueError("'issue' must be a string or integer issue token")
    text = str(issue).strip()
    if not text or len(text) > 200:
        raise ValueError("'issue' must be 1-200 characters once stripped")
    return text


def metadata_path(pages_dir: Path, name: str) -> Path:
    return pages_dir / METADATA_DIRECTORY / f"{name}.json"


def read_page_issue(pages_dir: Path, name: str) -> str | None:
    """Return the issue token recorded at publication, else None."""

    try:
        payload = json.loads(
            metadata_path(pages_dir, name).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    issue = payload.get("issue") if isinstance(payload, dict) else None
    return issue if isinstance(issue, str) and issue else None


def prepare_html(name: str, html: str) -> tuple[str, int, int]:
    """Normalize legacy loopback references and enforce the page contract."""

    if PAGE_NAME.fullmatch(name) is None:
        raise ValueError(f"page name is not allowlisted: {name}")
    normalized, endpoint_replacements = LOOPBACK_RULING.subn("/ruling", html)
    normalized, label_replacements = LOOPBACK_LABEL.subn(
        "same-origin /ruling", normalized
    )
    if LOOPBACK_RULING.search(normalized):
        raise ValueError(f"page still contains a loopback ruling endpoint: {name}")
    if "/ruling" not in normalized:
        raise ValueError(
            f"page does not submit to the relative /ruling endpoint: {name}"
        )
    if "localStorage" not in normalized:
        raise ValueError(
            f"page does not persist in-progress state to localStorage: {name}"
        )
    return normalized, endpoint_replacements, label_replacements


def publish_html(
    name: str, html: str, pages_dir: Path, issue: object = None
) -> Publication:
    """Validate and atomically publish trusted UTF-8 HTML.

    When *issue* is provided it is recorded as sidecar metadata so the inbox
    can pair the page with its ruling regardless of the filename; when it is
    absent any stale recorded issue from a prior publication is removed.

    Publications are serialized per process, and the commit order — remove
    the old sidecar, install the page, record the new sidecar — keeps every
    interruption window in the "no recorded issue" state, which degrades to
    filename inference instead of pairing a page with another publication's
    issue.
    """

    normalized, endpoint_replacements, label_replacements = prepare_html(name, html)
    issue_token = normalize_issue(issue)
    with _PUBLISH_LOCK:
        pages_dir.mkdir(parents=True, exist_ok=True)
        destination = pages_dir / name
        sidecar = metadata_path(pages_dir, name)
        sidecar.unlink(missing_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{name}.", suffix=".tmp", dir=pages_dir
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(normalized)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, destination)
            directory = os.open(pages_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
        if issue_token is not None:
            sidecar.parent.mkdir(mode=0o700, exist_ok=True)
            body = json.dumps({"issue": issue_token}, ensure_ascii=False) + "\n"
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{name}.", suffix=".tmp", dir=sidecar.parent
            )
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, sidecar)
            finally:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass
    return Publication(
        name=name,
        destination=destination,
        endpoint_replacements=endpoint_replacements,
        label_replacements=label_replacements,
        issue=issue_token,
    )


def publish_file(
    source: Path, pages_dir: Path, issue: object = None
) -> Publication:
    """Read one source page as UTF-8 and publish it through the shared contract."""

    if not source.is_file():
        raise ValueError(f"source page does not exist: {source}")
    try:
        html = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"page is not UTF-8: {source}") from exc
    return publish_html(source.name, html, pages_dir, issue=issue)
