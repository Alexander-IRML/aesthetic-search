# Changelog

Project changes worth remembering live here. Keep entries concise and focused on
what changed, why it matters, and any verification that was done.

## Unreleased

### Added

- Added configurable CLIP and DINOv2 model settings for embedding generation.
- Added an `embeddings` SQLite table for normalized CLIP vectors, DINO pooled vectors,
  and DINO patch grids.
- Added an embedding generation pipeline with model-version idempotency and run logging.
- Added optional `embed` dependencies for `torch` and `transformers`.
- Added brute-force DINO pooled-vector retrieval with same-artist filtering.
- Added a local HTML search demo generator for visual result inspection.
- Added a local gallery demo that samples several query images per artist and
  shows top-10 results when a query is selected.
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

- `python -m pytest -q`: 16 tests passed.
- `python -m ruff check .`: all checks passed.
- Real local corpus run: 143 images processed, 143 validated, 0 errors.
- Standardization idempotency rerun: 0 processed, 143 skipped, 0 errors.
- Real embedding run: 143 embeddings generated, 0 errors.
- Embedding idempotency rerun: 0 processed, 143 skipped, 0 errors.
- Local search demo generated at `data/search_demo.html`.
- Local gallery demo generated at `data/search_gallery.html`.
- Private image data, processed outputs, SQLite DB, demo HTML, and virtual environment
  are gitignored.
