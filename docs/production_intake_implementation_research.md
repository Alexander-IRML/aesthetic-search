# ArtSearch Production Intake Implementation Research

Status: recommended implementation choices for the next build stage

Researched: 2026-08-12

Current baseline: ArtSearch v0.3.1

Companion architecture: `docs/production_scale_roadmap.md`

Approved capacity and cost profile: `docs/production_capacity_profile.md`

## Recorded Operating Envelope

These product and budget choices were recorded on 2026-08-12:

| Input | Recorded target |
| --- | --- |
| Initial searchable corpus | 5,000 images |
| Three-month corpus | 100,000 images |
| Eight-month corpus | 500,000 images |
| Twelve-month corpus | 1,000,000 images |
| Normal first-year intake | About 2,000 candidates/day plus scheduled backfills |
| Required peak design rate | 10,000 source candidates/day and 5,000 published images/day |
| Maintenance intake | About 500 candidates/day |
| Recurring infrastructure budget | USD 20-50/month, excluding existing local hardware and electricity |
| Rejected byte retention | None after durable decision evidence is committed |
| Alpha audience | Approximately five invited testers; best-effort availability |
| Artwork gate objective | Approximately 95% measured automatic-ACCEPT precision, with REVIEW absorbing uncertainty |

The million-image milestone requires about 2,726 *published* images/day averaged
over the year after the initial 5,000. A flat 2,000/day reaches about 735,000
before accounting for rejects. The staged milestones require approximately:

| Interval | Required published/day | Source candidates/day at 60% acceptance |
| --- | ---: | ---: |
| Initial to month 3 | 1,056 | 1,760 |
| Month 3 to month 8 | 2,667 | 4,445 |
| Month 8 to month 12 | 4,167 | 6,945 |

The architecture therefore treats 2,000/day as the ordinary lane and supports
later backfill bursts. Even 10,000 candidates/day averages only 0.116
candidates/second; storage, reproducibility, and index scale are harder than raw
intake throughput.

## Executive Recommendation

The USD 20-50/month budget changes the first deployment from managed cloud to a
single-node, local-first production pilot:

| Concern | Recommended first choice |
| --- | --- |
| Deployment boundary | Existing local machine for compute; one small VPS only when private alpha serving needs it |
| Bluesky synchronization | Bluesky Tap for selected artist DIDs, behind a source connector interface |
| Bluesky enrichment | Public AppView for profile/post presentation metadata and periodic reconciliation |
| Canonical metadata | Local PostgreSQL with durable backups to Backblaze B2 |
| Object bytes | Local quarantine/cache plus one compact accepted derivative in B2 |
| Work dispatch | PostgreSQL job leases using `FOR UPDATE SKIP LOCKED` |
| Workflow truth | PostgreSQL stage, attempt, and dead-letter records |
| Transaction consistency | Jobs inserted in the same transaction as canonical state changes |
| CPU/model workers | Local versioned Python workers; models loaded sequentially within available RAM |
| Burst compute | Existing local compute first; rented GPU work requires separate explicit spend |
| Vector serving | Local Qdrant OSS with on-disk vectors/indexes and snapshots |
| Global retrieval | Qdrant named dense vectors plus sparse/text and payload indexes |
| Patch reranking | Compressed `16x16` artifacts in B2, fetched only for the shortlist into a bounded local cache |
| Telemetry | Durable PostgreSQL attempts plus OpenTelemetry-compatible structured events |
| Infrastructure | Docker Compose/service units now; cloud adapters and IaC retained as migration targets |

This profile assumes the current machine, its storage, and electricity are not
charged against the USD 20-50 recurring budget. Without existing compute and disk,
the million-image, multi-model goal is not credible at that price. The current
workspace reports about 935 GiB free disk, 8 GiB RAM, and 16 logical CPUs; no
NVIDIA GPU is visible inside the current WSL environment. The disk is adequate
only if raw/rejected bytes and full-corpus patch matrices are not retained. The
RAM requires on-disk Qdrant storage and model workers that do not keep every
large model resident simultaneously.

The code should expose narrow interfaces for source events, object storage,
queues, metadata, and vector publication. Those interfaces preserve local
SQLite workflows and make RDS, SQS, S3, or Qdrant Cloud migrations possible
without rewriting classification and feature logic.

The things I would explicitly *not* introduce yet are RDS, SQS, paid Qdrant Cloud,
Kubernetes, Kafka, Aurora, Triton, a service mesh, a custom distributed
scheduler, or separate deployments for every pipeline function. They solve
real problems, but the budget and five-user alpha do not pay for them yet.

## The Most Important Choices

### Choices to make now

1. Confirm that USD 20-50/month excludes the existing machine, disk, electricity,
   and any one-time storage upgrade.
2. Validate a 14-day compact-review-image TTL against the actual review cadence.
3. Decide the maximum accepted derivative quality after a visual size/quality
   benchmark; this document starts with a 1280-pixel WebP and a 250 KiB average
   target.
4. Define a deletion target. This document starts with p95 serving removal under
   15 minutes while the local node is online.
5. Define the adult-content escape-rate target separately from artwork-gate
   precision before exposing the alpha beyond a reviewed roster.

### Choices that should wait for evidence

- Begin with a PostgreSQL queue, then revisit SQS or Temporal after the first
  durable workflow exposes the real complexity of remote workers, human review,
  and cancellation.
- Patch compression and quantization should be decided by a quality and latency
  benchmark; the budget profile keeps patch artifacts outside Qdrant.
- Vector quantization should wait for exact-versus-quantized relevance tests.
- Triton should wait until direct PyTorch workers leave material GPU capacity
  unused or online inference needs dynamic batching across several models.
- Kafka should wait until event replay, fan-out count, or sustained throughput
  exceeds what the outbox and stage queues comfortably support.
- Kubernetes should wait until the number and scheduling diversity of services
  make Docker Compose and single-host service units operationally inadequate.

## Recommended System Shape

```text
Bluesky Tap / AppView / manual upload adapter
    -> source event transaction in PostgreSQL
    -> transactional PostgreSQL job
    -> bounded downloader
    -> bounded local quarantine + SHA-256
    -> decode/validate/canonical-pixel hash/PDQ candidate
    -> provenance + source labels + safety + SigLIP curation
    -> accepted/review/rejected policy record
    -> accepted canonical derivative and compact patches in B2; rejected bytes deleted
    -> versioned CLIP/SigLIP/DINO feature jobs
    -> PostgreSQL feature publication record
    -> local Qdrant versioned collection upsert
    -> reconciliation confirms canonical DB and serving index agree
```

Every arrow is at-least-once. Every output is protected by a deterministic
idempotency key. A queue message is only a request to examine canonical state;
it is never the only copy of that state.

## 1. Bluesky Source Synchronization

### Finding

The current author-feed poller is appropriate for a private pilot but is not
the strongest production synchronization boundary. Bluesky's Tap service is
designed for tracking selected repositories. It performs automatic historical
backfill, protocol verification, recovery/resync, repository and collection
filtering, and acknowledged at-least-once delivery. Live ordering is guaranteed
per repository. Tap can dynamically add the selected artist DIDs and uses
PostgreSQL for larger deployments.

Tap is still beta, so ArtSearch should not allow Tap event shapes to leak into
the domain model. Preserve a connector protocol such as:

```python
class SourceConnector(Protocol):
    async def events(self) -> AsyncIterator[SourceRecordEvent]: ...
    async def acknowledge(self, cursor: SourceCursor) -> None: ...
    async def reconcile(self, source_account_id: str) -> ReconcileReport: ...
```

### Recommended implementation

- Run one Tap deployment for the ArtSearch account set, backed by PostgreSQL.
- Track DIDs, never mutable handles, as the source account identity.
- Subscribe only to required Bluesky collections, initially posts and relevant
  profile records.
- Persist the normalized source event and its source revision in one database
  transaction before acknowledging Tap.
- Use unique source keys so Tap redelivery becomes a harmless upsert.
- Retain create, update, and delete events. A delete must enqueue serving
  removal and byte-retention evaluation, not merely remove an AppView URL.
- Continue to use AppView for presentation metadata, labels, and a scheduled
  reconciliation sample. It should not remain the only history of a post.
- Keep the existing author-feed collector as a local fallback and migration
  utility.

### Source identity

Use these keys:

```text
source_account_id = bluesky DID
source_item_id    = AT URI
source_revision   = repo rev plus record CID where available
source_media_id   = AT URI + media index + record CID
blob_id           = SHA-256 of the exact acquired bytes
asset_id          = SHA-256 of dimensions, mode, and canonical RGB pixels
```

Do not use a CDN URL as identity. Blob and thumbnail URLs can change while the
logical source record remains the same.

### Why not Jetstream as the primary source

Jetstream is simple JSON and can filter by DID or collection, but Bluesky's own
documentation describes the trust and backfill tradeoffs. It is useful for
best-effort live discovery. Tap is a better match for a provenance-sensitive,
selected-repository corpus because it verifies synchronization and owns
backfill/recovery.

## 2. Durable Work and Orchestration

### Recommended first choice: PostgreSQL job queue

Use a small number of coarse stage queues:

```text
acquire
inspect_and_curate
transform
embed
publish
```

Implement these as rows in a PostgreSQL `jobs` table. Workers atomically claim
ready rows with `FOR UPDATE SKIP LOCKED`, assign a bounded lease, and create an
attempt record. A heartbeat may extend a lease for model batches. Expired leases
become claimable again, bounded retries use `available_at`, and exhausted jobs
move to a queryable dead-letter state. Workers must still assume duplicate and
out-of-order execution.

The job payload should be a compact pointer, not an image or full source record:

```json
{
  "schema_version": 1,
  "job_id": "...",
  "asset_or_source_id": "...",
  "desired_stage": "inspect_and_curate",
  "input_version": "...",
  "traceparent": "..."
}
```

The worker reloads canonical state from PostgreSQL and confirms that the
requested input version is still current before doing expensive work. Stale
messages finish as a recorded no-op.

Because canonical state and the next job share PostgreSQL, insert them in one
transaction and avoid a dual write. PostgreSQL documents `SKIP LOCKED`
specifically as appropriate for queue-like consumers, although it is not
suitable for general consistent reads.

### SQS migration path

At this volume PostgreSQL is enough and removes a paid dependency. Preserve a
`JobQueue` interface. Introduce SQS Standard queues when workers spread across
machines or database queue contention becomes measurable. At that point, use a
transactional outbox to bridge PostgreSQL and SQS; SQS is at-least-once, so the
consumer idempotency rules do not change.

### When Temporal becomes the better choice

Temporal is credible here because its workflows are durable and recover from
recorded event history, while activities are ordinary idempotent functions with
retries and checkpointing. Move to Temporal Cloud when at least two of these
become true:

- Human review pauses individual asset workflows for hours or days.
- Cancellation and deletion must interrupt many outstanding downstream steps.
- Compensation logic spans several services and is becoming difficult to audit.
- Operators need one per-asset workflow timeline more than they need simple
  queue throughput.
- Retry/timer/state-machine code becomes a material part of the application.

Do not self-host Temporal initially. If selected, keep PostgreSQL as canonical
corpus truth; Temporal owns orchestration history, not artwork identity.

## 3. Canonical PostgreSQL Model

### Recommended table groups

| Table | Purpose |
| --- | --- |
| `source_accounts` | DID, mutable display metadata, source policy, tracking state |
| `source_items` | AT URI, repo revision, CID, source timestamps, deletion state |
| `source_media` | Media index, blob ref, declared metadata, current source version |
| `blobs` | Raw SHA-256, immutable object key, byte count, media type, acquisition evidence |
| `assets` | Canonical decoded pixel identity, dimensions, mode, and image-level hashes |
| `asset_blobs` | Raw encodings that decode to the same canonical asset |
| `asset_occurrences` | Many source media references that resolve to one asset |
| `derivatives` | Object key, transform fingerprint, dimensions, checksum |
| `policy_decisions` | Versioned SigLIP, safety, provenance, and human outcomes |
| `corpus_memberships` | Artwork/version inclusion in an immutable corpus version |
| `feature_sets` | Model commit, preprocessing, dtype, dimensions, normalization |
| `features` | One asset/version and feature-set publication record |
| `job_attempts` | Stage timing, worker version, retry and failure evidence |
| `jobs` | Transactional work requests, leases, retries, and dead-letter state |
| `outbox_events` | Later external queue/index events when a dual-write boundary exists |
| `index_publications` | Qdrant point/collection publication and reconciliation state |
| `tombstones` | Source delete, opt-out, policy removal, and purge completion |

### Modeling rules

- Do not put one mutable `status` column on an asset to represent the whole
  pipeline. Store independent versioned stage outcomes.
- Use relational columns for fields involved in constraints, joins, ordering,
  filters, and operations. Keep raw source envelopes or model evidence in
  bounded JSONB fields.
- Use unique constraints for source identity, content hashes, derivative
  fingerprints, policy snapshots, feature publications, and outbox keys.
- Use `INSERT ... ON CONFLICT` for idempotent writes, but define exactly which
  fields may update on conflict.
- Partition append-heavy tables such as attempts and policy evidence only when
  measurements justify it. Monthly range partitions are a sensible eventual
  fit, but PostgreSQL warns that unnecessary partition counts can harm planning.
- Keep migrations in Alembic and test both forward migration and restore.

### PostgreSQL product choice

Start with local PostgreSQL on the existing machine. Back up the schema and
data daily to an encrypted local artifact, copy that artifact to B2, retain a
small rotation, and perform a monthly restore test. Keep PostgreSQL private to
the host or private network.

Move to ordinary RDS PostgreSQL, not Aurora, when availability, remote workers,
or operational burden earns a managed database. At that point enable
encryption, automated backups, point-in-time recovery, deletion protection, and
private networking. SQLite remains the deterministic fixture and small local
workflow adapter, not the year-one job coordinator.

## 4. Object Storage

### Recommended first choice: local quarantine plus Backblaze B2

Use separate policy boundaries, either buckets or strongly isolated prefixes:

| Boundary | Contents | Typical access |
| --- | --- | --- |
| Local quarantine | Newly acquired untrusted bytes with a short processing TTL | Downloader and decoder only |
| B2 corpus | One sanitized compact derivative for every accepted asset | Intake, feature workers, private serving |
| B2 features | Compact patch artifacts and versioned feature manifests | Feature and retrieval workers |
| Local feature cache | Global build intermediates and bounded hot patch matrices | Feature and retrieval workers |
| B2 reports/backups | Database dumps, evaluation exports, manifests, selected snapshots | Internal tooling only |

Object keys should be content-addressed and immutable, for example:

```text
quarantine/sha256/ab/cd/<raw_sha256>
corpus/<asset_id>/<transform_fingerprint>.webp
backups/postgres/YYYY-MM-DD/<checksum>.dump.zst
```

Supply and verify checksums on upload. Store the independent application
SHA-256 in PostgreSQL; do not treat an object-store ETag as a universal content
hash. Use private buckets, narrowly scoped API tokens, and short-lived signed
delivery URLs or an authenticated image proxy where needed.

For accepted images, start by benchmarking a metadata-stripped, sRGB WebP with
a 1280-pixel longest edge, visually acceptable quality, and an average target
of 250 KiB. Retain no second model-sized derivative; resize the canonical image
in memory for embeddings. Generate small UI thumbnails on demand and keep them
in a bounded cache. Preserve image dimensions, source identity, raw hash, pixel
hash, transform fingerprint, and policy evidence in PostgreSQL.

Rejected images retain decision metadata only. Delete quarantine bytes as soon
as the durable REJECT record commits. REVIEW retains only a compact review
derivative for 14 days by default; expiry deletes it unless a reviewer has
promoted or explicitly retained the item. Accepted raw source bytes are deleted
after the canonical derivative and its checksum are durably stored.

### Versioning and deletion

Keep content-addressed corpus objects immutable and deletable. Do not apply an
object lock or retention mechanism that prevents source deletion or opt-out.
Give physical delete permission only to a narrowly scoped purge role so
ordinary workers cannot remove corpus objects.

### Cost envelope and later migration

Backblaze B2 currently starts at USD 6.95/TB-month, includes the first 10 GB,
and includes egress up to three times average stored data. Cloudflare R2
Standard is USD 0.015/GB-month with free egress. At the expected five-user query
volume, B2 is cheaper and its included egress is ample.

At one million images, a 250 KiB canonical object plus a 384 KiB raw float16
compact patch artifact is about 649 GB before compression and overhead. That is
about USD 4.44/month in B2 after the free allowance. Budget USD 6-10/month for
feature manifests, database backups, snapshots, temporary rollback versions,
and growth headroom.

Use a small S3-compatible object-store interface and compatibility tests rather
than allowing B2 semantics into domain code. Reconsider R2 when measured public
delivery or patch-read egress exceeds B2's included allowance; move to S3 only
when co-location with AWS compute produces a lower measured total cost.

## 5. Acquisition and Untrusted Image Handling

The production downloader must be stricter than an ordinary HTTP client:

- Accept only URLs emitted by a trusted source connector.
- Allowlist schemes and expected host families; reject credentials in URLs.
- Resolve DNS and block private, loopback, link-local, and metadata-service
  addresses to prevent SSRF.
- Revalidate every redirect target and cap redirect count.
- Use connect, read, write, and total timeouts plus a total byte cap.
- Stream to a bounded temporary object while calculating SHA-256.
- Check declared type, magic bytes, and decoder result independently.
- Apply per-host token buckets, jittered backoff, and `Retry-After` handling.
- Retry timeouts, connection resets, 429, and selected 5xx responses. Do not
  retry 404, unsupported media, policy denial, or malformed images forever.
- Never decode SVG or arbitrary active formats.

Perform decode in an isolated non-root container/task with a read-only root
filesystem, bounded temporary storage, memory/CPU/time limits, and no network
access. Enforce compressed byte, decoded pixel, dimension, frame, metadata, and
animation limits before transformation.

Pillow already provides useful decode behavior and decompression-bomb limits.
For high-volume resize and derivative generation, evaluate pyvips/libvips.
Libvips is demand-driven and keeps only active pixel regions in memory, which is
a strong fit for large batch transformations. Keep one canonical transform
specification and golden fixtures so switching engines does not silently change
feature preprocessing.

## 6. Deduplication and Provenance

Use a ladder rather than one magic duplicate flag:

| Signal | Use |
| --- | --- |
| Raw SHA-256 | Exact byte identity and object key |
| Canonical pixel SHA-256 | Same decoded RGB pixels despite metadata/container differences |
| PDQ perceptual hash | Near-duplicate candidate generation after threshold evaluation |
| Embedding similarity | Offline semantic/style duplicate investigation, not automatic identity |

PDQ is designed as a scalable perceptual hash and emits a quality score. Its
reference project warns that image conversion implementations can produce
slightly different hashes and recommends evaluating thresholds on local data.
Store the implementation and preprocessing version with every perceptual hash.

Never discard occurrence records when bytes deduplicate. One asset may have
several posts, artists, captions, and provenance claims. Exact storage
deduplication and attribution are separate questions.

Near-duplicate auto-merging should be deferred until review data supports a
threshold. Initially, create a duplicate-review edge and let publication policy
choose which occurrence is canonical.

## 7. Curation, Safety, and Review

Keep independent policy axes:

```text
is_useful_artwork
adult_safety_state
source_label_state
provenance_state
attribution_state
permission_or_opt_out_state
duplicate_state
quality_state
```

SigLIP answers the useful-artwork question. It must not be treated as an adult
safety, attribution, or permission model. Bluesky labels and text filters are
valuable evidence but should not be the only adult-content control, especially
for stylized and furry artwork where domain shift is likely.

Do not select a production adult classifier from its public benchmark alone.
Build a private, access-controlled, domain-representative evaluation set and
compare candidate models on false-safe rate, false-block rate, calibration,
latency, and behavior by content subtype. Until that exists:

- block source-labeled explicit content;
- route ambiguous visual content to review;
- sample accepted and rejected decisions for drift measurement;
- keep policy/model/version evidence;
- never expose unreviewed bytes in a public dashboard.

Review decisions should be append-only events. A newer policy can supersede an
older decision without erasing what the older model did.

## 8. Transform and Feature Contracts

Every derivative and feature needs a fingerprint over all behavior that can
change its bytes or vector:

```text
source asset hash
decoder and color-management version
EXIF orientation behavior
crop/pad/resize algorithm and dimensions
alpha background and output format/quality
model repository and full commit hash
processor configuration
normalization and pooling
dtype and output dimension
patch grid and patch derivation method
software/container version
```

Production workers should not download a moving Hugging Face `main` branch at
startup. Resolve and record a full commit hash, download the immutable snapshot
during an explicit model-build step, verify expected files/checksums, and make
the container or a private model artifact store the runtime source. Hugging
Face's download APIs support full commit revisions and version-aware caching.

Use direct PyTorch inference workers first:

- one model owner per worker process;
- `eval()` and inference mode;
- bounded dynamic microbatches;
- explicit device/dtype and OOM retry policy;
- batch shape and fill-rate metrics;
- one output commit after artifact validation;
- no silent model fallback.

Triton supports dynamic batching, concurrent models, health endpoints, and
metrics. Adopt it when several clients need shared online inference or direct
workers cannot achieve the target GPU utilization. It is optional machinery for
offline queue consumers, not a prerequisite for scale.

## 9. CPU and GPU Scheduling

### CPU stages

Run Tap consumption, acquisition, inspection, policy fusion, publication, and
reconciliation as pinned local containers or service units. They can share one
repository and image while using different commands and least-privilege
credentials. Scale worker concurrency from PostgreSQL job age and measured
resource use, not CPU alone.

### GPU stages

Use the existing machine for historical and incremental embedding builds. Give
each model recipe a bounded manifest, checkpoint completed asset IDs, keep the
model loaded across many images, and retain item-level evidence. Run different
large models sequentially if memory requires it. Rent interruptible GPU capacity
only when measured backlog cannot recover in the allowed batch window; the same
manifest contract should run there without changing workflow truth.

Do not create one process or remote job per image. A worker should process a
bounded manifest or drain a bounded job window while preserving item-level
evidence.

## 10. Qdrant Serving Design

### Collection strategy

Use a physical collection per complete serving feature recipe, then move an
alias atomically after shadow evaluation:

```text
artworks_2026_08_semanticA_visualB_v1
artworks_active -> artworks_2026_08_semanticA_visualB_v1
```

Qdrant documents aliases specifically for switching vector versions without
stopping queries. Never change the meaning, model, dimension, or preprocessing
of a named vector inside the live physical collection.

Initial named vectors:

| Vector | Role |
| --- | --- |
| `semantic` | CLIP/SigLIP-compatible text and image concept recall |
| `visual_global` | DINO pooled pose, composition, and appearance recall |
| `style` | Future starred DINO-LoRA style projection |
| `detail_patches` | Optional float16 multivector used only for shortlist MaxSim |
| `text_sparse` | Caption, alt text, tags, and artist/exact-term retrieval |

Keep payload compact and index only fields used in filters. Create payload
indexes before loading the collection so filtered HNSW is built with them.
Likely indexed fields are eligibility, deletion state, adult-safety state,
review state, artist ID, media type, and corpus version.

### Hybrid recall and fusion

Qdrant's Query API supports named dense and sparse prefetches, RRF, DBSF, and
multi-stage rescoring. Start with RRF because it combines rank positions without
pretending raw scores from different models share a calibrated scale. Move to
weighted RRF or calibrated score fusion only after train/validation judgments
exist.

For each retriever preserve raw score, source rank, candidate budget, and vector
version in the query trace even if Qdrant performs the server-side fusion.

### HNSW and quantization

Do not copy a blog's HNSW values. Benchmark the ArtSearch corpus and filter
distribution against exact search. Sweep `m`, `ef_construct`, query `hnsw_ef`,
on-disk settings, shard count, and candidate budget. Report recall@k, nDCG,
p50/p95/p99 latency, build time, update throughput, RAM, disk, and cost.

Scalar float32-to-int8 quantization is a plausible first compression test, but
Qdrant explicitly describes quantization as a recall/resource tradeoff. It does
not enter the default path until the exact-vs-quantized report passes a defined
quality bound.

### Availability choice

Use single-node Qdrant OSS for the first real deployment. Qdrant Cloud's free
tier is useful for a prototype or shadow index, but its 1 GB RAM and 4 GB disk
limits cannot be assumed to hold one million ArtSearch points with multiple
named vectors, payload indexes, and HNSW overhead. A single node matches the
five-user, best-effort alpha; retain snapshots and the ability to rebuild from
PostgreSQL and B2. Add replication only before promising meaningful external
availability.

## 11. DINO Patch Storage Decision

The budget profile chooses a separate patch feature store: one compressed,
checksummed artifact per artwork and feature fingerprint in B2, fetched only for
the shortlist and retained in a bounded local disk LRU. Keeping patch values out
of PostgreSQL and Qdrant avoids making the vector index hundreds of GiB larger
and keeps it cheap to rebuild.

### Capacity math

For the starred `16 x 16` grid and current 768-dimensional DINO features:

```text
256 patches x 768 dimensions x 2 bytes float16 = 393,216 bytes/image
                                                = 384 KiB/image
1,000,000 images                                = about 366 GiB raw
```

This excludes point metadata, segment overhead, temporary optimization space,
backups, and replication. At replication factor two, raw patch values alone are
about 732 GiB.

For comparison, one 768-dimensional float16 global vector is only about 1.43
GiB per million images before overhead. Patch storage, not global ANN vectors,
will dominate the feature bill.

### Required benchmark

At representative 10k, 100k, and projected 1M scales, measure object size,
upload throughput, p95 top-50 fetch plus rerank latency, local-cache hit rate,
concurrent query behavior, and exact ranking agreement. Compare float16 with
int8 patches and candidate compression formats. A Qdrant multivector remains an
experimental comparison, not the default storage path.

Also benchmark direct lower-resolution DINO inference against deterministic
pooling of the current `32 x 32` grid. The former saves ingest compute; the
latter may preserve more detail. The derivation method belongs in the feature
fingerprint.

## 12. Publication, Deletes, and Reconciliation

PostgreSQL is the source of truth and Qdrant is a rebuildable projection.
Publication follows this pattern:

1. Commit validated feature metadata and an outbox publication event.
2. The publisher idempotently upserts the complete Qdrant point.
3. Record the collection, point ID, feature recipe, payload hash, and Qdrant
   acknowledgement.
4. A reconciler samples continuously and performs periodic full comparisons of
   eligible PostgreSQL rows versus Qdrant points.
5. Index aliases move only after coverage, count, recall, and latency gates pass.

Deletion is an independent high-priority workflow:

```text
source delete or opt-out
    -> tombstone transaction
    -> query/API eligibility disabled
    -> Qdrant point removed from every live collection
    -> occurrence detached and shared-asset references checked
    -> derivative and source-byte retention policy executed when no permitted occurrence remains
    -> cache/CDN invalidated
    -> purge evidence recorded
```

The first operation should make the item ineligible even if physical byte purge
takes longer. Define and monitor both serving-removal and byte-purge SLOs.

## 13. Observability

Instrument every worker with OpenTelemetry and propagate trace context in queue
messages. OpenTelemetry's messaging conventions distinguish send, receive,
process, and settle operations and recommend propagating message creation
context. Use low-cardinality metric dimensions; asset IDs belong in traces and
logs, not metric labels.

### Durable per-attempt evidence

Store this in PostgreSQL:

```text
job_id, attempt_id, source/asset/artwork IDs
stage, desired input version, produced output version
queue, worker and container version
queued/start/finish timestamps
status, retryability, attempt number, reason code
bytes read/written, batch ID and batch size
model/feature/policy version
trace ID
```

### Aggregate metrics

- Stage successes, rejects, reviews, errors, and no-ops per minute.
- Queue depth and oldest-message age.
- Queue wait and execution p50/p95/p99.
- Download bytes, HTTP status, host throttle, retry, and permanent failure rates.
- Decode and transform throughput by format and size bucket.
- Exact and near-duplicate rates by source.
- SigLIP/safety outcome distribution and sampled-review disagreement.
- GPU utilization, memory, batch fill, images/second, OOM, and cost per 1,000.
- Feature coverage by feature-set version.
- Qdrant upsert/delete lag, point drift, index build state, RAM/disk, and query
  latency.
- End-to-end discovery-to-searchable freshness.

Qdrant exports Prometheus/OpenMetrics metrics. OpenTelemetry keeps application
instrumentation vendor-neutral. CloudWatch plus managed Grafana is a reasonable
AWS-first backend; Grafana Cloud or another OTLP backend can replace it without
changing domain events.

### Initial SLO placeholders

Do not call these promises until capacity tests validate them, but track them
from the first implementation:

| SLO | Internal starting target |
| --- | --- |
| No lost acknowledged source events | 100 percent, verified by reconciliation |
| Duplicate serving records | 0, enforced by identity constraints |
| Serving deletion after tombstone | p95 under 15 minutes |
| Incremental freshness | p95 under 60 minutes during normal load |
| Permanent item failure visibility | 100 percent in dead-letter/operator view |
| Feature/index version traceability | 100 percent of published points |

## 14. Security and Privacy

- Bind PostgreSQL and Qdrant to localhost or a private network; expose only the
  authenticated query/API boundary.
- Use separate least-privilege credentials for source reads, B2 objects,
  backups, query-only Qdrant access, and index publication.
- Keep credentials in a host secret store or injected secret files, never Git
  or container images.
- Give query services read-only Qdrant credentials and publishers scoped write
  credentials.
- Encrypt object storage, database backups, and network transport.
- Do not place URLs, captions, user query text, secrets, or image bytes in
  unbounded logs.
- Treat review images and alpha query uploads as sensitive, even when source
  artwork is public.
- Define retention and deletion for query images, text, sessions, judgments,
  and interaction telemetry before collecting alpha data.
- Audit access to review tooling and private objects.
- Scan dependencies and container images; patch Pillow/libvips and model-loading
  libraries promptly.
- Load only trusted model artifacts, prefer safetensors, and never unpickle
  arbitrary remote artifacts.

## 15. Deployment and Local Development

### Production deployment units

Keep the first deployment to a small set:

1. `source-sync`: Tap integration and AppView reconciliation.
2. `intake-worker`: acquisition, validation, policy, and derivatives, with
   commands or queue selection separating roles.
3. `feature-worker`: versioned GPU image for SigLIP/CLIP/DINO jobs.
4. `publisher`: Qdrant upsert/delete and reconciliation.
5. `operator-api`: replay, review, status, and deletion controls.

This is a modular monolith with worker roles, not a requirement for five
repositories. Keep shared domain types and migrations together while scaling
processes independently.

### Infrastructure as code

Start with a checked-in Docker Compose file, pinned container versions, service
units, PostgreSQL migrations, a declarative Qdrant bootstrap, B2 bucket-policy
documentation, and backup/restore scripts. Keep production secrets, dumps, and
state files out of Git. Add OpenTofu only when a VPS or managed services make
provisioned infrastructure large enough to justify state management.

### Local profile

Run local PostgreSQL and Qdrant with Docker Compose. Use a local filesystem
object adapter and an in-process queue adapter for most tests, plus B2 contract
tests against an isolated prefix. Continue to support SQLite for small
deterministic workflows.

The same domain service should receive injected adapters. Avoid branches such
as `if production:` inside classification and feature code.

## 16. Testing and Release Gates

### Correctness tests

- Source contract tests with recorded Tap/AppView create, update, delete,
  repost, quote, multi-image, label, and malformed events.
- Idempotency tests that deliver every stage message twice and concurrently.
- Crash-point tests after object upload, database commit, queue send, model
  artifact write, Qdrant upsert, and message acknowledgement.
- Decoder corpus tests for corrupt, huge, animated, truncated, metadata-heavy,
  and adversarial files.
- Golden transform tests across Pillow/libvips implementations.
- Model batch/single equivalence and immutable revision tests.
- Deletion tests that cover active indexes, caches, object versions, retries,
  and replayed stale messages.

### Integration tests

- PostgreSQL plus S3-compatible storage plus Qdrant in containers.
- B2 canary tests against an isolated bucket prefix, including upload, checksum,
  range/read, lifecycle, and delete behavior.
- Qdrant collection build, payload indexes, shadow query, alias switch, and
  rollback.
- Backup restore and canonical-to-Qdrant full rebuild.

### Load and quality gates

- Replay a representative manifest at 1x, 5x, and 10x target daily intake.
- Inject 429, timeout, worker death, interrupted batch work, PostgreSQL restart, and
  Qdrant unavailability.
- Require stable queue age and bounded retries after recovery.
- Measure cost and throughput per 1,000 images for each stage.
- Compare HNSW with exact truth under representative metadata filters.
- Compare patch storage options and `16 x 16` versus `32 x 32` quality.
- Require a calibration report before changing automatic acceptance or safety
  thresholds.

## 17. Capacity Worksheet

The initial operating envelope is now recorded. Measurements marked below must
be filled from pilot runs before provisioning beyond the local node:

| Input | Decision |
| --- | --- |
| Initial corpus | 5,000 accepted images |
| 12-month corpus | 1,000,000 accepted images |
| New source images/day | About 2,000 ordinary; design for 10,000 candidates and 5,000 accepts |
| Backfill target | Measure; must support milestone rates up to 4,167 accepted/day |
| Average and p95 source bytes | Current accepted mean about 688 KB; measure p95 on a larger sample |
| Accepted/review/reject rates | Measure from current pilot |
| Incremental freshness | Start with p95 under 24 hours; measure nightly batch completion |
| Query traffic | Five-user alpha; instrument before assigning a QPS target |
| Search latency | TBD p50/p95/p99 |
| Source byte retention | Accepted canonical only; 14-day compact REVIEW; no REJECT bytes |
| Feature retention | Active version plus one rollback version during migration |
| Alpha availability | Best effort, single node |
| Monthly budget | USD 20-50; alert at USD 40 and require a decision above USD 50 |

Use current runs to estimate each stage rather than guessing from model cards.
Record CPU-seconds, GPU-seconds, bytes read/written, and output bytes per image.

### Feature storage estimates per million images

These are raw values before database overhead, backups, and replication:

| Feature | Shape/dtype | Approximate raw size |
| --- | --- | ---: |
| One global vector | 768 x float16 | 1.43 GiB |
| Two global vectors | 2 x 768 x float16 | 2.86 GiB |
| DINO compact patches | 256 x 768 x float16 | 366 GiB |
| DINO current patches | 1024 x 768 x float16 | 1.43 TiB |

The starred compact grid is a major improvement, but patch storage still needs
its own budget and benchmark.

## 18. Recommended Build Order

### Slice 0: Contracts and measurements

- Complete the capacity worksheet with current-pipeline measurements.
- Freeze identity, stage-event, feature-set, policy, and deletion contracts.
- Add PostgreSQL migrations and adapter boundaries while keeping SQLite tests.
- Define object keys, idempotency keys, and reason codes.

Exit: one local image can traverse the new contracts without cloud services.

### Slice 1: Durable acquisition

- Add S3-compatible object storage and bounded acquisition.
- Implement raw and canonical-pixel hashes, immutable objects, and occurrence
  records.
- Add quarantine, decoder isolation, derivative fingerprints, and purge.

Exit: duplicate deliveries create one asset and multiple correct occurrences.

### Slice 2: Queue-backed workflow

- Add PostgreSQL job leases, dead-letter state, attempt records, retry classes,
  and replay CLI.
- Instrument OpenTelemetry and queue-age/throughput dashboards.
- Prove crash recovery and stale-message no-ops.

Exit: workers can be killed at every stage without losing or duplicating corpus
state.

### Slice 3: Bluesky Tap adapter

- Deploy Tap in a non-production environment.
- Normalize and persist create/update/delete events for the selected DID pool.
- Backfill a small reviewed artist cohort and reconcile it against AppView.

Exit: source history and deletes remain correct through restart and redelivery.

### Slice 4: Batch curation and features

- Package pinned SigLIP/CLIP/DINO artifacts by full commit.
- Add manifest-sized local batch jobs and incremental model workers.
- Publish feature coverage, throughput, OOM, and cost metrics.
- Add review sampling and safety-evaluation datasets.

Exit: a corpus version can be rebuilt reproducibly from source records and
objects.

### Slice 5: Qdrant shadow index

- Create versioned collections, named vectors, payload indexes, and publication
  reconciliation.
- Backfill global vectors and sparse text.
- Compare exact versus HNSW and test alias promotion/rollback.
- Validate B2 patch fetch/cache behavior and compare compact patch quality with
  the current full-grid baseline.

Exit: Qdrant meets measured recall, latency, filter, storage, and recovery gates
without replacing the current path blindly.

### Slice 6: Production pilot

- Turn on continuous sync for a bounded approved roster.
- Operate deletion, DLQ, restore, and reindex drills.
- Publish a private final dashboard from the versioned Qdrant path.
- Review weekly quality, throughput, storage, and cost reports.

Exit: the team can explain every missing, duplicated, rejected, accepted, and
served image from durable evidence.

## 19. Decision Summary

| Decision | Recommendation | Revisit when |
| --- | --- | --- |
| Tap vs polling | Tap primary, AppView enrich/reconcile | Tap beta behavior is insufficient |
| Queue | PostgreSQL leases and attempt/dead-letter tables | Work spans enough hosts or states to justify SQS/Temporal |
| Relational database | Local PostgreSQL, then the same database on one VPS | Availability or operations justify managed PostgreSQL |
| Object store | Backblaze B2 via an S3-compatible adapter | R2/S3 wins measured egress or co-location cost |
| Deployment | Docker Compose and service units | Service count or scheduling diversity proves this inadequate |
| Direct PyTorch vs Triton | Direct batch workers | Shared online inference or utilization demands Triton |
| Qdrant | Single-node OSS; free Cloud only for prototypes | Availability or operator burden justifies paid Cloud/replicas |
| Patch storage | B2 artifacts plus bounded local LRU | Measured latency or quality requires another representation |
| HNSW tuning | Dataset-specific benchmark | Every corpus/model/filter change |
| Quantization | Off initially | Exact-vs-quantized report passes |
| Near-duplicate merge | Review-only initially | Labeled threshold has acceptable errors |
| Adult model | Evaluate on domain data | Before any automatic public-safe claim |

## Primary Sources

- [Bluesky Tap introduction and delivery guarantees](https://docs.bsky.app/blog/introducing-tap)
- [Bluesky rate limits](https://docs.bsky.app/docs/advanced-guides/rate-limits)
- [Bluesky backfill guidance](https://docs.bsky.app/docs/advanced-guides/backfill)
- [Bluesky Jetstream tradeoffs](https://docs.bsky.app/blog/jetstream)
- [Amazon SQS visibility timeout and at-least-once behavior](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)
- [AWS messaging service decision guide](https://docs.aws.amazon.com/pdfs/decision-guides/latest/sns-or-sqs-or-eventbridge/sns-or-sqs-or-eventbridge.pdf)
- [AWS Batch GPU jobs](https://docs.aws.amazon.com/batch/latest/userguide/gpu-jobs.html)
- [Amazon S3 integrity checks](https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity.html)
- [Amazon S3 Versioning and Lifecycle](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html)
- [Amazon RDS backups](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_WorkingWithAutomatedBackups.html)
- [Amazon RDS encryption](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Overview.Encryption.html)
- [PostgreSQL locking and `SKIP LOCKED`](https://www.postgresql.org/docs/current/sql-select.html)
- [PostgreSQL declarative partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html)
- [Temporal workflow execution](https://docs.temporal.io/workflow-execution)
- [Temporal activities](https://docs.temporal.io/activities)
- [Qdrant production checklist](https://qdrant.tech/documentation/production-checklist/)
- [Qdrant hybrid and multi-stage queries](https://qdrant.tech/documentation/search/hybrid-queries/)
- [Qdrant vectors, datatypes, and multivectors](https://qdrant.tech/documentation/manage-data/vectors/)
- [Qdrant late-interaction multivector guidance](https://qdrant.tech/documentation/tutorials-search-engineering/using-multivector-representations/)
- [Qdrant collection aliases](https://qdrant.tech/documentation/manage-data/collections/)
- [Qdrant Cloud production cluster guidance](https://qdrant.tech/documentation/cloud/create-cluster/)
- [Qdrant quantization](https://qdrant.tech/documentation/quantization/)
- [Qdrant monitoring](https://qdrant.tech/documentation/ops-monitoring/monitoring/)
- [Hugging Face revision-pinned downloads](https://huggingface.co/docs/huggingface_hub/guides/download)
- [NVIDIA Triton dynamic batching](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/batcher.html)
- [libvips demand-driven processing](https://www.libvips.org/API/current/how-it-works.html)
- [Meta PDQ reference implementation](https://github.com/facebook/ThreatExchange/tree/main/pdq)
- [OpenTelemetry messaging conventions](https://opentelemetry.io/docs/specs/semconv/messaging/messaging-spans/)
- [Backblaze B2 pricing](https://www.backblaze.com/cloud-storage/pricing)
- [Backblaze B2 transaction pricing](https://www.backblaze.com/cloud-storage/transaction-pricing)
- [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/)
- [Cloudflare R2 architecture and S3 compatibility](https://developers.cloudflare.com/r2/how-r2-works/)
- [Qdrant Cloud pricing](https://qdrant.tech/pricing/)
- [Hetzner 2026 price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
