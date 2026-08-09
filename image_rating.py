"""Build self-contained, blinded image-rating briefs for Arachne.

The trusted producer embeds compact previews in a generated HTML brief and
writes the private source mapping to a separate provenance sidecar.  Runtime
pages see only positional labels and opaque item identifiers.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import random
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from image_review import (
    BuildResult,
    ImageReviewError,
    Preview,
    _atomic_write,
    _candidate_path,
    _exact_keys,
    _ffmpeg_preview,
    _fill_template,
    _identifier,
    _json_for_script,
    _original_preview,
    _plain_object,
    _sha256_file,
    _text,
)
from page_contract import normalize_issue, prepare_html


SCHEMA_VERSION = 1
PROVENANCE_SCHEMA_VERSION = 1
_TEMPLATE_PATH = Path(__file__).with_name("templates") / "image-rating.html"
_BRIEF_AGENT_PATH = Path(__file__).with_name("ui") / "brief-agent.js"
_DEFAULT_INSTRUCTIONS = (
    "Rate one blinded image at a time. Add an optional note when it helps "
    "explain the rating."
)
_DEFAULT_OPTIONS = (
    {"value": "yes", "label": "Yes"},
    {"value": "kind_of", "label": "Kind of"},
    {"value": "no", "label": "No"},
)

# The generic image-review helpers deliberately share one validation error
# family across both producer-side builders.
ImageRatingError = ImageReviewError


@dataclass(frozen=True)
class RatingItem:
    source_id: str
    path: Path


@dataclass(frozen=True)
class RatingOption:
    value: str
    label: str
    hint: str


@dataclass(frozen=True)
class RatingManifest:
    issue: str
    title: str
    subject: str
    question: str
    instructions: str
    item_noun: str
    items: tuple[RatingItem, ...]
    options: tuple[RatingOption, ...]
    source: Path


def load_manifest(source: Path) -> RatingManifest:
    """Load and strictly validate one image-rating manifest."""

    source = source.expanduser().resolve()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImageRatingError(f"manifest does not exist: {source}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImageRatingError(
            f"manifest is not valid UTF-8 JSON: {source}"
        ) from exc

    root = _plain_object(payload, "manifest")
    _exact_keys(
        root,
        "manifest",
        {"schema_version", "issue", "title", "subject", "question", "items"},
        {"instructions", "item_noun", "options"},
    )
    if (
        type(root["schema_version"]) is not int
        or root["schema_version"] != SCHEMA_VERSION
    ):
        raise ImageRatingError(
            f"schema_version must be {SCHEMA_VERSION}, "
            f"got {root['schema_version']!r}"
        )
    try:
        issue = normalize_issue(root["issue"])
    except ValueError as exc:
        raise ImageRatingError(str(exc)) from exc
    assert issue is not None

    title = _text(root["title"], "title", limit=160)
    subject = _text(root["subject"], "subject", limit=100)
    question = _text(root["question"], "question", limit=300)
    instructions = _text(
        root.get("instructions", _DEFAULT_INSTRUCTIONS),
        "instructions",
        limit=600,
    )
    item_noun = _text(root.get("item_noun", "Item"), "item_noun", limit=40)

    raw_items = root["items"]
    if not isinstance(raw_items, list) or len(raw_items) < 2:
        raise ImageRatingError("items must be a JSON array containing at least 2 items")
    items: list[RatingItem] = []
    seen_item_ids: set[str] = set()
    for item_index, raw_item in enumerate(raw_items):
        item_label = f"items[{item_index}]"
        item_value = _plain_object(raw_item, item_label)
        _exact_keys(item_value, item_label, {"id", "path"})
        source_id = _identifier(item_value["id"], f"{item_label}.id")
        if source_id in seen_item_ids:
            raise ImageRatingError(f"duplicate item id: {source_id}")
        seen_item_ids.add(source_id)
        path = _candidate_path(item_value["path"], f"{item_label}.path", source)
        items.append(RatingItem(source_id=source_id, path=path))

    raw_options: object = root.get("options", list(_DEFAULT_OPTIONS))
    if not isinstance(raw_options, list) or not raw_options:
        raise ImageRatingError("options must be a non-empty JSON array")
    options: list[RatingOption] = []
    seen_option_values: set[str] = set()
    for option_index, raw_option in enumerate(raw_options):
        option_label = f"options[{option_index}]"
        option_value = _plain_object(raw_option, option_label)
        _exact_keys(option_value, option_label, {"value", "label"}, {"hint"})
        value = _identifier(option_value["value"], f"{option_label}.value")
        if value in seen_option_values:
            raise ImageRatingError(f"duplicate option value: {value}")
        seen_option_values.add(value)
        label = _text(option_value["label"], f"{option_label}.label", limit=100)
        hint = ""
        if "hint" in option_value:
            hint = _text(option_value["hint"], f"{option_label}.hint", limit=240)
        options.append(RatingOption(value=value, label=label, hint=hint))

    return RatingManifest(
        issue=issue,
        title=title,
        subject=subject,
        question=question,
        instructions=instructions,
        item_noun=item_noun,
        items=tuple(items),
        options=tuple(options),
        source=source,
    )


def _opaque_id(issue: str, item: RatingItem, source_digest: str) -> str:
    material = "\0".join((issue, item.source_id, source_digest)).encode("utf-8")
    return "candidate-" + hashlib.sha256(material).hexdigest()[:16]


def _option_markup(option: RatingOption, *, field: str, palette_index: int) -> str:
    color = (
        "var(--yes)"
        if palette_index == 0
        else "var(--kind)"
        if palette_index == 1
        else "var(--no)"
        if palette_index == 2
        else "var(--neutral)"
    )
    hint = f"<small>{html.escape(option.hint)}</small>" if option.hint else ""
    return f"""
        <label class="rating" style="--rating-color:{color}">
          <input type="radio" name="{html.escape(field, quote=True)}"
            value="{html.escape(option.value, quote=True)}">
          <span><strong>{html.escape(option.label)}</strong>{hint}</span>
        </label>"""


def _sections_markup(
    manifest: RatingManifest,
    faces: list[dict[str, str]],
) -> str:
    sections: list[str] = []
    total = len(faces)
    for index, face in enumerate(faces, 1):
        blind_id = html.escape(face["blindId"], quote=True)
        label = html.escape(face["label"])
        label_attribute = html.escape(face["label"], quote=True)
        rating_field = face["ratingField"]
        comment_field = face["commentField"]
        options = "".join(
            _option_markup(option, field=rating_field, palette_index=option_index)
            for option_index, option in enumerate(manifest.options)
        )
        hidden = "" if index == 1 else " hidden"
        alt = html.escape(
            f"Blinded {manifest.subject} candidate {index:02d} — {manifest.question}",
            quote=True,
        )
        sections.append(
            f"""
      <section class="rating-card" data-decision="{blind_id}"
        data-label="{label_attribute}"{hidden}>
        <header>
          <p class="kicker">Blinded image rating</p>
          <h2>{label}</h2>
          <span>{index} of {total}</span>
        </header>
        <figure>
          <img data-review-image="{blind_id}" alt="{alt}"
            loading="lazy" decoding="async">
        </figure>
        <fieldset>
          <legend>{html.escape(manifest.question)}</legend>{options}
        </fieldset>
        <label class="comment-label" for="{html.escape(comment_field, quote=True)}">
          Optional note
        </label>
        <textarea id="{html.escape(comment_field, quote=True)}"
          name="{html.escape(comment_field, quote=True)}" rows="3"
          placeholder="Add context for this rating, if useful."></textarea>
        <nav aria-label="Rating navigation">
          <button type="button" class="secondary" data-review-previous>Previous</button>
          <button type="button" class="primary" data-review-next>Next</button>
        </nav>
        <p class="done" data-complete-note hidden>
          All items are rated. File the ruling below.
        </p>
      </section>"""
        )
    return "\n".join(sections)


def render_rating(
    manifest: RatingManifest,
    *,
    output_name: str,
    seed: str,
    ffmpeg: str | None,
    maximum_edge: int = 960,
    quality: int = 78,
    maximum_preview_bytes: int = 2_000_000,
) -> tuple[str, dict[str, Any]]:
    """Render one blinded rating page and its private provenance record."""

    if not isinstance(seed, str) or not seed:
        raise ImageRatingError("seed must be a non-empty string")
    if not 320 <= maximum_edge <= 2400:
        raise ImageRatingError("maximum_edge must be between 320 and 2400")
    if not 1 <= quality <= 100:
        raise ImageRatingError("quality must be between 1 and 100")
    if maximum_preview_bytes < 64_000:
        raise ImageRatingError("maximum_preview_bytes must be at least 64000")
    if ffmpeg is not None:
        resolved_ffmpeg = shutil.which(ffmpeg) if os.sep not in ffmpeg else ffmpeg
        if not resolved_ffmpeg or not Path(resolved_ffmpeg).is_file():
            raise ImageRatingError(f"ffmpeg executable not found: {ffmpeg}")
        ffmpeg = str(Path(resolved_ffmpeg).resolve())

    preview_cache: dict[str, Preview] = {}
    prepared_items: list[dict[str, Any]] = []
    opaque_ids: set[str] = set()
    for item in manifest.items:
        source_digest = _sha256_file(item.path)
        preview = preview_cache.get(source_digest)
        if preview is None:
            preview = (
                _ffmpeg_preview(
                    item.path,
                    ffmpeg=ffmpeg,
                    maximum_edge=maximum_edge,
                    quality=quality,
                    maximum_bytes=maximum_preview_bytes,
                )
                if ffmpeg is not None
                else _original_preview(
                    item.path,
                    maximum_bytes=maximum_preview_bytes,
                )
            )
            preview_cache[source_digest] = preview
        opaque_id = _opaque_id(manifest.issue, item, source_digest)
        if opaque_id in opaque_ids:
            raise ImageRatingError(f"opaque item id collision: {opaque_id}")
        opaque_ids.add(opaque_id)
        prepared_items.append(
            {
                "opaque_id": opaque_id,
                "source_id": item.source_id,
                "source_path": str(item.path),
                "source_sha256": source_digest,
                "preview_sha256": preview.preview_sha256,
                "preview_bytes": preview.preview_bytes,
                "data_uri": preview.data_uri,
            }
        )

    rng_digest = hashlib.sha256(seed.encode("utf-8")).digest()
    rng = random.Random(int.from_bytes(rng_digest, "big"))
    rng.shuffle(prepared_items)

    assets: dict[str, str] = {}
    faces: list[dict[str, str]] = []
    provenance_items: list[dict[str, Any]] = []
    for position, item in enumerate(prepared_items, 1):
        position_label = f"{manifest.item_noun} {position:02d}"
        opaque_id = item["opaque_id"]
        assets[opaque_id] = item["data_uri"]
        faces.append(
            {
                "blindId": opaque_id,
                "label": position_label,
                "ratingField": f"rating__{opaque_id}",
                "commentField": f"comment__{opaque_id}",
            }
        )
        provenance_items.append(
            {
                "position_label": position_label,
                "opaque_id": opaque_id,
                "source_id": item["source_id"],
                "source_path": item["source_path"],
                "source_sha256": item["source_sha256"],
                "preview_sha256": item["preview_sha256"],
                "preview_bytes": item["preview_bytes"],
            }
        )

    option_model = [
        {
            "value": option.value,
            "label": option.label,
            **({"hint": option.hint} if option.hint else {}),
        }
        for option in manifest.options
    ]
    review_model = {
        "title": manifest.title,
        "question": manifest.question,
        "options": option_model,
        "faces": faces,
    }
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    canonical_agent = _BRIEF_AGENT_PATH.read_text(encoding="utf-8").strip()
    rendered = _fill_template(
        template,
        {
            "@@ARACHNE_ISSUE@@": html.escape(manifest.issue, quote=True),
            "@@ARACHNE_DOCUMENT_TITLE@@": html.escape(manifest.title),
            "@@ARACHNE_SUBJECT@@": html.escape(manifest.subject),
            "@@ARACHNE_HEADING@@": html.escape(manifest.title),
            "@@ARACHNE_INSTRUCTIONS@@": html.escape(manifest.instructions),
            "@@ARACHNE_QUESTION@@": html.escape(manifest.question),
            "@@ARACHNE_SECTIONS@@": _sections_markup(manifest, faces),
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
        "subject": manifest.subject,
        "question": manifest.question,
        "source_manifest": str(manifest.source),
        "randomization_seed": seed,
        "preview": {
            "encoder": ffmpeg or "original-bytes",
            "maximum_edge": maximum_edge if ffmpeg else None,
            "quality": quality if ffmpeg else None,
            "maximum_bytes": maximum_preview_bytes,
        },
        "items": provenance_items,
    }
    return rendered, provenance


def build_rating(
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
    """Build and atomically write a rating page plus private source mapping."""

    manifest = load_manifest(manifest_path)
    output_path = output_path.expanduser().resolve()
    provenance_path = provenance_path.expanduser().resolve()
    if output_path == provenance_path:
        raise ImageRatingError("page and provenance paths must be different")
    chosen_seed = seed or secrets.token_hex(16)
    if not isinstance(chosen_seed, str) or not chosen_seed:
        raise ImageRatingError("seed must be a non-empty string")
    rendered, provenance = render_rating(
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
