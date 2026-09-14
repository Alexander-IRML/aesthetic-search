# ArtSearch Model Operations Dashboard

Status: implemented local operator surface

Implemented: 2026-08-17

Updated: 2026-09-12

The model operations dashboard is a localhost operator application for
maintaining corpus, classifier, retrieval, and ANN quality. Its monitoring
tabs read the canonical SQLite database and append-only evaluation artifacts.
Its review tab displays explicitly revealed local evidence and writes immutable
human labels and relevance judgments. The Discovery tab also exposes bounded local
operator jobs for incremental Bluesky intake and creator-graph expansion. Model
weights are loaded only by those background pipeline commands, never by a normal
dashboard snapshot.

## Launch

Install the project command after each entry-point change:

```bash
.venv/bin/python -m pip install -e . --no-deps
```

Run the dashboard from the repository:

```bash
.venv/bin/artsearch-model-dashboard
```

When present, the command loads the ignored `deploy/alpha/.env` file and selects
`configs/production.alpha-cloud.local.toml`; otherwise it uses the checked-in local
production config. Secret values are inherited by child jobs and are never returned
to the browser or written into job status.

The server binds to `127.0.0.1`, selects ports `8765` through `8774` if needed,
opens the system browser, and refreshes its snapshot every 30 seconds. Closing
the terminal or pressing `Ctrl+C` stops it.

Install a double-click launcher:

```bash
.venv/bin/artsearch-model-dashboard --install-desktop-launcher
```

Under WSL this creates `ArtSearch Model Dashboard.cmd` on the Windows Desktop.
The launcher enters Windows' default WSL distribution at this exact project
directory and starts the installed command directly, without a nested shell.
On a regular Linux desktop it creates an executable freedesktop launcher
instead. Startup failures remain visible in the launcher window with an exit
code and reinstall command.

Useful operator options:

```bash
# Inspect without probing Qdrant.
.venv/bin/artsearch-model-dashboard --no-qdrant-probe

# Export the same sanitized snapshot used by the UI.
.venv/bin/artsearch-model-dashboard \
  --no-qdrant-probe \
  --snapshot-json data/monitoring/model-health.json

# Supply explicit evidence rather than using automatic discovery.
.venv/bin/artsearch-model-dashboard \
  --decisions data/filter/decisions.jsonl \
  --judgments data/retrieval/judgments.jsonl \
  --filter-evaluation data/filter/reports/evaluation.json \
  --ann-evaluation data/retrieval/qdrant_ann.json

# Choose the append-only review outputs and reviewer identity.
.venv/bin/artsearch-model-dashboard \
  --review-labels data/filter/labels/dashboard.jsonl \
  --review-judgments data/retrieval/judgments-dashboard.jsonl \
  --annotator local-reviewer
```

Remote binding is rejected unless `--allow-remote` is explicit. The current UI
is intended for one local operator. Human review is disabled on non-loopback
bindings even when remote monitoring is explicitly enabled.

## What It Measures

### Overview

- searchable, validated, and serving-eligible corpus counts;
- current CLIP plus CSD-style embedding coverage;
- filter decision count, p95 latency, error rate, and freshness;
- Qdrant point and canonical-policy reconciliation;
- a severity-sorted action queue with observed values and configured targets.

### Model Quality

- SigLIP automatic-ACCEPT precision, 95% interval, recall, coverage, macro F1,
  and content-class accuracy from human-labeled evaluation output;
- ensemble and component retrieval P@1/P@5/P@10, MRR, MAP, nDCG, annotation
  coverage, and judged query count;
- exact-versus-HNSW mean and worst-query recall plus exact/approximate p50 and
  p95 latency for each Qdrant named vector;
- all-time search volume, rolling search p50/p95 latency and success rate,
  Great/Good/Bad counts, judged hit rate, complete-query Precision@10 and
  graded nDCG@10;
- Recall@100 from exact-versus-HNSW evaluation at K=100, kept separate from
  user feedback;
- finite-value and batch/single consistency status from the SigLIP smoke report.

Missing evaluation evidence is shown as **not measured** and raises an action
item. An absent report is never converted to a zero score or a passing state.

### Data And Drift

- decision, predicted-class, and route distributions;
- confidence, final score, margin, and filter latency distributions;
- 7-day recent versus 30-day baseline Jensen-Shannon divergence for decisions,
  content classes, and routes;
- recent-minus-baseline confidence and score shifts;
- recent/baseline filter p95 latency ratio;
- anonymous top-artist share, top-five share, and Herfindahl concentration;
- CLIP and CSD coverage, storage, and model-version cohorts.

Jensen-Shannon divergence is bounded from 0 for identical distributions to 1
for disjoint distributions. The dashboard requires the configured minimum
number of decisions in both cohorts before treating drift as measured. Initial
thresholds in `configs/model_dashboard.default.toml` are operator guardrails,
not empirically validated universal constants.

### Operations

- sanitized Qdrant connectivity, collection, alias, point count, and index
  ledger state;
- model identifiers, exact revisions, prompt version/hash, and config hashes;
- recent pipeline runs, processed/skipped/error counts, and object publication;
- decision recency, monitoring targets, and project-relative evidence sources.

### Discovery And Corpus Control

- a canvas cluster map of accepted corpus artists and PostgreSQL discovery
  candidates;
- compact per-artist pulled-post keys derived from canonical AT URIs, with aggregate
  decision state and image count;
- typed follow/suggested-account graph edges when PostgreSQL is online;
- **Pull unscanned posts**, which seeds missing high-water marks from prior decisions
  and runs the existing incremental, deduplicated SigLIP-to-SQLite pipeline;
- **Expand similar accounts**, with one-to-three-step depth and a hard total Bluesky
  HTTP-attempt budget;
- one background job at a time, persisted status, bounded logs, elapsed time, and
  final pipeline/campaign counters.

The refresh action downloads only newly observed candidates after the high-water
boundary. The expansion action rotates through existing corpus DIDs so repeated
runs do not always start from the same artists. Existing-corpus seeds are trusted
for the first graph hop; every newly discovered account still passes profile safety,
media sampling, SigLIP qualification, and normal review policy.

### Review And Feedback

The artwork-gate workspace reads the latest canonical SigLIP decision for each
candidate. It shows the predicted class, decision, route, final score,
confidence, margin, utility/noise scores, prompt-class scores, reason codes,
and the candidate's post and alt text. A review records:

- the actual content class;
- whether the image appears to be the source artist's own work;
- whether it belongs in the main corpus;
- an optional reviewer note and the exact source decision/model/config version.

The retrieval workspace samples a fresh query from the current compatible
embedding cohort and runs the selected exact mode: ensemble, CLIP subject, or
CSD style. Each useful/not-useful click records
the query and candidate IDs, mode, rank, result count, component scores,
shortlist metadata, corpus fingerprint, model version, session ID, and reviewer.
These judgments immediately enter the retrieval metrics on the next snapshot.

Review media is visible by default for the local operator. Bluesky posts with
configured adult labels or blocked text signals are removed before candidate
creation, so they cannot enter this review queue. The dashboard remains bound
to loopback and never places image bytes in its aggregate snapshot.

## Data And Privacy Boundary

The monitoring snapshot sent to the browser contains aggregate counts, metrics,
version lineage, action text, and project-relative evidence labels. It
intentionally excludes:

- image bytes and image paths;
- post text, alt text, candidate URLs, and artist identifiers;
- model hub credentials, Qdrant API keys, and HTTP headers;
- embedding vectors.

The review API is enabled only on loopback and requires a random per-launch
token embedded in the same-origin page. Writes additionally require JSON and a
custom token header, preventing ordinary cross-origin form submission. Local
files are constrained to the project root; remote candidate media goes through
the artwork filter's HTTPS host allowlist, byte/pixel limits, decoder, EXIF
transpose, and metadata-free JPEG re-encoding.

The server sends `no-store`, frame denial, MIME sniffing denial, no-referrer,
and restrictive content-security headers. Generated snapshots, labels, and
judgments live under the ignored `data/` boundary by default.

## Evidence Sources

Automatic discovery uses:

| Area | Default source |
| --- | --- |
| Corpus, vectors, runs, publications | `data/artsearch.db` |
| Filter operations | `data/filter/decisions.jsonl` |
| Filter labels | `data/filter/labels/dashboard.jsonl` |
| Filter quality | live SQLite + label evaluation, then newest saved report |
| SigLIP runtime smoke | newest `data/filter/reports/*smoke*.json` |
| Retrieval relevance | `data/retrieval/**/judgments*.jsonl` |
| ANN fidelity | newest `data/retrieval/**/qdrant_ann*.json` |
| Qdrant live state | configured REST endpoint, when probing is enabled |
| Online search, feedback, and approximate unique visitors | aggregate PostgreSQL product telemetry; visitor IPs are HMAC-hashed before persistence |
| Daily scheduler | `configs/intake_scheduler.default.toml` plus ignored scheduler JSONL events |
| Incremental source state | `bluesky_actor_intake_state` and `bluesky_roster_intake_state` in SQLite |

Every dashboard refresh rebuilds a read-only snapshot after a short cache
window. SQLite is opened in read-only mode. Operator review changes only the two
append-only JSONL streams; public result feedback appends immutable PostgreSQL
events. JSONL decisions are deduplicated to the latest
`(candidate_id, source_cid)` version, labels resolve to their exact decision
snapshot, and retrieval judgments use latest-event evaluation semantics.

The header **Corpus intake** control opens and focuses these Discovery controls. It
also retains the `artsearch:operator-action` browser event for local integrations.

## Current Limitations

- Online retrieval metrics remain statistically weak until enough searches have
  a completely judged top ten; partial judgments are shown only as hit rate and
  coverage.
- Filter quality is calculated as labels arrive, but remains statistically weak
  until the sample is representative and grouped across enough artists.
- Retrieval metrics describe the judged dashboard pool, not unobserved
  full-corpus recall.
- ANN recall requires a running Qdrant index and an exact comparison report.
- The server is a local operator tool, not a multi-user authenticated service.

These unknowns are visible in the UI because maintaining model performance
depends as much on knowing what has not been measured as on watching the values
that already exist.
