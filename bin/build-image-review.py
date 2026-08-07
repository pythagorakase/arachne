#!/usr/bin/env python3
"""Build one blinded, character-level Arachne image-review brief."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from image_review import ImageReviewError, build_review  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="character review manifest JSON")
    parser.add_argument("output", type=Path, help="self-contained output HTML")
    parser.add_argument(
        "--provenance",
        type=Path,
        default=None,
        help="private source mapping (default: OUTPUT.provenance.json)",
    )
    parser.add_argument(
        "--seed",
        default=None,
        help="reproducible randomization seed (default: securely generated)",
    )
    parser.add_argument(
        "--ffmpeg",
        default="ffmpeg",
        help="ffmpeg executable used for metadata-free WebP previews",
    )
    parser.add_argument(
        "--embed-originals",
        action="store_true",
        help="embed original bytes; intended only for tiny fixtures",
    )
    parser.add_argument("--maximum-edge", type=int, default=960)
    parser.add_argument("--quality", type=int, default=78)
    parser.add_argument("--maximum-preview-bytes", type=int, default=2_000_000)
    arguments = parser.parse_args()

    output = arguments.output.expanduser().resolve()
    provenance = (
        arguments.provenance.expanduser().resolve()
        if arguments.provenance is not None
        else output.with_suffix(".provenance.json")
    )
    try:
        result = build_review(
            arguments.manifest,
            output,
            provenance,
            seed=arguments.seed,
            ffmpeg=None if arguments.embed_originals else arguments.ffmpeg,
            maximum_edge=arguments.maximum_edge,
            quality=arguments.quality,
            maximum_preview_bytes=arguments.maximum_preview_bytes,
        )
    except (OSError, ImageReviewError, RuntimeError) as exc:
        parser.exit(1, f"Arachne image review failed: {exc}\n")

    print(f"page: {result.page} ({result.page_bytes} bytes)")
    print(f"provenance: {result.provenance}")
    print(f"issue: {result.issue}")
    print(f"seed: {result.seed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
