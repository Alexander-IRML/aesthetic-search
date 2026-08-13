from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from airflow.sdk import DAG, Param, get_current_context, task


PROJECT_ROOT = Path(os.environ.get("ARTSEARCH_PROJECT_ROOT", "/opt/artsearch")).resolve()
INTAKE_SCHEDULE = os.environ.get("ARTSEARCH_INTAKE_SCHEDULE") or None


def _project_path(value: str) -> Path:
    path = Path(value)
    resolved = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"Airflow path must remain under {PROJECT_ROOT}: {value}")
    return resolved


def _safe_run_name(run_id: str) -> str:
    readable = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id).strip(".-")[:80] or "run"
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:10]
    return f"{readable}-{digest}"


def _last_json_object(output: str) -> dict[str, object] | None:
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


with DAG(
    dag_id="artsearch_bluesky_intake",
    description="Collect, filter, import, manifest, and durably publish Bluesky artwork.",
    schedule=INTAKE_SCHEDULE,
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "artsearch",
        "retries": 2,
        "retry_delay": timedelta(minutes=5),
    },
    params={
        "actors_file": Param("config/bluesky_safe_pilot.local.txt", type="string"),
        "bluesky_config": Param("configs/bluesky.default.toml", type="string"),
        "filter_config": Param("configs/artwork_filter.default.toml", type="string"),
        "prompt_config": Param("configs/artwork_filter.prompts.v1.toml", type="string"),
        "app_config": Param("config/config.yaml", type="string"),
        "production_config": Param("configs/production.default.toml", type="string"),
        "run_root": Param("data/production/runs", type="string"),
        "max_pages": Param(1, type="integer", minimum=1, maximum=100),
        "page_limit": Param(100, type="integer", minimum=1, maximum=100),
        "feed_filter": Param("posts_with_media", type="string"),
        "deterministic_only": Param(False, type="boolean"),
        "fail_on_any_errors": Param(False, type="boolean"),
        "max_actor_error_rate": Param(0.05, type="number", minimum=0.0, maximum=1.0),
        "max_item_error_rate": Param(0.02, type="number", minimum=0.0, maximum=1.0),
    },
    render_template_as_native_obj=True,
    tags=["artsearch", "bluesky", "intake", "polars", "s3"],
) as dag:

    @task
    def prepare_run() -> dict[str, str]:
        context = get_current_context()
        params = context["params"]
        run_dir = _project_path(str(params["run_root"])) / _safe_run_name(str(context["run_id"]))
        run_dir.mkdir(parents=True, exist_ok=True)
        return {
            "run_id": str(context["run_id"]),
            "run_dir": str(run_dir),
            "candidates": str(run_dir / "candidates.jsonl"),
            "decisions": str(run_dir / "decisions.jsonl"),
            "checkpoints": str(run_dir / "actor-checkpoints.jsonl"),
            "manifest": str(run_dir / "corpus-manifest.parquet"),
            "metrics": str(run_dir / "intake-metrics.parquet"),
            "bundle": str(run_dir / "run-bundle.json"),
        }

    @task(execution_timeout=timedelta(hours=12))
    def collect_and_filter(run: dict[str, str]) -> dict[str, object]:
        params = get_current_context()["params"]
        command = [
            "artsearch-bluesky-pipeline",
            "run-authors",
            "--actors-file",
            str(_project_path(str(params["actors_file"]))),
            "--bluesky-config",
            str(_project_path(str(params["bluesky_config"]))),
            "--filter-config",
            str(_project_path(str(params["filter_config"]))),
            "--prompt-config",
            str(_project_path(str(params["prompt_config"]))),
            "--app-config",
            str(_project_path(str(params["app_config"]))),
            "--candidates-output",
            run["candidates"],
            "--decisions-output",
            run["decisions"],
            "--checkpoint",
            run["checkpoints"],
            "--overwrite-decisions",
            "--no-review-download",
            "--max-pages",
            str(params["max_pages"]),
            "--limit",
            str(params["page_limit"]),
            "--feed-filter",
            str(params["feed_filter"]),
        ]
        if bool(params["deterministic_only"]):
            command.append("--deterministic-only")

        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.stderr:
            print(completed.stderr, end="")
        summary = _last_json_object(completed.stdout)
        if completed.returncode != 0:
            candidate_count = int(summary.get("candidates", 0)) if summary else 0
            actor_count = int(summary.get("actors_started", 0)) if summary else 0
            actor_errors = int(summary.get("actor_errors", 0)) if summary else 0
            item_errors = (
                int(summary.get("classification_errors", 0)) + int(summary.get("route_errors", 0))
                if summary
                else 0
            )
            tolerable_item_errors = (
                summary is not None
                and actor_count > 0
                and actor_errors / actor_count <= float(params["max_actor_error_rate"])
                and candidate_count > 0
                and item_errors / candidate_count <= float(params["max_item_error_rate"])
                and not bool(params["fail_on_any_errors"])
            )
            if not tolerable_item_errors:
                detail = completed.stderr.strip()[-2000:] or completed.stdout.strip()[-2000:]
                raise RuntimeError(f"Bluesky pipeline exited {completed.returncode}: {detail}")
            print("Pipeline completed with isolated candidate or route errors.")
        if summary is None:
            raise RuntimeError("Bluesky pipeline did not emit its JSON summary")
        return {**run, "pipeline_summary": summary}

    @task(execution_timeout=timedelta(hours=4))
    def publish_originals(run: dict[str, object]) -> dict[str, object]:
        from artsearch.ingest.config import load_config
        from artsearch.production.config import load_production_config
        from artsearch.production.object_store import build_object_store
        from artsearch.production.publisher import (
            candidate_ids_from_jsonl,
            publish_corpus_originals,
        )

        params = get_current_context()["params"]
        production = load_production_config(_project_path(str(params["production_config"])))
        app = load_config(_project_path(str(params["app_config"])))
        corpus = publish_corpus_originals(
            app.database_path,
            app.root_dir,
            build_object_store(production.object_store),
            candidate_ids=candidate_ids_from_jsonl(
                str(run["decisions"]),
                required_decision="accept",
            ),
        )
        if corpus.failed or corpus.missing:
            raise RuntimeError(
                "accepted-original publication was incomplete: "
                f"failed={corpus.failed}, missing={corpus.missing}; "
                f"samples={list(corpus.errors[:5])}"
            )
        return {**run, "corpus_publish": corpus.to_dict()}

    @task(execution_timeout=timedelta(hours=12))
    def generate_retrieval_embeddings(run: dict[str, object]) -> dict[str, object]:
        from artsearch.embed.pipeline import generate_embeddings

        params = get_current_context()["params"]
        summary = generate_embeddings(_project_path(str(params["app_config"])))
        if summary["errors"]:
            raise RuntimeError(
                f"retrieval embedding generation had isolated errors: {summary['errors']}"
            )
        return {**run, "embedding_summary": summary}

    @task(execution_timeout=timedelta(hours=4))
    def sync_qdrant_index(run: dict[str, object]) -> dict[str, object]:
        from artsearch.ingest.config import load_config
        from artsearch.ingest.db import connect
        from artsearch.production.config import load_production_config
        from artsearch.retrieval.qdrant import (
            build_qdrant_client,
            sync_qdrant_from_sqlite,
        )

        params = get_current_context()["params"]
        production = load_production_config(_project_path(str(params["production_config"])))
        if not production.qdrant.enabled:
            return {**run, "qdrant_summary": {"enabled": False, "skipped": True}}
        app = load_config(_project_path(str(params["app_config"])))
        client = build_qdrant_client(production.qdrant)
        try:
            with connect(app.database_path) as connection:
                result = sync_qdrant_from_sqlite(
                    connection,
                    app,
                    production.qdrant,
                    client,
                )
        finally:
            client.close()
        return {**run, "qdrant_summary": {"enabled": True, **result.to_dict()}}

    @task
    def build_data_products(run: dict[str, object]) -> dict[str, object]:
        from artsearch.ingest.config import load_config
        from artsearch.production.manifests import build_corpus_manifest, build_intake_metrics

        params = get_current_context()["params"]
        app = load_config(_project_path(str(params["app_config"])))
        manifest = build_corpus_manifest(
            str(run["candidates"]),
            str(run["decisions"]),
            str(run["manifest"]),
            database_path=app.database_path,
        )
        metrics = build_intake_metrics(str(run["manifest"]), str(run["metrics"]))
        return {
            **run,
            "manifest_summary": manifest.to_dict(),
            "metrics_summary": metrics.to_dict(),
        }

    @task(execution_timeout=timedelta(hours=4))
    def publish_run(run: dict[str, object]) -> dict[str, object]:
        from artsearch.production.config import load_production_config
        from artsearch.production.object_store import (
            build_object_store,
            content_addressed_key,
            file_identity,
        )

        params = get_current_context()["params"]
        production = load_production_config(_project_path(str(params["production_config"])))
        store = build_object_store(production.object_store)

        artifact_namespaces = {
            "candidates": "runs/candidates",
            "decisions": "runs/decisions",
            "manifest": f"{production.manifests.object_prefix}/corpus",
            "metrics": f"{production.manifests.object_prefix}/metrics",
        }
        artifact_refs: dict[str, dict[str, object]] = {}
        for name, namespace in artifact_namespaces.items():
            source = Path(str(run[name]))
            digest, _ = file_identity(source)
            key = content_addressed_key(namespace, digest, suffix=source.suffix)
            artifact_refs[name] = store.put_file(
                source,
                key,
                expected_sha256=digest,
            ).to_dict()

        bundle = {
            "bundle_version": "artsearch-intake-run-v1",
            "run_id": run["run_id"],
            "production_config_version": production.version,
            "production_config_hash": production.config_hash,
            "pipeline_summary": run["pipeline_summary"],
            "manifest_summary": run["manifest_summary"],
            "metrics_summary": run["metrics_summary"],
            "embedding_summary": run["embedding_summary"],
            "qdrant_summary": run["qdrant_summary"],
            "artifacts": artifact_refs,
            "corpus_publish": run["corpus_publish"],
        }
        bundle_path = Path(str(run["bundle"]))
        bundle_path.write_text(
            json.dumps(bundle, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        bundle_digest, _ = file_identity(bundle_path)
        bundle_ref = store.put_file(
            bundle_path,
            content_addressed_key("runs/bundles", bundle_digest, suffix=".json"),
            expected_sha256=bundle_digest,
        )
        return {
            "run_id": run["run_id"],
            "bundle_uri": bundle_ref.uri,
            "manifest_uri": artifact_refs["manifest"]["uri"],
            "accepted_published": run["corpus_publish"]["published"],
            "accepted_unchanged": run["corpus_publish"]["unchanged"],
            "qdrant_points": run["qdrant_summary"].get("remote_count", 0),
        }

    collected = collect_and_filter(prepare_run())
    published = publish_originals(collected)
    embedded = generate_retrieval_embeddings(published)
    indexed = sync_qdrant_index(embedded)
    publish_run(build_data_products(indexed))
