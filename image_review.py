"""Build self-contained, blinded image-comparison briefs for Arachne.

The production server remains deliberately unaware of image files.  This
module runs on a trusted producer machine, converts candidates into compact
metadata-free previews, embeds those previews in one HTML document, and emits
a private provenance sidecar that maps opaque ballot values back to sources.
"""

from __future__ import annotations

import base64
import hashlib
import html
import itertools
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from page_contract import normalize_issue, prepare_html


SCHEMA_VERSION = 1
PROVENANCE_SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_IMAGE_MIME_TYPES = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
_TEMPLATE_PATH = Path(__file__).with_name("templates") / "image-comparison.html"
_BRIEF_AGENT_PATH = Path(__file__).with_name("ui") / "brief-agent.js"


class ImageReviewError(ValueError):
    """The review manifest or one of its candidate assets is invalid."""


@dataclass(frozen=True)
class Candidate:
    source_id: str
    path: Path


@dataclass(frozen=True)
class Pose:
    pose_id: str
    label: str
    note: str
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class ReviewManifest:
    issue: str
    title: str
    instructions: str
    subject_id: str
    subject_label: str
    poses: tuple[Pose, ...]
    source: Path


@dataclass(frozen=True)
class Preview:
    mime_type: str
    data_uri: str
    source_sha256: str
    preview_sha256: str
    preview_bytes: int


@dataclass(frozen=True)
class BuildResult:
    page: Path
    provenance: Path
    issue: str
    seed: str
    page_bytes: int


def _plain_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImageReviewError(f"{label} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise ImageReviewError(f"{label} must use string keys")
    return value


def _exact_keys(
    value: dict[str, Any],
    label: str,
    required: set[str],
    optional: set[str] = frozenset(),
) -> None:
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required - optional)
    if missing:
        raise ImageReviewError(f"{label} is missing: {', '.join(missing)}")
    if extra:
        raise ImageReviewError(f"{label} has unknown keys: {', '.join(extra)}")


def _text(value: object, label: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise ImageReviewError(f"{label} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit:
        raise ImageReviewError(f"{label} must be 1-{limit} characters")
    return normalized


def _identifier(value: object, label: str) -> str:
    text = _text(value, label, limit=80)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ImageReviewError(
            f"{label} must start with an alphanumeric character and use only "
            "letters, numbers, dots, underscores, or hyphens"
        )
    return text


def _candidate_path(value: object, label: str, source: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ImageReviewError(f"{label} must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = source.parent / path
    path = path.resolve()
    if not path.is_file():
        raise ImageReviewError(f"{label} is not a regular file: {path}")
    if path.suffix.lower() not in _IMAGE_MIME_TYPES:
        supported = ", ".join(sorted(_IMAGE_MIME_TYPES))
        raise ImageReviewError(f"{label} must be one of: {supported}")
    return path


def load_manifest(source: Path) -> ReviewManifest:
    """Load and strictly validate one character-level review manifest."""

    source = source.expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImageReviewError(f"manifest does not exist: {source}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageReviewError(f"manifest is not valid UTF-8 JSON: {source}") from exc

    root = _plain_object(payload, "manifest")
    _exact_keys(
        root,
        "manifest",
        {"schema_version", "issue", "title", "subject", "poses"},
        {"instructions"},
    )
    if root["schema_version"] != SCHEMA_VERSION:
        raise ImageReviewError(
            f"schema_version must be {SCHEMA_VERSION}, got {root['schema_version']!r}"
        )
    try:
        issue = normalize_issue(root["issue"])
    except ValueError as exc:
        raise ImageReviewError(str(exc)) from exc
    assert issue is not None
    title = _text(root["title"], "title", limit=160)
    instructions = _text(
        root.get(
            "instructions",
            "Choose the image that best preserves the character. "
            "Add a comment only when it helps explain the choice.",
        ),
        "instructions",
        limit=600,
    )

    subject = _plain_object(root["subject"], "subject")
    _exact_keys(subject, "subject", {"id", "label"})
    subject_id = _identifier(subject["id"], "subject.id")
    subject_label = _text(subject["label"], "subject.label", limit=100)

    raw_poses = root["poses"]
    if not isinstance(raw_poses, list) or not raw_poses:
        raise ImageReviewError("poses must be a non-empty JSON array")
    poses: list[Pose] = []
    seen_pose_ids: set[str] = set()
    for pose_index, raw_pose in enumerate(raw_poses, 1):
        pose_label = f"poses[{pose_index - 1}]"
        pose_value = _plain_object(raw_pose, pose_label)
        _exact_keys(
            pose_value,
            pose_label,
            {"id", "label", "candidates"},
            {"note"},
        )
        pose_id = _identifier(pose_value["id"], f"{pose_label}.id")
        if pose_id in seen_pose_ids:
            raise ImageReviewError(f"duplicate pose id: {pose_id}")
        seen_pose_ids.add(pose_id)
        label = _text(pose_value["label"], f"{pose_label}.label", limit=140)
        note = ""
        if "note" in pose_value and pose_value["note"] not in {None, ""}:
            note = _text(pose_value["note"], f"{pose_label}.note", limit=400)

        raw_candidates = pose_value["candidates"]
        if not isinstance(raw_candidates, list) or len(raw_candidates) not in {2, 3}:
            raise ImageReviewError(
                f"{pose_label}.candidates must contain exactly 2 or 3 images"
            )
        candidates: list[Candidate] = []
        seen_ids: set[str] = set()
        seen_paths: set[Path] = set()
        for candidate_index, raw_candidate in enumerate(raw_candidates):
            candidate_label = f"{pose_label}.candidates[{candidate_index}]"
            candidate_value = _plain_object(raw_candidate, candidate_label)
            _exact_keys(candidate_value, candidate_label, {"id", "path"})
            source_id = _identifier(
                candidate_value["id"], f"{candidate_label}.id"
            )
            if source_id in seen_ids:
                raise ImageReviewError(
                    f"duplicate candidate id in pose {pose_id}: {source_id}"
                )
            seen_ids.add(source_id)
            path = _candidate_path(
                candidate_value["path"], f"{candidate_label}.path", source
            )
            if path in seen_paths:
                raise ImageReviewError(
                    f"duplicate candidate path in pose {pose_id}: {path}"
                )
            seen_paths.add(path)
            candidates.append(Candidate(source_id=source_id, path=path))
        poses.append(
            Pose(
                pose_id=pose_id,
                label=label,
                note=note,
                candidates=tuple(candidates),
            )
        )

    return ReviewManifest(
        issue=issue,
        title=title,
        instructions=instructions,
        subject_id=subject_id,
        subject_label=subject_label,
        poses=tuple(poses),
        source=source,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _original_preview(path: Path, *, maximum_bytes: int) -> Preview:
    body = path.read_bytes()
    if len(body) > maximum_bytes:
        raise ImageReviewError(
            f"original preview exceeds {maximum_bytes} bytes; use ffmpeg: {path}"
        )
    mime_type = _IMAGE_MIME_TYPES[path.suffix.lower()]
    digest = hashlib.sha256(body).hexdigest()
    return Preview(
        mime_type=mime_type,
        data_uri=f"data:{mime_type};base64,{base64.b64encode(body).decode('ascii')}",
        source_sha256=digest,
        preview_sha256=digest,
        preview_bytes=len(body),
    )


def _ffmpeg_preview(
    path: Path,
    *,
    ffmpeg: str,
    maximum_edge: int,
    quality: int,
    maximum_bytes: int,
) -> Preview:
    source_sha256 = _sha256_file(path)
    with tempfile.TemporaryDirectory(prefix="arachne-image-review-") as temporary:
        output = Path(temporary) / "preview.jpg"
        # FFmpeg's MJPEG encoder is part of the baseline build, while optional
        # WebP encoders are absent from some otherwise capable installations.
        # qscale is inverse (2 is excellent, 31 is poor); retain a familiar
        # 1-100 producer-facing quality control and map it conservatively.
        jpeg_qscale = max(2, min(31, round((101 - quality) / 8) + 1))
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map_metadata",
            "-1",
            "-frames:v",
            "1",
            "-vf",
            f"scale={maximum_edge}:{maximum_edge}:force_original_aspect_ratio=decrease",
            "-c:v",
            "mjpeg",
            "-q:v",
            str(jpeg_qscale),
            "-y",
            str(output),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ImageReviewError(f"preview conversion failed for {path}: {exc}") from exc
        if completed.returncode != 0 or not output.is_file():
            detail = (completed.stderr or completed.stdout or "unknown ffmpeg error").strip()
            raise ImageReviewError(
                f"preview conversion failed for {path}: {detail[-1200:]}"
            )
        body = output.read_bytes()
    if not body.startswith(b"\xff\xd8") or not body.endswith(b"\xff\xd9"):
        raise ImageReviewError(f"ffmpeg did not produce a JPEG preview: {path}")
    if len(body) > maximum_bytes:
        raise ImageReviewError(
            f"compressed preview exceeds {maximum_bytes} bytes: {path}"
        )
    preview_sha256 = hashlib.sha256(body).hexdigest()
    return Preview(
        mime_type="image/jpeg",
        data_uri=f"data:image/jpeg;base64,{base64.b64encode(body).decode('ascii')}",
        source_sha256=source_sha256,
        preview_sha256=preview_sha256,
        preview_bytes=len(body),
    )


def _opaque_id(issue: str, pose_id: str, candidate: Candidate, digest: str) -> str:
    material = "\0".join((issue, pose_id, candidate.source_id, digest)).encode()
    return "candidate-" + hashlib.sha256(material).hexdigest()[:16]


def _json_for_script(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _fill_template(template: str, replacements: dict[str, str]) -> str:
    rendered = template
    for marker, value in replacements.items():
        count = rendered.count(marker)
        if count != 1:
            raise RuntimeError(f"template marker {marker} occurs {count} times")
        rendered = rendered.replace(marker, value)
    leftovers = sorted(set(re.findall(r"@@ARACHNE_[A-Z0-9_]+@@", rendered)))
    if leftovers:
        raise RuntimeError(f"unfilled template markers: {', '.join(leftovers)}")
    return rendered


def _option_markup(
    *,
    letter: str,
    opaque_id: str,
    field: str,
    subject: str,
    pose: str,
    matchup_number: int,
    matchup_count: int,
) -> str:
    escaped_letter = html.escape(letter)
    escaped_id = html.escape(opaque_id, quote=True)
    escaped_field = html.escape(field, quote=True)
    alt = html.escape(
        f"Option {letter} for {subject}, {pose}, comparison {matchup_number} of {matchup_count}",
        quote=True,
    )
    return f"""
          <article class="option-shell">
            <label class="option-card">
              <input class="choice-input" type="radio" name="{escaped_field}"
                value="{escaped_id}" aria-label="Choose option {escaped_letter}">
              <span class="option-letter" aria-hidden="true">{escaped_letter}</span>
              <span class="image-frame">
                <img data-review-image="{escaped_id}" alt="{alt}"
                  loading="lazy" decoding="async">
              </span>
              <span class="choose-label">Choose {escaped_letter}</span>
            </label>
            <button class="zoom-button" type="button" data-review-zoom="{escaped_id}"
              data-review-zoom-alt="{alt}">Inspect {escaped_letter}</button>
          </article>"""


def _sections_markup(
    manifest: ReviewManifest,
    rendered_poses: list[dict[str, Any]],
) -> str:
    sections: list[str] = []
    for pose_index, pose in enumerate(rendered_poses, 1):
        pose_id = html.escape(pose["id"], quote=True)
        label_text = html.escape(pose["label"])
        label_attribute = html.escape(pose["label"], quote=True)
        note = (
            f'<p class="pose-note">{html.escape(pose["note"])}</p>'
            if pose["note"]
            else ""
        )
        matchups: list[str] = []
        count = len(pose["matchups"])
        for matchup_index, matchup in enumerate(pose["matchups"], 1):
            left = _option_markup(
                letter="A",
                opaque_id=matchup["left"],
                field=matchup["field"],
                subject=manifest.subject_label,
                pose=pose["label"],
                matchup_number=matchup_index,
                matchup_count=count,
            )
            right = _option_markup(
                letter="B",
                opaque_id=matchup["right"],
                field=matchup["field"],
                subject=manifest.subject_label,
                pose=pose["label"],
                matchup_number=matchup_index,
                matchup_count=count,
            )
            matchups.append(
                f"""
      <fieldset class="matchup" data-review-matchup>
        <legend>Comparison {matchup_index} of {count}</legend>
        <div class="pair">{left}{right}
        </div>
      </fieldset>"""
            )
        comment_id = html.escape(f"comment-{pose['id']}", quote=True)
        comment_field = html.escape(pose["comment_field"], quote=True)
        sections.append(
            f"""
  <section class="pose" data-decision="{pose_id}" data-label="{label_attribute}">
    <header class="pose-header">
      <p class="eyebrow">Pose {pose_index} of {len(rendered_poses)}</p>
      <h2>{label_text}</h2>
      {note}
    </header>
    {''.join(matchups)}
    <label class="comment-label" for="{comment_id}">Optional comment</label>
    <textarea id="{comment_id}" name="{comment_field}" rows="3"
      placeholder="What made one version stronger, or what should change next?"></textarea>
  </section>"""
        )
    return "\n".join(sections)


def render_review(
    manifest: ReviewManifest,
    *,
    output_name: str,
    seed: str,
    ffmpeg: str | None,
    maximum_edge: int = 960,
    quality: int = 78,
    maximum_preview_bytes: int = 2_000_000,
) -> tuple[str, dict[str, Any]]:
    """Render one character-level page and its private provenance record."""

    if not 320 <= maximum_edge <= 2400:
        raise ImageReviewError("maximum_edge must be between 320 and 2400")
    if not 1 <= quality <= 100:
        raise ImageReviewError("quality must be between 1 and 100")
    if maximum_preview_bytes < 64_000:
        raise ImageReviewError("maximum_preview_bytes must be at least 64000")
    if ffmpeg is not None:
        resolved_ffmpeg = shutil.which(ffmpeg) if os.sep not in ffmpeg else ffmpeg
        if not resolved_ffmpeg or not Path(resolved_ffmpeg).is_file():
            raise ImageReviewError(f"ffmpeg executable not found: {ffmpeg}")
        ffmpeg = str(Path(resolved_ffmpeg).resolve())

    rng_digest = hashlib.sha256(seed.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(rng_digest, "big"))
    preview_cache: dict[Path, Preview] = {}
    assets: dict[str, str] = {}
    rendered_poses: list[dict[str, Any]] = []
    provenance_poses: list[dict[str, Any]] = []
    all_opaque_ids: set[str] = set()

    for pose in manifest.poses:
        candidates: list[dict[str, Any]] = []
        seen_source_digests: set[str] = set()
        for candidate in pose.candidates:
            preview = preview_cache.get(candidate.path)
            if preview is None:
                preview = (
                    _ffmpeg_preview(
                        candidate.path,
                        ffmpeg=ffmpeg,
                        maximum_edge=maximum_edge,
                        quality=quality,
                        maximum_bytes=maximum_preview_bytes,
                    )
                    if ffmpeg is not None
                    else _original_preview(
                        candidate.path, maximum_bytes=maximum_preview_bytes
                    )
                )
                preview_cache[candidate.path] = preview
            if preview.source_sha256 in seen_source_digests:
                raise ImageReviewError(
                    f"pose {pose.pose_id} contains byte-identical candidate images"
                )
            seen_source_digests.add(preview.source_sha256)
            opaque_id = _opaque_id(
                manifest.issue, pose.pose_id, candidate, preview.source_sha256
            )
            if opaque_id in all_opaque_ids:
                raise ImageReviewError(f"opaque candidate id collision: {opaque_id}")
            all_opaque_ids.add(opaque_id)
            assets[opaque_id] = preview.data_uri
            candidates.append(
                {
                    "opaque_id": opaque_id,
                    "source_id": candidate.source_id,
                    "source_path": str(candidate.path),
                    "source_sha256": preview.source_sha256,
                    "preview_sha256": preview.preview_sha256,
                    "preview_bytes": preview.preview_bytes,
                    "preview_mime_type": preview.mime_type,
                }
            )

        pairs = list(itertools.combinations(candidates, 2))
        rng.shuffle(pairs)
        matchups: list[dict[str, str]] = []
        for matchup_index, (first, second) in enumerate(pairs, 1):
            sides = [first["opaque_id"], second["opaque_id"]]
            rng.shuffle(sides)
            matchups.append(
                {
                    "field": f"vote__{pose.pose_id}__{matchup_index}",
                    "left": sides[0],
                    "right": sides[1],
                }
            )
        comment_field = f"comment__{pose.pose_id}"
        rendered_pose = {
            "id": pose.pose_id,
            "label": pose.label,
            "note": pose.note,
            "comment_field": comment_field,
            "matchups": matchups,
        }
        rendered_poses.append(rendered_pose)
        provenance_poses.append(
            {
                **rendered_pose,
                "candidates": candidates,
            }
        )

    review_model = {
        "title": manifest.title,
        "subject": manifest.subject_label,
        "poses": rendered_poses,
    }
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    canonical_agent = _BRIEF_AGENT_PATH.read_text(encoding="utf-8").strip()
    rendered = _fill_template(
        template,
        {
            "@@ARACHNE_DOCUMENT_TITLE@@": html.escape(manifest.title),
            "@@ARACHNE_HEADING@@": html.escape(manifest.title),
            "@@ARACHNE_ISSUE@@": html.escape(manifest.issue, quote=True),
            "@@ARACHNE_SUBJECT@@": html.escape(manifest.subject_label),
            "@@ARACHNE_INSTRUCTIONS@@": html.escape(manifest.instructions),
            "@@ARACHNE_SECTIONS@@": _sections_markup(manifest, rendered_poses),
            "@@ARACHNE_ASSETS_JSON@@": _json_for_script(assets),
            "@@ARACHNE_REVIEW_JSON@@": _json_for_script(review_model),
            "@@ARACHNE_BRIEF_AGENT@@": canonical_agent,
        },
    )
    rendered = prepare_html(output_name, rendered)
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "issue": manifest.issue,
        "title": manifest.title,
        "subject": {"id": manifest.subject_id, "label": manifest.subject_label},
        "source_manifest": str(manifest.source),
        "randomization_seed": seed,
        "preview": {
            "encoder": ffmpeg or "original-bytes",
            "maximum_edge": maximum_edge if ffmpeg else None,
            "quality": quality if ffmpeg else None,
            "maximum_bytes": maximum_preview_bytes,
        },
        "poses": provenance_poses,
    }
    return rendered, provenance


def _atomic_write(path: Path, body: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def build_review(
    manifest_path: Path,
    output_path: Path,
    provenance_path: Path,
    *,
    seed: str | None = None,
    ffmpeg: str | None = "ffmpeg",
    maximum_edge: int = 960,
    quality: int = 78,
    maximum_preview_bytes: int = 2_000_000,
) -> BuildResult:
    """Build and atomically write a review page plus private source mapping."""

    manifest = load_manifest(manifest_path)
    output_path = output_path.expanduser().resolve()
    provenance_path = provenance_path.expanduser().resolve()
    if output_path == provenance_path:
        raise ImageReviewError("page and provenance paths must be different")
    chosen_seed = seed or secrets.token_hex(16)
    if not isinstance(chosen_seed, str) or not chosen_seed:
        raise ImageReviewError("seed must be a non-empty string")
    rendered, provenance = render_review(
        manifest,
        output_name=output_path.name,
        seed=chosen_seed,
        ffmpeg=ffmpeg,
        maximum_edge=maximum_edge,
        quality=quality,
        maximum_preview_bytes=maximum_preview_bytes,
    )
    page_body = rendered.encode("utf-8")
    provenance["generated_at"] = datetime.now(UTC).isoformat()
    provenance["page"] = str(output_path)
    provenance["page_sha256"] = hashlib.sha256(page_body).hexdigest()
    provenance_body = (
        json.dumps(provenance, indent=2, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    _atomic_write(output_path, page_body, mode=0o600)
    _atomic_write(provenance_path, provenance_body, mode=0o600)
    return BuildResult(
        page=output_path,
        provenance=provenance_path,
        issue=manifest.issue,
        seed=chosen_seed,
        page_bytes=len(page_body),
    )
