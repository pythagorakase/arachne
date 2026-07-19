"""Shared validation and atomic publication for trusted decision pages."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html\Z")
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

    @property
    def replacements(self) -> int:
        return self.endpoint_replacements + self.label_replacements


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


def publish_html(name: str, html: str, pages_dir: Path) -> Publication:
    """Validate and atomically publish trusted UTF-8 HTML."""

    normalized, endpoint_replacements, label_replacements = prepare_html(name, html)
    pages_dir.mkdir(parents=True, exist_ok=True)
    destination = pages_dir / name
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
    return Publication(
        name=name,
        destination=destination,
        endpoint_replacements=endpoint_replacements,
        label_replacements=label_replacements,
    )


def publish_file(source: Path, pages_dir: Path) -> Publication:
    """Read one source page as UTF-8 and publish it through the shared contract."""

    if not source.is_file():
        raise ValueError(f"source page does not exist: {source}")
    try:
        html = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"page is not UTF-8: {source}") from exc
    return publish_html(source.name, html, pages_dir)
