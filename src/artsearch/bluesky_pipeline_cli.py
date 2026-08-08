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
from artsearch.bluesky.candidates import iter_author_image_candidates
from artsearch.bluesky.client import BlueskyAPIError, BlueskyClient
from artsearch.bluesky.config import BlueskyConfig, load_bluesky_config
from artsearch.bluesky.io import (
    JSONLActorCheckpointStore,
    JSONLCandidateStore,
    actor_checkpoint_key,
)
from artsearch.bluesky_pipeline import BlueskyArtworkPipeline, PipelineSummary
from artsearch.ingest.config import load_config


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
        help="Run newline-delimited Bluesky DIDs or handles.",
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
    actors = [args.actor] if args.command == "run-author" else _read_actor_file(args.actors_file)
    if not actors:
        raise ValueError("actor list is empty")
    checkpoint_path = (
        Path(args.checkpoint) if args.checkpoint else bluesky_config.storage.checkpoint_jsonl_path
    )
    checkpoint_store = JSONLActorCheckpointStore(checkpoint_path)
    settings = _collection_settings(bluesky_config, args)
    summary = PipelineSummary()
    skip_candidate_keys = (
        pipeline.completed_candidate_keys(decisions_path) if args.resume else set()
    )

    try:
        async with BlueskyClient(bluesky_config) as client:
            for actor in actors:
                checkpoint_key = actor_checkpoint_key(actor, settings)
                if args.resume and checkpoint_store.is_completed(checkpoint_key):
                    summary.actors_skipped += 1
                    continue
                summary.actors_started += 1
                actor_summary = PipelineSummary()

                async def candidates() -> AsyncIterator[ImageCandidate]:
                    async for candidate in iter_author_image_candidates(
                        client,
                        actor,
                        max_pages=args.max_pages,
                        limit=args.limit,
                        feed_filter=args.feed_filter,
                        moderation=bluesky_config.moderation,
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
                    print(f"{actor}: pipeline failed: {exc}", file=sys.stderr)
                    if args.fail_fast:
                        break
                    continue

                checkpoint_store.mark_completed(
                    key=checkpoint_key,
                    actor=actor,
                    candidate_count=actor_summary.candidates,
                    settings=settings,
                )
                actor_summary.actors_completed += 1
                summary.merge(actor_summary)
                print(
                    f"{actor}: {actor_summary.candidates} candidates, "
                    f"{actor_summary.accepted} accept, {actor_summary.review} review, "
                    f"{actor_summary.rejected} reject, "
                    f"{actor_summary.classification_errors + actor_summary.route_errors} errors",
                    file=sys.stderr,
                )
        return summary
    finally:
        candidate_store.abort()


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
