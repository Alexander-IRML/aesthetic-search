from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import sqlite3
from pathlib import Path
from typing import Any


MANIFEST_VERSION = "artsearch-corpus-manifest-v1"
METRICS_VERSION = "artsearch-intake-metrics-v1"


class DataDependencyError(RuntimeError):
    """Raised when a production data command is missing its optional runtime."""


@dataclass(frozen=True)
class ManifestBuildResult:
    path: str
    sha256: str
    byte_size: int
    row_count: int
    accepted_count: int
    review_count: int
    rejected_count: int
    error_count: int
    manifest_version: str = MANIFEST_VERSION

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


@dataclass(frozen=True)
class MetricsBuildResult:
    path: str
    sha256: str
    byte_size: int
    row_count: int
    metrics_version: str = METRICS_VERSION

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)


def build_corpus_manifest(
    candidates_path: str | Path,
    decisions_path: str | Path,
    output_path: str | Path,
    *,
    database_path: str | Path | None = None,
) -> ManifestBuildResult:
    """Join latest candidate/decision evidence into a streaming Parquet manifest."""

    pl = _polars()
    candidates_source = _required_file(candidates_path)
    decisions_source = _required_file(decisions_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    built_at = datetime.now(timezone.utc).isoformat()
    if Path(candidates_source).stat().st_size == 0 or Path(decisions_source).stat().st_size == 0:
        _write_empty_manifest(pl, output, built_at=built_at)
        digest, byte_size = _file_identity(output)
        return ManifestBuildResult(
            path=str(output.resolve()),
            sha256=digest,
            byte_size=byte_size,
            row_count=0,
            accepted_count=0,
            review_count=0,
            rejected_count=0,
            error_count=0,
        )

    candidates = (
        pl.scan_ndjson(candidates_source, infer_schema_length=10_000)
        .select(
            "candidate_id",
            "author_did",
            "author_handle",
            "post_uri",
            "post_cid",
            "image_index",
            "thumbnail_url",
            "fullsize_url",
            "created_at",
            "declared_width",
            "declared_height",
            "mime_type",
            "source",
            "is_repost",
            "is_quote_post",
        )
        .with_columns(pl.col("post_cid").fill_null("").alias("_source_cid"))
        .unique(subset=["candidate_id", "_source_cid"], keep="last", maintain_order=False)
    )
    candidate_ids = (
        candidates.select("candidate_id")
        .unique()
        .collect(engine="streaming")
        .get_column("candidate_id")
        .to_list()
    )
    object_mapping = _load_object_mapping(pl, database_path, candidate_ids)
    decision_scan = pl.scan_ndjson(decisions_source, infer_schema_length=10_000)
    decision_schema = decision_scan.collect_schema()
    visual_dtype = decision_schema.get("visual_scores")
    if visual_dtype is not None and visual_dtype.base_type() == pl.Struct:
        model_revision = pl.col("visual_scores").struct.field("model_revision")
    else:
        model_revision = pl.lit(None, dtype=pl.String)
    decisions = (
        decision_scan.select(
            "candidate_id",
            "source_cid",
            "image_sha256",
            "decision",
            "predicted_class",
            "accepted_for_main_corpus",
            "route",
            "final_score",
            "confidence",
            "duration_ms",
            "model_version",
            model_revision.alias("model_revision"),
            "config_version",
            "config_hash",
            "prompt_version",
            "software_version",
            "processed_at",
            "error_type",
        )
        .with_columns(
            pl.col("source_cid").fill_null("").alias("_source_cid"),
            pl.col("processed_at")
            .str.to_datetime(strict=False, time_zone="UTC")
            .alias("_processed_at"),
        )
        .sort(["candidate_id", "_source_cid", "_processed_at"])
        .unique(subset=["candidate_id", "_source_cid"], keep="last", maintain_order=False)
        .drop("source_cid")
    )
    manifest = (
        candidates.join(
            decisions,
            on=["candidate_id", "_source_cid"],
            how="inner",
            validate="1:1",
        )
        .join(object_mapping, on="candidate_id", how="left", validate="m:1")
        .with_columns(
            pl.lit(MANIFEST_VERSION).alias("manifest_version"),
            pl.lit(built_at).alias("manifest_built_at"),
            (pl.col("decision") == "accept").alias("is_accept"),
            (
                (pl.col("decision") == "accept")
                & pl.col("accepted_for_main_corpus").fill_null(False)
            ).alias("is_search_eligible"),
        )
        .drop("_source_cid", "_processed_at")
    )
    manifest.sink_parquet(
        output,
        compression="zstd",
        statistics=True,
        row_group_size=50_000,
        maintain_order=False,
        mkdir=True,
        engine="streaming",
    )

    summary = (
        pl.scan_parquet(output)
        .select(
            pl.len().alias("row_count"),
            (pl.col("decision") == "accept").sum().alias("accepted_count"),
            (pl.col("decision") == "review").sum().alias("review_count"),
            (pl.col("decision") == "reject").sum().alias("rejected_count"),
            (pl.col("decision") == "error").sum().alias("error_count"),
        )
        .collect()
        .row(0, named=True)
    )
    digest, byte_size = _file_identity(output)
    return ManifestBuildResult(
        path=str(output.resolve()),
        sha256=digest,
        byte_size=byte_size,
        row_count=int(summary["row_count"]),
        accepted_count=int(summary["accepted_count"]),
        review_count=int(summary["review_count"]),
        rejected_count=int(summary["rejected_count"]),
        error_count=int(summary["error_count"]),
    )


def build_intake_metrics(
    manifest_path: str | Path,
    output_path: str | Path,
) -> MetricsBuildResult:
    """Aggregate operational cohorts with Polars predicate/projection pushdown."""

    pl = _polars()
    manifest_source = _required_file(manifest_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    built_at = datetime.now(timezone.utc).isoformat()

    metrics = (
        pl.scan_parquet(manifest_source)
        .with_columns(
            pl.col("processed_at")
            .str.to_datetime(strict=False, time_zone="UTC")
            .dt.date()
            .alias("decision_date")
        )
        .group_by("decision_date", "source", "decision", "predicted_class")
        .agg(
            pl.len().alias("image_count"),
            pl.col("duration_ms").mean().alias("mean_duration_ms"),
            pl.col("duration_ms").quantile(0.95, interpolation="nearest").alias("p95_duration_ms"),
            pl.col("final_score").mean().alias("mean_final_score"),
            pl.col("is_search_eligible").sum().alias("search_eligible_count"),
            pl.col("author_did").n_unique().alias("artist_count"),
        )
        .with_columns(
            pl.lit(METRICS_VERSION).alias("metrics_version"),
            pl.lit(built_at).alias("metrics_built_at"),
        )
        .sort("decision_date", "source", "decision", "predicted_class")
    )
    metrics.sink_parquet(
        output,
        compression="zstd",
        statistics=True,
        maintain_order=True,
        mkdir=True,
        engine="streaming",
    )
    row_count = int(pl.scan_parquet(output).select(pl.len()).collect().item())
    digest, byte_size = _file_identity(output)
    return MetricsBuildResult(
        path=str(output.resolve()),
        sha256=digest,
        byte_size=byte_size,
        row_count=row_count,
    )


def manifest_summary(path: str | Path) -> dict[str, Any]:
    pl = _polars()
    source = _required_file(path)
    frame = (
        pl.scan_parquet(source)
        .group_by("decision")
        .agg(
            pl.len().alias("rows"),
            pl.col("author_did").n_unique().alias("artists"),
            pl.col("is_search_eligible").sum().alias("search_eligible"),
        )
        .sort("decision")
        .collect()
    )
    return {
        "path": str(Path(source).resolve()),
        "rows": int(frame["rows"].sum()),
        "decisions": frame.to_dicts(),
    }


def _required_file(path: str | Path) -> str:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"data source does not exist: {source}")
    return str(source)


def _polars() -> Any:
    try:
        import polars as pl
    except ImportError as exc:
        raise DataDependencyError(
            "Polars commands require the data extra: pip install -e '.[data]'"
        ) from exc
    return pl


def _file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _load_object_mapping(
    pl: Any,
    database_path: str | Path | None,
    candidate_ids: list[str],
) -> Any:
    schema = {
        "candidate_id": pl.String,
        "artwork_id": pl.String,
        "object_store_key": pl.String,
        "object_store_uri": pl.String,
        "original_sha256": pl.String,
        "original_byte_size": pl.Int64,
        "object_etag": pl.String,
        "object_published_at": pl.String,
    }
    if database_path is None or not candidate_ids:
        return pl.DataFrame(schema=schema).lazy()
    database = Path(database_path)
    if not database.is_file():
        return pl.DataFrame(schema=schema).lazy()

    rows: list[dict[str, Any]] = []
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'artwork_objects'"
        ).fetchone()
        if table_exists is None:
            return pl.DataFrame(schema=schema).lazy()
        for offset in range(0, len(candidate_ids), 500):
            batch = candidate_ids[offset : offset + 500]
            requested = ", ".join("(?)" for _ in batch)
            query = f"""
                WITH requested(candidate_id) AS (VALUES {requested}),
                latest_route AS (
                    SELECT r.candidate_id, r.artwork_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY r.candidate_id
                               ORDER BY r.routed_at DESC, r.rowid DESC
                           ) AS route_rank
                      FROM artwork_filter_routes AS r
                      JOIN requested AS q ON q.candidate_id = r.candidate_id
                     WHERE r.artwork_id IS NOT NULL
                ),
                candidate_artwork AS (
                    SELECT q.candidate_id,
                           COALESCE(lr.artwork_id, direct.artwork_id) AS artwork_id
                      FROM requested AS q
                      LEFT JOIN latest_route AS lr
                        ON lr.candidate_id = q.candidate_id AND lr.route_rank = 1
                      LEFT JOIN artworks AS direct
                        ON direct.source_platform = 'bluesky'
                       AND direct.source_id = q.candidate_id
                )
                SELECT ca.candidate_id,
                       o.artwork_id,
                       o.object_key AS object_store_key,
                       o.object_uri AS object_store_uri,
                       o.content_sha256 AS original_sha256,
                       o.byte_size AS original_byte_size,
                       o.etag AS object_etag,
                       o.published_at AS object_published_at
                  FROM candidate_artwork AS ca
                  JOIN artwork_objects AS o ON o.artwork_id = ca.artwork_id
                 WHERE o.role = 'original'
            """
            rows.extend(dict(row) for row in connection.execute(query, batch))
    finally:
        connection.close()
    return pl.DataFrame(rows, schema=schema).lazy()


def _write_empty_manifest(pl: Any, output: Path, *, built_at: str) -> None:
    schema = {
        "candidate_id": pl.String,
        "author_did": pl.String,
        "author_handle": pl.String,
        "post_uri": pl.String,
        "post_cid": pl.String,
        "image_index": pl.Int64,
        "thumbnail_url": pl.String,
        "fullsize_url": pl.String,
        "created_at": pl.String,
        "declared_width": pl.Int64,
        "declared_height": pl.Int64,
        "mime_type": pl.String,
        "source": pl.String,
        "is_repost": pl.Boolean,
        "is_quote_post": pl.Boolean,
        "image_sha256": pl.String,
        "decision": pl.String,
        "predicted_class": pl.String,
        "accepted_for_main_corpus": pl.Boolean,
        "route": pl.String,
        "final_score": pl.Float64,
        "confidence": pl.Float64,
        "duration_ms": pl.Float64,
        "model_version": pl.String,
        "model_revision": pl.String,
        "config_version": pl.String,
        "config_hash": pl.String,
        "prompt_version": pl.String,
        "software_version": pl.String,
        "processed_at": pl.String,
        "error_type": pl.String,
        "artwork_id": pl.String,
        "object_store_key": pl.String,
        "object_store_uri": pl.String,
        "original_sha256": pl.String,
        "original_byte_size": pl.Int64,
        "object_etag": pl.String,
        "object_published_at": pl.String,
        "manifest_version": pl.String,
        "manifest_built_at": pl.String,
        "is_accept": pl.Boolean,
        "is_search_eligible": pl.Boolean,
    }
    frame = pl.DataFrame(schema=schema).with_columns(
        pl.lit(MANIFEST_VERSION).cast(pl.String).alias("manifest_version"),
        pl.lit(built_at).cast(pl.String).alias("manifest_built_at"),
    )
    frame.write_parquet(output, compression="zstd", statistics=True)
