# ArtSearch

Data preparation and baseline visual search pipeline for artwork.

Current release: **v0.5.0**. See the
[v0.5.0 release notes](docs/releases/v0.5.0.md) for the closed-alpha cloud
search, CLIP/CSD retrieval, PostgreSQL telemetry, and deployment verification
record.

The current MVP ingests explicitly registered artist folders from `data/raw/`,
standardizes images into 448x448 JPEGs in `data/processed/`, tracks metadata in
SQLite, stores CLIP subject and CSD artistic-style embeddings, and can generate
local HTML search demos using reciprocal-rank fusion with auditable component
scores and ranks.

For a more detailed technical summary, see
[docs/mvp_report.md](docs/mvp_report.md).
The current component map, Bluesky pipeline sequence, and data-store relationship
diagram are in [docs/artsearch_architecture.puml](docs/artsearch_architecture.puml).
The production-scale Qdrant/HNSW, ingestion, hybrid retrieval, and alpha roadmap
is recorded in
[docs/production_scale_roadmap.md](docs/production_scale_roadmap.md).
The researched implementation choices for source synchronization, queues,
PostgreSQL, object storage, workers, Qdrant, observability, and deployment are in
[docs/production_intake_implementation_research.md](docs/production_intake_implementation_research.md).
The implemented Airflow, Spark, Polars, HTTPX, and S3-compatible data-platform
slice is documented in
[docs/production_data_platform.md](docs/production_data_platform.md), with its
component flow in
[docs/production_data_platform.puml](docs/production_data_platform.puml).
The Qdrant named-vector schema, safety gate, CLIP/CSD fusion funnel, sync and
repair behavior, ANN evaluation, and local/Cloud runbook are in
[docs/qdrant_retrieval.md](docs/qdrant_retrieval.md).
The executable local health dashboard, metric definitions, drift windows, and
privacy boundary are documented in
[docs/model_operations_dashboard.md](docs/model_operations_dashboard.md).
The accepted closed-alpha cloud topology, provider handoff, and public product
direction are recorded in
[docs/adr/0001-alpha-cloud-topology.md](docs/adr/0001-alpha-cloud-topology.md),
[docs/adr/0002-csd-style-retrieval.md](docs/adr/0002-csd-style-retrieval.md),
[docs/alpha_deployment_runbook.md](docs/alpha_deployment_runbook.md), and
[docs/public_search_experience.md](docs/public_search_experience.md).
The Python compatibility decision, pinned container targets, and complete local
Docker topology are in
[docs/runtime_containerization.md](docs/runtime_containerization.md).
The public upload-to-search API, same-origin frontend, security limits, model
compatibility contract, and hosted deployment boundary are documented in
[docs/public_image_search_api.md](docs/public_image_search_api.md).

## Container Workflow

ArtSearch containers standardize on the Python 3.12 minor. The root application
images currently use 3.12.14, while Airflow uses its official Python 3.12 image.
The root Compose stack runs PostgreSQL, Qdrant, Alembic migration, the private
model dashboard, Airflow, and an optional CPU-only ML toolbox without placing
corpus bytes in an image:

```bash
cp .env.example .env
docker compose build
docker compose up -d
docker compose ps
```

The dashboard is then available at `http://127.0.0.1:8765` and Airflow at
`http://127.0.0.1:8081`. Docker Desktop must have WSL integration enabled on
Windows. All published ports are loopback-only.

After publishing the active Qdrant alias, start the optional public-search
vertical slice with the exact pinned CLIP and CSD model image:

```bash
docker compose --profile search up -d api
```

The upload-first search workbench is then available at
`http://127.0.0.1:8000/search`. It accepts one JPEG, PNG, or WebP, encodes it in
memory into the active corpus spaces, performs CLIP/CSD reciprocal-rank fusion,
and returns a fixed top 10. The upload is not persisted.

## Cloud Alpha

The closed-alpha query path is deployed as a Cloudflare Worker plus one
scale-to-zero CPU Container. The Worker owns invite access, private R2 media,
and origin isolation; the Container owns exact CLIP/CSD query encoding and calls
Qdrant Cloud plus Neon PostgreSQL. Search traffic does not use the developer
laptop.

For a new environment or redeployment, print the account list, copy the cloud
configuration template, and validate the provider contracts with:

```bash
.venv/bin/artsearch-deploy accounts
cp configs/production.alpha-cloud.example.toml \
  configs/production.alpha-cloud.local.toml
.venv/bin/artsearch-deploy configure-postgres
.venv/bin/artsearch-deploy generate-session-secret
.venv/bin/artsearch-deploy preflight \
  --production-config configs/production.alpha-cloud.local.toml
```

The first preflight phase requires only R2 and Qdrant credentials. `--phase api`
adds PostgreSQL and session-secret requirements; `--phase full` reserves Modal
credentials for a future external inference worker. Secret values are checked
for presence but are never printed. The optional private intake control-plane
scaffold lives under `deploy/alpha/`; the deployed query service lives under
`deploy/cloudflare-search/`. See that package's README for access-secret
generation, deployment, and smoke checks.

## Local Workflow

Launch the local model operations and human-review dashboard, or install its
double-click desktop launcher:

```bash
.venv/bin/artsearch-model-dashboard
.venv/bin/artsearch-model-dashboard --install-desktop-launcher
```

It aggregates corpus and embedding coverage, SigLIP evaluation, retrieval rank
metrics, Qdrant ANN recall/latency, recent-vs-baseline drift, source diversity,
throughput, failures, and exact model/index lineage. The `Review & feedback`
tab writes append-only artwork labels and exact retrieval relevance judgments;
new evidence enters the dashboard metrics without editing SQLite. Missing human
or ANN evaluation evidence remains explicitly unmeasured.

For a private corpus, keep real artist registrations in the ignored local
manifest:

```bash
cp config/artists.yaml config/artists.local.yaml
# Edit config/artists.local.yaml with local folder names.
```

```bash
source .venv/bin/activate
python -m pytest -q
python -m ruff check .
python scripts/standardize.py
```

Production data products and S3-compatible publication use the `data` extra:

```bash
python -m pip install -e '.[data]'
artsearch-data build-manifest \
  --candidates data/bluesky/image_candidates.jsonl \
  --decisions data/filter/decisions.jsonl \
  --database data/artsearch.db \
  --output data/production/manifests/corpus.parquet
artsearch-data publish-corpus \
  --config configs/production.default.toml \
  --decisions data/filter/decisions.jsonl
```

Airflow and Spark are isolated in a Python 3.12/Java 17 development image:

```bash
AIRFLOW_UID="$(id -u)" docker compose \
  -f orchestration/airflow/compose.yaml up --build
```

The local image pins CPU-only PyTorch so a non-GPU workstation does not pull
the multi-gigabyte CUDA runtime. Move to a GPU worker image deliberately when
measured intake throughput justifies it.

The intake DAG orchestrates the existing Bluesky/SigLIP pipeline, Polars
manifest and metrics builds, and immutable accepted-original/run publication.
The separate Spark DAG reconciles all historical Parquet manifests and emits
corpus quality, duplicate, artist, and decision reports.

Daily local intake targets 5,000 source-image candidates across the ignored
583-artist roster. The collector rotates the roster, persists per-artist
high-water marks and pagination cursors in SQLite, and finishes the current
artist before stopping. Install or exercise the 10:00 AM Windows trigger with:

```bash
.venv/bin/artsearch-intake status
.venv/bin/artsearch-intake run-now
.venv/bin/artsearch-intake install-windows
```

The scheduled launcher runs only from 10:00 through 10:15 America/Chicago. A
sleeping or powered-off computer skips that day; it is not caught up later.
The default task also requires AC power and stops if the laptop switches to
battery. Docker Desktop with WSL integration is required for the Airflow runner;
the launcher starts Docker Desktop and waits for its engine when necessary.
The 5,000 figure is the daily source-candidate budget; the operations dashboard
records actual throughput and acceptance coverage so capacity can be tuned from
measured runs.

For an explicit one-off bulk intake, keep the supplied roster private at
`config/bluesky_actors.local.csv` and use the separate 20,000-candidate profile:

```bash
.venv/bin/artsearch-intake \
  --config configs/intake_scheduler.bulk.toml status
# Run only after reviewing the status, R2 canary prerequisites, and artist CSV:
.venv/bin/artsearch-intake \
  --config configs/intake_scheduler.bulk.toml run-now
```

That profile requires the ignored
`configs/production.alpha-cloud.local.toml` copied from the alpha-cloud example;
the scheduler passes it explicitly to Airflow so accepted originals publish to
R2 rather than the local object-store backend.

CSV rosters may contain a handle, DID, or `bsky.app/profile/...` URL. The intake
DAG validates Cloudflare R2 before collection, downloads SigLIP thumbnails and
accepted full-size files with separate bounded HTTP pools, then publishes
accepted originals through 16 parallel S3-compatible R2 workers. Content-
addressed keys, checksums, retries, and batched SQLite checkpoints make reruns
idempotent. See [the 20k rush runbook](docs/rush_20k_launch_runbook.md) before
starting a bulk run.

Qdrant provides the scalable retrieval-serving layer. SQLite remains the local
staging and offline-development database; PostgreSQL is canonical when the
production adapter is enabled. Start the local service and inspect eligibility
before syncing:

```bash
python -m pip install -e '.[qdrant]'
docker compose -f orchestration/airflow/compose.yaml up -d qdrant
artsearch-qdrant eligibility
artsearch-qdrant sync
artsearch-qdrant evaluate-ann --sample-size 50 --top-k 100
```

The production control plane uses SQLAlchemy Core, Alembic, psycopg 3, Neon
pooled/direct connections, PostgreSQL RLS-ready product telemetry, compact
`halfvec` archival CLIP/CSD values, and a transactional Qdrant outbox:

```bash
python -m pip install -e '.[postgres]'
artsearch-postgres --config configs/production.alpha-cloud.local.toml upgrade
artsearch-postgres --config configs/production.alpha-cloud.local.toml status
artsearch-postgres --config configs/production.alpha-cloud.local.toml \
  publish-sqlite --sqlite data/artsearch.db
artsearch-postgres --config configs/production.alpha-cloud.local.toml drain-qdrant
```

`DATABASE_URL` is the pooled runtime connection and `DATABASE_DIRECT_URL` is
reserved for Alembic and cutover publication. See the
[PostgreSQL control-plane runbook](docs/postgresql_control_plane.md) before a
live migration.

Public image search persists rolling p50/p95 latency, top-100 ranked candidate
evidence, top-10 impressions, and Great/Good/Bad relevance feedback. The model
operations dashboard reports search volume, complete-query Precision@10,
graded nDCG@10, exact-versus-HNSW Recall@100, and durable pipeline-stage success.

Only explicitly `demo_eligible` and SFW artwork enters Qdrant. Accepted Bluesky
images that passed the configured source-label/text moderation gate now receive
those flags automatically. Legacy, local, and source-unverified artwork stays
unknown and demo-ineligible until reviewed; SigLIP itself is still not an adult
content classifier.

Embedding generation uses optional ML dependencies:

```bash
python -m pip install -e '.[embed]'
python scripts/embed.py
python scripts/search_demo.py <artwork_id>
python scripts/open_gallery.py
python scripts/search_gallery.py --mode ensemble
python scripts/search_gallery.py --mode clip_subject
python scripts/search_gallery.py --mode csd_style
```

The default `ensemble` ranking is not a concatenated mega-vector. CLIP subject
and CSD style cosine retrieval each produce a configurable recall list; their
union is ordered with reciprocal-rank fusion. Configure
`retrieval.shortlist_size` in `config/config.yaml`. Every result retains its
fusion rank plus the original CLIP and CSD similarities and ranks.

The retrieval package also exposes CLIP text embedding plus normalized
mean-difference, query-shift, and Gram-Schmidt direction primitives. These are
the backend foundation for continuous subject/nameable-medium controls and
multi-exemplar unnameable-style axes. The static dashboard does not pretend to
run those model-backed controls in the browser; a live exploration service is
the next UI boundary for them.

Artwork-content filtering supports deterministic media/provenance rules and a
batched zero-shot SigLIP 2 classifier. Install the ML dependencies before using
visual classification:

```bash
python -m pip install -e '.[filter]'
python scripts/artwork_filter.py classify-image --path ./example.jpg --json
python scripts/artwork_filter.py classify-jsonl \
  --input data/bluesky/image_candidates.jsonl \
  --output data/filter/decisions.jsonl
python scripts/artwork_filter.py inspect-prompts --path ./example.jpg --top-k 15
python scripts/artwork_filter.py info
```

Use `--deterministic-only` to validate media and provenance without loading or
downloading a visual model. The default zero-shot thresholds are conservative
starting values, not calibrated probabilities; uncertain images are routed to
review.

Validate a downloaded checkpoint on explicitly selected local image files
before using it for a collection run. The paths below are examples and must be
replaced with files that actually exist:

```bash
python scripts/artwork_filter.py smoke-test-model \
  --path ./safe-art-example.jpg \
  --path ./safe-non-art-example.jpg \
  --output data/filter/reports/siglip-smoke.json
```

The smoke report checks finite embeddings, output dimensions, prompt scoring,
and approximate agreement between batch and single-image inference. It emits
filenames rather than parent directories and does not persist filter decisions.

Human review and calibration use an append-only workflow:

```bash
python scripts/artwork_filter.py export-review \
  --candidates data/bluesky/image_candidates.jsonl \
  --decisions data/filter/decisions.jsonl \
  --output data/filter/review/review-001.csv

python scripts/artwork_filter.py import-labels \
  --input data/filter/review/review-001.csv \
  --output data/filter/labels/artwork-labels.jsonl \
  --annotator local-reviewer

python scripts/artwork_filter.py evaluate \
  --candidates data/bluesky/image_candidates.jsonl \
  --decisions data/filter/decisions.jsonl \
  --labels data/filter/labels/artwork-labels.jsonl \
  --output data/filter/reports/evaluation.json

python scripts/artwork_filter.py calibrate \
  --candidates data/bluesky/image_candidates.jsonl \
  --decisions data/filter/decisions.jsonl \
  --labels data/filter/labels/artwork-labels.jsonl \
  --output data/filter/reports/calibration.json \
  --sweep-output data/filter/reports/threshold-sweep.csv
```

Complete the four `label_*` columns in the exported review CSV using the
documented content classes plus `uncertain`, original-work values
`yes`/`no`/`unknown`, and corpus-inclusion values `yes`/`no`/`review`. Exact
re-imports are skipped, while changed judgments append a new label event.
Evaluation replays the exact decision snapshot captured in each label.
Calibration refuses mixed model/config cohorts and recommends thresholds only
when the configured minimum sample count and 95% precision lower-bound targets
are met; it never edits the production TOML automatically.

Bluesky candidate collection uses the public AppView API and does not require
credentials for the initial read-only author-feed workflow:

```bash
read -r -p "Bluesky handle (without @): " BLUESKY_HANDLE

python scripts/bluesky_collect.py collect-author \
  --actor "$BLUESKY_HANDLE" \
  --max-pages 2 \
  --output data/bluesky/image_candidates.jsonl

python scripts/bluesky_collect.py collect-authors \
  --actors-file config/bluesky_actors.local.txt \
  --output data/bluesky/image_candidates.jsonl \
  --resume
```

The default collector runs in `public_safe_mode`. Before an image becomes an
`ImageCandidate`, it excludes active post/account labels including `porn`,
`sexual`, `sexual-figurative`, `nudity`, and `graphic-media`, plus conservative
adult-text markers. Non-blocking labels are retained on candidates and shown in
the Bluesky dashboard. This follows Bluesky's label model, where labels are
attached to API records and clients decide whether to hide or warn on them:
<https://docs.bsky.app/docs/advanced-guides/moderation>.

The full actor list is a discovery pool, not a reviewed roster. Build a
metadata-only pilot list before collecting images:

```bash
python scripts/bluesky_collect.py audit-actors \
  --actors-file config/bluesky_actors.local.txt \
  --pilot-output config/bluesky_safe_pilot.local.txt \
  --report-output data/bluesky/artist_audit.jsonl

python scripts/bluesky_pipeline.py run-authors \
  --actors-file config/bluesky_safe_pilot.local.txt \
  --max-pages 1 \
  --limit 15 \
  --no-review-download
```

The audit downloads no image bytes. It favors accounts with enough allowed
media, a low blocked-post rate, and feminine or mixed subject terms in captions
or alt text. Those terms are composition hints, not claims about an artist or
character's identity. Moderation labels and text signals are not a guarantee
that every unlabeled image is suitable for every setting, so a dedicated visual
adult-content classifier remains worthwhile before unattended large-scale
collection.

The complete collection-to-corpus workflow is available as one command:

```bash
read -r -p "Bluesky handle (without @): " BLUESKY_HANDLE

python scripts/bluesky_pipeline.py run-author \
  --actor "$BLUESKY_HANDLE" \
  --max-pages 2

python scripts/bluesky_pipeline.py run-authors \
  --actors-file config/bluesky_actors.local.txt \
  --max-pages 1 \
  --resume

python scripts/bluesky_pipeline.py process-jsonl \
  --input data/bluesky/image_candidates.jsonl
```

Pipeline decision JSONL is append-only by default. Use
`--overwrite-decisions` only when intentionally starting a fresh local decision
file. Candidate replacement is staged atomically, while `--resume` appends only
new `(candidate_id, post_cid)` versions and skips actors checkpointed with the
same collection settings. Transient API failures are retried with bounded
backoff; one bad actor does not stop the list unless `--fail-fast` is supplied.
Any actor, classification, or routing errors produce a non-zero command exit.

The checked-in configuration is deliberately a review-only pilot:
`automatic_accept_enabled = false` and `download_review_images = false`. A
default run collects candidates, classifies thumbnails, caches SigLIP features,
and writes decision evidence to JSONL and SQLite, but it does not put image
bytes or artwork rows into the retrieval corpus. After validating and
calibrating the model, explicitly set `automatic_accept_enabled = true` in the
filter config. High-confidence `ACCEPT` results will then download the full-size
image, strip metadata by re-encoding it, standardize it, and upsert the artwork
row into `data/artsearch.db`. Run `python scripts/embed.py` afterward to create
the retrieval CLIP and CSD embeddings for newly imported artwork.

Review-image downloads remain an explicit config opt-in. `--no-review-download`
can enforce the no-download behavior when using a different configuration.
After changing model, prompt, or threshold configuration, run
`process-jsonl --resume` against the existing candidate file; resume matching
includes the post CID, so edited Bluesky posts are classified again.

Browse the latest Bluesky candidates and their complete filter evidence in the
local gallery:

```bash
.venv/bin/artsearch-open-gallery --source bluesky

# Generate the HTML without launching a browser.
.venv/bin/artsearch-open-gallery --source bluesky --no-open
```

The Bluesky view reads `data/bluesky/image_candidates.jsonl` and
`data/filter/decisions.jsonl` by default. It is read-only, selects the latest
decision per candidate, and loads thumbnails or full-size images directly from
Bluesky's CDN. Use `--candidates`, `--decisions`, or `--output` to override
those paths.

To use the artwork-like SigLIP output as the active heavy-ML corpus, first
inspect the exact selection:

```bash
python scripts/rebuild_bluesky_corpus.py --dry-run
```

Then archive the existing active database/raw/processed files, rebuild a fresh
SQLite corpus from the selected full-size Bluesky images, compute pinned CLIP
and CSD embeddings, and write the retrieval workbench:

```bash
python scripts/rebuild_bluesky_corpus.py --confirm-replace
.venv/bin/artsearch-open-gallery
```

The final workbench has two tabs. **Browse** shows the primary ensemble ranking.
Each result reports its final fusion rank, independent CLIP subject rank, CSD
style rank, scores, and recall-set size. **Evaluation** compares that complete
recipe against both component retrievers. Dashboard generation precomputes
three seeded, artist-balanced review sessions by default. Each session uses one
random query per artist without repeating query images between sessions. Every
displayed result is a top-k `MATCH` guess. Mark each result `Yes` or `No` for the
mode-specific question; the dashboard reports annotation
coverage, judged precision, P@1/5/10, hit rate, pool recall, MRR, MAP, nDCG,
score calibration, model revisions, linked SigLIP gate evidence, and the exact
component evidence used for each ensemble judgment.

Judgments are kept in browser storage for immediate use. Exporting cumulative
append-only JSONL after judging the active session unlocks **Next review
session**; changing a judgment after export locks advancement until the updated
history is exported again. Each successive file contains the corpus judgment
history through that session, so the latest export is sufficient for backend
evaluation. Previous sessions remain available from the session selector, and
imports restore judgments for the same corpus fingerprint. Use
`--review-sessions` to change the precomputed pool size and `--review-seed` to
reproduce an exact query selection:

```bash
.venv/bin/artsearch-open-gallery \
  --review-sessions 3 \
  --review-seed pilot-001
```

The workbench remains a self-contained static HTML file. All session rankings
are computed while the file is generated, so larger review-session pools take
longer to build but require no local server while labeling.

Produce a reproducible backend report from an export with:

```bash
python scripts/evaluate_retrieval.py \
  --judgments ./retrieval-judgments-CORPUS_ID.jsonl \
  --output data/retrieval/evaluation.json
```

`pool recall` is intentionally named: true full-corpus recall requires
relevance judgments outside the displayed result pool. Similarity scores are
not presented as calibrated probabilities.

The seed policy accepts only the latest decision matching the candidate's
current post CID, an artwork-like SigLIP class, and resolved direct-post
provenance. Reposts and quote posts are excluded by default. The original
review/reject decision JSONL remains unchanged; promoted SQLite evidence gets
the stable `accept.siglip_corpus_seed` reason. Prior active corpus files are
moved under ignored `data/corpus_archive/<timestamp>/`, so replacement is
reversible.

Storage is intentionally split by responsibility:

- Candidate and decision JSONL are under ignored `data/bluesky/` and
  `data/filter/` paths.
- Review CSV, append-only human labels, and evaluation reports remain under the
  same ignored `data/filter/` privacy boundary.
- SigLIP image and prompt embeddings are atomic `.npy` files under ignored
  `data/cache/artwork_filter/`.
- Accepted and review image bytes remain in ignored filesystem directories.
- SQLite stores source metadata, complete filter evidence, routes, standardized
  artwork metadata, and links to local files rather than duplicating image bytes.

The first visual run may download the configured Hugging Face checkpoint. No
model is loaded and no network request is made at Python import time. Pin the
model revision in `configs/artwork_filter.default.toml` after validating the
baseline.

Artwork classification does not grant copyright permission, verify attribution,
or override artist opt-outs and upstream deletion handling.

Generated image data, processed outputs, Bluesky candidate and decision JSONL,
feature caches, review images, the SQLite DB, local demo HTML, and private local
artist/actor manifests are intentionally ignored by git.
