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
# Sidecar publication metadata lives under a dot-directory the page allowlist
# can never serve: PAGE_NAME requires a leading alphanumeric and the router
# rejects multi-segment paths.
METADATA_DIRECTORY = ".meta"

# Page and sidecar are two files, so a publication is two commits. This lock
# serializes whole publications within a process (the server is the only
# production writer), so concurrent same-name publishes cannot interleave one
# publication's page with another's metadata.
_PUBLISH_LOCK = threading.Lock()


@dataclass(frozen=True)
class Publication:
    """The safe, committed result of publishing one decision page."""

    name: str
    destination: Path
    issue: str


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


def _inferred_issue(name: str) -> str:
    """Infer the filing token used by conventional decision page names."""

    stem = name[: -len(".html")]
    if stem.startswith("decision_"):
        token = stem.removeprefix("decision_").split("_", 1)[0]
        if token:
            inferred = normalize_issue(token)
            assert inferred is not None
            return inferred
    inferred = normalize_issue(stem)
    assert inferred is not None
    return inferred


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


def prepare_html(name: str, html: str) -> str:
    """Enforce the v2 brief-owned capture page contract."""

    if PAGE_NAME.fullmatch(name) is None:
        raise ValueError(f"page name is not allowlisted: {name}")
    if "/ruling" in html:
        raise ValueError(
            f"page references the forbidden /ruling capture endpoint under v2: {name}"
        )
    if "localStorage" in html:
        raise ValueError(
            f"page references forbidden localStorage capture state under v2: {name}"
        )
    return html


def publish_html(
    name: str,
    html: str,
    pages_dir: Path,
    issue: object = None,
) -> Publication:
    """Validate and atomically publish trusted UTF-8 HTML.

    An explicit issue token is recorded when supplied; otherwise the token is
    inferred from the conventional ``decision_<issue>_*.html`` page name.

    Publications are serialized per process, and the commit order — remove the
    old sidecar (durably), install the page, record the new sidecar — keeps
    every interruption window in the "no recorded metadata" state, so a crash
    can never pair a page with another publication's issue. A page left without
    a sidecar simply falls back to filename inference until it is republished.
    """

    normalized = prepare_html(name, html)
    issue_token = normalize_issue(issue) or _inferred_issue(name)
    with _PUBLISH_LOCK:
        pages_dir.mkdir(parents=True, exist_ok=True)
        destination = pages_dir / name
        sidecar = metadata_path(pages_dir, name)
        sidecar.unlink(missing_ok=True)
        # Durably order the stale-sidecar removal ahead of the new page: fsync
        # the metadata directory now, so a crash after the page is installed
        # but before the new sidecar is written recovers the new page with NO
        # sidecar, never previous metadata paired with the new page.
        meta_dir = sidecar.parent
        if meta_dir.is_dir():
            directory = os.open(meta_dir, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
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
            directory = os.open(sidecar.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    return Publication(
        name=name,
        destination=destination,
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
