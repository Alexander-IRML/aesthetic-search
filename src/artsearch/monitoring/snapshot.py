from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from math import log2
import os
from pathlib import Path
import sqlite3
from statistics import fmean
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from sqlalchemy.exc import SQLAlchemyError

from artsearch import __version__
from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.prompt_bank import load_prompt_bank
from artsearch.ingest.config import AppConfig, load_config
from artsearch.monitoring.config import MonitoringConfig, load_monitoring_config
from artsearch.monitoring.review import evaluate_filter_labels_from_database
from artsearch.postgres.database import PostgresConfigurationError, build_engine
from artsearch.postgres.metrics import ProductMetricsRepository, empty_product_metrics
from artsearch.production.config import ProductionConfig, load_production_config
from artsearch.production.scheduler import load_scheduler_config
from artsearch.retrieval.evaluation import (
    evaluate_retrieval_judgments,
    load_retrieval_judgments,
)


@dataclass(frozen=True)
class MonitoringInputs:
    app_config_path: Path = Path("config/config.yaml")
    filter_config_path: Path = Path("configs/artwork_filter.default.toml")
    prompt_config_path: Path = Path("configs/artwork_filter.prompts.v1.toml")
    production_config_path: Path = Path("configs/production.default.toml")
    monitoring_config_path: Path = Path("configs/model_dashboard.default.toml")
    decisions_paths: tuple[Path, ...] = ()
    retrieval_judgment_paths: tuple[Path, ...] = ()
    filter_evaluation_path: Path | None = None
    ann_evaluation_path: Path | None = None
    filter_label_path: Path = Path("data/filter/labels/dashboard.jsonl")
    review_judgment_path: Path = Path("data/retrieval/judgments-dashboard.jsonl")
    review_annotator: str = "local-reviewer"
    probe_qdrant: bool = True


def build_monitoring_snapshot(inputs: MonitoringInputs | None = None) -> dict[str, Any]:
    """Build one read-only, auditable view of current model and data health."""

    resolved = inputs or MonitoringInputs()
    generated_at = datetime.now(timezone.utc)
    app = load_config(resolved.app_config_path)
    filter_config = load_artwork_filter_config(resolved.filter_config_path)
    prompt_bank = load_prompt_bank(resolved.prompt_config_path)
    production = load_production_config(resolved.production_config_path)
    monitoring = load_monitoring_config(resolved.monitoring_config_path)
    sources = _resolve_sources(app.root_dir, resolved)

    errors: list[dict[str, str]] = []
    database = _database_snapshot(app, production, errors)
    filtering = _filter_snapshot(
        sources["decisions"],
        filter_config,
        monitoring,
        errors,
    )
    saved_filter_evaluation = _load_optional_json(
        sources["filter_evaluation"],
        "filter_evaluation",
        errors,
    )
    live_filter_evaluation = _live_filter_evaluation(
        app.database_path,
        sources["filter_labels"],
        errors,
    )
    filter_evaluation = live_filter_evaluation or saved_filter_evaluation
    retrieval = _retrieval_snapshot(sources["retrieval_judgments"], errors)
    ann_evaluation = _load_optional_json(
        sources["ann_evaluation"],
        "ann_evaluation",
        errors,
    )
    product = _product_metrics_snapshot(
        production,
        monitoring,
        ann_evaluation,
    )
    smoke = _load_optional_json(sources["siglip_smoke"], "siglip_smoke", errors)
    qdrant = _qdrant_snapshot(
        production,
        database,
        timeout_seconds=monitoring.qdrant_probe_timeout_seconds,
        probe=resolved.probe_qdrant,
    )
    operations = {
        **database.pop("operations"),
        "daily_intake": filtering["daily"],
        "latest_decision_at": filtering["latest_processed_at"],
        "decision_age_days": filtering["age_days"],
        "scheduler": _scheduler_snapshot(app.root_dir, errors),
    }
    operations["pipeline_success"] = _pipeline_success_snapshot(
        database,
        filtering,
        qdrant,
        operations,
        product,
    )
    alerts = _build_alerts(
        database,
        filtering,
        filter_evaluation,
        retrieval,
        ann_evaluation,
        smoke,
        qdrant,
        product,
        monitoring,
        errors,
    )
    level = _overall_level(alerts)
    measured = _evidence_coverage(filter_evaluation, retrieval, ann_evaluation)

    return {
        "schema_version": "1.1",
        "generated_at": generated_at.isoformat(),
        "application_version": __version__,
        "monitoring": {
            "config_version": monitoring.version,
            "config_hash": monitoring.config_hash,
            "refresh_seconds": monitoring.refresh_seconds,
            "overall_level": level,
            "evidence_coverage": measured,
            "targets": monitoring.targets.model_dump(mode="json"),
        },
        "alerts": alerts,
        "corpus": database["corpus"],
        "embeddings": database["embeddings"],
        "filtering": filtering,
        "filter_evaluation": filter_evaluation,
        "retrieval": retrieval,
        "ann_evaluation": ann_evaluation,
        "qdrant": qdrant,
        "product": product,
        "operations": operations,
        "siglip_smoke": smoke,
        "lineage": {
            "clip": {
                "model_id": app.models.clip_model_name,
                "revision": app.models.clip_model_version,
                "dimension": production.qdrant.clip_dimension,
            },
            "csd": {
                "model_id": app.models.csd_model_name,
                "revision": app.models.csd_model_version,
                "dimension": production.qdrant.csd_dimension,
            },
            "siglip": {
                "model_id": filter_config.model.model_id,
                "revision": filter_config.model.revision or "unpinned",
                "prompt_version": prompt_bank.version,
                "prompt_hash": prompt_bank.prompt_hash,
                "config_hash": filter_config.config_hash,
                "automatic_accept_enabled": filter_config.policy.automatic_accept_enabled,
            },
            "qdrant": {
                "collection": production.qdrant.collection_name,
                "alias": production.qdrant.alias_name,
                "datatype": production.qdrant.datatype,
            },
        },
        "sources": {key: _source_labels(app.root_dir, value) for key, value in sources.items()},
    }


def _resolve_sources(root: Path, inputs: MonitoringInputs) -> dict[str, Any]:
    decisions = tuple(_resolve(root, path) for path in inputs.decisions_paths)
    if not decisions:
        default = root / "data/filter/decisions.jsonl"
        decisions = (default,) if default.exists() else ()
    judgments = tuple(_resolve(root, path) for path in inputs.retrieval_judgment_paths)
    if not judgments:
        judgments = _discover(
            root, ("data/retrieval/judgments*.jsonl", "data/retrieval/**/judgments*.jsonl")
        )
    review_judgments = _resolve(root, inputs.review_judgment_path)
    if review_judgments.exists() and review_judgments not in judgments:
        judgments = (*judgments, review_judgments)
    return {
        "decisions": decisions,
        "retrieval_judgments": judgments,
        "filter_labels": _resolve(root, inputs.filter_label_path),
        "filter_evaluation": _explicit_or_latest(
            root,
            inputs.filter_evaluation_path,
            ("data/filter/reports/evaluation*.json",),
        ),
        "ann_evaluation": _explicit_or_latest(
            root,
            inputs.ann_evaluation_path,
            ("data/retrieval/qdrant_ann*.json", "data/retrieval/**/qdrant_ann*.json"),
        ),
        "siglip_smoke": _explicit_or_latest(
            root,
            None,
            ("data/filter/reports/*smoke*.json",),
        ),
    }


def _live_filter_evaluation(
    database_path: Path,
    labels_path: Path,
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    if not labels_path.exists() or not database_path.exists():
        return None
    try:
        return evaluate_filter_labels_from_database(database_path, labels_path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        errors.append(
            {
                "area": "filter_evaluation",
                "message": f"Live human-label evaluation failed: {exc}",
            }
        )
        return None


def _database_snapshot(
    app: AppConfig,
    production: ProductionConfig,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    if not app.database_path.exists():
        errors.append(
            {
                "area": "database",
                "message": "Canonical SQLite database does not exist.",
            }
        )
        return _empty_database_snapshot()
    try:
        connection = sqlite3.connect(
            f"file:{app.database_path}?mode=ro",
            uri=True,
            timeout=2.0,
        )
        connection.row_factory = sqlite3.Row
        with connection:
            corpus = _corpus_metrics(connection)
            embeddings = _embedding_metrics(connection, app, production, corpus)
            operations = _operation_metrics(connection)
            corpus["serving_eligible"] = _serving_eligible_count(
                connection,
                app,
                production,
            )
        connection.close()
    except sqlite3.Error as exc:
        errors.append({"area": "database", "message": f"SQLite read failed: {exc}"})
        return _empty_database_snapshot()
    return {"corpus": corpus, "embeddings": embeddings, "operations": operations}


def _corpus_metrics(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT COUNT(*) AS artworks,
               COUNT(DISTINCT artist_id) AS artists,
               SUM(validated = 1) AS validated,
               SUM(validated = 1 AND processed_path IS NOT NULL
                   AND duplicate_of IS NULL) AS searchable,
               SUM(is_sfw = 1) AS safe,
               SUM(is_sfw = 0) AS unsafe,
               SUM(is_sfw IS NULL) AS safety_unknown,
               SUM(demo_eligible = 1) AS demo_eligible,
               SUM(duplicate_of IS NOT NULL) AS duplicates
          FROM artworks
        """
    ).fetchone()
    return {
        **_numeric_row(row),
        "by_source": _group_counts(connection, "artworks", "source_platform"),
        "by_review_status": _group_counts(connection, "artworks", "review_status"),
    }


def _embedding_metrics(
    connection: sqlite3.Connection,
    app: AppConfig,
    production: ProductionConfig,
    corpus: dict[str, Any],
) -> dict[str, Any]:
    qdrant = production.qdrant
    row = connection.execute(
        """
        SELECT COUNT(*) AS rows,
               SUM(clip_vector IS NOT NULL) AS clip_any,
               SUM(csd_style IS NOT NULL) AS csd_any,
               SUM(clip_vector IS NOT NULL AND clip_dim = ?
                   AND model_name_clip = ? AND model_version_clip = ?) AS clip_current,
               SUM(csd_style IS NOT NULL AND csd_style_dim = ?
                   AND model_name_csd = ? AND model_version_csd = ?) AS csd_current,
               SUM(clip_vector IS NOT NULL AND clip_dim = ?
                   AND model_name_clip = ? AND model_version_clip = ?
                   AND csd_style IS NOT NULL AND csd_style_dim = ?
                   AND model_name_csd = ? AND model_version_csd = ?) AS complete_current,
               SUM(LENGTH(clip_vector)) AS clip_bytes,
               SUM(LENGTH(csd_style)) AS csd_bytes
          FROM embeddings
        """,
        (
            qdrant.clip_dimension,
            app.models.clip_model_name,
            app.models.clip_model_version,
            qdrant.csd_dimension,
            app.models.csd_model_name,
            app.models.csd_model_version,
            qdrant.clip_dimension,
            app.models.clip_model_name,
            app.models.clip_model_version,
            qdrant.csd_dimension,
            app.models.csd_model_name,
            app.models.csd_model_version,
        ),
    ).fetchone()
    metrics = _numeric_row(row)
    denominator = int(corpus.get("searchable", 0))
    complete = int(metrics["complete_current"])
    metrics.update(
        {
            "current_complete": complete,
            "global_coverage": _ratio(complete, denominator),
            "searchable_denominator": denominator,
            "versions": _embedding_versions(connection),
            "qdrant_ledger": int(
                connection.execute(
                    "SELECT COUNT(*) FROM vector_index_points WHERE collection_name = ?",
                    (qdrant.collection_name,),
                ).fetchone()[0]
            ),
        }
    )
    return metrics


def _operation_metrics(connection: sqlite3.Connection) -> dict[str, Any]:
    recent_runs = [
        dict(row)
        for row in connection.execute(
            """
            SELECT run_id, script_name, started_at, finished_at,
                   images_processed, images_skipped, errors_count
              FROM runs
             ORDER BY run_id DESC
             LIMIT 20
            """
        )
    ]
    events = _group_counts(connection, "run_events", "level")
    objects = connection.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(byte_size), 0) AS bytes FROM artwork_objects"
    ).fetchone()
    route_statuses = (
        _group_counts(connection, "artwork_filter_routes", "status")
        if _sqlite_table_exists(connection, "artwork_filter_routes")
        else []
    )
    route_counts = {str(item["label"]): int(item["count"]) for item in route_statuses}
    routed = sum(route_counts.values())
    completed_runs = [item for item in recent_runs if item["finished_at"] is not None]
    successful_runs = [item for item in completed_runs if int(item["errors_count"] or 0) == 0]
    return {
        "recent_runs": recent_runs,
        "events_by_level": events,
        "published_objects": int(objects["count"]),
        "published_bytes": int(objects["bytes"]),
        "route_statuses": route_statuses,
        "route_success_rate": _ratio(
            route_counts.get("stored", 0) + route_counts.get("duplicate", 0),
            routed,
        ),
        "recent_run_success_rate": _ratio(len(successful_runs), len(completed_runs)),
        "source_state": _source_state_metrics(connection),
    }


def _source_state_metrics(connection: sqlite3.Connection) -> dict[str, Any]:
    if not _sqlite_table_exists(connection, "bluesky_actor_intake_state"):
        return {
            "tracked_actors": 0,
            "completed_actors": 0,
            "backlogged_actors": 0,
            "actors_with_errors": 0,
            "resumable_cursors": 0,
            "latest_attempt_at": None,
        }
    row = connection.execute(
        """
        SELECT COUNT(*) AS tracked_actors,
               COALESCE(SUM(last_scan_complete = 1), 0) AS completed_actors,
               COALESCE(SUM(last_scan_complete = 0 AND last_error IS NULL), 0)
                   AS backlogged_actors,
               COALESCE(SUM(last_error IS NOT NULL), 0) AS actors_with_errors,
               COALESCE(SUM(resume_cursor IS NOT NULL), 0) AS resumable_cursors,
               MAX(last_attempt_at) AS latest_attempt_at
          FROM bluesky_actor_intake_state
        """
    ).fetchone()
    return {
        "tracked_actors": int(row["tracked_actors"]),
        "completed_actors": int(row["completed_actors"]),
        "backlogged_actors": int(row["backlogged_actors"]),
        "actors_with_errors": int(row["actors_with_errors"]),
        "resumable_cursors": int(row["resumable_cursors"]),
        "latest_attempt_at": row["latest_attempt_at"],
    }


def _scheduler_snapshot(root: Path, errors: list[dict[str, str]]) -> dict[str, Any]:
    config_path = root / "configs/intake_scheduler.default.toml"
    configured: dict[str, Any] = {
        "candidate_target": None,
        "daily_time": None,
        "timezone": None,
        "actor_count": None,
        "last_status": "not_run",
        "last_run_id": None,
        "last_event_at": None,
    }
    if config_path.exists():
        try:
            config, _, project_root = load_scheduler_config(config_path)
            actors_path = (
                config.actors_file
                if config.actors_file.is_absolute()
                else project_root / config.actors_file
            )
            configured.update(
                {
                    "candidate_target": config.candidate_target,
                    "daily_time": config.daily_time,
                    "timezone": config.timezone,
                    "actor_count": _noncomment_line_count(actors_path),
                }
            )
        except (OSError, ValueError) as exc:
            errors.append(
                {"area": "scheduler", "message": f"Scheduler config could not be read: {exc}"}
            )

    event_paths = sorted((root / "data/production/scheduler").glob("*.jsonl"), reverse=True)
    for path in event_paths:
        try:
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        except OSError as exc:
            errors.append({"area": "scheduler", "message": f"Could not read {path.name}: {exc}"})
            continue
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                configured.update(
                    {
                        "last_status": event.get("status", "unknown"),
                        "last_run_id": event.get("run_id"),
                        "last_event_at": event.get("timestamp"),
                    }
                )
                return configured
    return configured


def _noncomment_line_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    return sum(
        1
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _sqlite_table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def _filter_snapshot(
    paths: Sequence[Path],
    filter_config: Any,
    monitoring: MonitoringConfig,
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    history_events = invalid_lines = 0
    for path in paths:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    history_events += 1
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        invalid_lines += 1
                        continue
                    if not isinstance(item, dict) or not item.get("candidate_id"):
                        invalid_lines += 1
                        continue
                    key = (str(item["candidate_id"]), str(item.get("source_cid") or ""))
                    prior = latest.get(key)
                    if prior is None or str(item.get("processed_at") or "") >= str(
                        prior.get("processed_at") or ""
                    ):
                        latest[key] = item
        except OSError as exc:
            errors.append({"area": "filtering", "message": f"Could not read {path.name}: {exc}"})

    rows = list(latest.values())
    durations = _float_values(rows, "duration_ms")
    confidences = _float_values(rows, "confidence")
    scores = _float_values(rows, "final_score")
    margins = [
        float(value)
        for item in rows
        if isinstance(item.get("visual_scores"), dict)
        and _finite((value := item["visual_scores"].get("confidence_margin")))
    ]
    outcomes = Counter(str(item.get("decision") or "unknown") for item in rows)
    artist_counts = Counter(
        str(item.get("author_did") or item.get("author_handle") or "")
        for item in rows
        if item.get("author_did") or item.get("author_handle")
    )
    latest_timestamp = max((str(item.get("processed_at") or "") for item in rows), default="")
    return {
        "history_events": history_events,
        "latest_decisions": len(rows),
        "invalid_lines": invalid_lines,
        "outcomes": _counter_rows(outcomes),
        "outcome_rates": {key: _ratio(value, len(rows)) for key, value in sorted(outcomes.items())},
        "predicted_classes": _counter_rows(
            Counter(str(item.get("predicted_class") or "unknown") for item in rows)
        ),
        "routes": _counter_rows(Counter(str(item.get("route") or "unknown") for item in rows)),
        "models": _counter_rows(
            Counter(
                f"{item.get('model_version') or item.get('model_id') or 'unknown'}@"
                f"{item.get('model_revision') or 'unversioned'}"
                for item in rows
            )
        ),
        "artists": len({str(item.get("author_did")) for item in rows if item.get("author_did")}),
        "artist_concentration": _artist_concentration(artist_counts),
        "latency_ms": _distribution(durations),
        "confidence": _distribution(confidences),
        "final_score": _distribution(scores),
        "margin": _distribution(margins),
        "low_confidence_rate": _ratio(
            sum(
                value < filter_config.thresholds.force_review_below_confidence
                for value in confidences
            ),
            len(confidences),
        ),
        "low_margin_rate": _ratio(
            sum(value < filter_config.thresholds.minimum_margin for value in margins),
            len(margins),
        ),
        "latest_processed_at": latest_timestamp or None,
        "age_days": _age_days(latest_timestamp),
        "daily": _daily_decisions(rows),
        "drift": _filter_drift(rows, monitoring),
        "policy": {
            "automatic_accept_enabled": filter_config.policy.automatic_accept_enabled,
            "accept_score": filter_config.thresholds.accept_score,
            "reject_score": filter_config.thresholds.reject_score,
            "minimum_margin": filter_config.thresholds.minimum_margin,
            "minimum_confidence": filter_config.thresholds.force_review_below_confidence,
        },
    }


def _retrieval_snapshot(
    paths: Sequence[Path],
    errors: list[dict[str, str]],
) -> dict[str, Any]:
    judgments = []
    for path in paths:
        if not path.exists():
            continue
        try:
            judgments.extend(load_retrieval_judgments(path))
        except (OSError, ValueError) as exc:
            errors.append({"area": "retrieval", "message": f"Could not load {path.name}: {exc}"})
    if not judgments:
        return {
            "measured": False,
            "dataset": {
                "history_events": 0,
                "latest_judgments": 0,
                "query_count": 0,
                "corpus_fingerprints": [],
                "mixed_corpora": False,
            },
            "overall": None,
            "per_mode": {},
        }
    report = evaluate_retrieval_judgments(judgments)
    return {"measured": True, **report}


def _qdrant_snapshot(
    production: ProductionConfig,
    database: dict[str, Any],
    *,
    timeout_seconds: float,
    probe: bool,
) -> dict[str, Any]:
    config = production.qdrant
    configured_url = os.environ.get(config.url_env, "").strip() or config.url.strip()
    base = {
        "enabled": config.enabled,
        "endpoint": _safe_endpoint(configured_url),
        "collection": config.collection_name,
        "alias": config.alias_name,
        "datatype": config.datatype,
        "serving_eligible": int(database["corpus"].get("serving_eligible", 0)),
        "ledger_points": int(database["embeddings"].get("qdrant_ledger", 0)),
        "remote_status": "not_probed",
        "remote_points": None,
        "alias_target": None,
        "error": None,
    }
    if not config.enabled:
        return {**base, "remote_status": "disabled"}
    if not probe:
        return base
    if not configured_url:
        return {**base, "remote_status": "misconfigured", "error": "Qdrant URL is missing"}
    api_key = os.environ.get(config.api_key_env, "").strip()
    if config.require_api_key and not api_key:
        return {
            **base,
            "remote_status": "credential_missing",
            "error": f"{config.api_key_env} is not configured",
        }
    headers = {"accept": "application/json"}
    if api_key:
        headers["api-key"] = api_key
    try:
        collection = _http_json(
            f"{configured_url.rstrip('/')}/collections/{quote(config.collection_name)}",
            headers,
            timeout_seconds,
        ).get("result", {})
        aliases = (
            _http_json(
                f"{configured_url.rstrip('/')}/aliases",
                headers,
                timeout_seconds,
            )
            .get("result", {})
            .get("aliases", [])
        )
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
        return {
            **base,
            "remote_status": "offline",
            "error": f"{type(exc).__name__}: {exc}",
        }
    alias_target = next(
        (
            str(item.get("collection_name"))
            for item in aliases
            if item.get("alias_name") == config.alias_name
        ),
        None,
    )
    return {
        **base,
        "remote_status": str(collection.get("status") or "online"),
        "remote_points": _optional_int(collection.get("points_count")),
        "alias_target": alias_target,
    }


def _product_metrics_snapshot(
    production: ProductionConfig,
    monitoring: MonitoringConfig,
    ann_evaluation: dict[str, Any] | None,
) -> dict[str, Any]:
    config = production.postgres
    window_days = monitoring.product_metrics_window_days
    if not config.enabled:
        product = empty_product_metrics(status="disabled", window_days=window_days)
    else:
        engine = None
        try:
            engine = build_engine(config)
            product = ProductMetricsRepository(
                engine,
                window_days=window_days,
            ).snapshot()
        except PostgresConfigurationError as exc:
            product = empty_product_metrics(
                status="credential_missing",
                window_days=window_days,
                error=str(exc),
            )
        except (SQLAlchemyError, OSError) as exc:
            product = empty_product_metrics(
                status="offline",
                window_days=window_days,
                error=f"PostgreSQL metrics query failed ({type(exc).__name__}).",
            )
        finally:
            if engine is not None:
                engine.dispose()
    product["recall_at_100"] = _ann_recall_at_100(ann_evaluation)
    return product


def _ann_recall_at_100(report: dict[str, Any] | None) -> dict[str, Any]:
    metrics = [] if report is None else report.get("metrics", [])
    matching = [
        item
        for item in metrics
        if isinstance(item, dict)
        and int(item.get("top_k") or 0) == 100
        and item.get("mean_recall") is not None
        and item.get("minimum_recall") is not None
    ]
    if not matching:
        return {
            "measured": False,
            "source": "exact_vs_hnsw",
            "query_count": 0,
            "mean_recall": None,
            "minimum_recall": None,
            "vectors": [],
        }
    return {
        "measured": True,
        "source": "exact_vs_hnsw",
        "query_count": max(int(item.get("query_count") or 0) for item in matching),
        "mean_recall": fmean(float(item["mean_recall"]) for item in matching),
        "minimum_recall": min(float(item["minimum_recall"]) for item in matching),
        "vectors": [
            {
                "vector_name": str(item.get("vector_name") or "unknown"),
                "mean_recall": float(item["mean_recall"]),
                "minimum_recall": float(item["minimum_recall"]),
            }
            for item in matching
        ],
    }


def _pipeline_success_snapshot(
    database: dict[str, Any],
    filtering: dict[str, Any],
    qdrant: dict[str, Any],
    operations: dict[str, Any],
    product: dict[str, Any],
) -> dict[str, Any]:
    latest_decisions = int(filtering.get("latest_decisions") or 0)
    decision_error_rate = float(filtering.get("outcome_rates", {}).get("error") or 0.0)
    decision_success = 1.0 - decision_error_rate if latest_decisions else None
    qdrant_points = qdrant.get("remote_points")
    serving_eligible = int(database["corpus"].get("serving_eligible") or 0)
    stage_rates = [
        value
        for value in (
            decision_success,
            operations.get("route_success_rate"),
            database["embeddings"].get("global_coverage"),
            operations.get("recent_run_success_rate"),
        )
        if value is not None
    ]
    return {
        "overall_measured_stage_mean": fmean(stage_rates) if stage_rates else None,
        "filter_decision_success_rate": decision_success,
        "filter_decisions": latest_decisions,
        "routing_success_rate": operations.get("route_success_rate"),
        "route_statuses": operations.get("route_statuses", []),
        "embedding_coverage": database["embeddings"].get("global_coverage"),
        "object_publication_coverage": _ratio(
            int(operations.get("published_objects") or 0),
            int(database["corpus"].get("searchable") or 0),
        ),
        "qdrant_index_coverage": (
            _ratio(int(qdrant_points), serving_eligible) if qdrant_points is not None else None
        ),
        "recent_run_success_rate": operations.get("recent_run_success_rate"),
        "postgres": product.get("pipeline", {}),
    }


def _build_alerts(
    database: dict[str, Any],
    filtering: dict[str, Any],
    filter_evaluation: dict[str, Any] | None,
    retrieval: dict[str, Any],
    ann_evaluation: dict[str, Any] | None,
    smoke: dict[str, Any] | None,
    qdrant: dict[str, Any],
    product: dict[str, Any],
    monitoring: MonitoringConfig,
    errors: Sequence[dict[str, str]],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = [
        _alert(
            f"source.{index}",
            "critical",
            error["area"],
            "Monitoring source could not be read",
            error["message"],
            "Repair the source before trusting downstream metrics.",
        )
        for index, error in enumerate(errors)
    ]
    targets = monitoring.targets
    coverage = database["embeddings"].get("global_coverage")
    if coverage is not None and coverage < targets.embedding_coverage_critical:
        alerts.append(
            _alert(
                "embedding.coverage.critical",
                "critical",
                "embeddings",
                "Global embedding coverage is critically low",
                f"Current CLIP+CSD coverage is {_percent(coverage)}.",
                "Run the embedding worker and inspect failed run events.",
                coverage,
                targets.embedding_coverage_critical,
            )
        )
    elif coverage is not None and coverage < targets.embedding_coverage_warning:
        alerts.append(
            _alert(
                "embedding.coverage.warning",
                "warning",
                "embeddings",
                "Global embedding coverage is below target",
                f"Current CLIP+CSD coverage is {_percent(coverage)}.",
                "Backfill missing current-model embeddings before indexing.",
                coverage,
                targets.embedding_coverage_warning,
            )
        )

    error_rate = filtering["outcome_rates"].get("error", 0.0)
    if error_rate >= targets.filter_error_rate_critical:
        alerts.append(
            _alert(
                "filter.error_rate.critical",
                "critical",
                "filter",
                "Filter error rate is critically high",
                f"{_percent(error_rate)} of latest decisions are ERROR.",
                "Inspect typed download/model failures before the next intake run.",
                error_rate,
                targets.filter_error_rate_critical,
            )
        )
    elif error_rate >= targets.filter_error_rate_warning:
        alerts.append(
            _alert(
                "filter.error_rate.warning",
                "warning",
                "filter",
                "Filter error rate is elevated",
                f"{_percent(error_rate)} of latest decisions are ERROR.",
                "Review error subtypes and retry only transient failures.",
                error_rate,
                targets.filter_error_rate_warning,
            )
        )
    p95_latency = filtering["latency_ms"].get("p95")
    if p95_latency is not None and p95_latency > targets.filter_p95_latency_ms:
        alerts.append(
            _alert(
                "filter.latency.p95",
                "warning",
                "filter",
                "Filter p95 latency exceeds the alpha target",
                f"Observed p95 is {p95_latency:.0f} ms.",
                "Check batch size, device utilization, cache hit rate, and download timing.",
                p95_latency,
                targets.filter_p95_latency_ms,
            )
        )
    if (
        filtering["age_days"] is not None
        and filtering["age_days"] > targets.decision_freshness_days
    ):
        alerts.append(
            _alert(
                "filter.freshness",
                "warning",
                "operations",
                "Decision evidence is stale",
                f"The newest filter decision is {filtering['age_days']:.1f} days old.",
                "Run a small current intake cohort before changing thresholds or models.",
                filtering["age_days"],
                targets.decision_freshness_days,
            )
        )
    _filter_evaluation_alerts(alerts, filter_evaluation, targets)
    _retrieval_alerts(alerts, retrieval, targets)
    _ann_alerts(alerts, ann_evaluation, qdrant, targets)
    _drift_alerts(alerts, filtering, targets)
    _product_alerts(alerts, product, targets)

    safety_unknown = int(database["corpus"].get("safety_unknown", 0))
    if safety_unknown:
        alerts.append(
            _alert(
                "corpus.safety_unknown",
                "warning",
                "policy",
                "Safety review is incomplete",
                f"{safety_unknown} corpus artworks have unknown safety status.",
                "Review safety and demo eligibility explicitly before alpha publication.",
                safety_unknown,
                0,
            )
        )
    if qdrant["enabled"] and qdrant["remote_status"] in {
        "offline",
        "misconfigured",
        "credential_missing",
    }:
        severity = "warning" if qdrant["serving_eligible"] else "info"
        alerts.append(
            _alert(
                "qdrant.connectivity",
                severity,
                "qdrant",
                "Qdrant serving index is not online",
                qdrant["error"] or qdrant["remote_status"],
                "Start the local service or configure the Cloud endpoint before serving search.",
            )
        )
    remote_points = qdrant.get("remote_points")
    if qdrant["remote_status"] in {"red", "grey"}:
        alerts.append(
            _alert(
                "qdrant.collection.unhealthy",
                "critical",
                "qdrant",
                "Qdrant collection reports an unhealthy state",
                f"Collection status is {qdrant['remote_status']}.",
                "Stop serving from this collection and inspect Qdrant cluster health.",
            )
        )
    elif qdrant["remote_status"] == "yellow":
        alerts.append(
            _alert(
                "qdrant.collection.degraded",
                "warning",
                "qdrant",
                "Qdrant collection reports a degraded state",
                "Collection status is yellow.",
                "Inspect optimizer and shard status before promoting a new index.",
            )
        )
    if remote_points is not None and remote_points != qdrant["serving_eligible"]:
        alerts.append(
            _alert(
                "qdrant.count_mismatch",
                "critical",
                "qdrant",
                "Qdrant point count does not match canonical eligibility",
                f"Remote has {remote_points}; canonical policy allows {qdrant['serving_eligible']}.",
                "Run an idempotent Qdrant sync and do not promote a mismatched collection.",
                remote_points,
                qdrant["serving_eligible"],
            )
        )
    if remote_points is not None and qdrant["alias_target"] != qdrant["collection"]:
        alerts.append(
            _alert(
                "qdrant.alias_mismatch",
                "critical",
                "qdrant",
                "Qdrant read alias does not target the configured collection",
                f"Alias target is {qdrant['alias_target'] or 'missing'}.",
                "Promote the verified collection alias before serving retrieval.",
            )
        )
    if qdrant["enabled"] and qdrant["ledger_points"] != qdrant["serving_eligible"]:
        alerts.append(
            _alert(
                "qdrant.ledger_mismatch",
                "warning",
                "qdrant",
                "Qdrant publication ledger is behind canonical eligibility",
                f"Ledger has {qdrant['ledger_points']}; policy allows "
                f"{qdrant['serving_eligible']}.",
                "Run reconciliation and inspect failed publication checkpoints.",
                qdrant["ledger_points"],
                qdrant["serving_eligible"],
            )
        )
    if smoke is not None and not bool(smoke.get("passed")):
        alerts.append(
            _alert(
                "siglip.smoke",
                "critical",
                "filter",
                "SigLIP smoke test failed",
                "Finite-value or batch/single consistency checks did not pass.",
                "Stop intake and inspect model preprocessing or device behavior.",
            )
        )
    return sorted(
        alerts, key=lambda item: (_severity_rank(item["severity"]), item["area"], item["id"])
    )


def _product_alerts(
    alerts: list[dict[str, Any]],
    product: dict[str, Any],
    targets: Any,
) -> None:
    status = str(product.get("status") or "disabled")
    if status not in {"online", "disabled"}:
        alerts.append(
            _alert(
                "search.telemetry.unavailable",
                "warning",
                "search",
                "Online search telemetry is unavailable",
                str(product.get("error") or status),
                "Restore the PostgreSQL runtime connection before evaluating alpha traffic.",
            )
        )
        return
    if status == "disabled":
        return

    search = product.get("search", {})
    p95_latency = search.get("latency_p95_ms")
    if p95_latency is not None and float(p95_latency) > targets.search_p95_latency_ms:
        alerts.append(
            _alert(
                "search.latency.p95",
                "warning",
                "search",
                "Search p95 latency exceeds the alpha target",
                f"Rolling p95 is {float(p95_latency):.0f} ms.",
                "Inspect query encoding, Qdrant, catalog hydration, and queue timing.",
                p95_latency,
                targets.search_p95_latency_ms,
            )
        )
    error_rate = search.get("error_rate")
    if error_rate is not None and float(error_rate) >= targets.search_error_rate_critical:
        alerts.append(
            _alert(
                "search.error_rate.critical",
                "critical",
                "search",
                "Search error rate is critically high",
                f"{_percent(float(error_rate))} of completed attempts failed.",
                "Inspect recent query errors before inviting more alpha traffic.",
                error_rate,
                targets.search_error_rate_critical,
            )
        )
    elif error_rate is not None and float(error_rate) >= targets.search_error_rate_warning:
        alerts.append(
            _alert(
                "search.error_rate.warning",
                "warning",
                "search",
                "Search error rate is elevated",
                f"{_percent(float(error_rate))} of completed attempts failed.",
                "Review error types and latency breakdowns in PostgreSQL telemetry.",
                error_rate,
                targets.search_error_rate_warning,
            )
        )

    feedback = product.get("feedback", {})
    complete_queries = int(feedback.get("fully_judged_queries") or 0)
    if complete_queries < targets.minimum_complete_feedback_queries:
        alerts.append(
            _alert(
                "search.feedback.sample",
                "info",
                "evaluation",
                "Online retrieval feedback is still sparse",
                f"Only {complete_queries} searches have a completely judged top ten.",
                "Rate every displayed result for more searches before tuning retrieval.",
                complete_queries,
                targets.minimum_complete_feedback_queries,
            )
        )
    else:
        precision = feedback.get("precision_at_10")
        if precision is not None and float(precision) < targets.feedback_precision_at_10:
            alerts.append(
                _alert(
                    "search.feedback.precision",
                    "warning",
                    "evaluation",
                    "Online Precision@10 is below target",
                    f"Completely judged searches measure {_percent(float(precision))}.",
                    "Inspect failed results and fusion/component ranks before retuning.",
                    precision,
                    targets.feedback_precision_at_10,
                )
            )
        ndcg = feedback.get("ndcg_at_10")
        if ndcg is not None and float(ndcg) < targets.feedback_ndcg_at_10:
            alerts.append(
                _alert(
                    "search.feedback.ndcg",
                    "warning",
                    "evaluation",
                    "Online nDCG@10 is below target",
                    f"Great/Good/Bad ordering measures {_percent(float(ndcg))}.",
                    "Inspect whether Great matches are ranking above merely Good results.",
                    ndcg,
                    targets.feedback_ndcg_at_10,
                )
            )

    recall = product.get("recall_at_100", {})
    if not recall.get("measured"):
        alerts.append(
            _alert(
                "search.recall100.missing",
                "info",
                "evaluation",
                "Recall@100 has not been measured",
                "No exact-versus-HNSW report at K=100 is available.",
                "Run the Qdrant ANN evaluator at K=100 against a representative query set.",
            )
        )
    pipeline = product.get("pipeline", {})
    pipeline_rates = {
        "filter decisions": pipeline.get("decision_success_rate"),
        "corpus routes": pipeline.get("route_success_rate"),
        "completed runs": pipeline.get("run_success_rate"),
    }
    measured_rates = {
        name: float(value) for name, value in pipeline_rates.items() if value is not None
    }
    if measured_rates:
        weakest_name, weakest_rate = min(measured_rates.items(), key=lambda item: item[1])
        if weakest_rate < targets.pipeline_success_rate_warning:
            alerts.append(
                _alert(
                    "pipeline.success_rate",
                    "warning",
                    "operations",
                    "A pipeline stage is below its success target",
                    f"The weakest rolling stage is {weakest_name} at {_percent(weakest_rate)}.",
                    "Inspect typed stage errors before increasing intake throughput.",
                    weakest_rate,
                    targets.pipeline_success_rate_warning,
                )
            )


def _filter_evaluation_alerts(
    alerts: list[dict[str, Any]],
    report: dict[str, Any] | None,
    targets: Any,
) -> None:
    if report is None:
        alerts.append(
            _alert(
                "filter.evaluation.missing",
                "warning",
                "evaluation",
                "Automatic ACCEPT precision is not measured",
                "No filter evaluation report with human labels was found.",
                "Label a held-out artist-grouped cohort and generate an evaluation report.",
            )
        )
        return
    matched = int(report.get("dataset", {}).get("matched", 0))
    if matched < targets.minimum_filter_labels:
        alerts.append(
            _alert(
                "filter.evaluation.sample",
                "warning",
                "evaluation",
                "Filter evaluation cohort is too small",
                f"Only {matched} labels are matched to exact decision snapshots.",
                "Continue labeling before changing automatic acceptance thresholds.",
                matched,
                targets.minimum_filter_labels,
            )
        )
    metrics = report.get("decision_metrics", {})
    interval = metrics.get("automatic_accept_precision_95ci")
    lower = interval[0] if isinstance(interval, list) and interval else None
    if lower is not None and lower < targets.filter_accept_precision:
        alerts.append(
            _alert(
                "filter.evaluation.precision",
                "critical",
                "evaluation",
                "Automatic ACCEPT precision is below target",
                f"The 95% lower bound is {_percent(float(lower))}.",
                "Keep automatic acceptance disabled and recalibrate on validation labels.",
                lower,
                targets.filter_accept_precision,
            )
        )


def _retrieval_alerts(
    alerts: list[dict[str, Any]],
    retrieval: dict[str, Any],
    targets: Any,
) -> None:
    dataset = retrieval["dataset"]
    judgments = int(dataset.get("latest_judgments", 0))
    queries = int(dataset.get("query_count", 0))
    if (
        judgments < targets.minimum_retrieval_judgments
        or queries < targets.minimum_retrieval_queries
    ):
        alerts.append(
            _alert(
                "retrieval.evaluation.sample",
                "warning",
                "evaluation",
                "Retrieval quality is not sufficiently measured",
                f"Current evidence has {judgments} judgments across {queries} queries.",
                "Export more artist-balanced review sessions before tuning fusion weights.",
                judgments,
                targets.minimum_retrieval_judgments,
            )
        )
        return
    if dataset.get("mixed_corpora"):
        alerts.append(
            _alert(
                "retrieval.evaluation.mixed",
                "critical",
                "evaluation",
                "Retrieval judgments mix corpus fingerprints",
                "Metrics combine outputs generated from different corpus states.",
                "Evaluate each corpus/index version separately before comparison.",
            )
        )
    ensemble = retrieval.get("per_mode", {}).get("ensemble", {})
    p5 = ensemble.get("at_k", {}).get("5", {}).get("judged_precision")
    ndcg = ensemble.get("ndcg")
    if p5 is not None and p5 < targets.retrieval_precision_at_5:
        alerts.append(
            _alert(
                "retrieval.precision_at_5",
                "warning",
                "retrieval",
                "Ensemble judged precision@5 is below target",
                f"Observed judged precision@5 is {_percent(float(p5))}.",
                "Inspect CLIP/CSD rank evidence and hard negatives before changing fusion.",
                p5,
                targets.retrieval_precision_at_5,
            )
        )
    if ndcg is not None and ndcg < targets.retrieval_ndcg:
        alerts.append(
            _alert(
                "retrieval.ndcg",
                "warning",
                "retrieval",
                "Ensemble nDCG is below target",
                f"Observed nDCG is {_percent(float(ndcg))}.",
                "Review CLIP and CSD rank disagreement on judged queries.",
                ndcg,
                targets.retrieval_ndcg,
            )
        )


def _ann_alerts(
    alerts: list[dict[str, Any]],
    report: dict[str, Any] | None,
    qdrant: dict[str, Any],
    targets: Any,
) -> None:
    if report is None:
        severity = "warning" if qdrant["serving_eligible"] else "info"
        alerts.append(
            _alert(
                "ann.evaluation.missing",
                severity,
                "evaluation",
                "HNSW recall has not been measured",
                "No exact-versus-approximate Qdrant evaluation report was found.",
                "Run evaluate-ann after at least two approved points are synchronized.",
            )
        )
        return
    for metric in report.get("metrics", []):
        mean_recall = metric.get("mean_recall")
        minimum_recall = metric.get("minimum_recall")
        name = str(metric.get("vector_name") or "vector")
        if mean_recall is not None and mean_recall < targets.ann_mean_recall_at_k:
            alerts.append(
                _alert(
                    f"ann.{name}.mean_recall",
                    "critical",
                    "qdrant",
                    f"{name} ANN mean recall is below target",
                    f"Observed mean recall is {_percent(float(mean_recall))}.",
                    "Increase hnsw_ef or rebuild the index after representative evaluation.",
                    mean_recall,
                    targets.ann_mean_recall_at_k,
                )
            )
        if minimum_recall is not None and minimum_recall < targets.ann_minimum_recall_at_k:
            alerts.append(
                _alert(
                    f"ann.{name}.minimum_recall",
                    "warning",
                    "qdrant",
                    f"{name} ANN worst-query recall is below target",
                    f"Observed minimum recall is {_percent(float(minimum_recall))}.",
                    "Inspect filtered hard queries and tune HNSW search effort.",
                    minimum_recall,
                    targets.ann_minimum_recall_at_k,
                )
            )


def _drift_alerts(
    alerts: list[dict[str, Any]],
    filtering: dict[str, Any],
    targets: Any,
) -> None:
    drift = filtering["drift"]
    class_js = drift.get("class_js_divergence")
    if drift["measured"] and class_js is not None:
        if class_js >= targets.class_distribution_js_critical:
            alerts.append(
                _alert(
                    "drift.class_distribution.critical",
                    "critical",
                    "drift",
                    "Content-class distribution changed sharply",
                    f"Recent-vs-baseline Jensen-Shannon divergence is {class_js:.3f}.",
                    "Inspect source mix, prompt/model versions, and a recent image sample.",
                    class_js,
                    targets.class_distribution_js_critical,
                )
            )
        elif class_js >= targets.class_distribution_js_warning:
            alerts.append(
                _alert(
                    "drift.class_distribution.warning",
                    "warning",
                    "drift",
                    "Content-class distribution is drifting",
                    f"Recent-vs-baseline Jensen-Shannon divergence is {class_js:.3f}.",
                    "Compare recent intake sources and review borderline class predictions.",
                    class_js,
                    targets.class_distribution_js_warning,
                )
            )
    elif filtering["latest_decisions"] >= targets.minimum_drift_cohort:
        alerts.append(
            _alert(
                "drift.evidence.insufficient",
                "info",
                "drift",
                "A baseline drift cohort is not available",
                f"Recent and baseline cohorts contain {drift['recent_count']} and "
                f"{drift['baseline_count']} decisions.",
                "Keep collecting version-consistent decisions before interpreting drift.",
                min(drift["recent_count"], drift["baseline_count"]),
                targets.minimum_drift_cohort,
            )
        )

    concentration = filtering["artist_concentration"]
    top_share = concentration.get("top_artist_share")
    if (
        filtering["latest_decisions"] >= targets.minimum_drift_cohort
        and top_share is not None
        and top_share > targets.maximum_top_artist_share
    ):
        alerts.append(
            _alert(
                "intake.artist_concentration",
                "warning",
                "drift",
                "Intake is concentrated in one artist",
                f"The largest anonymous artist cohort is {_percent(top_share)} of decisions.",
                "Broaden source sampling before calibration or retrieval evaluation.",
                top_share,
                targets.maximum_top_artist_share,
            )
        )


def _serving_eligible_count(
    connection: sqlite3.Connection,
    app: AppConfig,
    production: ProductionConfig,
) -> int:
    config = production.qdrant
    row = connection.execute(
        """
        WITH decision_links AS (
            SELECT r.artwork_id, d.decision_key, d.decision,
                   d.accepted_for_main_corpus, d.processed_at, 0 AS priority
              FROM artwork_filter_routes AS r
              JOIN artwork_filter_decisions AS d ON d.decision_key = r.decision_key
             WHERE r.artwork_id IS NOT NULL
            UNION ALL
            SELECT a.artwork_id, d.decision_key, d.decision,
                   d.accepted_for_main_corpus, d.processed_at, 1 AS priority
              FROM artworks AS a
              JOIN artwork_filter_decisions AS d ON d.candidate_id = a.source_id
             WHERE a.source_platform = 'bluesky'
        ), ranked AS (
            SELECT decision_links.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY artwork_id
                       ORDER BY processed_at DESC, decision_key DESC, priority
                   ) AS decision_rank
              FROM decision_links
        )
        SELECT COUNT(*)
          FROM artworks AS a
          JOIN embeddings AS e ON e.artwork_id = a.artwork_id
          LEFT JOIN ranked AS d ON d.artwork_id = a.artwork_id AND d.decision_rank = 1
         WHERE a.validated = 1
           AND a.processed_path IS NOT NULL
           AND a.duplicate_of IS NULL
           AND a.demo_eligible = 1
           AND (? = 0 OR a.is_sfw = 1)
           AND (? = 0 OR a.source_platform != 'bluesky'
                OR (d.decision = 'accept' AND d.accepted_for_main_corpus = 1))
           AND e.clip_vector IS NOT NULL AND e.clip_dim = ?
           AND e.csd_style IS NOT NULL AND e.csd_style_dim = ?
           AND e.model_name_clip = ? AND e.model_version_clip = ?
           AND e.model_name_csd = ? AND e.model_version_csd = ?
        """,
        (
            int(config.require_sfw),
            int(config.require_bluesky_accept),
            config.clip_dimension,
            config.csd_dimension,
            app.models.clip_model_name,
            app.models.clip_model_version,
            app.models.csd_model_name,
            app.models.csd_model_version,
        ),
    ).fetchone()
    return int(row[0])


def _embedding_versions(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "clip_model": row["model_name_clip"],
            "clip_revision": row["model_version_clip"],
            "csd_model": row["model_name_csd"],
            "csd_revision": row["model_version_csd"],
            "count": int(row["count"]),
        }
        for row in connection.execute(
            """
            SELECT model_name_clip, model_version_clip, model_name_csd,
                   model_version_csd, COUNT(*) AS count
              FROM embeddings
             GROUP BY model_name_clip, model_version_clip,
                      model_name_csd, model_version_csd
             ORDER BY count DESC
            """
        )
    ]


def _artist_concentration(counts: Counter[str]) -> dict[str, float | int | None]:
    total = sum(counts.values())
    ordered = sorted(counts.values(), reverse=True)
    shares = [count / total for count in ordered] if total else []
    return {
        "artists": len(counts),
        "labeled_decisions": total,
        "top_artist_share": shares[0] if shares else None,
        "top_five_share": sum(shares[:5]) if shares else None,
        "herfindahl_index": sum(share * share for share in shares) if shares else None,
    }


def _filter_drift(
    rows: Sequence[dict[str, Any]],
    monitoring: MonitoringConfig,
) -> dict[str, Any]:
    timestamped = [
        (timestamp, item)
        for item in rows
        if (timestamp := _parse_datetime(item.get("processed_at"))) is not None
    ]
    if not timestamped:
        return _empty_drift_snapshot(monitoring)
    latest = max(timestamp for timestamp, _ in timestamped)
    recent_start = latest - timedelta(days=monitoring.drift_recent_days)
    baseline_start = recent_start - timedelta(days=monitoring.drift_baseline_days)
    recent = [item for timestamp, item in timestamped if timestamp >= recent_start]
    baseline = [
        item for timestamp, item in timestamped if baseline_start <= timestamp < recent_start
    ]
    minimum = monitoring.targets.minimum_drift_cohort
    recent_confidence = _float_values(recent, "confidence")
    baseline_confidence = _float_values(baseline, "confidence")
    recent_scores = _float_values(recent, "final_score")
    baseline_scores = _float_values(baseline, "final_score")
    recent_latency = _float_values(recent, "duration_ms")
    baseline_latency = _float_values(baseline, "duration_ms")
    baseline_p95 = _percentile(baseline_latency, 95)
    recent_p95 = _percentile(recent_latency, 95)
    return {
        "measured": len(recent) >= minimum and len(baseline) >= minimum,
        "minimum_cohort": minimum,
        "recent_days": monitoring.drift_recent_days,
        "baseline_days": monitoring.drift_baseline_days,
        "window_end": latest.isoformat(),
        "recent_start": recent_start.isoformat(),
        "baseline_start": baseline_start.isoformat(),
        "recent_count": len(recent),
        "baseline_count": len(baseline),
        "decision_js_divergence": _field_js_divergence(recent, baseline, "decision"),
        "class_js_divergence": _field_js_divergence(recent, baseline, "predicted_class"),
        "route_js_divergence": _field_js_divergence(recent, baseline, "route"),
        "confidence_mean_delta": _mean_delta(recent_confidence, baseline_confidence),
        "final_score_mean_delta": _mean_delta(recent_scores, baseline_scores),
        "latency_p95_ratio": (
            recent_p95 / baseline_p95
            if recent_p95 is not None and baseline_p95 not in {None, 0.0}
            else None
        ),
        "recent_classes": _counter_rows(_field_counter(recent, "predicted_class")),
        "baseline_classes": _counter_rows(_field_counter(baseline, "predicted_class")),
    }


def _empty_drift_snapshot(monitoring: MonitoringConfig) -> dict[str, Any]:
    return {
        "measured": False,
        "minimum_cohort": monitoring.targets.minimum_drift_cohort,
        "recent_days": monitoring.drift_recent_days,
        "baseline_days": monitoring.drift_baseline_days,
        "window_end": None,
        "recent_start": None,
        "baseline_start": None,
        "recent_count": 0,
        "baseline_count": 0,
        "decision_js_divergence": None,
        "class_js_divergence": None,
        "route_js_divergence": None,
        "confidence_mean_delta": None,
        "final_score_mean_delta": None,
        "latency_p95_ratio": None,
        "recent_classes": [],
        "baseline_classes": [],
    }


def _field_counter(rows: Sequence[dict[str, Any]], key: str) -> Counter[str]:
    return Counter(str(item.get(key) or "unknown") for item in rows)


def _field_js_divergence(
    recent: Sequence[dict[str, Any]],
    baseline: Sequence[dict[str, Any]],
    key: str,
) -> float | None:
    return _jensen_shannon(_field_counter(recent, key), _field_counter(baseline, key))


def _jensen_shannon(left: Counter[str], right: Counter[str]) -> float | None:
    left_total = sum(left.values())
    right_total = sum(right.values())
    if not left_total or not right_total:
        return None
    labels = set(left) | set(right)
    divergence = 0.0
    for label in labels:
        left_probability = left[label] / left_total
        right_probability = right[label] / right_total
        midpoint = (left_probability + right_probability) / 2.0
        if left_probability:
            divergence += 0.5 * left_probability * log2(left_probability / midpoint)
        if right_probability:
            divergence += 0.5 * right_probability * log2(right_probability / midpoint)
    return divergence


def _mean_delta(recent: Sequence[float], baseline: Sequence[float]) -> float | None:
    if not recent or not baseline:
        return None
    return fmean(recent) - fmean(baseline)


def _daily_decisions(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in rows:
        processed = str(item.get("processed_at") or "")
        buckets[processed[:10] or "unknown"].append(item)
    result = []
    for day, items in sorted(buckets.items()):
        outcomes = Counter(str(item.get("decision") or "unknown") for item in items)
        durations = _float_values(items, "duration_ms")
        result.append(
            {
                "date": day,
                "count": len(items),
                "accept": outcomes.get("accept", 0),
                "review": outcomes.get("review", 0),
                "reject": outcomes.get("reject", 0),
                "error": outcomes.get("error", 0),
                "p95_duration_ms": _percentile(durations, 95),
            }
        )
    return result[-30:]


def _group_counts(
    connection: sqlite3.Connection,
    table: str,
    column: str,
) -> list[dict[str, Any]]:
    return [
        {"label": str(row["label"] or "unknown"), "count": int(row["count"])}
        for row in connection.execute(
            f"SELECT {column} AS label, COUNT(*) AS count "
            f"FROM {table} GROUP BY {column} ORDER BY count DESC, label"
        )
    ]


def _numeric_row(row: sqlite3.Row) -> dict[str, int]:
    return {key: int(row[key] or 0) for key in row.keys()}


def _empty_database_snapshot() -> dict[str, Any]:
    return {
        "corpus": {
            "artworks": 0,
            "artists": 0,
            "validated": 0,
            "searchable": 0,
            "safe": 0,
            "unsafe": 0,
            "safety_unknown": 0,
            "demo_eligible": 0,
            "duplicates": 0,
            "serving_eligible": 0,
            "by_source": [],
            "by_review_status": [],
        },
        "embeddings": {
            "rows": 0,
            "clip_any": 0,
            "csd_any": 0,
            "clip_current": 0,
            "csd_current": 0,
            "complete_current": 0,
            "clip_bytes": 0,
            "csd_bytes": 0,
            "current_complete": 0,
            "global_coverage": None,
            "searchable_denominator": 0,
            "versions": [],
            "qdrant_ledger": 0,
        },
        "operations": {
            "recent_runs": [],
            "events_by_level": [],
            "published_objects": 0,
            "published_bytes": 0,
            "route_statuses": [],
            "route_success_rate": None,
            "recent_run_success_rate": None,
            "source_state": {
                "tracked_actors": 0,
                "completed_actors": 0,
                "backlogged_actors": 0,
                "actors_with_errors": 0,
                "resumable_cursors": 0,
                "latest_attempt_at": None,
            },
        },
    }


def _load_optional_json(
    path: Path | None,
    area: str,
    errors: list[dict[str, str]],
) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append({"area": area, "message": f"Could not read {path.name}: {exc}"})
        return None
    if not isinstance(value, dict):
        errors.append({"area": area, "message": f"{path.name} is not a JSON object"})
        return None
    return value


def _http_json(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    with urlopen(Request(url, headers=headers), timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("HTTP response is not a JSON object")
    return value


def _discover(root: Path, patterns: Sequence[str]) -> tuple[Path, ...]:
    paths = {
        path.resolve() for pattern in patterns for path in root.glob(pattern) if path.is_file()
    }
    return tuple(sorted(paths, key=lambda path: (path.stat().st_mtime_ns, str(path))))


def _explicit_or_latest(
    root: Path,
    explicit: Path | None,
    patterns: Sequence[str],
) -> Path | None:
    if explicit is not None:
        return _resolve(root, explicit)
    discovered = _discover(root, patterns)
    return discovered[-1] if discovered else None


def _resolve(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else (root / value).resolve()


def _source_labels(root: Path, value: Any) -> Any:
    if isinstance(value, tuple):
        return [_relative_label(root, path) for path in value]
    if isinstance(value, Path):
        return _relative_label(root, value)
    return None


def _relative_label(root: Path, path: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def _safe_endpoint(url: str) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return parsed.scheme or "local"
    host = parsed.hostname or "unknown"
    return f"{parsed.scheme}://{host}{f':{parsed.port}' if parsed.port else ''}"


def _float_values(rows: Iterable[dict[str, Any]], key: str) -> list[float]:
    return [float(value) for item in rows if _finite((value := item.get(key)))]


def _finite(value: object) -> bool:
    if not isinstance(value, (int, float)):
        return False
    return value == value and value not in {float("inf"), float("-inf")}


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean": fmean(values) if values else None,
        "p50": _percentile(values, 50),
        "p95": _percentile(values, 95),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return None if not denominator else float(numerator) / float(denominator)


def _counter_rows(counter: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"label": label, "count": count}
        for label, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _age_days(value: str) -> float | None:
    parsed = _parse_datetime(value)
    if parsed is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86_400)


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_int(value: object) -> int | None:
    return None if value is None else int(value)


def _alert(
    alert_id: str,
    severity: str,
    area: str,
    title: str,
    detail: str,
    action: str,
    observed: object | None = None,
    target: object | None = None,
) -> dict[str, Any]:
    return {
        "id": alert_id,
        "severity": severity,
        "area": area,
        "title": title,
        "detail": detail,
        "action": action,
        "observed": observed,
        "target": target,
    }


def _severity_rank(value: str) -> int:
    return {"critical": 0, "warning": 1, "info": 2}.get(value, 3)


def _overall_level(alerts: Sequence[dict[str, Any]]) -> str:
    severities = {item["severity"] for item in alerts}
    if "critical" in severities:
        return "critical"
    if "warning" in severities:
        return "attention"
    return "healthy"


def _evidence_coverage(
    filter_evaluation: dict[str, Any] | None,
    retrieval: dict[str, Any],
    ann_evaluation: dict[str, Any] | None,
) -> dict[str, bool]:
    return {
        "filter_quality": filter_evaluation is not None,
        "retrieval_quality": bool(retrieval.get("measured")),
        "ann_quality": ann_evaluation is not None,
    }


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"
