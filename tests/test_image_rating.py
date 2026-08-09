from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

import image_rating
from image_rating import ImageRatingError, build_rating, load_manifest, render_rating
from page_contract import prepare_html


REPO = Path(__file__).resolve().parents[1]


def png_pixel(red: int, green: int, blue: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        checksum = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", 2, 2, 8, 6, 0, 0, 0)
    row = bytes((0, red, green, blue, 255, red, green, blue, 255))
    pixels = zlib.compress(row + row)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


class ImageRatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.images: list[Path] = []
        for index in range(1, 6):
            path = self.root / f"private-rating-source-{index}.png"
            path.write_bytes(png_pixel(index * 31, index * 19, index * 11))
            self.images.append(path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def payload(self, *, count: int = 4) -> dict[str, object]:
        return {
            "schema_version": 1,
            "issue": "project-identity-rating-v1",
            "title": "Identity recovery rating",
            "subject": "Emilia",
            "question": "Does this face look like Emilia?",
            "instructions": "Judge facial geometry and recognition.",
            "item_noun": "Face",
            "items": [
                {"id": f"private-source-id-{index}", "path": str(self.images[index])}
                for index in range(count)
            ],
        }

    def write_payload(self, payload: dict[str, object]) -> Path:
        manifest = self.root / "rating.json"
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        return manifest

    def write_manifest(self, *, count: int = 4) -> Path:
        return self.write_payload(self.payload(count=count))

    def test_render_is_deterministic_and_seed_changes_order(self) -> None:
        manifest = load_manifest(self.write_manifest())
        first_html, first_provenance = render_rating(
            manifest,
            output_name="decision_identity_rating.html",
            seed="stable-seed",
            ffmpeg=None,
        )
        second_html, second_provenance = render_rating(
            manifest,
            output_name="decision_identity_rating.html",
            seed="stable-seed",
            ffmpeg=None,
        )
        other_html, other_provenance = render_rating(
            manifest,
            output_name="decision_identity_rating.html",
            seed="different-seed",
            ffmpeg=None,
        )

        self.assertEqual(first_html, second_html)
        self.assertEqual(first_provenance, second_provenance)
        first_order = [item["source_id"] for item in first_provenance["items"]]
        other_order = [item["source_id"] for item in other_provenance["items"]]
        self.assertNotEqual(first_order, other_order)
        self.assertNotEqual(first_html, other_html)

    def test_rendered_page_is_blind_self_contained_and_contract_valid(self) -> None:
        manifest = load_manifest(self.write_manifest())
        rendered, provenance = render_rating(
            manifest,
            output_name="decision_identity_rating.html",
            seed="blind-seed",
            ffmpeg=None,
        )

        self.assertEqual(prepare_html("decision_identity_rating.html", rendered), rendered)
        for image in self.images[:4]:
            self.assertNotIn(str(image), rendered)
            self.assertNotIn(image.name, rendered)
        for source_index in range(4):
            self.assertNotIn(f"private-source-id-{source_index}", rendered)
        for item in provenance["items"]:
            self.assertIn(item["opaque_id"], rendered)
        self.assertNotIn("/ruling", rendered)
        self.assertNotIn("localStorage", rendered)
        self.assertNotIn("<script src=", rendered)
        self.assertIn("window.arachneCaptureHooks", rendered)

        canonical = (REPO / "ui" / "brief-agent.js").read_text(
            encoding="utf-8"
        ).strip()
        embedded = rendered.split(
            '<script data-arachne-brief-agent>', 1
        )[1].split("</script>", 1)[0].strip()
        self.assertEqual(embedded, canonical)

        assets_match = re.search(
            r'<script id="arachne-review-assets" type="application/json">(.*?)</script>',
            rendered,
            re.DOTALL,
        )
        self.assertIsNotNone(assets_match)
        assert assets_match is not None
        assets = json.loads(assets_match.group(1))
        self.assertEqual(len(assets), 4)
        for value in assets.values():
            self.assertEqual(rendered.count(value), 1)

    def test_default_and_custom_options_and_rating_structure(self) -> None:
        manifest = load_manifest(self.write_manifest(count=3))
        rendered, provenance = render_rating(
            manifest,
            output_name="decision_identity_rating.html",
            seed="structure-seed",
            ffmpeg=None,
        )
        self.assertEqual(
            len(re.findall(r'<section class="rating-card" data-decision="', rendered)),
            3,
        )
        for item in provenance["items"]:
            opaque_id = item["opaque_id"]
            self.assertEqual(rendered.count(f'name="rating__{opaque_id}"'), 3)
        for value in ("yes", "kind_of", "no"):
            self.assertEqual(rendered.count(f'value="{value}"'), 3)
        self.assertIn("const historyButtons = sections.map", rendered)
        self.assertIn("history.append(button)", rendered)
        self.assertIn("<h2>Set</h2>", rendered)

        custom = self.payload(count=2)
        custom["options"] = [
            {"value": "strong", "label": "Strong match", "hint": "Clearly recognizable."},
            {"value": "weak", "label": "Weak match"},
            {"value": "reject", "label": "Reject"},
            {"value": "unsure", "label": "Unsure", "hint": "Needs another view."},
        ]
        custom_manifest = load_manifest(self.write_payload(custom))
        custom_html, _ = render_rating(
            custom_manifest,
            output_name="decision_custom_rating.html",
            seed="custom-seed",
            ffmpeg=None,
        )
        for option in custom["options"]:
            assert isinstance(option, dict)
            self.assertIn(f'value="{option["value"]}"', custom_html)
            self.assertIn(str(option["label"]), custom_html)
        self.assertIn("Clearly recognizable.", custom_html)
        self.assertIn("Needs another view.", custom_html)

    def test_manifest_validation_and_identical_images_are_allowed(self) -> None:
        cases: list[tuple[dict[str, object], str]] = []
        unknown = self.payload()
        unknown["unexpected"] = True
        cases.append((unknown, "unknown keys"))
        missing_question = self.payload()
        del missing_question["question"]
        cases.append((missing_question, "missing: question"))
        boolean_version = self.payload()
        boolean_version["schema_version"] = True
        cases.append((boolean_version, "schema_version must be 1"))
        too_short = self.payload(count=1)
        cases.append((too_short, "at least 2"))
        duplicate = self.payload(count=2)
        duplicate_items = duplicate["items"]
        assert isinstance(duplicate_items, list)
        assert isinstance(duplicate_items[1], dict)
        duplicate_items[1]["id"] = duplicate_items[0]["id"]
        cases.append((duplicate, "duplicate item id"))
        bad_option = self.payload(count=2)
        bad_option["options"] = [{"value": "not safe", "label": "Bad"}]
        cases.append((bad_option, "must start with an alphanumeric"))

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ImageRatingError, message):
                    load_manifest(self.write_payload(payload))

        self.images[1].write_bytes(self.images[0].read_bytes())
        manifest = load_manifest(self.write_manifest(count=3))
        with mock.patch(
            "image_rating._original_preview",
            wraps=image_rating._original_preview,
        ) as original_preview:
            rendered, provenance = render_rating(
                manifest,
                output_name="decision_identical_rating.html",
                seed="duplicate-control-seed",
                ffmpeg=None,
            )
        self.assertEqual(len(provenance["items"]), 3)
        self.assertEqual(len({item["opaque_id"] for item in provenance["items"]}), 3)
        self.assertEqual(original_preview.call_count, 2)
        self.assertIn("data:image/png;base64,", rendered)

    def test_build_writes_private_files_and_complete_provenance(self) -> None:
        manifest = self.write_manifest(count=3)
        page = self.root / "out" / "decision_identity_rating.html"
        provenance_path = self.root / "private" / "identity.provenance.json"
        result = build_rating(
            manifest,
            page,
            provenance_path,
            seed="write-seed",
            ffmpeg=None,
        )

        self.assertEqual(result.page, page.resolve())
        self.assertEqual(result.provenance, provenance_path.resolve())
        self.assertEqual(os.stat(page).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(provenance_path).st_mode & 0o777, 0o600)
        record = json.loads(provenance_path.read_text(encoding="utf-8"))
        self.assertEqual(record["randomization_seed"], "write-seed")
        self.assertEqual(record["subject"], "Emilia")
        self.assertEqual(record["question"], "Does this face look like Emilia?")
        self.assertEqual(
            record["page_sha256"],
            hashlib.sha256(result.page.read_bytes()).hexdigest(),
        )
        expected_labels = ["Face 01", "Face 02", "Face 03"]
        self.assertEqual(
            [item["position_label"] for item in record["items"]],
            expected_labels,
        )
        self.assertEqual(
            {item["source_id"] for item in record["items"]},
            {"private-source-id-0", "private-source-id-1", "private-source-id-2"},
        )

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_real_ffmpeg_path_produces_metadata_free_jpeg_previews(self) -> None:
        manifest = load_manifest(self.write_manifest(count=2))
        rendered, provenance = render_rating(
            manifest,
            output_name="decision_identity_rating.html",
            seed="ffmpeg-seed",
            ffmpeg=shutil.which("ffmpeg"),
            maximum_edge=320,
        )

        self.assertIn("data:image/jpeg;base64,", rendered)
        self.assertTrue(all(item["preview_bytes"] > 100 for item in provenance["items"]))


if __name__ == "__main__":
    unittest.main()
