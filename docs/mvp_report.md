# ArtSearch MVP Report

ArtSearch is an early-stage visual search system for artwork. The current MVP
builds the foundation for a private image corpus: deterministic data
preparation, SQLite-backed metadata tracking, multimodal embedding generation,
baseline vector retrieval, and a local HTML inspection interface.

The project is intentionally structured as a pipeline rather than a one-off
notebook. Raw source images remain untouched, processed artifacts are
regenerable, and metadata is recorded in a database so future ingestion,
deduplication, embedding, and review workflows can be made repeatable.

## Current Capability

The current system can ingest artist folders from a private local corpus,
standardize images into a canonical model-ready format, generate CLIP and DINOv2
embeddings, and run several baseline similarity modes over the processed
collection. It also generates local HTML demos that make retrieval quality
visible without requiring a deployed web app.

At this stage, retrieval supports CLIP subject similarity, DINOv2 pooled global
visual similarity, and DINOv2 patch MaxSim local-detail similarity. These modes
are kept separate so their behavior can be compared directly before any blended
ranking is introduced.

## Data Preparation

The ingestion pipeline starts from explicitly registered artist folders. Artist
registration is manifest-driven, which avoids inferring identity from arbitrary
folder names and gives future automated importers a stable entry point. The same
registration logic can later be reused by API-based importers or scraping
adapters without changing the database contract.

Images are standardized into 448x448 JPEG outputs. The canonical size is chosen
to be compatible with patch-based vision models, and JPEG is used because the
processed directory is derived data rather than an archival source. The original
files remain in `data/raw/` and are never modified by the standardization step.

The crop, scale, and pad coordinate calculations are implemented as pure
functions. This keeps the geometric transform logic unit-testable independently
from image I/O, which is important for later features that may need to map model
patches or UI selections back to original image coordinates.

## Metadata And Storage

The project uses SQLite as a local metadata store. The schema currently tracks:

- `artists`: registered source identities and folder mappings.
- `artworks`: raw paths, processed paths, hashes, validation state, duplicate
  review metadata, and transform metadata.
- `pipeline_runs`: top-level records for ingestion and embedding runs.
- `run_events`: structured event logs for skips, errors, duplicate detection,
  and other pipeline observations.
- `embeddings`: CLIP vectors, DINO pooled vectors, DINO patch grids, model names,
  model versions, and dimensional metadata.

Exact duplicate files are skipped during ingestion and logged instead of creating
new artwork rows. Near-duplicates are preserved as distinct rows but given a
review state, because visually similar images can still represent legitimate
variants, studies, edits, or alternate versions.

## Embedding Pipeline

The embedding phase uses optional ML dependencies so the core data-prep pipeline
can still be installed and tested without large model packages. When the
embedding dependencies are installed, the project generates:

- CLIP image embeddings for future semantic and text-aligned search.
- DINOv2 pooled image embeddings for current visual similarity retrieval.
- DINOv2 patch embeddings for future local-feature matching and reranking.

Embeddings are stored as binary vector blobs with explicit dimensional metadata.
Model name and model version are recorded for idempotency: if an artwork already
has embeddings for the configured model versions, reruns can skip it safely.
This keeps repeated local runs fast and protects the database from accidental
mixed-model comparisons.

## Retrieval Baseline

The retrieval MVP builds in-memory brute-force indexes from validated artworks
that have current embeddings. Vector modes load CLIP or DINOv2 pooled vectors,
L2-normalize them, and score candidates with a dot product, which is equivalent
to cosine similarity for normalized vectors.

DINO patch MaxSim loads the stored patch grid for each image, computes the full
query-patch by candidate-patch similarity matrix, takes the best candidate patch
for each query patch, and averages those best-match scores. This makes MaxSim a
local-detail and structural correspondence signal rather than a global style
score.

The current search excludes the query image and filters out same-artist results.
It also supports shared filters for same-artist inclusion, review status, and
SFW metadata.

This baseline is intentionally explicit. It establishes measurable retrieval
paths before adding more complex ranking logic such as CLIP/DINO score blending,
mask-guided patch matching, or learned rerankers.

## Local Evaluation UI

The project includes two local HTML demo generators:

- `scripts/search_demo.py` writes a single-query page for one artwork ID.
- `scripts/search_gallery.py` writes a small gallery interface that samples a
  few query images per artist and displays top-k matches when a query is
  selected.

These demos are generated into `data/search_*.html` and reference local
processed image paths. They do not embed image bytes directly into the HTML, and
the generated files are intentionally ignored by git. This keeps qualitative
evaluation fast while preserving the privacy boundary around the local corpus.

## Privacy And Repository Hygiene

The repository is designed so private corpus artifacts stay local. Git ignores:

- `data/raw/`
- `data/processed/`
- `data/*.db`
- `data/search_*.html`
- `config/artists.local.yaml`
- `.venv/`

The tracked `config/artists.yaml` is now a public-safe example manifest. Real
local artist registrations belong in `config/artists.local.yaml`, which the CLI
prefers automatically when present. Git history was rewritten so earlier private
artist folder names are no longer present in reachable commits.

## Verification

Current verification includes automated tests and real local pipeline runs.

Automated checks:

- `pytest`: 21 tests passing.
- `ruff`: all checks passing.

Test coverage currently includes:

- SQLite schema initialization.
- Transform math for crop, scale, and pad behavior.
- Pipeline edge cases and path handling.
- Embedding BLOB serialization and deserialization.
- Embedding idempotency.
- Retrieval filtering and ranking behavior.
- Patch MaxSim scoring and diagnostics.
- Demo output generation.

Local corpus verification:

- 143 images processed successfully.
- 143 validated artworks recorded.
- 0 standardization errors.
- 143 embeddings generated.
- Embedding rerun skipped already-current records as expected.
- Local search and gallery demos generated successfully.

## Current Limitations

The current retrieval ranking is a baseline, not the final search strategy.
DINOv2 pooled embeddings provide a global visual similarity signal, which can
capture broad subject, composition, and style likeness, but it is not yet
fine-grained enough for all visual-search use cases.

Known limitations:

- Retrieval signals are separate modes and are not yet blended or benchmarked
  against human judgments.
- Patch MaxSim is unmasked and can still be influenced by low-information
  patches.
- Retrieval is brute-force in memory rather than indexed with FAISS or another
  approximate nearest-neighbor system.
- The gallery is a static local demo rather than an interactive web service.
- Duplicate review states exist in the schema, but there is not yet a human
  review UI.

## Next Technical Milestones

The next useful improvements are:

- Add a designed retrieval-mode UI for subject, global visual, and local-detail
  modes.
- Collect human judgments before introducing weighted CLIP/DINO score fusion.
- Add visual patch overlays for MaxSim diagnostics.
- Add score diagnostics to the gallery demo so ranking behavior is easier to
  inspect.
- Add a lightweight local web UI for browsing, querying, and duplicate review.
- Introduce an ANN index once the corpus grows beyond brute-force comfort.

## Resume-Relevant Framing

This project demonstrates a complete early ML product pipeline: data ingestion,
image preprocessing, metadata design, duplicate handling, model embedding
generation, vector retrieval, qualitative evaluation tooling, automated tests,
and privacy-conscious repository hygiene.

Technically, it combines Python pipeline engineering, SQLite schema design,
computer-vision preprocessing, transformer-based embedding extraction, vector
similarity search, and local evaluation UX. The system is still an MVP, but it
already has the core architecture needed to evolve from a private prototype into
a larger-scale visual search application.
