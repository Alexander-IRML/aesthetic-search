# Changelog

Project changes worth remembering live here. Keep entries concise and focused on
what changed, why it matters, and any verification that was done.

## Unreleased

No unreleased changes.

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
