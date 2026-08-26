# ArtSearch 20k Rush Runbook

Status: the first local bulk intake completed on 2026-08-23. R2 publication is
waiting on the ignored cloud configuration and R2 credentials. Cloud query
launch still requires a pooled PostgreSQL URL and populated R2 catalog objects.

## First Bulk Run Result

The private 1,000-artist roster completed one recent-feed pass followed by a
three-page recovery pass for the 308 accounts that remained below the
20-candidate cap. The durable run artifacts are under the ignored directory
`data/production/runs/bulk-20260822/`.

Latest-state results across 14,905 unique candidates:

| Decision | Count | Share |
| --- | ---: | ---: |
| Accept | 8,284 | 55.6% |
| Review | 2,252 | 15.1% |
| Reject | 4,368 | 29.3% |
| Error | 1 | less than 0.1% |

The accepted and review pools total 10,536 candidates. The conservative gate
therefore produced fewer than 10,000 automatic accepts, but human review can
take the usable corpus above 10,000 without widening the automatic threshold.
SQLite contains 8,279 materialized Bluesky artworks after content and source
deduplication. Every referenced raw file, standardized file, and stored route
was present during final reconciliation; no router failures were recorded.

The one unresolved candidate returned HTTP 504 from both configured Bluesky
image variants after bounded retries. It remains an auditable `ERROR` rather
than being treated as rejected content. The decision JSONL is append-only and
contains 325 historical retry rows beyond its 14,905 current candidate keys;
latest-state consumers must continue selecting the newest decision per
`(candidate_id, source_cid)`.

No objects from this run have been published to R2 yet. Before publication,
populate `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the ignored
`deploy/alpha/.env`, and create
`configs/production.alpha-cloud.local.toml` from the checked-in example with
the real R2 account endpoint. Run the R2 write/read/delete canary before
starting the 16-worker corpus publisher.

Configure those ignored files without exposing credentials in terminal history:

```bash
.venv/bin/python scripts/configure_r2_credentials.py
```

The bucket is fixed to `artsearch-artifacts-alpha`. The helper asks for exactly
three values: the jurisdiction endpoint, Access Key ID, and either the
ready-made Secret Access Key or Cloudflare's one-time API Token Value. The
script derives the S3 secret in memory when needed and does not persist the
parent token value. The Token ID itself is not needed by the corpus publisher.

## Capacity Decision

The current Qdrant layout is suitable for 10,000 to 20,000 artworks:

- Two named dense vectors per artwork: CLIP 512 and CSD 768.
- Float16 storage: `20,000 * (512 + 768) * 2 = 51.2 MB` of raw vectors.
- One pre-created collection with all nine payload indexes already present.
- 256-point upload batches and one shard on the closed-alpha cluster.
- One stable read alias, `artworks_active`, for atomic collection promotion.
- Western North America placement for query compute; place PostgreSQL in the
  same region family as the existing western-US Qdrant cluster.

HNSW graph and payload overhead add to the raw-vector number, but this remains
well inside the Qdrant free cluster's 1 GB RAM and 4 GB disk envelope. Do not
enable quantization, on-disk vectors, extra shards, or parallel upload workers
for this first 20k push. Those changes add complexity before capacity requires
them.

## Daily Target

The normal scheduler target is 5,000 collected image candidates per day, not
5,000 guaranteed accepts. Before using the bulk profile, launch a separate
500-candidate canary run and preserve every decision, route, model version, and
publication checkpoint. The production pipeline then processes a larger run in
bounded inference batches and 256-object R2 checkpoint batches; it does not
materialize all candidates or images in memory at once.

After the first 500-candidate canary, calculate the observed acceptance yield:

```text
required candidates per day = accepted-artwork goal / days / acceptance yield
```

At a 50 percent yield, 5,000 candidates produce about 2,500 accepted artworks
per day: roughly four days for 10,000 accepted artworks and eight days for
20,000. Reaching 20,000 in four days at that yield requires about 10,000
candidates per day. Raise the Airflow `candidate_target` only after the canary
confirms disk, R2 publication, error rate, and end-to-end throughput; the DAG
already accepts a higher per-run value without a code change.

The checked-in bulk profile targets at most 20,000 collected candidates in one
restartable run and caps each artist at 20 source-image candidates. With a
1,000-artist roster this prevents prolific accounts from dominating the sample.
It does not promise 20,000 accepted artworks because moderation and SigLIP still
remove unsuitable candidates:

```text
configs/intake_scheduler.bulk.toml
config/bluesky_actors.local.csv
configs/artwork_filter.bulk.toml
configs/production.alpha-cloud.local.toml
```

The roster loader accepts CSV columns named `did`, `bluesky_did`, `actor`,
`handle`, `bluesky_handle`, `profile_url`, `bluesky_url`, or `url`. It strips
leading `@`, extracts identities from `bsky.app/profile/...` URLs, deduplicates
case-insensitively, and preserves first-seen order. The private CSV is ignored
by Git and excluded from Docker image build contexts, while the read-only
`config/` bind mount makes it visible to Airflow at runtime.

Create the ignored cloud config by copying
`configs/production.alpha-cloud.example.toml`, then replace its R2 account
endpoint. The bulk scheduler passes that exact config path into the DAG and
refuses to start when it is missing, preventing an accidental local-object-store
run.

The bulk filter policy uses the pinned SigLIP revision and a `0.60` useful-art
threshold selected from the current 38 human labels. It accepts configured art
classes, including sketches, while rejected visual classes, routing classes,
reposts, source moderation, and deterministic media rules retain precedence.
The zero-shot class softmax remains audit evidence rather than calibrated
probability.

The order for each run is:

1. Prove R2 write, read, and delete access with a 128-byte canary.
2. Collect Bluesky candidates from the private actor roster.
3. Download thumbnails with bounded concurrency and run batched SigLIP triage.
4. Download accepted full-size images, standardize them locally, and checkpoint
   decisions/routes in SQLite. Rejected images are never retained full-size.
5. Publish immutable accepted originals to R2 with bounded parallel workers.
6. Compute pinned CLIP plus CSD embeddings.
7. Publish metadata and object references to PostgreSQL.
8. Drain the PostgreSQL Qdrant outbox, or sync the same slice from SQLite during
   the pre-cutover period.
9. Reconcile PostgreSQL eligibility, Qdrant point count, and R2 publication
   counts before starting the next slice.

Initial bulk concurrency is deliberately separated by resource:

| Stage | Initial bound | Configuration |
| --- | ---: | --- |
| SigLIP thumbnail HTTP | 16 | `downloads.max_concurrency` |
| Accepted full-size HTTP | 24 | `downloads.fullsize_max_concurrency` |
| R2 object PUT/HEAD | 16 | `object_store.upload_concurrency` |
| R2 SQLite checkpoints | 256 objects | `upload_checkpoint_batch_size` |

R2 objects use content-addressed keys, `Content-MD5` upload validation,
SHA-256 object metadata, MIME type, and immutable cache metadata. Publication
retries transient failures up to five times with exponential jitter. Successful
transfers are checkpointed by the single SQLite owner; worker threads never
write SQLite directly. The run summary records bytes uploaded, duration,
objects/second, MiB/second, unchanged checkpoints, and failures.

Do not increase all three concurrency values together. If R2 returns repeated
5xx responses, lower R2 uploads to 8 first. If Bluesky/CDN requests return 429,
lower the two HTTP bounds. SigLIP and CLIP/CSD inference remain single-owner
batched stages regardless of network concurrency.

Qdrant upload time should be minor. CLIP/CSD encoding is the expensive stage.
Record candidates and accepted images per minute and use the first 500-candidate
slice to decide whether the rest of the week's embedding job stays local or
moves to an ephemeral GPU.

## Start-Of-Day Gates

```bash
.venv/bin/artsearch-qdrant status \
  --production-config configs/production.alpha-cloud.local.toml

.venv/bin/artsearch-qdrant eligibility \
  --production-config configs/production.alpha-cloud.local.toml

.venv/bin/artsearch-deploy preflight \
  --production-config configs/production.alpha-cloud.local.toml
```

Before each run, require:

- Qdrant reports green and `artworks_active` targets the configured collection.
- Local/SQL eligibility and Qdrant exact point counts match the prior checkpoint.
- The Airflow R2 canary succeeds before the collector task is released.
- At least 20 percent free disk remains on the intake host.
- The model versions in `config/config.yaml` have not changed mid-run.

## Publish And Reconcile

```bash
.venv/bin/artsearch-embed --config config/config.yaml

.venv/bin/artsearch-data publish-corpus \
  --config configs/production.alpha-cloud.local.toml \
  --decisions data/filter/decisions.jsonl \
  --upload-concurrency 16

.venv/bin/artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml \
  publish-sqlite --sqlite data/artsearch.db

.venv/bin/artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml \
  drain-qdrant
```

During the current SQLite-led canary, use the existing idempotent Qdrant sync
instead of the final two PostgreSQL commands:

```bash
.venv/bin/artsearch-qdrant sync \
  --production-config configs/production.alpha-cloud.local.toml
```

Never run two embedding owners over the same corpus database, and do not run
multiple uncontrolled Qdrant drain workers against the free half-vCPU cluster.

## End-Of-Day Acceptance

- Decision counts add up to the candidate count.
- Every accepted artwork has a local canonical image and current CLIP/CSD row.
- Every PostgreSQL catalog result has a corresponding R2 object reference.
- Qdrant exact point count equals serving-eligible PostgreSQL count.
- A 50-query ANN evaluation completes without dimension or alias errors.
- One browser upload returns ten image URLs that all load through `/media/`.
- Error, review, throughput, p50, and p95 timing counts are saved for the day.

Pause the next slice on count drift, repeated model failures, R2 hash mismatch,
Qdrant red/grey state, or disk pressure. Individual bad images should still
become auditable per-candidate errors rather than aborting a slice.

Cloudflare recommends its S3-compatible or Workers API rather than the
rate-limited management REST API for high-throughput object operations. These
artworks are small enough for single PUTs; multipart upload would add overhead
and is intended for much larger files. R2 can return 5xx responses when a bucket
is driven with excessive concurrency, which is why the worker count is bounded
and retryable instead of unbounded.
