from __future__ import annotations

import argparse
import json


REQUIRED_COLUMNS = {
    "candidate_id",
    "post_cid",
    "author_did",
    "author_handle",
    "decision",
    "predicted_class",
    "image_sha256",
    "original_sha256",
    "object_store_uri",
    "is_search_eligible",
    "duration_ms",
    "processed_at",
    "manifest_built_at",
}


def main() -> None:
    args = _parser().parse_args()
    from pyspark.sql import SparkSession, Window, functions as F

    builder = SparkSession.builder.appName("artsearch-corpus-audit")
    if _uses_s3(args.input) or _uses_s3(args.output):
        builder = _configure_s3(builder, args)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    try:
        manifests = spark.read.parquet(_spark_uri(args.input))
        missing = sorted(REQUIRED_COLUMNS - set(manifests.columns))
        if missing:
            raise ValueError(f"manifest is missing required columns: {', '.join(missing)}")

        source_version_window = Window.partitionBy("candidate_id", "post_cid").orderBy(
            F.to_timestamp("processed_at").desc_nulls_last(),
            F.col("manifest_built_at").desc_nulls_last(),
        )
        source_versions = (
            manifests.withColumn("_source_latest", F.row_number().over(source_version_window))
            .where(F.col("_source_latest") == 1)
            .drop("_source_latest")
        )
        current_candidate_window = Window.partitionBy("candidate_id").orderBy(
            F.to_timestamp("processed_at").desc_nulls_last(),
            F.col("manifest_built_at").desc_nulls_last(),
            F.col("post_cid").desc_nulls_last(),
        )
        corpus = (
            source_versions.withColumn(
                "_candidate_latest",
                F.row_number().over(current_candidate_window),
            )
            .where(F.col("_candidate_latest") == 1)
            .drop("_candidate_latest")
            .cache()
        )
        output = _spark_uri(args.output).rstrip("/")

        _write(source_versions, f"{output}/source_versions")

        decision_summary = corpus.groupBy("decision", "predicted_class").agg(
            F.count("*").alias("image_count"),
            F.approx_count_distinct("author_did").alias("artist_count"),
            F.sum(F.col("is_search_eligible").cast("long")).alias("search_eligible_count"),
            F.avg("duration_ms").alias("mean_duration_ms"),
            F.percentile_approx("duration_ms", 0.95, 10_000).alias("p95_duration_ms"),
        )
        _write(decision_summary, f"{output}/decision_summary")

        artist_summary = (
            corpus.where(F.col("author_did").isNotNull())
            .groupBy("author_did")
            .agg(
                F.first("author_handle", ignorenulls=True).alias("author_handle"),
                F.count("*").alias("image_count"),
                F.sum((F.col("decision") == "accept").cast("long")).alias("accepted_count"),
                F.sum((F.col("decision") == "review").cast("long")).alias("review_count"),
                F.sum((F.col("decision") == "reject").cast("long")).alias("rejected_count"),
            )
        )
        _write(artist_summary, f"{output}/artist_summary")

        duplicate_hashes = (
            corpus.withColumn(
                "content_sha256",
                F.coalesce("original_sha256", "image_sha256"),
            )
            .where(F.col("content_sha256").isNotNull())
            .groupBy("content_sha256")
            .agg(
                F.count("*").alias("occurrences"),
                F.collect_set("candidate_id").alias("candidate_ids"),
                F.collect_set("author_did").alias("author_dids"),
            )
            .where(F.col("occurrences") > 1)
        )
        _write(duplicate_hashes, f"{output}/duplicate_hashes")

        quality_wide = corpus.agg(
            F.count("*").cast("long").alias("corpus_rows"),
            F.sum(F.col("author_did").isNull().cast("long")).alias("missing_author_did"),
            F.sum(F.col("image_sha256").isNull().cast("long")).alias("missing_image_sha256"),
            F.sum(
                ((F.col("decision") == "accept") & F.col("original_sha256").isNull()).cast("long")
            ).alias("accepted_missing_original_hash"),
            F.sum(
                (F.col("is_search_eligible") & F.col("object_store_uri").isNull()).cast("long")
            ).alias("search_eligible_missing_object"),
            F.sum(
                (
                    F.col("decision").isNull()
                    | ~F.col("decision").isin("accept", "review", "reject", "error")
                ).cast("long")
            ).alias("invalid_decision"),
        )
        quality = quality_wide.select(
            F.explode(
                F.create_map(
                    F.lit("corpus_rows"),
                    F.col("corpus_rows"),
                    F.lit("missing_author_did"),
                    F.col("missing_author_did"),
                    F.lit("missing_image_sha256"),
                    F.col("missing_image_sha256"),
                    F.lit("accepted_missing_original_hash"),
                    F.col("accepted_missing_original_hash"),
                    F.lit("search_eligible_missing_object"),
                    F.col("search_eligible_missing_object"),
                    F.lit("invalid_decision"),
                    F.col("invalid_decision"),
                )
            ).alias("metric", "value")
        )
        _write(quality, f"{output}/quality")

        summary = {row["metric"]: int(row["value"] or 0) for row in quality.collect()}
        print(json.dumps({"output": output, "quality": summary}, sort_keys=True))
    finally:
        spark.stop()


def _configure_s3(builder: object, args: argparse.Namespace) -> object:
    builder = builder.config(
        "spark.hadoop.fs.s3a.aws.credentials.provider",
        "software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider",
    ).config("spark.hadoop.fs.s3a.path.style.access", args.path_style)
    if args.s3_endpoint:
        builder = builder.config("spark.hadoop.fs.s3a.endpoint", args.s3_endpoint)
    if args.s3_region:
        builder = builder.config("spark.hadoop.fs.s3a.endpoint.region", args.s3_region)
    return builder


def _write(frame: object, path: str) -> None:
    frame.write.mode("overwrite").parquet(path)


def _spark_uri(value: str) -> str:
    return f"s3a://{value[5:]}" if value.startswith("s3://") else value


def _uses_s3(value: str) -> bool:
    return value.startswith(("s3://", "s3a://"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit cumulative ArtSearch manifests.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--s3-endpoint", default="")
    parser.add_argument("--s3-region", default="us-east-1")
    parser.add_argument("--path-style", choices=("true", "false"), default="true")
    return parser


if __name__ == "__main__":
    main()
