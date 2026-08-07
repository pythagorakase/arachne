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

from image_review import ImageReviewError, build_review, load_manifest, render_review
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


class ImageReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.images = []
        for index in range(1, 6):
            path = self.root / f"private-source-name-{index}.png"
            path.write_bytes(png_pixel(index * 30, index * 20, index * 10))
            self.images.append(path)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_manifest(self, *, candidate_counts: tuple[int, ...] = (2, 3)) -> Path:
        poses = []
        cursor = 0
        for pose_index, count in enumerate(candidate_counts, 1):
            candidates = []
            for candidate_index in range(count):
                candidates.append(
                    {
                        "id": f"source-{pose_index}-{candidate_index + 1}",
                        "path": str(self.images[cursor]),
                    }
                )
                cursor += 1
            poses.append(
                {
                    "id": f"s{pose_index:02d}-pose",
                    "label": f"Pose {pose_index}",
                    "note": "Judge identity, geometry, and expression.",
                    "candidates": candidates,
                }
            )
        manifest = self.root / "review.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "issue": "project-character-review-v1",
                    "title": "Character reference review",
                    "subject": {"id": "character", "label": "Character"},
                    "instructions": "Choose the stronger reference.",
                    "poses": poses,
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_two_candidates_render_once_as_pair_and_three_once_as_ranking(self) -> None:
        manifest = load_manifest(self.write_manifest())
        first_html, first_provenance = render_review(
            manifest,
            output_name="decision_character_review.html",
            seed="stable-seed",
            ffmpeg=None,
        )
        second_html, second_provenance = render_review(
            manifest,
            output_name="decision_character_review.html",
            seed="stable-seed",
            ffmpeg=None,
        )

        self.assertEqual(first_html, second_html)
        self.assertEqual(first_provenance, second_provenance)
        self.assertEqual(
            [pose["decision"]["mode"] for pose in first_provenance["poses"]],
            ["pair", "ranking"],
        )
        self.assertEqual(first_html.count('data-review-pair>'), 1)
        self.assertEqual(first_html.count('data-review-ranking>'), 1)
        three_way = first_provenance["poses"][1]
        ranking = three_way["decision"]["order"]
        self.assertEqual(len(ranking), 3)
        self.assertEqual(len(set(ranking)), 3)
        for opaque_id in ranking:
            self.assertEqual(
                first_html.count(f'data-review-image="{opaque_id}"'), 1
            )
        self.assertEqual(first_html.count('data-decision="'), 2)
        self.assertIn('value="tie"', first_html)
        self.assertIn("Use this ranking", first_html)
        self.assertIn("data-review-next", first_html)

    def test_rendered_page_is_blind_self_contained_and_contract_valid(self) -> None:
        manifest = load_manifest(self.write_manifest())
        rendered, provenance = render_review(
            manifest,
            output_name="decision_character_review.html",
            seed="blind-seed",
            ffmpeg=None,
        )

        self.assertEqual(
            prepare_html("decision_character_review.html", rendered), rendered
        )
        for image in self.images:
            self.assertNotIn(str(image), rendered)
            self.assertNotIn(image.name, rendered)
        for pose in provenance["poses"]:
            for candidate in pose["candidates"]:
                self.assertNotIn(candidate["source_id"], rendered)
                self.assertIn(candidate["opaque_id"], rendered)
        self.assertNotIn("/ruling", rendered)
        self.assertNotIn("localStorage", rendered)
        self.assertNotIn("<script src=", rendered)
        self.assertIn("data:image/png;base64,", rendered)
        self.assertIn("loading=\"lazy\"", rendered)
        self.assertIn("window.arachneCaptureHooks", rendered)
        self.assertIn("data-review-lightbox", rendered)

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
        self.assertEqual(len(assets), 5)
        for value in assets.values():
            self.assertEqual(rendered.count(value), 1)

    def test_comment_is_optional_but_ranking_requires_a_confirmed_order(self) -> None:
        manifest = load_manifest(self.write_manifest(candidate_counts=(3,)))
        rendered, _ = render_review(
            manifest,
            output_name="decision_character_review.html",
            seed="hook-seed",
            ffmpeg=None,
        )

        self.assertEqual(rendered.count("<textarea"), 1)
        self.assertIn('section.dataset.reviewMode === "ranking"', rendered)
        self.assertIn("ids.length === 3 && new Set(ids).size === 3", rendered)
        self.assertIn("Arrange the cards, then confirm the order.", rendered)
        self.assertIn('window.matchMedia("(max-width: 720px)")', rendered)
        self.assertNotIn("textarea:checked", rendered)

    def test_build_writes_owner_only_page_and_private_provenance(self) -> None:
        manifest = self.write_manifest(candidate_counts=(2,))
        page = self.root / "out" / "decision_character_review.html"
        provenance = self.root / "private" / "character.provenance.json"
        result = build_review(
            manifest,
            page,
            provenance,
            seed="write-seed",
            ffmpeg=None,
        )

        self.assertEqual(result.page, page.resolve())
        self.assertEqual(result.provenance, provenance.resolve())
        self.assertEqual(os.stat(page).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(provenance).st_mode & 0o777, 0o600)
        record = json.loads(provenance.read_text(encoding="utf-8"))
        self.assertEqual(record["randomization_seed"], "write-seed")
        self.assertEqual(record["issue"], "project-character-review-v1")
        self.assertEqual(
            record["page_sha256"],
            hashlib.sha256(result.page.read_bytes()).hexdigest(),
        )

    def test_manifest_rejects_any_candidate_count_other_than_two_or_three(self) -> None:
        manifest = self.write_manifest(candidate_counts=(1,))
        with self.assertRaisesRegex(ImageReviewError, "exactly 2 or 3"):
            load_manifest(manifest)

    def test_render_rejects_byte_identical_candidates(self) -> None:
        self.images[1].write_bytes(self.images[0].read_bytes())
        manifest = load_manifest(self.write_manifest(candidate_counts=(2,)))
        with self.assertRaisesRegex(ImageReviewError, "byte-identical"):
            render_review(
                manifest,
                output_name="decision_character_review.html",
                seed="duplicate-seed",
                ffmpeg=None,
            )

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    def test_real_ffmpeg_path_produces_metadata_free_jpeg_previews(self) -> None:
        manifest = load_manifest(self.write_manifest(candidate_counts=(2,)))
        rendered, provenance = render_review(
            manifest,
            output_name="decision_character_review.html",
            seed="ffmpeg-seed",
            ffmpeg=shutil.which("ffmpeg"),
            maximum_edge=320,
        )

        self.assertIn("data:image/jpeg;base64,", rendered)
        candidates = provenance["poses"][0]["candidates"]
        self.assertEqual(
            {candidate["preview_mime_type"] for candidate in candidates},
            {"image/jpeg"},
        )
        self.assertTrue(all(candidate["preview_bytes"] > 100 for candidate in candidates))


if __name__ == "__main__":
    unittest.main()
