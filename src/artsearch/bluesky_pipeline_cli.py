from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
import sys

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.corpus import CorpusRouter
from artsearch.artwork_filter.corpus_store import ArtworkFilterCorpusStore
from artsearch.artwork_filter.errors import ArtworkFilterError
from artsearch.artwork_filter.factory import build_artwork_filter_service
from artsearch.artwork_filter.persistence import JSONLDecisionStore
from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.artwork_filter.service import ArtworkFilterService
from artsearch.bluesky.candidates import AuthorScanProgress, iter_author_image_candidates
from artsearch.bluesky.client import BlueskyAPIError, BlueskyClient
from artsearch.bluesky.config import BlueskyConfig, load_bluesky_config
from artsearch.bluesky.io import (
    JSONLActorCheckpointStore,
    JSONLCandidateStore,
    actor_checkpoint_key,
)
from artsearch.bluesky.roster import read_actor_roster
from artsearch.bluesky.state import SQLiteBlueskyIntakeStateStore, rotated_roster
from artsearch.bluesky_pipeline import BlueskyArtworkPipeline, PipelineSummary
from artsearch.ingest.config import AppConfig, load_config


DEFAULT_BLUESKY_CONFIG = "configs/bluesky.default.toml"
DEFAULT_FILTER_CONFIG = "configs/artwork_filter.default.toml"
DEFAULT_PROMPTS = "configs/artwork_filter.prompts.v1.toml"
DEFAULT_APP_CONFIG = "config/config.yaml"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect, classify, route, and import Bluesky artwork candidates."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_author = subparsers.add_parser("run-author", help="Run one Bluesky author feed.")
    run_author.add_argument("--actor", required=True)
    _add_pipeline_args(run_author, collection=True)

    run_authors = subparsers.add_parser(
        "run-authors",
        help="Run Bluesky DIDs or handles from a local text or CSV roster.",
    )
    run_authors.add_argument("--actors-file", required=True)
    _add_pipeline_args(run_authors, collection=True)

    process = subparsers.add_parser(
        "process-jsonl",
        help="Filter and import an existing ImageCandidate JSONL file.",
    )
    process.add_argument("--input", required=True)
    _add_pipeline_args(process, collection=False)

    args = parser.parse_args()
    try:
        summary = asyncio.run(_run(args))
    except (ArtworkFilterError, BlueskyAPIError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary.to_dict(), sort_keys=True))
    if summary.has_errors:
        raise SystemExit(1)


def _add_pipeline_args(parser: argparse.ArgumentParser, *, collection: bool) -> None:
    parser.add_argument("--bluesky-config", default=DEFAULT_BLUESKY_CONFIG)
    parser.add_argument("--filter-config", default=DEFAULT_FILTER_CONFIG)
    parser.add_argument("--prompt-config", default=DEFAULT_PROMPTS)
    parser.add_argument("--app-config", default=DEFAULT_APP_CONFIG)
    parser.add_argument("--decisions-output")
    parser.add_argument(
        "--overwrite-decisions",
        action="store_true",
        help="Replace the decision JSONL instead of preserving prior audit rows.",
    )
    parser.add_argument("--deterministic-only", action="store_true")
    parser.add_argument("--no-review-download", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume completed decisions or, during collection, skip checkpointed actors.",
    )
    if collection:
        parser.add_argument("--candidates-output")
        parser.add_argument("--append-candidates", action="store_true")
        parser.add_argument("--checkpoint")
        parser.add_argument("--fail-fast", action="store_true")
        parser.add_argument("--max-pages", type=int)
        parser.add_argument("--limit", type=int)
        parser.add_argument("--feed-filter")
        parser.add_argument(
            "--max-candidates-per-actor",
            type=_positive_int,
            help="Stop after this many source-image candidates from each actor.",
        )
        parser.add_argument(
            "--max-candidates",
            type=_positive_int,
            help=(
                "Daily target across the actor roster. The current actor finishes so its "
                "incremental checkpoint remains valid."
            ),
        )
        parser.add_argument(
            "--no-incremental",
            action="store_true",
            help="Ignore persistent per-actor high-water marks for this run.",
        )
        parser.add_argument(
            "--no-roster-rotation",
            action="store_true",
            help="Start an actor-file run at its first line instead of the persisted position.",
        )


async def _run(args: argparse.Namespace) -> PipelineSummary:
    bluesky_config = load_bluesky_config(args.bluesky_config)
    filter_config = load_artwork_filter_config(args.filter_config)
    app_config = load_config(args.app_config)
    decisions_path = (
        Path(args.decisions_output)
        if args.decisions_output
        else filter_config.storage.decision_jsonl_path
    )
    if args.resume and args.overwrite_decisions:
        raise ValueError("--resume cannot be combined with --overwrite-decisions")
    decision_store = JSONLDecisionStore(
        decisions_path,
        append=not args.overwrite_decisions,
    )
    service = build_artwork_filter_service(
        filter_config,
        prompt_config=args.prompt_config,
        decision_store=decision_store,
        deterministic_only=args.deterministic_only,
    )
    router = CorpusRouter(
        filter_config,
        raw_dir=app_config.raw_dir,
        download_review_images=False if args.no_review_download else None,
    )
    corpus_store = ArtworkFilterCorpusStore(app_config, filter_config)

    completed = False
    try:
        summary = await _run_configured_pipeline(
            args,
            bluesky_config=bluesky_config,
            app_config=app_config,
            decisions_path=decisions_path,
            service=service,
            router=router,
            corpus_store=corpus_store,
        )
        decision_store.commit(allow_empty=True)
        completed = True
        return summary
    finally:
        if not completed:
            decision_store.abort()
        await router.aclose()
        await service.aclose()


async def _run_configured_pipeline(
    args: argparse.Namespace,
    *,
    bluesky_config: BlueskyConfig,
    app_config: AppConfig,
    decisions_path: Path,
    service: ArtworkFilterService,
    router: CorpusRouter,
    corpus_store: ArtworkFilterCorpusStore,
) -> PipelineSummary:
    if args.command == "process-jsonl":
        pipeline = BlueskyArtworkPipeline(service, router, corpus_store)
        return await pipeline.process_jsonl(
            args.input,
            resume_decisions_path=decisions_path if args.resume else None,
        )

    candidates_path = (
        Path(args.candidates_output)
        if args.candidates_output
        else bluesky_config.storage.candidates_jsonl_path
    )
    candidate_store = JSONLCandidateStore(
        candidates_path,
        append=args.append_candidates or args.resume,
    )
    pipeline = BlueskyArtworkPipeline(
        service,
        router,
        corpus_store,
        candidate_store=candidate_store,
    )
    actors = [args.actor] if args.command == "run-author" else read_actor_roster(args.actors_file)
    if not actors:
        raise ValueError("actor list is empty")
    checkpoint_path = (
        Path(args.checkpoint) if args.checkpoint else bluesky_config.storage.checkpoint_jsonl_path
    )
    checkpoint_store = JSONLActorCheckpointStore(checkpoint_path)
    settings = _collection_settings(bluesky_config, args)
    candidate_target = args.max_candidates or 0
    summary = PipelineSummary(candidate_target=candidate_target)
    skip_candidate_keys = (
        pipeline.completed_candidate_keys(decisions_path) if args.resume else set()
    )
    incremental = not bool(args.no_incremental)
    state_store = SQLiteBlueskyIntakeStateStore(app_config.database_path) if incremental else None
    roster_key: str | None = None
    if (
        args.command == "run-authors"
        and state_store is not None
        and not bool(args.no_roster_rotation)
    ):
        roster_key, start_index = state_store.roster_start(actors)
        actor_entries = rotated_roster(actors, start_index)
    else:
        actor_entries = list(enumerate(actors))

    try:
        async with BlueskyClient(bluesky_config) as client:
            for actor_index, actor in actor_entries:
                if candidate_target and summary.candidates >= candidate_target:
                    summary.budget_reached = 1
                    break
                checkpoint_key = actor_checkpoint_key(actor, settings)
                if args.resume and checkpoint_store.is_completed(checkpoint_key):
                    summary.actors_skipped += 1
                    _advance_roster(
                        state_store,
                        roster_key,
                        len(actors),
                        actor_index + 1,
                    )
                    continue
                summary.actors_started += 1
                actor_summary = PipelineSummary()
                actor_state = state_store.actor_state(actor) if state_store is not None else None
                progress = AuthorScanProgress(
                    newest_post_uri=(
                        actor_state.pending_high_water_post_uri if actor_state is not None else None
                    )
                )

                async def candidates() -> AsyncIterator[ImageCandidate]:
                    async for candidate in iter_author_image_candidates(
                        client,
                        actor,
                        max_pages=args.max_pages,
                        limit=args.limit,
                        feed_filter=args.feed_filter,
                        max_candidates=args.max_candidates_per_actor,
                        moderation=bluesky_config.moderation,
                        cursor=actor_state.resume_cursor if actor_state is not None else None,
                        stop_post_uri=(
                            actor_state.high_water_post_uri if actor_state is not None else None
                        ),
                        progress=progress,
                    ):
                        yield candidate

                try:
                    await pipeline.process_stream(
                        candidates(),
                        summary=actor_summary,
                        skip_candidate_keys=skip_candidate_keys,
                    )
                except BlueskyAPIError as exc:
                    summary.merge(actor_summary)
                    summary.actor_errors += 1
                    if state_store is not None:
                        state_store.record_error(actor, str(exc))
                    print(f"{actor}: pipeline failed: {exc}", file=sys.stderr)
                    _advance_roster(
                        state_store,
                        roster_key,
                        len(actors),
                        actor_index + 1,
                    )
                    if args.fail_fast:
                        break
                    continue

                if state_store is not None:
                    initial_scan = actor_state is not None and not actor_state.high_water_post_uri
                    scan_complete = bool(
                        initial_scan
                        or progress.reached_high_water
                        or progress.feed_exhausted
                        or progress.candidate_limit_reached
                    )
                    state_store.record_scan(
                        actor,
                        newest_post_uri=progress.newest_post_uri,
                        candidate_count=actor_summary.candidates,
                        scan_complete=scan_complete,
                        resume_cursor=progress.next_cursor,
                    )
                    if scan_complete:
                        summary.source_state_updates += 1
                    else:
                        summary.actors_backlogged += 1

                checkpoint_store.mark_completed(
                    key=checkpoint_key,
                    actor=actor,
                    candidate_count=actor_summary.candidates,
                    settings=settings,
                )
                actor_summary.actors_completed += 1
                actor_summary.actors_quota_reached += int(progress.candidate_limit_reached)
                summary.merge(actor_summary)
                _advance_roster(
                    state_store,
                    roster_key,
                    len(actors),
                    actor_index + 1,
                )
                print(
                    f"{actor}: {actor_summary.candidates} candidates, "
                    f"{actor_summary.accepted} accept, {actor_summary.review} review, "
                    f"{actor_summary.rejected} reject, "
                    f"{actor_summary.classification_errors + actor_summary.route_errors} errors",
                    file=sys.stderr,
                )
        if candidate_target and summary.candidates >= candidate_target:
            summary.budget_reached = 1
        return summary
    finally:
        candidate_store.abort()


def _collection_settings(config: BlueskyConfig, args: argparse.Namespace) -> dict[str, object]:
    return {
        "base_url": config.api.base_url,
        "max_pages": args.max_pages if args.max_pages is not None else config.api.max_pages,
        "limit": args.limit if args.limit is not None else config.api.page_limit,
        "feed_filter": args.feed_filter or config.api.feed_filter,
        "max_candidates_per_actor": args.max_candidates_per_actor,
        "moderation": config.moderation.model_dump(mode="json"),
    }


def _advance_roster(
    state_store: SQLiteBlueskyIntakeStateStore | None,
    roster_key: str | None,
    actor_count: int,
    next_index: int,
) -> None:
    if state_store is not None and roster_key is not None:
        state_store.advance_roster(roster_key, actor_count, next_index)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed
