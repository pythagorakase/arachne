"""Shared validation and atomic publication for trusted decision pages."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.html\Z")
# Sidecar publication metadata (the issue token and axis manifest) lives under
# a dot-directory the page allowlist can never serve: PAGE_NAME requires a
# leading alphanumeric and the router rejects multi-segment paths.
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


def metadata_path(pages_dir: Path, name: str) -> Path:
    return pages_dir / METADATA_DIRECTORY / f"{name}.json"


def _keys_message(keys: set[object]) -> str:
    return ", ".join(sorted(repr(key) for key in keys)) or "none"


def _validate_keys(
    value: dict[object, object],
    path: str,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing:
        raise ValueError(
            f"{path} is missing required field(s): {_keys_message(missing)}"
        )
    if extra:
        raise ValueError(
            f"{path} contains unsupported field(s): {_keys_message(extra)}"
        )


def _nonempty_string(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value.strip()


def validate_axes_manifest(manifest: object) -> dict[str, Any]:
    """Validate and normalize one v2 axis manifest.

    The returned object contains only the frozen schema's fields, has canonical
    whitespace around identifiers and display strings, and always includes the
    optional ``overall_notes`` field. It is safe for callers to persist directly.
    """

    if not isinstance(manifest, dict):
        raise ValueError("axes manifest must be a JSON object")
    _validate_keys(
        manifest,
        "axes manifest",
        required={"contract", "issue", "title", "axes"},
        optional={"repo", "overall_notes"},
    )
    if manifest["contract"] != "v2":
        raise ValueError("axes manifest 'contract' must equal 'v2'")
    if not isinstance(manifest["issue"], str):
        raise ValueError("axes manifest 'issue' must be a string")
    issue = normalize_issue(manifest["issue"])
    assert issue is not None
    title = _nonempty_string(manifest["title"], "axes manifest 'title'")
    has_repo = "repo" in manifest
    repo = manifest.get("repo")
    if has_repo and not isinstance(repo, str):
        raise ValueError("axes manifest 'repo' must be a string when present")
    overall_notes = manifest.get("overall_notes", False)
    if not isinstance(overall_notes, bool):
        raise ValueError("axes manifest 'overall_notes' must be a boolean")
    axes_value = manifest["axes"]
    if not isinstance(axes_value, list) or not axes_value:
        raise ValueError("axes manifest 'axes' must be a non-empty array")

    normalized_axes: list[dict[str, Any]] = []
    axis_ids: set[str] = set()
    for axis_index, axis in enumerate(axes_value):
        axis_path = f"axes manifest 'axes[{axis_index}]'"
        if not isinstance(axis, dict):
            raise ValueError(f"{axis_path} must be an object")
        _validate_keys(
            axis,
            axis_path,
            required={"id", "label", "select", "notes", "options"},
        )
        axis_id = _nonempty_string(axis["id"], f"{axis_path}.id")
        if axis_id in axis_ids:
            raise ValueError(f"axes manifest contains duplicate axis id {axis_id!r}")
        axis_ids.add(axis_id)
        label = _nonempty_string(axis["label"], f"{axis_path}.label")
        if axis["select"] != "one":
            raise ValueError(f"{axis_path}.select must equal 'one'")
        if not isinstance(axis["notes"], bool):
            raise ValueError(f"{axis_path}.notes must be a boolean")
        options_value = axis["options"]
        if not isinstance(options_value, list) or not options_value:
            raise ValueError(f"{axis_path}.options must be a non-empty array")

        normalized_options: list[dict[str, str]] = []
        option_ids: set[str] = set()
        for option_index, option in enumerate(options_value):
            option_path = f"{axis_path}.options[{option_index}]"
            if not isinstance(option, dict):
                raise ValueError(f"{option_path} must be an object")
            _validate_keys(
                option,
                option_path,
                required={"id", "label"},
            )
            option_id = _nonempty_string(option["id"], f"{option_path}.id")
            if option_id in option_ids:
                raise ValueError(
                    f"{axis_path} contains duplicate option id {option_id!r}"
                )
            option_ids.add(option_id)
            normalized_options.append(
                {
                    "id": option_id,
                    "label": _nonempty_string(
                        option["label"], f"{option_path}.label"
                    ),
                }
            )
        normalized_axes.append(
            {
                "id": axis_id,
                "label": label,
                "select": "one",
                "notes": axis["notes"],
                "options": normalized_options,
            }
        )

    normalized: dict[str, Any] = {
        "contract": "v2",
        "issue": issue,
        "title": title,
    }
    if has_repo:
        assert isinstance(repo, str)
        normalized["repo"] = repo.strip()
    normalized["overall_notes"] = overall_notes
    normalized["axes"] = normalized_axes
    return normalized


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


def read_page_axes(pages_dir: Path, name: str) -> dict[str, Any] | None:
    """Return the axis manifest recorded at publication, else None."""

    try:
        payload = json.loads(
            metadata_path(pages_dir, name).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    axes = payload.get("axes") if isinstance(payload, dict) else None
    return axes if isinstance(axes, dict) else None


def prepare_html(
    name: str, html: str, axes: object
) -> tuple[str, dict[str, Any]]:
    """Enforce the v2 argument-only page and axis-manifest contract."""

    if PAGE_NAME.fullmatch(name) is None:
        raise ValueError(f"page name is not allowlisted: {name}")
    normalized_axes = validate_axes_manifest(axes)
    if "/ruling" in html:
        raise ValueError(
            f"page references the forbidden /ruling capture endpoint under v2: {name}"
        )
    if "localStorage" in html:
        raise ValueError(
            f"page references forbidden localStorage capture state under v2: {name}"
        )
    return html, normalized_axes


def publish_html(
    name: str,
    html: str,
    pages_dir: Path,
    axes: object,
    issue: object = None,
) -> Publication:
    """Validate and atomically publish trusted UTF-8 HTML.

    The required axis manifest supplies the authoritative issue token. When an
    explicit *issue* is also provided, it must agree with that token.

    Publications are serialized per process, and the commit order — remove
    the old sidecar, install the page, record the new sidecar — keeps every
    interruption window in the "no recorded issue" state, which degrades to
    filename inference instead of pairing a page with another publication's
    issue.
    """

    normalized, normalized_axes = prepare_html(name, html, axes)
    issue_token = normalized_axes["issue"]
    explicit_issue = normalize_issue(issue)
    if explicit_issue is not None and explicit_issue != issue_token:
        raise ValueError(
            f"explicit issue {explicit_issue!r} disagrees with axes manifest "
            f"issue {issue_token!r}"
        )
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
        sidecar.parent.mkdir(mode=0o700, exist_ok=True)
        body = json.dumps(
            {"issue": issue_token, "axes": normalized_axes}, ensure_ascii=False
        ) + "\n"
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
    source: Path, pages_dir: Path, axes: object, issue: object = None
) -> Publication:
    """Read one source page as UTF-8 and publish it through the shared contract."""

    if not source.is_file():
        raise ValueError(f"source page does not exist: {source}")
    try:
        html = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"page is not UTF-8: {source}") from exc
    return publish_html(source.name, html, pages_dir, axes, issue=issue)
