from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Sequence

from artsearch.ingest.config import load_config
from artsearch.production.capacity import build_capacity_plan
from artsearch.production.config import load_production_config
from artsearch.retrieval.faiss_index import (
    FaissIndexError,
    FaissSearchService,
    build_faiss_snapshot,
    evaluate_faiss_snapshot,
    faiss_model_bundle_version,
    faiss_snapshot_status,
    promote_faiss_snapshot,
)


DEFAULT_APP_CONFIG = "config/config.yaml"
DEFAULT_PRODUCTION_CONFIG = "configs/production.default.toml"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    service: FaissSearchService | None = None
    try:
        app = load_config(args.app_config)
        production = load_production_config(args.production_config)
        config = production.faiss
        if args.command == "config":
            _print(
                {
                    "enabled": config.enabled,
                    "artifact_root": str(config.artifact_root),
                    "alias_name": config.alias_name,
                    "dimensions": {
                        "clip": config.clip_dimension,
                        "csd": config.csd_dimension,
                    },
                    "nlist": config.nlist,
                    "nprobe": config.nprobe,
                    "pq": {
                        "bits": config.pq_bits,
                        "clip_subquantizers": config.clip_pq_subquantizers,
                        "csd_subquantizers": config.csd_pq_subquantizers,
                    },
                    "model_bundle_version": faiss_model_bundle_version(app.models, config),
                }
            )
            return 0
        if args.command == "status":
            _print(faiss_snapshot_status(config))
            return 0
        if args.command == "capacity-plan":
            plan = build_capacity_plan(
                config,
                production.scale_budget,
                target_points=args.points,
                average_cached_image_bytes=args.average_image_bytes,
                average_original_bytes=args.average_original_bytes,
            )
            payload = plan.to_dict()
            if args.output:
                _write_json(args.output, payload)
            _print(payload)
            return 0 if plan.promotion_ready else 3
        if not config.enabled:
            raise FaissIndexError("FAISS is disabled in the production configuration")
        if args.command == "build":
            result = build_faiss_snapshot(
                app.database_path,
                app,
                config,
                snapshot_version=args.snapshot_version,
                production_config_hash=production.config_hash,
                force_flat=args.force_flat,
            )
            _print(result.to_dict())
            return 0
        if args.command == "evaluate":
            service = FaissSearchService.from_snapshot(config, args.snapshot_version)
            report = evaluate_faiss_snapshot(
                service,
                sample_size=args.sample_size,
                top_k=args.top_k,
                seed=args.seed,
            )
            payload = report.to_dict()
            _write_json(args.output, payload)
            _print(payload)
            return 0 if report.quality_gate_passed else 3
        if args.command == "promote":
            _print(
                promote_faiss_snapshot(
                    config,
                    args.snapshot_version,
                    evaluation_path=args.evaluation,
                    budget_path=args.budget,
                )
            )
            return 0
        if args.command == "search-artwork":
            service = FaissSearchService.from_active(config)
            hits = service.search_by_artwork(
                args.artwork_id,
                top_k=args.top_k,
                include_query_artist=not args.exclude_query_artist,
            )
            _print({"results": [hit.to_dict() for hit in hits]})
            return 0
        raise AssertionError(f"unhandled command: {args.command}")
    except (FaissIndexError, OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    finally:
        if service is not None:
            service.close()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artsearch-faiss",
        description="Build, evaluate, and atomically promote compressed ArtSearch snapshots.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("config", "Print the resolved non-secret FAISS contract."),
        ("status", "Inspect the active immutable snapshot."),
    ):
        command = subparsers.add_parser(name, help=help_text)
        _add_config_paths(command)

    capacity = subparsers.add_parser(
        "capacity-plan",
        help="Evaluate storage and recurring spend against the hard monthly budget.",
    )
    _add_config_paths(capacity)
    capacity.add_argument("--points", type=_positive_int)
    capacity.add_argument("--average-image-bytes", type=_positive_int)
    capacity.add_argument("--average-original-bytes", type=_positive_int, default=723_000)
    capacity.add_argument("--output")

    build = subparsers.add_parser("build", help="Build a new inactive immutable snapshot.")
    _add_config_paths(build)
    build.add_argument("--snapshot-version", default=_default_snapshot_version())
    build.add_argument(
        "--force-flat",
        action="store_true",
        help="Use exact flat indexes for a small smoke-test snapshot.",
    )

    evaluate = subparsers.add_parser(
        "evaluate",
        help="Measure ANN recall and latency against exact corpus scans.",
    )
    _add_config_paths(evaluate)
    evaluate.add_argument("--snapshot-version", required=True)
    evaluate.add_argument("--sample-size", type=_positive_int, default=20)
    evaluate.add_argument("--top-k", type=_positive_int, default=100)
    evaluate.add_argument("--seed", type=int, default=0)
    evaluate.add_argument("--output", required=True)

    promote = subparsers.add_parser(
        "promote",
        help="Activate a snapshot only when quality and cost reports pass.",
    )
    _add_config_paths(promote)
    promote.add_argument("--snapshot-version", required=True)
    promote.add_argument("--evaluation", required=True)
    promote.add_argument("--budget", required=True)

    search = subparsers.add_parser(
        "search-artwork",
        help="Search the active snapshot using one indexed artwork.",
    )
    _add_config_paths(search)
    search.add_argument("artwork_id")
    search.add_argument("--top-k", type=_positive_int, default=20)
    search.add_argument("--exclude-query-artist", action="store_true")
    return parser


def _add_config_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app-config", default=DEFAULT_APP_CONFIG)
    parser.add_argument("--production-config", default=DEFAULT_PRODUCTION_CONFIG)


def _default_snapshot_version() -> str:
    return datetime.now(timezone.utc).strftime("faiss-%Y%m%dT%H%M%SZ")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _write_json(path: str | Path, payload: dict[str, object]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
