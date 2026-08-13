# ArtSearch Production Data Platform

Status: implemented local/alpha integration

Implemented: 2026-08-13

This layer turns the current Bluesky-to-SQLite vertical slice into a scheduled,
durable data workflow. It uses each requested technology at the scale where it
adds value, while retaining the existing classifier, router, and embedding
code as ordinary Python services.

## Responsibility Map

| Technology | ArtSearch responsibility | Deliberate boundary |
| --- | --- | --- |
| HTTPX | Pooled asynchronous AppView and image requests, explicit timeouts, bounded connections, streaming byte limits, retry policy | Does not own workflow state |
| Polars | Lazy per-run JSONL to Parquet manifests and daily intake metrics | Default engine for 5K to 1M-row local data products |
| s3fs | Provider-neutral adapter for AWS S3, Backblaze B2, R2, or local-compatible testing | Credentials stay in the AWS environment/profile chain |
| Apache Airflow | Scheduling, dependencies, retries, task history, run parameters, and compact XCom summaries | Does not replace per-item idempotency or canonical metadata |
| Apache Spark | Corpus-wide reconciliation, duplicate audits, artist/decision aggregation, and large backfills over all manifests | Does not sit in the image-download or SigLIP hot path |

This split is intentional. At the approved intake rate, Polars is simpler and
faster to operate for one run. Spark becomes useful when reading many immutable
runs together, or when historical feature/index backfills outgrow one process.
Python publication uses s3fs; Spark uses Hadoop's native `s3a://` connector
instead of mounting S3 as a filesystem. Polars builds each run beside its local
inputs and uploads the finished immutable Parquet object, avoiding cloud
credentials and partial remote outputs inside the ETL expression graph.

## End-to-End Flow

```text
Airflow artsearch_bluesky_intake
    -> existing Bluesky pipeline
       -> HTTPX author-feed pagination and bounded image streaming
       -> rules + SigLIP classification
       -> ACCEPT originals in local working cache
       -> decisions/routes/artworks in SQLite
    -> Polars latest-source/latest-decision join
       -> corpus-manifest.parquet
       -> intake-metrics.parquet
    -> s3fs immutable publication
       -> candidates + decisions + manifest + metrics + run bundle
       -> only accepted originals, content-addressed by SHA-256
       -> object URI checkpoint in artwork_objects

Airflow artsearch_corpus_spark_audit
    -> Spark reads every immutable corpus manifest via S3A
    -> deduplicates repeated candidate/source versions by processed timestamp
    -> selects the latest source version per candidate for current-corpus metrics
    -> writes decision, artist, duplicate-hash, and data-quality Parquet reports
```

Rejected image bytes are never published. Their bounded decision and candidate
evidence remains available for reproducibility. Review images remain disabled
by the intake DAG. The local raw directory is a working cache, while the object
store is the durable accepted-original boundary.

## Durable Object Layout

With `bucket=example` and `prefix=artsearch`, representative keys are:

```text
s3://example/artsearch/corpus/originals/sha256/ab/cd/<sha256>.jpg
s3://example/artsearch/runs/candidates/sha256/ab/cd/<sha256>.jsonl
s3://example/artsearch/runs/decisions/sha256/ab/cd/<sha256>.jsonl
s3://example/artsearch/manifests/corpus/sha256/ab/cd/<sha256>.parquet
s3://example/artsearch/manifests/metrics/sha256/ab/cd/<sha256>.parquet
s3://example/artsearch/runs/bundles/sha256/ab/cd/<sha256>.json
s3://example/artsearch/audits/<airflow-run-id>/...
```

Uploads are immutable by key. Local SHA-256 and byte count are checked before
publication; the S3 adapter verifies remote bytes before recording success.
`artwork_objects` checkpoints the key, URI, hash, size, and ETag. If the object
is later missing, the corpus publisher repairs it from the local accepted cache.

## Local Data Commands

Install the lightweight production data dependencies into the app environment:

```bash
.venv/bin/python -m pip install -e '.[data]'
```

Build and inspect Parquet products without Airflow:

```bash
.venv/bin/artsearch-data build-manifest \
  --candidates data/bluesky/image_candidates.jsonl \
  --decisions data/filter/decisions.jsonl \
  --database data/artsearch.db \
  --output data/production/manifests/corpus.parquet

.venv/bin/artsearch-data build-metrics \
  --manifest data/production/manifests/corpus.parquet \
  --output data/production/manifests/intake-metrics.parquet

.venv/bin/artsearch-data manifest-summary \
  --manifest data/production/manifests/corpus.parquet
```

The local default exercises the same immutable object-store contract without a
cloud account:

```bash
.venv/bin/artsearch-data publish-corpus \
  --config configs/production.default.toml \
  --decisions data/filter/decisions.jsonl
```

For B2/S3, create an ignored runtime config and export credentials:

```bash
cp configs/production.s3.example.toml configs/production.s3.local.toml
export AWS_ACCESS_KEY_ID='...'
export AWS_SECRET_ACCESS_KEY='...'
export AWS_DEFAULT_REGION='...'

.venv/bin/artsearch-data publish-corpus \
  --config configs/production.s3.local.toml \
  --decisions data/filter/decisions.jsonl
```

The decision file keeps scheduled publication proportional to one intake run
and selects only its `accept` rows. Use `--candidates` to scope a deliberate
repair regardless of decision file, or omit both only for a full-corpus repair.

Neither config template nor application logging contains credentials.

## Airflow Development Stack

Airflow and Spark are isolated from the Python 3.14 ML environment. The image
uses Airflow 3.3 on Python 3.12, Java 17, PySpark 4.2, Polars, and s3fs.

```bash
AIRFLOW_UID="$(id -u)" docker compose \
  -f orchestration/airflow/compose.yaml up --build
```

Open `http://localhost:8080`, obtain the standalone credentials from the
container log, and trigger `artsearch_bluesky_intake`. Its default actor file
is the ignored safe pilot roster and its default schedule is manual. Set
`ARTSEARCH_INTAKE_SCHEDULE` to a cron expression only after a pilot succeeds.

The DAG exposes run parameters for actor/config paths, page count, page size,
feed filter, deterministic-only operation, and strict item-error handling. Each
Airflow run gets an isolated local directory. Large evidence stays in
JSONL/Parquet/object storage; XCom carries only paths, counts, and object URIs.
The default allows at most 5% actor failures and 2% isolated candidate/route
failures; either threshold is a run parameter, and strict mode permits none.
Hugging Face and Torch caches live under the ignored `data/cache/` mount.

Trigger `artsearch_corpus_spark_audit` separately with the manifest input glob,
unique audit output prefix, S3 endpoint, and region. The DAG uses the
`spark_default` local master and local object-store paths by default in the
development container. Override them with `s3a://...` paths plus the endpoint
and region for B2/S3. A future remote Spark deployment changes the Airflow
connection without changing the job.

The Spark S3A dependencies are pinned as a tested family in the DAG:
`hadoop-aws:3.4.2` and AWS SDK bundle `2.29.52`. If the Spark image's Hadoop
version changes, update and validate these together; mixed Hadoop AWS JARs are
not a supported upgrade strategy.

## Failure and Resume Semantics

- Airflow retries run-level failures; the app still owns candidate-level typed
  errors and idempotency.
- SQLite upserts and decision keys prevent duplicate corpus rows.
- S3 keys are content-addressed, so task retries do not create new objects.
- `artwork_objects` lets publication skip verified prior uploads and repair
  missing objects.
- Each run publishes immutable evidence instead of mutating a shared manifest.
- Spark writes to one run-specific audit prefix with overwrite semantics.
- Rejected bytes are not stored; accepted originals are published only after
  the classifier and router commit their evidence.

Airflow's standalone SQLite metadata database is appropriate for local
development and the tiny alpha, not a high-availability deployment. The
production roadmap still moves canonical metadata and Airflow metadata to
PostgreSQL when remote workers or operational load justify it.

## Verification Status

The ordinary project suite validates the HTTP config, object-store contracts,
SQLite publication checkpoint/repair behavior, and real Polars lazy
JSONL-to-Parquet processing. Airflow DAGs and the Spark job are syntax-checked
in this workspace. Their runtime integration requires Docker/Java, which is
provided by the checked-in image rather than the host environment.

## Resume-Accurate Description

A concise description after running the container stack is:

> Built an idempotent artwork data platform using Airflow 3 orchestration,
> bounded asynchronous HTTPX acquisition, Polars streaming ETL, S3-compatible
> content-addressed storage through s3fs, and Spark SQL corpus reconciliation;
> preserved model/config lineage and resumable SQLite publication checkpoints.

Do not claim a million-image Spark benchmark, production SLA, or measured cloud
throughput until those runs and measurements actually exist.

## Primary References

- [Airflow 3 public SDK interface](https://airflow.apache.org/docs/apache-airflow/stable/public-airflow-interface.html)
- [Airflow SparkSubmitOperator](https://airflow.apache.org/docs/apache-airflow-providers-apache-spark/stable/operators.html)
- [Polars cloud storage](https://docs.pola.rs/user-guide/io/cloud-storage/)
  and [lazy Parquet scanning](https://docs.pola.rs/api/python/stable/reference/api/polars.scan_parquet.html)
- [HTTPX asynchronous clients](https://www.python-httpx.org/async/)
  and [connection resource limits](https://www.python-httpx.org/advanced/resource-limits/)
- [s3fs documentation](https://s3fs.readthedocs.io/en/latest/)
- [Hadoop S3A connector](https://hadoop.apache.org/docs/current/hadoop-aws/tools/hadoop-aws/)
