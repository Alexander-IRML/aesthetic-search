from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path

from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
from airflow.sdk import DAG, Param


PROJECT_ROOT = Path(os.environ.get("ARTSEARCH_PROJECT_ROOT", "/opt/artsearch")).resolve()
SPARK_SCHEDULE = os.environ.get("ARTSEARCH_SPARK_AUDIT_SCHEDULE") or None


with DAG(
    dag_id="artsearch_corpus_spark_audit",
    description="Reconcile all immutable intake manifests with Spark SQL.",
    schedule=SPARK_SCHEDULE,
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    default_args={
        "owner": "artsearch",
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
    },
    params={
        "input_uri": Param(
            "file:///opt/artsearch/data/object_store/artsearch/"
            "manifests/corpus/sha256/*/*/*.parquet",
            type="string",
        ),
        "output_uri": Param(
            "file:///opt/artsearch/data/object_store/artsearch/audits",
            type="string",
        ),
        "s3_endpoint": Param("", type="string"),
        "s3_region": Param("us-east-1", type="string"),
        "path_style": Param("true", enum=["true", "false"]),
    },
    render_template_as_native_obj=True,
    tags=["artsearch", "spark", "parquet", "audit", "s3"],
) as dag:
    SparkSubmitOperator(
        task_id="audit_corpus_manifests",
        conn_id="spark_default",
        application=str(PROJECT_ROOT / "jobs/spark/corpus_audit.py"),
        application_args=[
            "--input",
            "{{ params.input_uri }}",
            "--output",
            "{{ params.output_uri }}/{{ run_id }}",
            "--s3-endpoint",
            "{{ params.s3_endpoint }}",
            "--s3-region",
            "{{ params.s3_region }}",
            "--path-style",
            "{{ params.path_style }}",
        ],
        packages=("org.apache.hadoop:hadoop-aws:3.4.2,software.amazon.awssdk:bundle:2.29.52"),
        driver_memory="2g",
        executor_memory="2g",
        conf={
            "spark.sql.adaptive.enabled": "true",
            "spark.sql.parquet.compression.codec": "zstd",
            "spark.sql.shuffle.partitions": "16",
        },
        verbose=False,
    )
