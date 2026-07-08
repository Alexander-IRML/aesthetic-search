# Changelog

Project changes worth remembering live here. Keep entries concise and focused on
what changed, why it matters, and any verification that was done.

## Unreleased

### Added

- Built the initial ArtSearch data preparation pipeline.
- Added explicit artist registration from `config/artists.yaml`.
- Added configurable image standardization to 448x448 JPEG outputs.
- Added SQLite metadata tables for artists, artworks, pipeline runs, and run events.
- Added exact duplicate skipping, near-duplicate review flags, and validation metadata.
- Added pure transform math for crop/scale/pad coordinate mapping.
- Added CLI entrypoints for database initialization, artist registration, and corpus standardization.
- Added unit tests for schema initialization, transform math, and pipeline edge cases.

### Verified

- `python -m pytest -q`: 11 tests passed.
- `python -m ruff check .`: all checks passed.
- Real local corpus run: 143 images processed, 143 validated, 0 errors.
- Idempotency rerun: 0 processed, 143 skipped, 0 errors.
- Private image data, processed outputs, SQLite DB, and virtual environment are gitignored.
