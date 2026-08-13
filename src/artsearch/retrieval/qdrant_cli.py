from __future__ import annotations

import argparse
import json
import os
from typing import Any

from artsearch.ingest.config import load_config
from artsearch.ingest.db import connect, init_db
from artsearch.production.config import load_production_config
from artsearch.retrieval.qdrant import (
    QdrantIntegrationError,
    QdrantSearchService,
    build_qdrant_client,
    ensure_qdrant_collection,
    evaluate_qdrant_ann,
    model_bundle_version,
    promote_qdrant_alias,
    sync_qdrant_from_sqlite,
)


DEFAULT_APP_CONFIG = "config/config.yaml"
DEFAULT_PRODUCTION_CONFIG = "configs/production.default.toml"


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    client: Any | None = None
    try:
        app = load_config(args.app_config)
        production = load_production_config(args.production_config)
        qdrant = production.qdrant

        if args.command == "config":
            configured_url = os.environ.get(qdrant.url_env, "").strip() or qdrant.url
            print(
                json.dumps(
                    {
                        "enabled": qdrant.enabled,
                        "endpoint_configured": bool(configured_url),
                        "api_key_configured": bool(os.environ.get(qdrant.api_key_env, "").strip()),
                        "collection_name": qdrant.collection_name,
                        "alias_name": qdrant.alias_name,
                        "vectors": {
                            qdrant.clip_vector_name: qdrant.clip_dimension,
                            qdrant.dino_vector_name: qdrant.dino_dimension,
                        },
                        "datatype": qdrant.datatype,
                        "require_sfw": qdrant.require_sfw,
                        "require_bluesky_accept": qdrant.require_bluesky_accept,
                        "model_bundle_version": model_bundle_version(app.models, qdrant),
                    },
                    sort_keys=True,
                )
            )
            return

        if args.command == "eligibility":
            with connect(app.database_path) as connection:
                init_db(connection)
                row = connection.execute(
                    """
                    SELECT COUNT(*) AS artworks,
                           SUM(CASE WHEN validated = 1 THEN 1 ELSE 0 END) AS validated,
                           SUM(CASE WHEN is_sfw = 1 THEN 1 ELSE 0 END) AS safe,
                           SUM(CASE WHEN is_sfw = 0 THEN 1 ELSE 0 END) AS unsafe,
                           SUM(CASE WHEN is_sfw IS NULL THEN 1 ELSE 0 END) AS safety_unknown,
                           SUM(CASE WHEN demo_eligible = 1 THEN 1 ELSE 0 END)
                               AS demo_eligible,
                           SUM(CASE WHEN validated = 1 AND is_sfw = 1
                                         AND demo_eligible = 1
                                    THEN 1 ELSE 0 END) AS policy_eligible
                      FROM artworks
                    """
                ).fetchone()
            print(json.dumps(dict(row), sort_keys=True))
            return

        if args.command == "set-policy":
            safety = {"safe": 1, "unsafe": 0, "unknown": None}.get(args.safety)
            assignments: list[str] = []
            values: list[object] = []
            if args.safety is not None:
                assignments.append("is_sfw = ?")
                values.append(safety)
            if args.demo_eligible is not None:
                assignments.append("demo_eligible = ?")
                values.append(int(args.demo_eligible))
            if not assignments:
                raise ValueError("set-policy requires --safety or a demo eligibility flag")
            with connect(app.database_path) as connection:
                init_db(connection)
                placeholders = ", ".join("?" for _ in args.artwork_id)
                cursor = connection.execute(
                    f"""
                    UPDATE artworks
                       SET {", ".join(assignments)}
                     WHERE artwork_id IN ({placeholders})
                    """,
                    (*values, *args.artwork_id),
                )
                connection.commit()
            print(
                json.dumps(
                    {"requested": len(set(args.artwork_id)), "updated": cursor.rowcount},
                    sort_keys=True,
                )
            )
            return

        client = build_qdrant_client(qdrant)
        if args.command == "init":
            result = ensure_qdrant_collection(client, qdrant, app.models).to_dict()
            result["alias_promoted"] = (
                promote_qdrant_alias(client, qdrant) if args.promote else False
            )
            print(json.dumps(result, sort_keys=True))
            return

        if args.command == "status":
            ensure = ensure_qdrant_collection(client, qdrant, app.models)
            aliases = {
                alias.alias_name: alias.collection_name for alias in client.get_aliases().aliases
            }
            print(
                json.dumps(
                    {
                        **ensure.to_dict(),
                        "alias_target": aliases.get(qdrant.alias_name),
                        "point_count": int(client.count(qdrant.collection_name, exact=True).count),
                    },
                    sort_keys=True,
                )
            )
            return

        if args.command == "sync":
            with connect(app.database_path) as connection:
                result = sync_qdrant_from_sqlite(
                    connection,
                    app,
                    qdrant,
                    client,
                    force=args.force,
                    prune=not args.no_prune,
                    promote=not args.no_promote,
                )
            print(json.dumps(result.to_dict(), sort_keys=True))
            return

        if args.command == "evaluate-ann":
            with connect(app.database_path) as connection:
                result = evaluate_qdrant_ann(
                    connection,
                    app,
                    qdrant,
                    client,
                    sample_size=args.sample_size,
                    top_k=args.top_k,
                    seed=args.seed,
                )
            print(json.dumps(result.to_dict(), sort_keys=True))
            return

        if args.command == "search-artwork":
            with connect(app.database_path) as connection:
                init_db(connection)
                hits = QdrantSearchService(client, qdrant).search_by_artwork(
                    connection,
                    app,
                    args.artwork_id,
                    top_k=args.top_k,
                    include_query_artist=not args.exclude_query_artist,
                    use_patch_rerank=not args.no_patch_rerank,
                )
            print(json.dumps([hit.to_dict() for hit in hits], sort_keys=True))
            return

        if args.command == "search-text":
            from artsearch.embed.models import HuggingFaceEmbeddingProvider

            vector = HuggingFaceEmbeddingProvider(app).embed_texts([args.text])[0]
            hits = QdrantSearchService(client, qdrant).search_by_clip_vector(
                vector,
                top_k=args.top_k,
            )
            print(json.dumps([hit.to_dict() for hit in hits], sort_keys=True))
            return

        raise AssertionError(f"unhandled command: {args.command}")
    except (OSError, QdrantIntegrationError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    finally:
        if client is not None:
            client.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish and query the ArtSearch CLIP/DINO Qdrant serving index."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    config = subparsers.add_parser(
        "config",
        help="Print the non-secret resolved vector-index contract.",
    )
    _add_config_paths(config)

    eligibility = subparsers.add_parser(
        "eligibility",
        help="Report safety and explicit alpha-demo eligibility in canonical SQLite.",
    )
    _add_config_paths(eligibility)

    policy = subparsers.add_parser(
        "set-policy",
        help="Explicitly set safety/demo policy for named artwork IDs.",
    )
    _add_config_paths(policy)
    policy.add_argument("--artwork-id", action="append", required=True)
    policy.add_argument("--safety", choices=["safe", "unsafe", "unknown"])
    demo = policy.add_mutually_exclusive_group()
    demo.add_argument(
        "--demo-eligible",
        dest="demo_eligible",
        action="store_true",
    )
    demo.add_argument(
        "--demo-ineligible",
        dest="demo_eligible",
        action="store_false",
    )
    policy.set_defaults(demo_eligible=None)

    initialize = subparsers.add_parser(
        "init",
        help="Create or validate the physical collection and payload indexes.",
    )
    _add_config_paths(initialize)
    initialize.add_argument(
        "--promote",
        action="store_true",
        help="Point the stable alias at this collection even if it is empty.",
    )

    status = subparsers.add_parser(
        "status",
        help="Inspect collection compatibility, alias target, and point count.",
    )
    _add_config_paths(status)

    sync = subparsers.add_parser(
        "sync",
        help="Idempotently reconcile eligible current SQLite embeddings into Qdrant.",
    )
    _add_config_paths(sync)
    sync.add_argument("--force", action="store_true", help="Re-upsert every eligible point.")
    sync.add_argument(
        "--no-prune",
        action="store_true",
        help="Do not delete points that are no longer eligible.",
    )
    sync.add_argument(
        "--no-promote",
        action="store_true",
        help="Do not atomically move the stable alias after reconciliation.",
    )

    evaluate = subparsers.add_parser(
        "evaluate-ann",
        help="Compare HNSW results with exact Qdrant search for both named vectors.",
    )
    _add_config_paths(evaluate)
    evaluate.add_argument("--sample-size", type=int, default=50)
    evaluate.add_argument("--top-k", type=int, default=20)
    evaluate.add_argument("--seed", type=int, default=0)

    artwork = subparsers.add_parser(
        "search-artwork",
        help="Run CLIP subject + DINO global RRF and optional exact patch reranking.",
    )
    _add_config_paths(artwork)
    artwork.add_argument("artwork_id")
    artwork.add_argument("--top-k", type=int, default=20)
    artwork.add_argument("--exclude-query-artist", action="store_true")
    artwork.add_argument("--no-patch-rerank", action="store_true")

    text = subparsers.add_parser(
        "search-text",
        help="Encode a text query with CLIP and search the semantic named vector.",
    )
    _add_config_paths(text)
    text.add_argument("text")
    text.add_argument("--top-k", type=int, default=20)
    return parser


def _add_config_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--app-config", default=DEFAULT_APP_CONFIG)
    parser.add_argument("--production-config", default=DEFAULT_PRODUCTION_CONFIG)


if __name__ == "__main__":
    main()
