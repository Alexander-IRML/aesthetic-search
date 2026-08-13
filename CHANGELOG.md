# Changelog

Project changes worth remembering live here. Keep entries concise and focused on
what changed, why it matters, and any verification that was done.

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
