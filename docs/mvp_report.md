# ArtSearch MVP Report

ArtSearch is an early-stage visual search system for artwork. The current MVP
builds the foundation for a private image corpus: deterministic data
preparation, SQLite-backed metadata tracking, multimodal embedding generation,
staged visual retrieval, and a local HTML evaluation interface.

The project is intentionally structured as a pipeline rather than a one-off
notebook. Raw source images remain untouched, processed artifacts are
regenerable, and metadata is recorded in a database so future ingestion,
deduplication, embedding, and review workflows can be made repeatable.

## Current Capability

The current system can ingest artist folders from a private local corpus,
standardize images into a canonical model-ready format, generate CLIP and DINOv2
embeddings, and run staged visual retrieval over the processed collection.
It also generates local HTML demos that make retrieval quality visible without
requiring a deployed web app.

Primary retrieval now uses DINOv2 pooled image embeddings for full-corpus recall
and DINOv2 patch grids for shortlist-only late-interaction reranking. CLIP runs
as a separate semantic measurement and is reported beside the final results; it
is deliberately excluded from ensemble ordering. The component baselines remain
available for diagnosis and human relevance evaluation.

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

* `artists`: registered source identities and folder mappings.
* `artworks`: raw paths, processed paths, hashes, validation state, duplicate
review metadata, and transform metadata.
* `pipeline\\\_runs`: top-level records for ingestion and embedding runs.
* `run\\\_events`: structured event logs for skips, errors, duplicate detection,
and other pipeline observations.
* `embeddings`: CLIP vectors, DINO pooled vectors, DINO patch grids, model names,
model versions, and dimensional metadata.

Exact duplicate files are skipped during ingestion and logged instead of creating
new artwork rows. Near-duplicates are preserved as distinct rows but given a
review state, because visually similar images can still represent legitimate
variants, studies, edits, or alternate versions.

## Embedding Pipeline

The embedding phase uses optional ML dependencies so the core data-prep pipeline
can still be installed and tested without large model packages. When the
embedding dependencies are installed, the project generates:

* CLIP image embeddings for semantic evidence and subject retrieval.
* DINOv2 pooled image embeddings for global recall and visual-style diagnosis.
* DINOv2 patch embeddings for local-feature matching and final reranking.

Embeddings are stored as binary vector blobs with explicit dimensional metadata.
Model name and model version are recorded for idempotency: if an artwork already
has embeddings for the configured model versions, reruns can skip it safely.
This keeps repeated local runs fast and protects the database from accidental
mixed-model comparisons.

## Retrieval Ensemble

The retrieval MVP builds exact in-memory global-vector indices from validated
artworks with current pinned embeddings. Its default recipe has two stages:

1. L2-normalized DINO pooled vectors score every eligible candidate by cosine
similarity and select a configurable recall shortlist.
2. SQLite loads patch BLOBs only for the query and shortlist. A ColBERT-style
late interaction score reranks that shortlist into the final result order.

Strict MaxSim is the default. A configurable top-N variant can average several
candidate matches per query patch for controlled A/B evaluation. CLIP cosine
scores and ranks are calculated over the same eligible candidate set as a
parallel semantic lens, but they do not alter the DINO funnel. There is no raw
score sum or vector concatenation.

The query image is always excluded. Same-artist, review-state, and safety
filters are applied before shortlisting, so displayed Stage 1 ranks describe the
actual eligible pool. Exact global search is appropriate for the current corpus;
the shortlist contract allows a later ANN implementation without changing the
reranker or result evidence.

## Local Evaluation UI

The project includes two local HTML demo generators:

* `scripts/search\\\_demo.py` writes a single-query page for one artwork ID.
* `scripts/search\\\_gallery.py` writes a retrieval workbench that samples query
images, displays the ensemble ranking, and compares component baselines.

These demos are generated into `data/search\\\_\\\*.html` and reference local
processed image paths. They do not embed image bytes directly into the HTML, and
the generated files are intentionally ignored by git. This keeps qualitative
evaluation fast while preserving the privacy boundary around the local corpus.

## Privacy And Repository Hygiene

The repository is designed so private corpus artifacts stay local. Git ignores:

* `data/raw/`
* `data/processed/`
* `data/\\\*.db`
* `data/search\\\_\\\*.html`
* `config/artists.local.yaml`
* `.venv/`

The tracked `config/artists.yaml` is now a public-safe example manifest. Real
local artist registrations belong in `config/artists.local.yaml`, which the CLI
prefers automatically when present. Git history was rewritten so earlier private
artist folder names are no longer present in reachable commits.

## Verification

Current verification includes automated tests and real local pipeline runs.

Automated checks:

* `pytest`: 104 tests passing.
* `ruff`: all checks passing.

Test coverage currently includes:

* SQLite schema initialization.
* Transform math for crop, scale, and pad behavior.
* Pipeline edge cases and path handling.
* Embedding BLOB serialization and deserialization.
* Embedding idempotency.
* Retrieval filtering and ranking behavior.
* Demo output generation.

Local corpus verification:

* 117 validated artworks in the active Bluesky-derived corpus.
* 12 source artists represented.
* 117 current CLIP/DINO embedding records.
* Embedding rerun skipped already-current records as expected.
* Local search and gallery demos generated successfully.

## Current Limitations

The current retrieval ranking is an interpretable first ensemble, not a learned
final search strategy. Its stages have distinct responsibilities and can still
fail independently.

Known limitations:

* Retrieval is brute-force in memory rather than indexed with FAISS or another
approximate nearest-neighbor system.
* The gallery is a static local demo rather than an interactive web service.
* CLIP text-delta and exemplar-direction math exists, but interactive exploration
sliders require a live local model service rather than static HTML.
* Full-corpus Stage 1 recall cannot be measured until relevance labels include
candidates outside the displayed pool.

## Next Technical Milestones

The next useful improvements are:

* Add a lightweight local service for CLIP text-delta sliders and exemplar facet
controls.
* Evaluate strict MaxSim against top-N patch aggregation with saved judgments.
* Measure pooled-shortlist recall using labels outside the displayed top-k.
* Add patch correspondence overlays for result-level explanation.
* Introduce an ANN index once the corpus grows beyond brute-force comfort.

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
