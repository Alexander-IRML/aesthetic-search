# Ten-Million Retrieval Runbook

Status: implemented shadow path; staged benchmarks required before cutover

This runbook operates the budget-constrained FAISS retrieval path accepted in
ADR 0003. It does not replace the current Qdrant alpha until an explicit,
measured promotion.

## What Exists

- A shared corpus-eligibility query for Qdrant and FAISS.
- Independent CLIP and CSD FAISS indexes with OPQ/IVF/PQ at scale.
- Memory-mapped FP16 vectors for bounded exact reranking.
- Immutable snapshots with manifests, checksums, compact SQLite catalogs, and
  an atomic active pointer.
- Exact-versus-ANN recall and search-latency evaluation.
- A hard USD 50/month capacity gate with configured minimum headroom.
- API backend selection through `vector_backend = "qdrant" | "faiss"`.
- Compact PostgreSQL publication and optional local-image cleanup for the scale
  profile.

## What Does Not Exist Yet

- A mutable delta index between immutable snapshot rebuilds.
- Automated snapshot upload/download and host rollout.
- A bounded R2 display-image cache or source-deletion reconciliation worker.
- Compaction of all historical decision evidence in the local SQLite staging
  database.
- Representative 100k, 1M, or 10M benchmark evidence.
- High availability. The budget accepts one serving host.

Those are rollout gates, not hidden claims of completion.

## Install

Use the pinned Python 3.12 runtime for deployed builds:

```bash
python -m pip install -e '.[scale,api,data,postgres]'
```

Copy the examples to ignored local files before adding provider-specific
values:

```bash
cp configs/production.scale-10m.example.toml \
  configs/production.scale-10m.local.toml
cp configs/search_api.scale-10m.example.toml \
  configs/search_api.scale-10m.local.toml
cp configs/artwork_filter.scale-10m.example.toml \
  configs/artwork_filter.scale-10m.local.toml
```

Keep Qdrant live in the alpha configuration during all shadow work. The scale
production example intentionally has `qdrant.enabled = false`; do not deploy
that file over the alpha by accident.

## Inspect The Contract

```bash
artsearch-faiss config \
  --production-config configs/production.scale-10m.local.toml

artsearch-faiss status \
  --production-config configs/production.scale-10m.local.toml
```

The API refuses a snapshot whose CLIP model, CSD model, dimensions, or model
bundle fingerprint differs from the query encoder contract.

## Run The Cost Gate

Generate and retain the capacity evidence used for promotion:

```bash
artsearch-faiss capacity-plan \
  --production-config configs/production.scale-10m.local.toml \
  --points 10000000 \
  --output data/indexes/reports/capacity-10m.json
```

Exit code 3 means the configured bill lacks the required headroom. Update the
assumptions from actual provider prices and measured image sizes before each
production promotion. Do not weaken the gate merely to make a build pass.

## Prepare Corpus Features

The FAISS builder reads only eligible rows with exact current CLIP and CSD model
versions. Complete intake and embedding first:

```bash
python scripts/embed.py
```

At scale, embedding generation streams missing or stale SQLite rows with
keyset pagination. It counts already-current rows once instead of loading the
whole corpus or emitting one no-op event per artwork.

The scale profile does not bulk-publish accepted renditions to R2. It publishes
source references and compact metadata, and the API can return trusted Bluesky
CDN URLs. The Airflow cleanup task runs last and only after durable manifests,
run bundles, model lineage, and required PostgreSQL publication succeed.

Use `configs/artwork_filter.scale-10m.local.toml` as the Airflow
`filter_config`. It disables the per-image SigLIP embedding cache so rejected
candidate features do not become a second unbounded corpus. The small shared
prompt-embedding cache remains available. Preserve decision evidence in the
immutable run bundle for audit and threshold replay.

Preview cleanup separately at any time:

```bash
artsearch-data prune-local-images \
  --config configs/production.scale-10m.local.toml \
  --decisions data/filter/decisions.jsonl
```

Add `--apply` only after reviewing the report. Cleanup requires accepted corpus
status, current CLIP and CSD vectors, and a source blob CID or media URL. Paths
outside configured raw/processed roots are never removed.

## Build An Inactive Snapshot

```bash
SNAPSHOT=faiss-$(date -u +%Y%m%dT%H%M%SZ)

artsearch-faiss build \
  --production-config configs/production.scale-10m.local.toml \
  --snapshot-version "$SNAPSHOT"
```

Small snapshots below the configured threshold use exact flat indexes. Do not
use `--force-flat` for a large corpus. The build writes into a temporary
directory and renames it only after indexes, rerank matrices, catalog, manifest,
checksums, and `READY` marker are complete.

The builder holds one consistent SQLite read transaction across counting,
training, and population. Pause intake writers during large builds so the
SQLite WAL cannot grow without bound while that read view remains open.

Plan host storage for the active snapshot, one rollback snapshot, one snapshot
under construction, operating-system space, and build scratch. The current
estimate makes 120 GB a minimum and 160 GB a more comfortable target. Measure
peak RAM and disk during the 100k and 1M rehearsals before buying the 10M host.

## Evaluate Against Exact Search

```bash
artsearch-faiss evaluate \
  --production-config configs/production.scale-10m.local.toml \
  --snapshot-version "$SNAPSHOT" \
  --sample-size 500 \
  --top-k 100 \
  --seed 20260908 \
  --output "data/indexes/reports/$SNAPSHOT-evaluation.json"
```

The initial gates require mean Recall@100 of at least 0.90 independently for
CLIP and CSD, plus end-to-end warm search p95 no greater than 250 ms on the
serving host. Also inspect minimum per-query recall, filtered artist searches,
build duration, resident memory, disk, and p99. The CLI's small default sample
is for smoke tests, not production approval.

## Promote Atomically

```bash
artsearch-faiss promote \
  --production-config configs/production.scale-10m.local.toml \
  --snapshot-version "$SNAPSHOT" \
  --evaluation "data/indexes/reports/$SNAPSHOT-evaluation.json" \
  --budget data/indexes/reports/capacity-10m.json
```

Promotion verifies all snapshot checksums and rejects stale, malformed,
non-finite, weaker-than-current, wrong-population, wrong-version, or over-budget
evidence. It then atomically writes the active pointer. Existing service
processes continue using their already-open snapshot until restarted or
explicitly reloaded.

## Serve And Verify

Start the API with the scale search and production configurations:

```bash
artsearch-api \
  --api-config configs/search_api.scale-10m.local.toml \
  --production-config configs/production.scale-10m.local.toml
```

Verify `/health/live`, `/health/ready`, and one known-image search. Readiness
reports `vector_backend=faiss`, the exact active snapshot version, and model
bundle fingerprint. Compare ranking and telemetry against the Qdrant shadow
path before changing public traffic.

## Staged Rollout

1. Build and compare at 100k; tune `nlist`, `nprobe`, shortlist size, and PQ.
2. Repeat at 1M on the intended host class and measure peak build resources.
3. Recompute the provider cost plan from real storage and host prices.
4. Build the full snapshot, evaluate it, and run a Qdrant-versus-FAISS shadow
   comparison on saved queries and human judgments.
5. Promote FAISS for a small traffic slice, then retire managed vectors only
   after the rollback window and cost/quality review.

## Rollback

Keep the previous snapshot and its reports. Re-promote that version with its
still-valid evaluation and a current capacity report, restart the API, and
verify readiness. If the FAISS service itself is unhealthy, restore the API's
Qdrant configuration; the Qdrant collection is not deleted by FAISS promotion.
