from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.errors import ArtworkFilterError
from artsearch.bluesky_corpus import (
    archive_active_corpus,
    seed_siglip_corpus,
    select_siglip_corpus,
    selection_report,
)
from artsearch.ingest.config import load_config
from artsearch.retrieval.search import RetrievalMode


DEFAULT_CANDIDATES = "data/bluesky/image_candidates.jsonl"
DEFAULT_DECISIONS = "data/filter/decisions.jsonl"
DEFAULT_FILTER_CONFIG = "configs/artwork_filter.default.toml"
DEFAULT_APP_CONFIG = "config/config.yaml"


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    try:
        report = _run(args)
    except (ArtworkFilterError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2, sort_keys=True))
    seed = report.get("seed")
    embeddings = report.get("embeddings")
    if isinstance(seed, dict) and seed.get("errors", 0):
        raise SystemExit(1)
    if isinstance(embeddings, dict) and embeddings.get("errors", 0):
        raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Replace the active ArtSearch corpus with current artwork-like SigLIP "
            "decisions, then compute CLIP/DINO embeddings and write the retrieval gallery."
        )
    )
    parser.add_argument("--candidates", default=DEFAULT_CANDIDATES)
    parser.add_argument("--decisions", default=DEFAULT_DECISIONS)
    parser.add_argument("--filter-config", default=DEFAULT_FILTER_CONFIG)
    parser.add_argument("--app-config", default=DEFAULT_APP_CONFIG)
    parser.add_argument("--archive-root")
    parser.add_argument("--max-items", type=int)
    parser.add_argument(
        "--include-provenance-review",
        action="store_true",
        help="Include reposts and quote posts despite unresolved authorship.",
    )
    parser.add_argument(
        "--allow-config-mismatch",
        action="store_true",
        help="Allow decisions made with a different artwork-filter configuration.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the selection report without changing files or SQLite.",
    )
    parser.add_argument(
        "--confirm-replace",
        action="store_true",
        help="Required to archive and replace the active corpus.",
    )
    parser.add_argument("--skip-embeddings", action="store_true")
    parser.add_argument("--skip-gallery", action="store_true")
    parser.add_argument("--gallery-output")
    parser.add_argument("--gallery-sample-per-artist", type=int, default=12)
    parser.add_argument("--gallery-top-k", type=int, default=10)
    parser.add_argument(
        "--gallery-mode",
        choices=[mode.value for mode in RetrievalMode],
        default=RetrievalMode.ENSEMBLE.value,
    )
    return parser


def _run(args: argparse.Namespace) -> dict[str, object]:
    filter_config = load_artwork_filter_config(args.filter_config)
    app_config = load_config(args.app_config)
    selection = select_siglip_corpus(
        args.candidates,
        args.decisions,
        required_config_hash=(None if args.allow_config_mismatch else filter_config.config_hash),
        include_provenance_review=args.include_provenance_review,
        max_items=args.max_items,
    )
    report: dict[str, object] = {
        "selection": selection_report(selection),
        "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        return report
    if not args.confirm_replace:
        raise ValueError("pass --confirm-replace after reviewing a --dry-run report")
    if not selection.items:
        raise ValueError("no current artwork-like SigLIP decisions were selected")
    if args.gallery_sample_per_artist <= 0:
        raise ValueError("--gallery-sample-per-artist must be positive")
    if args.gallery_top_k <= 0:
        raise ValueError("--gallery-top-k must be positive")

    print(
        f"Archiving the active corpus before importing {len(selection.items)} images...",
        file=sys.stderr,
        flush=True,
    )
    archive = archive_active_corpus(
        app_config,
        archive_root=args.archive_root,
    )
    report["archive"] = {
        "directory": str(archive.archive_dir),
        "manifest": str(archive.manifest_path),
        "moved": archive.moved,
    }

    print("Downloading full-size Bluesky images and rebuilding SQLite...", file=sys.stderr)
    seed = asyncio.run(
        seed_siglip_corpus(
            selection,
            app_config=app_config,
            filter_config=filter_config,
        )
    )
    report["seed"] = seed

    if not args.skip_embeddings and seed["imported"] + seed["unchanged"] > 0:
        print("Computing CLIP and DINO embeddings...", file=sys.stderr, flush=True)
        from artsearch.embed.pipeline import generate_embeddings

        report["embeddings"] = generate_embeddings(args.app_config)
    else:
        report["embeddings"] = {"skipped_stage": True}

    if not args.skip_gallery and not args.skip_embeddings:
        print("Writing the Bluesky-backed retrieval gallery...", file=sys.stderr, flush=True)
        from artsearch.retrieval.demo import write_gallery_demo

        gallery_path = write_gallery_demo(
            config_path=args.app_config,
            output_path=Path(args.gallery_output) if args.gallery_output else None,
            sample_per_artist=args.gallery_sample_per_artist,
            top_k=args.gallery_top_k,
            mode=args.gallery_mode,
            include_same_artist=True,
        )
        report["gallery"] = {
            "path": str(gallery_path),
            "mode": args.gallery_mode,
            "include_same_artist": True,
        }
    else:
        report["gallery"] = {"skipped_stage": True}
    return report


if __name__ == "__main__":
    main()
