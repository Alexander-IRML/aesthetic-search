from __future__ import annotations

import argparse
import json
from pathlib import Path

from artsearch.production.config import load_production_config
from artsearch.production.manifests import (
    build_corpus_manifest,
    build_intake_metrics,
    manifest_summary,
)
from artsearch.production.object_store import (
    build_object_store,
    content_addressed_key,
    file_identity,
    object_ref_json,
)
from artsearch.production.publisher import candidate_ids_from_jsonl, publish_corpus_originals


DEFAULT_CONFIG = "configs/production.default.toml"


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "build-manifest":
            result = build_corpus_manifest(
                args.candidates,
                args.decisions,
                args.output,
                database_path=args.database,
            )
            print(json.dumps(result.to_dict(), sort_keys=True))
            return
        if args.command == "build-metrics":
            result = build_intake_metrics(args.manifest, args.output)
            print(json.dumps(result.to_dict(), sort_keys=True))
            return
        if args.command == "manifest-summary":
            print(json.dumps(manifest_summary(args.manifest), sort_keys=True))
            return

        config = load_production_config(args.config)
        store = build_object_store(config.object_store)
        if args.command == "publish-file":
            source = Path(args.path)
            key = args.key
            if key is None:
                digest, _ = file_identity(source)
                key = content_addressed_key(args.namespace, digest, suffix=source.suffix)
            ref = store.put_file(source, key)
            print(object_ref_json(ref))
            return
        if args.command == "fetch-file":
            print(object_ref_json(store.get_file(args.key, args.output)))
            return
        if args.command == "delete-object":
            print(json.dumps({"key": args.key, "deleted": store.delete(args.key)}))
            return
        if args.command == "publish-corpus":
            candidate_ids = None
            if args.decisions:
                candidate_ids = candidate_ids_from_jsonl(
                    args.decisions,
                    required_decision="accept",
                )
            elif args.candidates:
                candidate_ids = candidate_ids_from_jsonl(args.candidates)
            result = publish_corpus_originals(
                args.database,
                args.project_root,
                store,
                namespace=args.namespace,
                candidate_ids=candidate_ids,
            )
            print(json.dumps(result.to_dict(), sort_keys=True))
            return
        raise AssertionError(f"unhandled command: {args.command}")
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build ArtSearch Parquet data products and publish immutable objects."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser(
        "build-manifest",
        help="Join the latest candidate and decision evidence into Parquet with Polars.",
    )
    manifest.add_argument("--candidates", required=True)
    manifest.add_argument("--decisions", required=True)
    manifest.add_argument("--output", required=True)
    manifest.add_argument(
        "--database",
        help="Optional SQLite database used to attach durable accepted-original object URIs.",
    )

    metrics = subparsers.add_parser(
        "build-metrics",
        help="Aggregate a corpus manifest into daily intake metrics with Polars.",
    )
    metrics.add_argument("--manifest", required=True)
    metrics.add_argument("--output", required=True)

    summary = subparsers.add_parser(
        "manifest-summary",
        help="Print compact decision and eligibility counts from a Parquet manifest.",
    )
    summary.add_argument("--manifest", required=True)

    publish = subparsers.add_parser("publish-file", help="Idempotently upload one object.")
    _add_object_config(publish)
    publish.add_argument("--path", required=True)
    publish.add_argument("--key")
    publish.add_argument("--namespace", default="artifacts")

    fetch = subparsers.add_parser("fetch-file", help="Download and verify one object.")
    _add_object_config(fetch)
    fetch.add_argument("--key", required=True)
    fetch.add_argument("--output", required=True)

    delete = subparsers.add_parser("delete-object", help="Delete one exact object key.")
    _add_object_config(delete)
    delete.add_argument("--key", required=True)

    corpus = subparsers.add_parser(
        "publish-corpus",
        help="Publish uncheckpointed accepted originals and record their object URIs.",
    )
    _add_object_config(corpus)
    corpus.add_argument("--database", default="data/artsearch.db")
    corpus.add_argument("--project-root", default=".")
    corpus.add_argument("--namespace", default="corpus/originals")
    scope = corpus.add_mutually_exclusive_group()
    scope.add_argument(
        "--candidates",
        help="Restrict a repair to candidate IDs in one candidate JSONL.",
    )
    scope.add_argument(
        "--decisions",
        help="Publish only ACCEPT candidate IDs in one run's decision JSONL.",
    )
    return parser


def _add_object_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=DEFAULT_CONFIG)
