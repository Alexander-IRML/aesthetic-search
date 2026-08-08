from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys

from artsearch.artwork_filter.errors import ArtworkFilterError
from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.bluesky.audit import (
    audit_actors,
    select_safe_pilot,
    write_actor_file,
    write_audit_report,
)
from artsearch.bluesky.candidates import iter_author_image_candidates
from artsearch.bluesky.client import BlueskyAPIError, BlueskyClient
from artsearch.bluesky.config import BlueskyConfig, load_bluesky_config
from artsearch.bluesky.io import (
    JSONLActorCheckpointStore,
    JSONLCandidateStore,
    actor_checkpoint_key,
)


@dataclass
class CollectionSummary:
    actors_started: int = 0
    actors_completed: int = 0
    actors_skipped: int = 0
    actor_errors: int = 0
    candidates: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect Bluesky image candidates.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_author = subparsers.add_parser(
        "collect-author",
        help="Collect image candidates from one Bluesky author feed.",
    )
    _add_collection_args(collect_author)
    collect_author.add_argument("--actor", required=True, help="Author DID or handle.")

    collect_authors = subparsers.add_parser(
        "collect-authors",
        help="Collect image candidates from a newline-delimited local actor list.",
    )
    _add_collection_args(collect_authors)
    collect_authors.add_argument("--actors-file", required=True)

    audit = subparsers.add_parser(
        "audit-actors",
        help="Create a metadata-only public-safe pilot roster without downloading images.",
    )
    audit.add_argument("--config", default="configs/bluesky.default.toml")
    audit.add_argument("--actors-file", required=True)
    audit.add_argument("--report-output", default="data/bluesky/artist_audit.jsonl")
    audit.add_argument(
        "--pilot-output",
        default="config/bluesky_safe_pilot.local.txt",
    )
    audit.add_argument("--scan-limit", type=int, default=50)
    audit.add_argument("--max-actors", type=int)
    audit.add_argument("--pilot-size", type=int, default=12)
    audit.add_argument("--min-safe-candidates", type=int, default=3)
    audit.add_argument("--max-excluded-fraction", type=float, default=0.10)
    audit.add_argument("--concurrency", type=int, default=6)

    info = subparsers.add_parser("info", help="Print Bluesky collection config.")
    info.add_argument("--config", default="configs/bluesky.default.toml")

    args = parser.parse_args()
    if args.command == "info":
        config = load_bluesky_config(args.config)
        print(f"version: {config.version}")
        print(f"api_base_url: {config.api.base_url}")
        print(f"feed_filter: {config.api.feed_filter}")
        print(f"public_safe_mode: {config.moderation.public_safe_mode}")
        print(f"excluded_labels: {','.join(config.moderation.excluded_labels)}")
        print(f"config_hash: {config.config_hash}")
        return

    try:
        if args.command == "audit-actors":
            audit_summary = asyncio.run(_audit_actor_file(args))
            print(json.dumps(audit_summary, sort_keys=True))
            return
        summary = asyncio.run(_collect(args))
    except (ArtworkFilterError, BlueskyAPIError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary.to_dict(), sort_keys=True))
    if summary.actor_errors:
        raise SystemExit(1)


def _add_collection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="configs/bluesky.default.toml")
    parser.add_argument("--output")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--feed-filter")
    parser.add_argument("--append", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Append candidates and skip actors completed with the same collection settings.",
    )
    parser.add_argument("--checkpoint")
    parser.add_argument("--fail-fast", action="store_true")


async def _audit_actor_file(args: argparse.Namespace) -> dict[str, int | str]:
    config = load_bluesky_config(args.config)
    actors = _read_actor_file(args.actors_file)
    if args.max_actors is not None:
        if args.max_actors <= 0:
            raise ValueError("--max-actors must be positive")
        actors = actors[: args.max_actors]
    if not actors:
        raise ValueError("actor list is empty")

    async with BlueskyClient(config) as client:
        audits = await audit_actors(
            client,
            actors,
            moderation=config.moderation,
            limit=args.scan_limit,
            concurrency=args.concurrency,
        )
    pilot = select_safe_pilot(
        audits,
        count=args.pilot_size,
        min_allowed_candidates=args.min_safe_candidates,
        max_excluded_fraction=args.max_excluded_fraction,
    )
    report_path = write_audit_report(audits, args.report_output)
    pilot_path = write_actor_file(pilot, args.pilot_output)
    return {
        "actors_audited": len(audits),
        "actor_errors": sum(audit.status == "error" for audit in audits),
        "eligible_pilot_actors": len(pilot),
        "report": str(report_path),
        "pilot": str(pilot_path),
    }


async def _collect(args: argparse.Namespace) -> CollectionSummary:
    config = load_bluesky_config(args.config)
    output_path = Path(args.output) if args.output else config.storage.candidates_jsonl_path
    checkpoint_path = (
        Path(args.checkpoint) if args.checkpoint else config.storage.checkpoint_jsonl_path
    )
    actors = (
        [args.actor] if args.command == "collect-author" else _read_actor_file(args.actors_file)
    )
    if not actors:
        raise ValueError("actor list is empty")

    candidate_store = JSONLCandidateStore(output_path, append=args.append or args.resume)
    checkpoint_store = JSONLActorCheckpointStore(checkpoint_path)
    settings = _collection_settings(config, args)
    try:
        return await _collect_actors(
            config,
            args,
            actors=actors,
            candidate_store=candidate_store,
            checkpoint_store=checkpoint_store,
            settings=settings,
        )
    finally:
        candidate_store.abort()


async def _collect_actors(
    config: BlueskyConfig,
    args: argparse.Namespace,
    *,
    actors: list[str],
    candidate_store: JSONLCandidateStore,
    checkpoint_store: JSONLActorCheckpointStore,
    settings: dict[str, object],
) -> CollectionSummary:
    summary = CollectionSummary()
    async with BlueskyClient(config) as client:
        for actor in actors:
            checkpoint_key = actor_checkpoint_key(actor, settings)
            if args.resume and checkpoint_store.is_completed(checkpoint_key):
                summary.actors_skipped += 1
                continue

            summary.actors_started += 1
            actor_count = 0
            batch: list[ImageCandidate] = []
            try:
                async for candidate in iter_author_image_candidates(
                    client,
                    actor,
                    max_pages=args.max_pages,
                    limit=args.limit,
                    feed_filter=args.feed_filter,
                    moderation=config.moderation,
                ):
                    batch.append(candidate)
                    actor_count += 1
                    if len(batch) >= config.api.page_limit:
                        candidate_store.append_many(batch)
                        batch = []
            except BlueskyAPIError as exc:
                if batch:
                    candidate_store.append_many(batch)
                candidate_store.commit()
                summary.candidates += actor_count
                summary.actor_errors += 1
                print(f"{actor}: collection failed: {exc}", file=sys.stderr)
                if args.fail_fast:
                    break
                continue

            if batch:
                candidate_store.append_many(batch)
            candidate_store.commit(allow_empty=True)
            checkpoint_store.mark_completed(
                key=checkpoint_key,
                actor=actor,
                candidate_count=actor_count,
                settings=settings,
            )
            summary.candidates += actor_count
            summary.actors_completed += 1
            print(f"{actor}: {actor_count} image candidates", file=sys.stderr)

    return summary


def _read_actor_file(path: str | Path) -> list[str]:
    actors = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        actor = line.strip()
        if actor and not actor.startswith("#"):
            actors.append(actor)
    return actors


def _collection_settings(config: BlueskyConfig, args: argparse.Namespace) -> dict[str, object]:
    return {
        "base_url": config.api.base_url,
        "max_pages": args.max_pages if args.max_pages is not None else config.api.max_pages,
        "limit": args.limit if args.limit is not None else config.api.page_limit,
        "feed_filter": args.feed_filter or config.api.feed_filter,
        "moderation": config.moderation.model_dump(mode="json"),
    }
