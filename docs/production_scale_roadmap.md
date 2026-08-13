# ArtSearch V2 Production-Scale Roadmap

Status: architecture north star and delivery plan

Recorded: 2026-08-12

Current implementation baseline: v0.3.1

This document records the intended path from the current private MVP to a
production-scale visual search system. It translates the supplied "Aesthetic
Search V2" architecture diagram into staged engineering work. It is a planning
document, not a claim that every component shown here is implemented.

Concrete infrastructure research, implementation tradeoffs, and the recommended
first production stack are recorded in
`docs/production_intake_implementation_research.md`.
The approved corpus, intake, retention, quality, and USD 20-50/month envelope is
recorded in `docs/production_capacity_profile.md`.
The first production data-platform slice, including Airflow, Spark, Polars,
HTTPX, and S3-compatible storage, is implemented and documented in
`docs/production_data_platform.md`.

The current implementation remains documented in `docs/mvp_report.md` and
`docs/artsearch_architecture.puml`. Those documents describe what runs today;
this document describes where the system is going.

## Product Targets

The next era of work has three major targets.

1. Build reliable, observable tooling for large-scale image intake and feature
   preparation.
2. Move retrieval from exact in-memory corpus scans toward Qdrant, HNSW, named
   vectors, metadata filtering, hybrid recall, and staged reranking.
3. Deliver a closed alpha and demo that records enough ranking and interaction
   evidence to evaluate and improve the system.

These targets share one foundation: stable identities, reproducible model and
index versions, idempotent jobs, deletion propagation, and end-to-end
observability. Scale work without those contracts would only make mistakes
faster.

### Approved Operating Envelope

The initial searchable corpus is 5,000 accepted artworks, growing to 100,000 at
three months, 500,000 at eight months, and 1,000,000 at twelve months. Ordinary
first-year intake is about 2,000 candidates/day, with the implementation sized
for backfills of 10,000 candidates/day and 5,000 accepted images/day. Mature
maintenance is about 500 candidates/day.

The first alpha serves approximately five invited users on best-effort,
single-node infrastructure. Rejected bytes are deleted after durable evidence
commits. The automatic artwork gate targets at least 95% measured precision,
with uncertainty routed to review. The complete arithmetic and cost model lives
in the capacity profile.

## Current Baseline

ArtSearch v0.3.1 establishes a useful vertical slice:

| Area | Current behavior |
| --- | --- |
| Collection | Manual folders and read-only Bluesky author-feed collection |
| Qualification | Decode/media rules, provenance rules, text signals, and SigLIP 2 zero-shot artwork triage |
| Corpus | Local raw and processed files with SQLite metadata and audit evidence |
| Features | CLIP global image vectors, DINOv2 pooled vectors, and DINOv2 patch grids |
| Recall | Exact in-memory DINO pooled cosine search |
| Precision | DINO patch MaxSim reranking over a configurable shortlist |
| Semantic evidence | CLIP cosine rank is calculated independently and displayed diagnostically |
| Evaluation | Static local workbench, review sessions, judgment exports, and offline metrics |
| Production data platform | Airflow intake/audit DAGs, HTTPX connection controls, Polars Parquet manifests, s3fs immutable storage, and Spark corpus reconciliation |

The current default ranking is not a weighted CLIP/DINO mega-vector. DINO
pooled similarity creates the shortlist, DINO patch MaxSim determines final
order, and CLIP remains a separate semantic lens. That separation is worth
preserving as retrieval becomes more sophisticated.

## Architectural Commitments

The following are directional commitments for the production design.

| Decision | Direction |
| --- | --- |
| Vector retrieval | Qdrant with HNSW-backed named global vectors and payload filters |
| Retrieval shape | Multi-representation, multi-stage candidate funnel |
| Corpus bytes | Object storage rather than database BLOBs |
| Canonical metadata | Durable relational store; Qdrant is a derived serving index, not the source of truth |
| Patch features | Low-resolution DINO grids stored as compressed B2 artifacts and fetched only for reranking candidates |
| Hybrid recall | Union candidates from semantic, visual, style/facet, sparse/text, and metadata-aware retrieval |
| Ranking evidence | Preserve every component score and stage rank independently |
| Deployment | Versioned index builds with shadow validation and alias-based promotion |
| Learning loop | Join offline judgments and consented alpha interactions to exact model/index versions |

### Starred North-Star Decisions

**[STAR] Compact patch late interaction:** retain DINO patch MaxSim, but make a
`16x16` token grid the initial production target instead of the current
`32x32` grid. This reduces stored patch tokens by 4x and the pairwise MaxSim
similarity matrix by approximately 16x. The full-resolution grid remains an
offline quality ceiling for ablation tests, not a production requirement.

**[STAR] Learned artistic-style representation:** after sufficient human style
judgments exist, adapt a frozen DINO backbone with LoRA and a compact projection
head. Style-specific contrastive or ranking supervision should define the
target space; a DINO-style teacher consistency objective can preserve useful
visual structure. The resulting normalized vector becomes a versioned Qdrant
`style` named vector. This future style vector complements rather than replaces
the low-resolution patch-detail reranker.

For the frugal alpha, these choices are now fixed: Backblaze B2 object storage,
PostgreSQL metadata and job leases, single-node Qdrant OSS, local model workers,
and Docker Compose/service units. They remain adapter boundaries so measured
growth can justify a managed replacement later.

## Production North Star

```text
OFFLINE SUPPLY PLANE

Manual uploads / Bluesky / future connectors
    -> source discovery and durable source records
    -> bounded download into object storage
    -> decode, validation, hashing, duplicate checks
    -> moderation, provenance, SigLIP gate, human review
    -> canonical derivatives and corpus-version membership
    -> versioned embedding jobs
    -> metadata database + Qdrant named-vector indexes + versioned patch storage

ONLINE QUERY PLANE

Text / reference image / positive and negative examples / filters / controls
    -> query intent compiler
    -> semantic, visual, style, facet, sparse, and metadata query plans
    -> parallel ANN and filtered recall
    -> candidate union and deduplication
    -> exact normalized multi-score fusion
    -> low-resolution DINO patch late interaction [STAR]
    -> optional joint VLM reranker
    -> diversity, duplicate, safety, and policy pass
    -> ranked results with complete evidence

LEARNING AND OPERATIONS PLANE

Ingest events + ranking traces + judgments + interactions
    -> quality and relevance evaluation
    -> latency, throughput, capacity, and cost observability
    -> calibration, fusion-weight training, hard-negative mining
    -> versioned model/index candidates returned through shadow evaluation
```

## Target 1: Large-Scale Image Intake

### Supply-Plane Responsibilities

The intake system should separate source discovery from byte acquisition and
from expensive model work. Each stage must be resumable and independently
observable.

| Stage | Responsibility | Durable output |
| --- | --- | --- |
| Discover | Read source APIs or upload manifests | Source item and media-reference records |
| Acquire | Download bytes with limits, retries, and checksums | Immutable object plus content hash |
| Validate | Decode, inspect dimensions/format, strip unsafe metadata | Validated media record |
| Deduplicate | Exact hash first; perceptual/semantic review later | Canonical asset ID and duplicate relationship |
| Curate | Apply policy, provenance, SigLIP, safety, and human decisions | Versioned inclusion decision |
| Transform | Produce model-ready and display derivatives | Versioned derivative objects |
| Embed | Run model-specific batch workers | Feature artifact with model fingerprint |
| Publish | Upsert metadata and vectors into serving indexes | Index publication record |

Every stage should accept at-least-once delivery. Idempotency must come from
stable keys and state transitions rather than an assumption that a worker runs
only once.

### Stable Identity and Lineage

The production data model needs distinct identities for these concepts:

| Identity | Meaning |
| --- | --- |
| `source_item_id` | A source post, upload, or external record |
| `source_media_id` | One media position/version within that source item |
| `asset_id` | Canonical decoded image content, normally content-addressed |
| `artwork_id` | Corpus-visible logical artwork record |
| `artwork_version_id` | Exact bytes, transform, and policy snapshot used for features |
| `feature_set_id` | Model, revision, preprocessing, dtype, normalization, and dimensions |
| `corpus_version` | Immutable membership snapshot used to build an index |
| `index_version` | Physical serving-index build tied to corpus and feature versions |

Source edits, deletes, opt-outs, and policy changes must be able to invalidate
derived objects and index points without destroying historical audit evidence.
Serving removal and historical retention are separate policies.

### Recommended Storage Boundaries

| Store | Owns | Does not own |
| --- | --- | --- |
| Object store | Accepted canonical images, temporary review previews, patch-feature artifacts, and backups | Workflow truth or ranking policy |
| Relational metadata store | Identity, provenance, workflow state, decisions, versions, deletion state | Large image or patch BLOBs |
| Qdrant | Derived global vectors and filterable serving payload | Canonical metadata or full audit history |
| Queue/job system | Pending work, leases, retry scheduling, dead letters | Permanent corpus truth |
| Metrics/log system | Aggregated operational telemetry and searchable events | User-facing result state |

SQLite should remain supported for local development and deterministic fixtures.
Production metadata and job coordination should not depend on one local SQLite
writer.

### Intake Observability

The system should record both immutable stage attempts and aggregate metrics.
An ingest attempt needs at least:

| Field | Purpose |
| --- | --- |
| `run_id`, `job_id`, `attempt_id` | Correlate a unit of work and retries |
| `source_item_id`, `asset_id`, `artwork_version_id` | Trace the item through the pipeline |
| `stage` and `worker_version` | Identify what code performed the operation |
| `input_version`, `output_version` | Reproduce or invalidate derived work |
| `queued_at`, `started_at`, `finished_at` | Measure queue delay and execution time |
| `status`, `retryable`, `attempt_number` | Drive retry and dead-letter behavior |
| `bytes_read`, `bytes_written` | Measure bandwidth and storage growth |
| `error_type`, `reason_code` | Aggregate actionable failure classes |

Required intake dashboards should include:

- Items discovered, downloaded, decoded, curated, embedded, and published per
  minute.
- Images per second for SigLIP, CLIP, DINO global, and DINO patch batches.
- Queue depth, oldest-item age, worker concurrency, and end-to-end freshness lag.
- p50, p95, and p99 queue and execution latency for every stage.
- Retry, permanent failure, dead-letter, duplicate, and stale-version rates.
- ACCEPT, REVIEW, REJECT, and ERROR distributions by source and policy version.
- Cache hit rate, network bytes, object-store growth, vector count, and patch
  feature growth.
- CPU, RAM, GPU utilization, GPU memory, batch fill rate, and cost per thousand
  successfully published images.

## Target 2: Large-Corpus Retrieval

### Qdrant Role

Qdrant should serve the high-recall global-vector layer. A logical artwork point
should carry named vectors and a compact payload. Illustrative vector slots are:

| Named vector | Initial role |
| --- | --- |
| `semantic` | Text/image concept retrieval using the selected multimodal model |
| `visual_global` | DINO global appearance, pose, composition, and visual structure |
| `style` | Starred DINO-LoRA style projection after enough preference labels exist |
| `facet_*` | Selected compositional or visual facets justified by evaluation |

The exact model behind a logical slot must not change inside a live physical
collection. New model revisions or dimensions require a new index version,
backfill, shadow comparison, and atomic alias switch.

Qdrant payload should contain only fields needed for filtering and result
assembly, such as artwork ID, artist ID, media type, safety state, review state,
source state, corpus version, and deletion state. The relational store remains
canonical when payload and metadata disagree.

### HNSW and Filtered ANN

HNSW parameters, payload indexes, quantization, replication, and shard count
must be benchmarked against an exact-search truth set. The benchmark must report
recall at each candidate budget, filtered-query behavior, build time, update
cost, memory, disk, and latency percentiles. ANN adoption is successful only if
its recall loss is known and acceptable.

Metadata filters should be applied during ANN retrieval where possible, not
only after ranking. Important filters include safety, source/deletion state,
media type, artist inclusion or exclusion, review state, and policy eligibility.

### Hybrid Candidate Recall

"Hybrid" here should mean a union of complementary retrievers, not an opaque
sum of incompatible raw scores.

| Retriever | Intended contribution |
| --- | --- |
| Semantic dense ANN | Named concepts, text-to-image, broad subject intent |
| Visual dense ANN | Reference-image appearance, pose, layout, visual structure |
| Style/facet ANN | Preference directions and selected visual attributes |
| Sparse/text retrieval | Captions, tags, artist metadata, exact names, uncommon terms |
| Metadata retrieval | Constraint-aware inclusion and targeted browsing |

Each retriever should return artwork IDs, source rank, raw score, normalized
score, retriever version, and candidate budget. Candidate union then deduplicates
by canonical artwork/asset identity while preserving all source evidence.

Reciprocal-rank fusion is a useful first robust baseline. Distribution-based or
calibrated weighted fusion can follow once judgments show how scores behave.
Raw cosine values from different models should never be added without explicit
normalization and evaluation.

### Precision Funnel

The supplied architecture suggests this starting funnel shape:

| Stage | Starting candidate scale | Purpose |
| --- | ---: | --- |
| Multi-ANN and filtered recall | 2,000-10,000 union | Maximize coverage across representations |
| Exact normalized fusion | About 300 | Recompute trustworthy component scores and apply query controls |
| Low-resolution patch late interaction | About 50 | Match local anatomy, pose, objects, marks, and composition with a compact DINO grid |
| Optional joint VLM reranker | About 20 | Reason about complex query-image relations when value exceeds cost |
| Diversity/deduplication/policy | Final page | Avoid redundant or disallowed results |

These are experimental budgets, not service-level objectives. They should be
tuned from corpus size, latency measurements, and recall curves.

### Patch Feature Strategy

DINO patch MaxSim is the current ColBERT-style late-interaction layer. At scale,
patch matrices must not be globally HNSW-indexed or loaded for the entire
corpus. Store them in B2 as versioned compressed artifacts addressable by
`artwork_version_id` and `feature_set_id`, then fetch only the rerank shortlist
through a bounded local disk cache. A Qdrant multivector remains an ablation,
not the budget-profile default.

Production patch artifacts should initially target a `16x16` token grid rather
than the current `32x32` grid. The implementation may obtain that grid through a
lower-resolution encoder pass or deterministic spatial pooling; the choice must
be benchmarked because the former also reduces ingest compute while the latter
preserves the current global DINO pass. Patch resolution, derivation method,
storage dtype, and normalization must be part of the feature-set fingerprint.

The reranker should batch-fetch only the compact patch grids for its candidate
set, perform GPU- or vectorized-CPU scoring, and report fetch time separately
from compute time. Before promotion, compare `16x16` retrieval against the
current `32x32` baseline using the same saved judgments and candidate pools.
Further quantization, token pruning, and approximate late interaction remain
evidence-dependent optimizations.

### Retrieval Version Contract

Every result page must be attributable to a complete retrieval recipe:

- Corpus version and Qdrant physical collection/index version.
- Model IDs, immutable revisions, preprocess versions, dimensions, and
  normalization policy for every representation.
- Query compiler version and generated query-vector fingerprints.
- Metadata-filter and policy versions.
- ANN parameters and candidate budgets.
- Fusion method, normalization method, component weights, and calibration.
- Patch feature version, MaxSim variant, and rerank budget.
- Diversity, duplicate, and safety policy versions.
- Experiment assignment and application release version.

## Target 3: Alpha and Demo

### Product Surface

The static workbench should evolve into a service-backed alpha without losing
its inspectability. The first alpha should support image queries, text queries,
mixed queries, filters, ranked results, score evidence for internal reviewers,
and explicit feedback controls.

The alpha does not need every north-star component. It needs one complete,
versioned, observable search path that users can understand and that the team can
evaluate.

### Search Trace

Every query should create a trace containing the requested evidence:

| Field | Recording guidance |
| --- | --- |
| `query_id` | Globally unique and stable across all query-related events |
| `session_id` | Pseudonymous session identifier with explicit retention policy |
| `query_modality` | Text, image, mixed, positive edit, negative edit, or browse |
| `candidate_set` | Candidate IDs and stage membership; use a bounded artifact for large sets |
| `ranking_scores` | Raw/normalized component scores, stage ranks, and final score |
| `model_version` | Complete model and preprocessing fingerprints |
| `index_version` | Corpus, physical index, HNSW, payload, and patch-store versions |
| `latency_breakdown` | Compile, encode, each recall source, union, fusion, patch, policy, fetch, and total |
| `clicked_result` | Artwork ID from a result impression |
| `result_rank` | Rank as actually shown when the interaction occurred |
| `like_dislike` | Explicit preference event, not inferred from a click |
| `save` | Explicit save event and target collection when applicable |
| `follow_artist` | Explicit artist-follow event attributable to the impression |
| `reformulation` | Child query linked to `parent_query_id` and reformulation type |
| `timestamp` | Server timestamp plus client timestamp when useful |

Useful additions are `request_id`, pseudonymous `user_id`, filter snapshot,
experiment assignment, page/cursor, result artwork ID, dwell time, client build,
and consent/retention class.

### Event Shape

Do not force the complete trace into one unbounded row. Use three joinable event
families:

| Event | Content |
| --- | --- |
| Query event | Intent, modality, filters, versions, latency, candidate artifact reference, parent query |
| Impression event | One displayed result with rank and complete score breakdown |
| Interaction event | Click, like, dislike, save, follow, hide, or reformulation linked to query and impression |

For an early alpha, retaining full candidate traces in compressed object
artifacts is reasonable. At larger volume, keep the full final and rerank sets,
plus sampled/debug full-recall traces and compact digests for ordinary traffic.
Retention and access controls must be decided before collecting user queries.

### Evaluation Layers

| Layer | Primary questions |
| --- | --- |
| Intake gate | What is automatic-accept precision, coverage, review rate, and failure rate? |
| ANN recall | Does each approximate index recover the exact-search neighbors and judged positives? |
| Candidate union | Which retriever contributes unique relevant candidates and where is recall lost? |
| Fusion | Does normalized multi-score fusion improve nDCG, MAP, MRR, and edit satisfaction? |
| Patch rerank | Does late interaction improve fine-detail relevance over the fused shortlist? |
| Policy | What are duplicate, safety, attribution, and constraint violation rates? |
| Online alpha | What are click, save, like, follow, reformulation, abandonment, and successful-session rates? |

Clicks are useful but biased by rank and presentation. Explicit judgments,
randomized interleaving experiments, hard negatives, and curated failure sets
remain necessary.

## Cross-Cutting Operational Requirements

### Reliability

- Per-item failures must not abort an ingest run.
- Retries must be bounded and classified as transient or permanent.
- Dead-letter items must retain enough evidence for replay.
- Job leases and heartbeats must recover work after worker failure.
- Index publication must be atomic from the query service's perspective.
- Deletion and opt-out propagation needs a measurable service-level objective.
- A model or index failure should degrade explicitly, never silently change the
  ranking recipe.

### Security and Privacy

- Keep source credentials in a managed secret store and out of workers that do
  not require them.
- Record provenance and permission/opt-out state independently from content
  classification.
- Treat user query images, text, sessions, and interaction histories as
  potentially sensitive.
- Minimize raw query retention, use pseudonymous identifiers, and document
  retention and deletion behavior before alpha launch.
- Do not execute untrusted SVG or embedded content; retain byte and pixel limits.
- Audit access to source assets, query artifacts, and reviewer tooling.

### Reproducibility

- No serving vector exists without an immutable feature-set fingerprint.
- No index alias moves without an evaluation report and rollback target.
- No learned fusion or style model trains without a versioned dataset split.
- Offline evaluation must be able to replay the candidate and ranking recipe
  from a query trace.
- Schema migrations and backfills must report coverage and mixed-version rows.

## Delivery Roadmap

### Phase 0: Capacity Envelope and Contracts

The initial corpus size, daily rates, user count, byte-retention policy,
availability posture, quality target, and cost envelope are now recorded.
Remaining work is to measure source-byte distributions, acceptance rates,
per-stage throughput, query traffic, and latency on representative pilot runs.

Deliverables:

- Stable identity, lineage, feature-set, corpus-version, and index-version
  contracts.
- Ingest stage-event and query-trace schemas.
- Baseline measurements from the current SQLite/exact-search implementation.
- A representative, legally and operationally usable benchmark corpus and qrels.
- Explicit SLO placeholders and an owner for each operational metric.

Exit condition: the team can answer what "large scale" means for the next two
milestones and can compare future systems against a reproducible baseline.

### Phase 1: Production Intake Foundation

Deliverables:

- Durable source records, object storage, idempotent acquisition, and canonical
  content hashes.
- Queue-backed stage workers with bounded retries, dead letters, and backpressure.
- Batch-oriented SigLIP and heavy-feature workers with independent scaling.
- Relational workflow state and complete source-to-feature lineage.
- Throughput, queue, latency, quality, capacity, and cost dashboards.
- Tested deletion, opt-out, stale-source, and reprocessing workflows.

Exit condition: a backfill or continuous source stream can be interrupted,
resumed, audited, and reprocessed without duplicate serving records or lost
items.

### Phase 2: Qdrant Shadow Retrieval

Deliverables:

- Versioned Qdrant collections with named vectors and indexed payload filters.
- Bulk backfill and incremental upsert/delete publication jobs.
- Exact-vs-HNSW recall, latency, memory, disk, and filter benchmark suite.
- Shadow queries comparing Qdrant candidates with the current exact baseline.
- Alias-based promotion and rollback procedure.

Exit condition: Qdrant meets the chosen recall and latency envelope under
representative filters, and index drift is observable.

### Phase 3: Hybrid Funnel and Patch Service

Deliverables:

- Parallel semantic, visual, sparse/text, and metadata-aware recall.
- Candidate union, canonical deduplication, source attribution, and first RRF
  baseline.
- Exact score recomputation and versioned normalization/fusion.
- Batched patch retrieval and DINO MaxSim reranking path.
- Low-resolution-versus-full-resolution patch ablations covering quality,
  storage, fetch latency, and compute latency.
- Per-stage recall and ablation reports using saved judgments.

Exit condition: every final result explains which recall paths found it, how
each stage changed its rank, and whether the hybrid funnel beats single-index
baselines.

### Phase 4: Closed Alpha

Deliverables:

- Authenticated or access-controlled query API and responsive search UI.
- Image, text, and mixed-query compilation with clear filters and controls.
- Query, impression, and interaction telemetry with consent and retention rules.
- Product and retrieval dashboards joining behavior to exact model/index
  versions.
- Feedback controls for click, like/dislike, save, follow artist, and
  reformulation.
- Operational runbooks, rollback, incident visibility, and user-report flow.

Exit condition: invited users can complete real search sessions, the team can
diagnose every served ranking, and feedback can be converted into an offline
evaluation set without guessing which model produced it.

### Phase 5: Learning and Advanced Reranking

Deliverables are evidence-dependent: the starred DINO-LoRA style projection,
calibrated fusion, preference models, hard-negative mining, facet models, and an
optional joint VLM reranker. Style training should use content-balanced
positives and hard negatives, held-out evaluation, and teacher consistency as a
preservation signal rather than assuming self-distillation alone defines style.
Each component must beat a simpler baseline at an acceptable latency and cost
before entering the default path.

## Decisions Intentionally Deferred

These are part of the north star but should not be treated as immediate
requirements:

- Replacing the currently pinned DINOv2 model with DINOv3 or another encoder.
- Choosing SigLIP, CLIP, or another model for the production semantic index.
- Training a learned style projection before enough human preference data exists.
- Storing many speculative facet vectors before showing incremental value.
- Adding a VLM reranker before candidate quality and latency are understood.
- Choosing Kafka or a broad microservice decomposition before job volume demands
  it.
- Enabling vector quantization before exact-vs-quantized quality is measured.
- Learning fusion weights before query traces and relevance judgments are
  trustworthy.

## Principal Risks

| Risk | Control |
| --- | --- |
| Intake volume amplifies bad gate decisions | Conservative acceptance, sampled review, per-source monitoring, rollbackable corpus versions |
| Vector and metadata drift | Canonical relational state, outbox/publication records, reconciliation jobs |
| ANN silently loses relevant results | Exact-search benchmark, shadow traffic, recall-at-budget alerts |
| Patch storage or compute dominates cost | Short rerank sets, compression experiments, separate fetch/compute telemetry |
| User feedback becomes popularity bias | Preserve impressions, rank, negatives, exploration assignments, and explicit judgments |
| Sensitive query or corpus data leaks | Data minimization, access control, retention policy, deletion workflows, private artifact boundaries |
| Model upgrades invalidate comparisons | Immutable revisions, feature fingerprints, new physical indexes, cohort-aware evaluation |
| Infrastructure outruns product evidence | Closed-alpha milestones and evidence gates before advanced components |

## Immediate Planning Packet

The capacity sheet is complete enough to begin contract implementation. The
remaining planning packet is:

1. An identity and lifecycle schema covering sources, assets, artworks,
   versions, features, corpus membership, deletion, and opt-out behavior.
2. A Qdrant benchmark plan specifying named vectors, payload filters, exact
   truth generation, candidate budgets, and promotion thresholds.
3. A query telemetry specification for query, impression, interaction, and
   candidate-trace records, including consent and retention classes.
4. A pilot benchmark report filling the remaining measured throughput, byte,
   acceptance-rate, QPS, and latency fields.

That packet is the bridge from the v0.3.1 MVP to production implementation. It
keeps the ambitious architecture intact while giving every expensive component
a measurable reason to exist.
