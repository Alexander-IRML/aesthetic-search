from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import sys

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import func, select, text

from artsearch.postgres.database import (
    PostgresConfigurationError,
    build_engine,
    database_health,
    redact_database_url,
    resolve_database_url,
)
from artsearch.postgres.migration import (
    SQLiteMigrationError,
    candidate_ids_from_jsonl,
    publish_sqlite_snapshot,
)
from artsearch.postgres.schema import (
    artists,
    artworks,
    filter_decisions,
    interaction_events,
    outbox_events,
    queries,
    retrieval_candidates,
    result_impressions,
    sqlite_imports,
)
from artsearch.production.config import load_production_config


DEFAULT_CONFIG = "configs/production.default.toml"
PROJECT_ROOT = Path(os.environ.get("ARTSEARCH_PROJECT_ROOT", Path.cwd())).resolve()


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_production_config(args.config)
        if args.command == "config":
            payload = {
                "enabled": config.postgres.enabled,
                "require_tls": config.postgres.require_tls,
                "pooled_url_env": config.postgres.database_url_env,
                "direct_url_env": config.postgres.direct_url_env,
                "application_name": config.postgres.application_name,
                "pool_size": config.postgres.pool_size,
                "max_overflow": config.postgres.max_overflow,
                "telemetry_retention_days": config.postgres.telemetry_retention_days,
                "outbox_batch_size": config.postgres.outbox_batch_size,
            }
            _print(payload)
            return 0
        if args.command == "upgrade":
            engine = build_engine(config.postgres, direct=True)
            try:
                alembic = AlembicConfig(str(PROJECT_ROOT / "alembic.ini"))
                with engine.begin() as connection:
                    alembic.attributes["connection"] = connection
                    command.upgrade(alembic, args.revision)
            finally:
                engine.dispose()
            _print({"status": "upgraded", "revision": args.revision})
            return 0
        if args.command == "status":
            engine = build_engine(config.postgres, direct=args.direct)
            try:
                payload = _status(engine)
                payload["endpoint"] = redact_database_url(
                    resolve_database_url(config.postgres, direct=args.direct)
                )
            finally:
                engine.dispose()
            _print(payload)
            return 0
        if args.command == "publish-sqlite":
            from artsearch.ingest.config import load_config
            from artsearch.postgres.model_lineage import model_bundle_record

            candidate_ids = (
                candidate_ids_from_jsonl(args.candidates_jsonl) if args.candidates_jsonl else None
            )
            engine = build_engine(config.postgres, direct=True)
            app = load_config(args.app_config)
            try:
                report = publish_sqlite_snapshot(
                    args.sqlite,
                    engine,
                    candidate_ids=candidate_ids,
                    allow_new_full_snapshot=args.allow_new_full_snapshot,
                    allow_existing_catalog=args.allow_existing_catalog,
                    qdrant_collection_name=(
                        config.qdrant.collection_name
                        if config.qdrant.enabled and not args.no_qdrant_outbox
                        else None
                    ),
                    model_bundle=model_bundle_record(app.models, config.qdrant),
                )
            finally:
                engine.dispose()
            _print(report.to_dict())
            return 0
        if args.command == "prune-telemetry":
            days = args.days or config.postgres.telemetry_retention_days
            engine = build_engine(config.postgres)
            try:
                with engine.begin() as connection:
                    deleted = connection.execute(
                        text("SELECT product.prune_telemetry(make_interval(days => :days))"),
                        {"days": days},
                    ).scalar_one()
            finally:
                engine.dispose()
            _print({"retention_days": days, "sessions_deleted": int(deleted)})
            return 0
        if args.command == "drain-qdrant":
            from artsearch.ingest.config import load_config
            from artsearch.postgres.qdrant_outbox import drain_qdrant_outbox
            from artsearch.retrieval.qdrant import build_qdrant_client

            app = load_config(args.app_config)
            engine = build_engine(config.postgres)
            client = build_qdrant_client(config.qdrant)
            try:
                result = drain_qdrant_outbox(
                    engine,
                    app,
                    config.qdrant,
                    client,
                    worker_id=args.worker_id,
                    batch_size=config.postgres.outbox_batch_size,
                    max_events=args.max_events,
                )
            finally:
                client.close()
                engine.dispose()
            _print(result.to_dict())
            return 0
    except (
        PostgresConfigurationError,
        SQLiteMigrationError,
        OSError,
        RuntimeError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="artsearch-postgres",
        description="Manage the ArtSearch PostgreSQL control plane and SQLite cutover.",
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("config", help="Print non-secret PostgreSQL configuration.")
    upgrade = subparsers.add_parser("upgrade", help="Apply Alembic with the direct URL.")
    upgrade.add_argument("--revision", default="head")

    status = subparsers.add_parser("status", help="Check connectivity and canonical counts.")
    status.add_argument("--direct", action="store_true", help="Use the direct endpoint.")

    publish = subparsers.add_parser(
        "publish-sqlite",
        help="Transactionally publish a full or candidate-scoped SQLite snapshot.",
    )
    publish.add_argument("--sqlite", default="data/artsearch.db")
    publish.add_argument("--app-config", default="config/config.yaml")
    publish.add_argument(
        "--candidates-jsonl",
        help="Limit publication to candidate IDs present in one decision/candidate JSONL.",
    )
    publish.add_argument("--allow-new-full-snapshot", action="store_true")
    publish.add_argument("--allow-existing-catalog", action="store_true")
    publish.add_argument(
        "--no-qdrant-outbox",
        action="store_true",
        help="Do not enqueue reconciliation events for published artworks.",
    )

    prune = subparsers.add_parser("prune-telemetry", help="Apply detailed telemetry retention.")
    prune.add_argument("--days", type=int)

    drain = subparsers.add_parser(
        "drain-qdrant",
        help="Publish pending PostgreSQL outbox events into Qdrant.",
    )
    drain.add_argument("--app-config", default="config/config.yaml")
    drain.add_argument("--worker-id", default="artsearch-postgres-cli")
    drain.add_argument("--max-events", type=int)
    return parser


def _status(engine: object) -> dict[str, object]:
    payload = database_health(engine)  # type: ignore[arg-type]
    with engine.connect() as connection:  # type: ignore[union-attr]
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        counts = {
            "artists": artists,
            "artworks": artworks,
            "filter_decisions": filter_decisions,
            "queries": queries,
            "retrieval_candidates": retrieval_candidates,
            "result_impressions": result_impressions,
            "interaction_events": interaction_events,
            "outbox_pending": outbox_events,
            "sqlite_imports": sqlite_imports,
        }
        resolved: dict[str, int] = {}
        for name, table in counts.items():
            statement = select(func.count()).select_from(table)
            if name == "outbox_pending":
                statement = statement.where(outbox_events.c.status == "pending")
            resolved[name] = int(connection.execute(statement).scalar_one())
    return {**payload, "alembic_revision": revision, "counts": resolved}


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
