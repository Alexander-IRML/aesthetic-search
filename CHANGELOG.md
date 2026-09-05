# Changelog

Project changes worth remembering live here. Keep entries concise and focused on
what changed, why it matters, and any verification that was done.

## v0.6.0 - 2026-09-05

### Added

- Added CSD preprocessing, tensor-transfer, backbone, projection, and output
  timings to the API response and public request trace.
- Added end-to-end search timing from browser and Cloudflare Container startup
  through upload/decode, telemetry initialization, CLIP, CSD, normalization,
  Qdrant, and catalog lookup, with an on-page request trace and `Server-Timing`.
- Added a non-starting runtime-status endpoint and red/green model-state light
  without a keep-warm poll or any change to the 15-minute idle timeout.
- Added a post-retrieval RAG assistant backed by Cloudflare Workers AI. It
  summarizes bounded Bluesky post and alt-text metadata only after the top ten
  search results have rendered.
- Added a constrained artist-search agent tool that reruns the original
  in-memory query image against one displayed artist through an exact Qdrant
  metadata filter, with the scope recorded in query telemetry.

### Changed

- Moved query telemetry initialization alongside inference, preserving ordered
  completion and feedback writes while exposing only its residual wait.
- Raised the single scale-to-zero search Container to 2 vCPU and the required
  6 GiB memory floor after local CSD profiling measured near-twofold thread
  scaling; rejected tested dynamic INT8 variants because they changed style
  embeddings beyond the quality gate.
- Moved completed-query telemetry writes behind the search response on one
  ordered worker while preserving search-before-feedback persistence ordering.
- Removed the shared alpha passcode so the hosted search workbench can serve as
  a public resume demo.
- Moved the frontend from the sleeping model Container to Cloudflare static
  assets, allowing `/search` to load without paying the model cold-start cost.
- Added edge search, feedback, content-type, and upload-size controls while
  retaining the one-Container and one-inference cost ceilings.
- Extended the browser's cold-start wait and surfaced a distinct model-warming
  state instead of appearing to load indefinitely.

### Safety And Privacy

- Kept query image bytes in memory only and kept private R2 corpus objects
  behind the same-origin Worker media route.
- Added per-client and per-location limits for search, feedback, and assistant
  traffic while preserving the single-container cost ceiling.
- Kept provider credentials in Cloudflare-managed secrets and ignored local
  environment files; the release contains no corpus images, databases, model
  weights, or generated vectors.

### Verified

- `python -m pytest -q`: 245 tests passed and 2 optional tests skipped.
- Ruff checks, changed-file formatting checks, Worker type generation,
  TypeScript validation, and the Cloudflare container dry run passed.
- The live deployment reported one healthy 2-vCPU, 6-GiB container and returned
  ten results through CLIP, CSD, Qdrant, Neon, and R2.
- One warm validation request reduced measured edge time from about 5.2 seconds
  to 2.7 seconds and CSD time from about 3.4 seconds to 1.7 seconds. This is a
  diagnostic sample, not a production p95 measurement.

See [the detailed v0.6.0 release notes](docs/releases/v0.6.0.md) for the public
alpha architecture, performance investigation, verification, and known limits.

## v0.5.0 - 2026-08-26

### Highlights

- Shipped the first invite-only cloud alpha: browser image upload, exact
  CLIP/CSD query encoding, Qdrant multi-vector retrieval, Neon catalog and
  telemetry, and private R2 result delivery through Cloudflare.
- Replaced the DINO global/patch retrieval contract with a smaller and clearer
  CLIP subject plus CSD artistic-style fusion contract.
- Moved the production metadata boundary from laptop SQLite to PostgreSQL while
  retaining SQLite for resumable intake staging and offline repair.

### Added

- Added a same-origin FastAPI search workbench with bounded JPEG/PNG/WebP
  uploads, fixed top-10 results, source attribution, stable error envelopes,
  health checks, and Great/Good/Bad relevance feedback.
- Added exact pinned CLIP and CSD query encoders, Qdrant named-vector prefetch
  plus reciprocal-rank fusion, per-artist result diversity, and strict active-
  index model-lineage validation.
- Added durable query, top-100 candidate, top-10 impression, timing, model,
  index, and graded-feedback telemetry with dashboard p50/p95, judged hit rate,
  Precision@10, nDCG@10, and exact-versus-HNSW Recall@100 reporting.
- Added a Cloudflare Worker and scale-to-zero Container deployment with a
  private R2 binding, one-container cost ceiling, signed 12-hour alpha sessions,
  an access-code gate that runs before container wake, and managed runtime
  secrets.
- Added resumable 5,000-candidate daily and 20,000-candidate bulk intake
  launchers, rotating Bluesky roster checkpoints, R2 credential canaries, and
  bounded parallel accepted-object publication.
- Added a Python 3.12 container contract with 3.12.14 control, CPU-ML, and test
  targets plus the official Airflow 3.3.1 Python 3.12 base. The local Compose
  stack includes PostgreSQL, Qdrant, Alembic, the review dashboard, Airflow,
  and an opt-in ML toolbox.
- Added tested direct-dependency constraints, Airflow upstream constraints,
  build-time `pip check`, non-root application execution, private loopback port
  publication, persistent service volumes, and static container-contract tests.
- Pinned pandas 2.3.3 in the Airflow/Spark image after a live Spark 4.2 smoke
  test exposed PySpark's incomplete pandas 3.x support warning.
- Added a Neon-ready PostgreSQL 17 control plane using SQLAlchemy Core,
  Alembic, psycopg 3, `pgvector`, `pg_trgm`, UUIDv7 identifiers, and
  RLS-ready product telemetry.
- Added guarded, idempotent SQLite snapshot publication with candidate-scoped
  daily imports, exact row verification, model lineage, and compact CLIP/CSD
  `halfvec` archival vectors.
- Added a transactional PostgreSQL outbox that repairs and publishes the
  canonical demo-eligible corpus to Qdrant with bounded claims, retries,
  drift detection, and deterministic point identities.
- Added query, ranking-stage, impression, click, preference, save, follow, and
  reformulation repositories plus a configurable 180-day telemetry-retention
  job.
- Added `artsearch-postgres` migration, status, publication, retention, and
  Qdrant-drain commands, including separate pooled runtime and direct migration
  connection handling.

### Changed

- Replaced DINO pooled and patch representations with a pinned, checksum-
  verified 768-dimensional Contrastive Style Descriptor.
- Replaced patch MaxSim ranking with transparent reciprocal-rank fusion over
  independent CLIP subject and CSD style recall lists.
- Migrated SQLite, Qdrant, dashboards, human judgments, model monitoring,
  configuration, and architecture documentation to the CLIP/CSD contract.
- Added the `artworks_clip3_csd1_v1` physical collection contract so legacy
  DINO vectors cannot be mixed into the current serving index.
- Built and promoted `artworks_clip3_csd1_v2` with all 8,279 approved artworks,
  retaining v1 as an atomic rollback target.
- Corrected pre-promotion ANN evaluation to compare approximate and exact
  searches on the same physical collection instead of mixing the live alias
  with the candidate collection.
- Retained parsing support for historical DINO judgment evidence while removing
  active DINO and patch commands.
- Made PostgreSQL the canonical cloud metadata and workflow authority while
  retaining SQLite as worker-local staging and offline-development storage.
- Updated the Airflow intake DAG to publish each completed SQLite candidate
  scope to PostgreSQL before draining its committed Qdrant outbox.

### Safety And Privacy

- Kept database credentials in the ignored `deploy/alpha/.env` file, validated
  TLS requirements without logging URLs, and kept local images, SQLite files,
  model caches, and generated artifacts ignored.
- Kept query uploads in memory only, served corpus media through an
  authenticated read-only Worker binding, and prevented unauthenticated
  requests from waking paid model compute.
- Extended Git and Docker exclusions for environment variants, private keys,
  local databases, corpus/vector products, model weights, and profiling output.

### Verified

- `python -m pytest -q`: 234 tests passed and 2 optional tests skipped.
- The Cloudflare Worker TypeScript check, container deployment, access matrix,
  readiness probe, authenticated R2 media fetch, and a real 10-result image
  search completed successfully.
- Neon schema migration `20260823_0003`, the 8,279-artwork catalog, the
  `artworks_active` Qdrant alias, point-count parity, and exact-versus-HNSW ANN
  checks were verified before release.
- Secret-pattern scans found only explicit placeholders and test fixtures among
  commit candidates; no image, database, vector, model-weight, roster, or local
  environment file is tracked.

See [the detailed v0.5.0 release notes](docs/releases/v0.5.0.md) for the alpha
architecture, verification record, and known limitations.

## v0.3.2 - 2026-08-13

### Highlights

- Added a rebuildable Qdrant serving index while retaining SQLite as the
  canonical corpus and embedding store.
- Added scalable semantic-visual retrieval with separate CLIP subject and DINO
  global HNSW recall, reciprocal-rank fusion, and shortlisted DINO patch MaxSim
  reranking.

### Added

- Added versioned named-vector collection creation, stable alias promotion,
  float16 storage, compact metadata payloads, and strict model-lineage checks.
- Added idempotent SQLite-to-Qdrant synchronization with content hashes, stale
  point pruning, remote drift repair, exact count verification, and the
  `vector_index_points` publication ledger.
- Added `artsearch-qdrant` commands for configuration inspection, explicit
  safety/demo policy, collection lifecycle, sync, status, image/text search,
  and ANN recall/latency evaluation against exact Qdrant search.
- Added the pinned local Qdrant service and CLIP/DINO embedding plus index sync
  stages to the Airflow development stack.
- Added local and Qdrant Cloud configuration templates, an operator runbook,
  and updated application/data-platform architecture diagrams.

### Safety And Privacy

- Added an independent `demo_eligible` policy flag. Qdrant requires both an
  explicit safe judgment and explicit demo approval; SigLIP acceptance does not
  imply either one.
- Existing rows migrate to demo-ineligible. The current 117-artwork corpus is
  safety-unknown and therefore produced zero serving points in the live-metadata
  smoke check.
- Kept images, SQLite databases, embedding artifacts, local Qdrant storage,
  credentials, and local Cloud configuration outside Git.

### Verified

- `python -m pytest -q`: 130 tests passed.
- `python -m ruff check .`: all checks passed.
- Changed Python files passed `ruff format --check`, compile checks passed, and
  Git whitespace checks passed.
- In-process Qdrant tests covered collection compatibility, model lineage,
  aliasing, idempotent sync, pruning, drift repair, CLIP/DINO RRF, DINO patch
  reranking, artist diversity, and ANN evaluation.
- The installed CLI completed a real-corpus eligibility report and a zero-point
  in-memory reconciliation without publishing unreviewed artwork.

See [the detailed v0.3.2 release notes](docs/releases/v0.3.2.md) for the full
retrieval contract and runtime boundaries.

## v0.3.1 - 2026-08-13

### Highlights

- Added the first production data-platform layer around the existing Bluesky,
  SigLIP, SQLite, and retrieval pipeline while preserving a single-machine alpha
  deployment path.
- Added Airflow orchestration, Polars manifests and intake metrics, immutable
  local/S3-compatible corpus publication, and a separate Spark corpus-audit job.

### Added

- Added run-scoped intake and corpus-audit DAGs with retries, bounded failure
  behavior, and explicit handoffs between collection, filtering, publication,
  manifests, and metrics.
- Added SHA-256-addressed local and S3-compatible object stores, publication
  verification, canonical duplicate reuse, and SQLite `artwork_objects`
  checkpoints.
- Added HTTPX connection-pool controls for Bluesky and image acquisition.
- Added Python 3.12, Airflow 3.3, Java 17, and PySpark 4.2 development assets,
  plus capacity, implementation, operations, and architecture documentation.

### Privacy

- Kept the entire private `data/` tree, local rosters and S3 settings, Airflow
  state, downloaded models, and generated corpus artifacts outside Git and the
  Docker build context.
- Restricted durable publication to accepted decisions; rejected image bytes
  are not retained by the new publisher.

### Verified

- `python -m pytest -q`: 122 tests passed.
- `python -m ruff check .`: all checks passed.
- All Python files changed for v0.3.1 passed `ruff format --check`.
- All 1,328 current files under `data/` matched an ignore rule, with no tracked
  or pending image, database, vector, Parquet, or model artifact found.

See [the detailed v0.3.1 release notes](docs/releases/v0.3.1.md) for the full
runtime and verification record.

## v0.3.0 - 2026-08-07

### Highlights

- Completed the first end-to-end Bluesky path from public feed collection,
  auditable artwork triage, and corpus routing through SQLite import, heavy
  retrieval embeddings, and local visual evaluation.
- Turned the retrieval gallery into a repeatable review workbench with separate
  corpus and final-result views, model evidence, judgment export, and seeded
  follow-up sessions.

### Added

- Added a read-only Bluesky public AppView collector with paged author-feed
  traversal, image-candidate extraction, retry handling, moderation controls,
  actor-list workflows, and appendable or replaceable JSONL audit streams.
- Added `artsearch.artwork_filter`: typed candidate/result schemas, bounded
  local and HTTP image loading, deterministic media and provenance rules,
  independent text signals, stable reason codes, and structured failures.
- Added a versioned multi-class SigLIP 2 prompt bank and lazy backend with
  batched CPU/GPU inference, normalized embeddings, bounded OOM recovery,
  prompt caching, and finite-value validation.
- Added streaming `classify-jsonl`, one-image classification, prompt inspection,
  deterministic-only operation, configuration-aware resume behavior, and a
  real-model smoke test that compares batch and single inference.
- Added atomic image-feature caching with validated shapes and dtypes; damaged
  entries are ignored and removed instead of entering a decision.
- Added append-only filter-decision JSONL plus boundary-prioritized review CSV,
  cumulative label import, exact decision replay, per-class and artist-level
  evaluation, and conservative threshold calibration reports.
- Added an integrated Bluesky pipeline that collects or replays candidates,
  classifies them in ordered batches, downloads full-size ACCEPT/REVIEW images,
  strips source metadata, and preserves evidence for REJECT/ERROR outcomes.
- Added SQLite `artwork_filter_decisions` and `artwork_filter_routes` audit
  tables, idempotent accepted-artwork upserts, canonical image processing, and
  a rebuild command for promoting a filtered Bluesky corpus into retrieval.
- Added explicit CLIP subject, DINOv2 pooled style, and DINOv2 patch-detail
  retrieval evidence with model-version checks and local diagnostics.
- Added a staged ensemble retriever: configurable component weighting, pooled
  shortlist generation, patch MaxSim reranking, same-artist controls, and
  per-component score explanations instead of concatenating unlike vectors.
- Added retrieval direction primitives for controlled CLIP text/image and DINO
  style exploration, along with offline relevance metrics and judgment export.
- Added corpus and final retrieval dashboards with image provenance, filter and
  ensemble evidence, an evaluation tab, model decisions, review controls, and
  missing-image diagnostics.
- Added seeded, artist-balanced multi-session review generation with unique
  query selection, saved progress, cumulative exports, and an export gate before
  revealing the next precomputed session.
- Added a complete PlantUML application, sequence, and SQLite architecture
  document covering collection through retrieval.

### Changed

- Changed the gallery data source from the original manually registered corpus
  to accepted SigLIP-gated Bluesky artwork promoted through the same SQLite
  embedding and retrieval layer.
- Made retrieval composition explicit and inspectable: CLIP subject, DINO pooled
  style, and DINO patch detail remain separate scores combined by policy.
- Changed unlabeled artwork safety metadata to default to unknown instead of
  implicitly safe.
- Kept live artist pools and corpus-specific analysis local while retaining
  public example manifests and generic architecture documentation.
- Cleaned stale documentation that referred to the old `pipeline_runs` table
  name; the current schema uses `runs` and `run_events`.

### Privacy

- Made the complete `data/` tree private and generated-by-default, covering
  source images, processed images, SQLite databases, caches, review artifacts,
  decision streams, dashboard exports, screenshots, and corpus archives.
- Kept local artist manifests, Bluesky actor lists, corpus-specific reports, and
  downloaded model artifacts out of Git while retaining safe templates and
  artifact documentation.

### Verified

- `python -m pytest`: 105 tests passed.
- `python -m ruff check .`: all checks passed.

## v0.2.1 - 2026-07-14

### Added

- Added configurable CLIP and DINOv2 model settings for embedding generation.
- Added an `embeddings` SQLite table for normalized CLIP vectors, DINO pooled vectors,
  and DINO patch grids.
- Added an embedding generation pipeline with model-version idempotency and run logging.
- Added optional `embed` dependencies for `torch` and `transformers`.
- Added brute-force DINO pooled-vector retrieval with same-artist filtering.
- Added named retrieval modes for CLIP subject, DINO pooled, and DINO patch
  MaxSim scoring.
- Added shared retrieval filters for top-k, same-artist inclusion, review
  status, and SFW metadata.
- Added patch MaxSim diagnostics for inspecting which query/candidate patches
  drive local-detail matches.
- Added a local HTML search demo generator for visual result inspection.
- Added a local gallery demo that samples several query images per artist and
  shows top-10 results when a query is selected.
- Added an open-gallery launcher that writes a fresh timestamped gallery file
  and opens it locally.
- Added a technical MVP report summarizing the current architecture, retrieval
  baseline, verification, privacy boundary, and next milestones.
- Added a designer-facing brief for the first real ArtSearch evaluation UI.
- Added a designer tracker note for the multi-signal retrieval workbench
  architecture and upcoming mode/filter design.
- Added an ignored local artist manifest path so private corpus registration can
  stay out of the public repo.
- Added tests for embedding BLOB storage, embedding idempotency, retrieval filtering,
  and demo output.
- Built the initial ArtSearch data preparation pipeline.
- Added explicit artist registration from `config/artists.yaml`.
- Added configurable image standardization to 448x448 JPEG outputs.
- Added SQLite metadata tables for artists, artworks, pipeline runs, and run events.
- Added exact duplicate skipping, near-duplicate review flags, and validation metadata.
- Added pure transform math for crop/scale/pad coordinate mapping.
- Added CLI entrypoints for database initialization, artist registration, and corpus
  standardization.
- Added unit tests for schema initialization, transform math, and pipeline edge cases.

### Verified

- `python -m pytest -q`: 21 tests passed.
- `python -m ruff check .`: all checks passed.
- Real local corpus run: 143 images processed, 143 validated, 0 errors.
- Standardization idempotency rerun: 0 processed, 143 skipped, 0 errors.
- Real embedding run: 143 embeddings generated, 0 errors.
- Embedding idempotency rerun: 0 processed, 143 skipped, 0 errors.
- Local search demo generated at `data/search_demo.html`.
- Local gallery demo generated at `data/search_gallery.html`.
- Private image data, processed outputs, SQLite DB, demo HTML, and virtual environment
  are gitignored.
