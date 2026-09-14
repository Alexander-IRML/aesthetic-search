from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any
import uuid

from sqlalchemy.exc import SQLAlchemyError

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.corpus import CorpusRouter
from artsearch.artwork_filter.corpus_store import ArtworkFilterCorpusStore
from artsearch.artwork_filter.errors import ArtworkFilterError
from artsearch.artwork_filter.factory import build_artwork_filter_service
from artsearch.artwork_filter.persistence import JSONLDecisionStore
from artsearch.bluesky.client import (
    BlueskyAPIError,
    BlueskyClient,
    BlueskyRequestBudget,
)
from artsearch.bluesky.config import load_bluesky_config
from artsearch.bluesky.io import JSONLCandidateStore
from artsearch.bluesky.roster import read_actor_roster
from artsearch.bluesky_pipeline import BlueskyArtworkPipeline
from artsearch.discovery.bluesky import (
    ActorSearchOracle,
    BlueskyHydrator,
    ManualSeedOracle,
    OutboundFollowOracle,
    PostSearchOracle,
    StarterPackSearchOracle,
    SuggestedFollowOracle,
)
from artsearch.discovery.config import (
    DiscoveryConfig,
    load_discovery_config,
    override_discovery_limits,
)
from artsearch.discovery.models import DiscoveryRequest, ReviewDecision
from artsearch.discovery.pipeline import (
    ArtworkPipelineMediaProcessor,
    DiscoveryCoordinator,
    InitialDiscoverySource,
)
from artsearch.discovery.repository import DiscoveryRepository
from artsearch.ingest.config import load_config
from artsearch.postgres.database import PostgresConfigurationError, build_engine
from artsearch.production.config import load_production_config


DEFAULT_DISCOVERY_CONFIG = "configs/discovery.default.toml"
DEFAULT_BLUESKY_CONFIG = "configs/bluesky.default.toml"
DEFAULT_FILTER_CONFIG = "configs/artwork_filter.default.toml"
DEFAULT_PROMPTS = "configs/artwork_filter.prompts.v1.toml"
DEFAULT_APP_CONFIG = "config/config.yaml"
DEFAULT_PRODUCTION_CONFIG = "configs/production.default.toml"


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        payload = _execute(args)
    except (
        ArtworkFilterError,
        BlueskyAPIError,
        PostgresConfigurationError,
        SQLAlchemyError,
        KeyError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    if args.command == "run" and payload.get("has_errors"):
        return 1
    return 0


def _execute(args: argparse.Namespace) -> dict[str, Any]:
    production = load_production_config(args.production_config)
    if not production.postgres.enabled:
        raise ValueError(
            "corpus discovery requires [postgres].enabled=true in the production config"
        )
    engine = build_engine(production.postgres)
    repository = DiscoveryRepository(engine)
    try:
        if args.command == "status":
            return repository.snapshot()
        if args.command == "review-creator":
            state = repository.review_creator(
                args.did,
                ReviewDecision(args.decision),
                reviewer=args.reviewer,
                note=args.note,
                campaign_id=_optional_uuid(args.campaign_id),
            )
            return {"did": args.did, "state": state.value, "status": "recorded"}
        if args.command == "exclude":
            repository.add_exclusion(
                subject_type=args.subject_type,
                subject_key=args.subject_key,
                scope=args.scope,
                reason_code=args.reason_code,
                reason=args.reason,
                source="operator_cli",
                created_by=args.reviewer,
            )
            return {
                "status": "excluded",
                "subject_type": args.subject_type,
                "subject_key": args.subject_key,
                "scope": args.scope,
            }
        if args.command == "request-removal":
            request_id = repository.request_removal(
                subject_type=args.subject_type,
                subject_key=args.subject_key,
                request_type=args.request_type,
                requester_reference=args.requester_reference,
                reason=args.reason,
            )
            return {"status": "open", "request_id": str(request_id)}
        if args.command == "approve-removal":
            request_id = uuid.UUID(args.request_id)
            repository.approve_removal(request_id, reviewer=args.reviewer)
            return {"status": "approved", "request_id": str(request_id)}
        if args.command == "run":
            return asyncio.run(_run_campaign(args, repository))
    finally:
        engine.dispose()
    raise ValueError(f"unsupported command: {args.command}")


async def _run_campaign(
    args: argparse.Namespace,
    repository: DiscoveryRepository,
) -> dict[str, Any]:
    discovery = override_discovery_limits(
        load_discovery_config(args.discovery_config),
        creator_budget=args.creator_budget,
        media_budget=args.media_budget,
        max_tasks=args.max_tasks,
        max_depth=args.max_depth,
        request_budget=args.request_budget,
    )
    bluesky = load_bluesky_config(args.bluesky_config)
    filter_config = load_artwork_filter_config(args.filter_config)
    app = load_config(args.app_config)
    candidates_path = _resolve(
        app.root_dir,
        args.candidates_output or bluesky.storage.candidates_jsonl_path,
    )
    decisions_path = _resolve(
        app.root_dir,
        args.decisions_output or filter_config.storage.decision_jsonl_path,
    )
    append = not args.overwrite_output
    candidate_store = JSONLCandidateStore(candidates_path, append=append)
    decision_store = JSONLDecisionStore(decisions_path, append=append)
    service = build_artwork_filter_service(
        filter_config,
        prompt_config=args.prompt_config,
        decision_store=decision_store,
        deterministic_only=args.deterministic_only,
    )
    router = CorpusRouter(
        filter_config,
        raw_dir=app.raw_dir,
        download_review_images=args.download_review,
    )
    corpus_store = ArtworkFilterCorpusStore(app, filter_config)
    pipeline = BlueskyArtworkPipeline(
        service,
        router,
        corpus_store,
        candidate_store=candidate_store,
    )
    campaign_name = args.campaign_name or (
        "discovery-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    )
    completed = False
    request_budget = BlueskyRequestBudget(discovery.crawl.max_api_requests_per_run)
    try:
        async with BlueskyClient(bluesky, request_budget=request_budget) as client:
            sources = _initial_sources(args, discovery, client)
            if not sources:
                raise ValueError("no discovery sources are enabled or configured")
            coordinator = DiscoveryCoordinator(
                discovery,
                repository,
                BlueskyHydrator(client, moderation=bluesky.moderation),
                ArtworkPipelineMediaProcessor(pipeline),
                outbound_oracle=(
                    OutboundFollowOracle(client) if discovery.sources.outbound_follows else None
                ),
                suggested_oracle=(
                    SuggestedFollowOracle(client) if discovery.sources.suggested_follows else None
                ),
                request_budget=request_budget,
                worker_id=args.worker_id,
            )
            summary = await coordinator.run(campaign_name, sources)
        candidate_store.commit(allow_empty=True)
        decision_store.commit(allow_empty=True)
        completed = True
        return {
            **summary.to_dict(),
            "has_errors": summary.has_errors,
            "candidates_output": str(candidates_path),
            "decisions_output": str(decisions_path),
            "discovery_config_version": discovery.version,
            "discovery_config_hash": discovery.config_hash,
        }
    finally:
        if not completed:
            candidate_store.abort()
            decision_store.abort()
        await router.aclose()
        await service.aclose()


def _initial_sources(
    args: argparse.Namespace,
    config: DiscoveryConfig,
    client: BlueskyClient,
) -> list[InitialDiscoverySource]:
    sources: list[InitialDiscoverySource] = []
    seeds = list(args.seed)
    if args.actors_file:
        seeds.extend(read_actor_roster(args.actors_file))
    seeds = list(dict.fromkeys(value.strip() for value in seeds if value.strip()))
    if config.sources.manual_seeds and seeds:
        source_ref = (
            f"actors-file:{Path(args.actors_file).name}" if args.actors_file else "cli-seeds"
        )
        sources.append(
            InitialDiscoverySource(
                oracle=ManualSeedOracle(seeds, source_ref=source_ref),
                request=DiscoveryRequest(
                    limit=min(len(seeds), config.crawl.daily_creator_budget),
                ),
                name="manual-seeds",
                expand_without_sampling=bool(args.trusted_seeds),
            )
        )
    if config.sources.actor_search and not args.manual_seeds_only:
        oracle = ActorSearchOracle(client)
        sources.extend(
            InitialDiscoverySource(
                oracle=oracle,
                request=DiscoveryRequest(
                    query=query,
                    limit=config.crawl.max_search_results_per_query,
                ),
                name=f"actor-search:{query}",
            )
            for query in config.sources.actor_queries
        )
    if config.sources.post_search and not args.manual_seeds_only:
        oracle = PostSearchOracle(client)
        sources.extend(
            InitialDiscoverySource(
                oracle=oracle,
                request=DiscoveryRequest(
                    query=query,
                    limit=config.crawl.max_search_results_per_query,
                ),
                name=f"post-search:{query}",
            )
            for query in config.sources.post_queries
        )
    if config.sources.starter_pack_search and not args.manual_seeds_only:
        oracle = StarterPackSearchOracle(
            client,
            max_members=config.crawl.max_list_members_per_pack,
        )
        sources.extend(
            InitialDiscoverySource(
                oracle=oracle,
                request=DiscoveryRequest(
                    query=query,
                    limit=config.crawl.max_starter_packs_per_query,
                ),
                name=f"starter-pack-search:{query}",
            )
            for query in config.sources.starter_pack_queries
        )
    return sources


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artsearch-discovery",
        description="Discover, qualify, and expand the ArtSearch creator corpus.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run one bounded discovery campaign.")
    _add_postgres_config(run)
    run.add_argument("--discovery-config", default=DEFAULT_DISCOVERY_CONFIG)
    run.add_argument("--bluesky-config", default=DEFAULT_BLUESKY_CONFIG)
    run.add_argument("--filter-config", default=DEFAULT_FILTER_CONFIG)
    run.add_argument("--prompt-config", default=DEFAULT_PROMPTS)
    run.add_argument("--app-config", default=DEFAULT_APP_CONFIG)
    run.add_argument("--actors-file")
    run.add_argument("--seed", action="append", default=[])
    run.add_argument("--campaign-name")
    run.add_argument("--worker-id", default="artsearch-discovery-cli")
    run.add_argument("--creator-budget", type=_positive_int)
    run.add_argument("--media-budget", type=_positive_int)
    run.add_argument("--max-tasks", type=_positive_int)
    run.add_argument("--max-depth", type=_nonnegative_int)
    run.add_argument(
        "--request-budget",
        type=_positive_int,
        help="Maximum actual Bluesky HTTP attempts, including retries.",
    )
    run.add_argument(
        "--trusted-seeds",
        action="store_true",
        help="Expand existing-corpus seeds without sampling them again first.",
    )
    run.add_argument(
        "--manual-seeds-only",
        action="store_true",
        help="Skip configured search sources while retaining graph expansion oracles.",
    )
    run.add_argument("--candidates-output")
    run.add_argument("--decisions-output")
    run.add_argument("--overwrite-output", action="store_true")
    run.add_argument("--deterministic-only", action="store_true")
    run.add_argument("--download-review", action="store_true")

    status = subparsers.add_parser("status", help="Print discovery and frontier metrics.")
    _add_postgres_config(status)

    review = subparsers.add_parser(
        "review-creator",
        help="Apply an auditable human decision to one candidate creator.",
    )
    _add_postgres_config(review)
    review.add_argument("--did", required=True)
    review.add_argument(
        "--decision", choices=[item.value for item in ReviewDecision], required=True
    )
    review.add_argument("--reviewer", default="local-reviewer")
    review.add_argument("--note", default="")
    review.add_argument("--campaign-id")

    exclude = subparsers.add_parser("exclude", help="Block a subject before future work.")
    _add_postgres_config(exclude)
    exclude.add_argument(
        "--subject-type",
        choices=["creator", "post", "media", "blob_hash", "source"],
        required=True,
    )
    exclude.add_argument("--subject-key", required=True)
    exclude.add_argument(
        "--scope",
        choices=["discovery", "index", "publication", "all"],
        default="all",
    )
    exclude.add_argument("--reason-code", required=True)
    exclude.add_argument("--reason", default="")
    exclude.add_argument("--reviewer", default="local-reviewer")

    removal = subparsers.add_parser(
        "request-removal",
        help="Open an opt-out, deletion, report, or operator removal request.",
    )
    _add_postgres_config(removal)
    removal.add_argument("--subject-type", choices=["creator", "post", "media"], required=True)
    removal.add_argument("--subject-key", required=True)
    removal.add_argument(
        "--request-type",
        choices=["artist_opt_out", "source_deletion", "report", "operator_block"],
        required=True,
    )
    removal.add_argument("--requester-reference")
    removal.add_argument("--reason", default="")

    approve = subparsers.add_parser(
        "approve-removal",
        help="Approve a request and emit a durable derived-store removal event.",
    )
    _add_postgres_config(approve)
    approve.add_argument("--request-id", required=True)
    approve.add_argument("--reviewer", default="local-reviewer")
    return parser


def _add_postgres_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--production-config", default=DEFAULT_PRODUCTION_CONFIG)


def _optional_uuid(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
