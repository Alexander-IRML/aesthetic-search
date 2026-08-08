# ArtSearch

Data preparation and baseline visual search pipeline for artwork.

The current MVP ingests explicitly registered artist folders from `data/raw/`,
standardizes images into 448x448 JPEGs in `data/processed/`, tracks metadata in
SQLite, stores CLIP and DINOv2 embeddings, and can generate local HTML search
demos using a two-stage DINO ensemble with auditable CLIP, pooled-DINO, and
patch-DINO component evidence.

For a more detailed technical summary, see
[docs/mvp_report.md](docs/mvp_report.md).
The current component map, Bluesky pipeline sequence, and SQLite relationship
diagram are in [docs/artsearch_architecture.puml](docs/artsearch_architecture.puml).

## Local Workflow

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

Embedding generation uses optional ML dependencies:

```bash
python -m pip install -e '.[embed]'
python scripts/embed.py
python scripts/search_demo.py <artwork_id>
python scripts/open_gallery.py
python scripts/search_gallery.py --mode ensemble
python scripts/search_gallery.py --mode clip_subject
python scripts/search_gallery.py --mode dino_pooled
python scripts/search_gallery.py --mode dino_patch_maxsim
python scripts/patch_diagnostics.py <query_artwork_id> <candidate_artwork_id>
```

The default `ensemble` ranking is a staged recipe, not a concatenated or
weighted mega-vector. DINO pooled cosine similarity searches the eligible
corpus and selects the configured shortlist. Patch-token late interaction then
reranks only those candidates. CLIP subject similarity is computed over the
same eligible corpus and displayed as a parallel semantic rank, but it cannot
change ensemble ordering. Configure `retrieval.shortlist_size` and
`retrieval.patch_match_top_n` in `config/config.yaml`; top-N patch matching
supports direct A/B testing against strict MaxSim (`top_n = 1`).

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
the retrieval CLIP and DINO embeddings for newly imported artwork.

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
and DINO embeddings, and write the retrieval workbench:

```bash
python scripts/rebuild_bluesky_corpus.py --confirm-replace
.venv/bin/artsearch-open-gallery
```

The final workbench has two tabs. **Browse** shows the primary ensemble ranking.
Each result reports its final patch rank, pooled-recall rank, independent CLIP
semantic rank, scores, and shortlist size. **Evaluation** compares that complete
recipe against the three component baselines. Dashboard generation precomputes
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
