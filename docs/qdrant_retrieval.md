# ArtSearch Qdrant Retrieval

Status: implemented local/Cloud-ready vertical slice

Implemented: 2026-08-13

Qdrant is a derived online serving index. It does not replace the canonical
SQLite metadata database, immutable accepted images in object storage, or the
CLIP/DINO embedding workers. Losing a Qdrant collection must be recoverable by
creating a new physical collection and syncing current canonical rows.

## Retrieval Contract

One Qdrant point represents one explicitly demo-approved artwork:

| Field | Purpose |
| --- | --- |
| Point ID | Deterministic UUIDv5 derived from `artwork_id` |
| `clip_subject` | 512-dimensional normalized CLIP cosine vector |
| `dino_global` | 768-dimensional normalized DINOv2 cosine vector |
| Payload | Compact filtering and model-lineage fields only |

The initial physical collection is `artworks_clip3_dino1_v1`; reads use the
stable `artworks_active` alias. Model migrations build and validate a different
physical collection before atomically moving the alias.

The Cloud configuration uses float16 storage for both global vectors. DINO
patch matrices remain outside Qdrant and are loaded only for the fused
shortlist. This avoids multiplying index storage by hundreds of patch vectors
per artwork.

## Eligibility Policy

An artwork is indexable only when all applicable conditions hold:

```text
validated = true
processed image exists in canonical metadata
not an exact-duplicate child
demo_eligible = true
is_sfw = true
current configured CLIP embedding
current configured DINO embedding
latest Bluesky filter decision = ACCEPT
```

`is_sfw` and `demo_eligible` are independent from SigLIP's artwork decision.
SigLIP answers whether an image is useful artwork; it is not an NSFW detector
and cannot grant alpha-display approval.

Existing databases migrate `demo_eligible` to false. Therefore the first sync
is deliberately empty until policy is reviewed. Inspect the state with:

```bash
.venv/bin/artsearch-qdrant eligibility
```

Approve a specific reviewed artwork explicitly:

```bash
.venv/bin/artsearch-qdrant set-policy \
  --artwork-id ARTWORK_ID \
  --safety safe \
  --demo-eligible
```

The command accepts repeated `--artwork-id` values. No bulk "assume everything
is safe" default exists.

## Query Funnel

An image-to-image request performs:

```text
CLIP subject HNSW prefetch, top 200 ──┐
                                      ├─ Qdrant RRF, top 100
DINO global HNSW prefetch, top 200 ───┘
                                               |
                                               v
                               exact DINO patch MaxSim, top 50
                                               |
                                               v
                              max two results per artist, top 20
```

CLIP and DINO are never concatenated. Qdrant records each retriever's raw score
and rank plus the RRF rank; local patch reranking records its own score and
rank. Text queries use CLIP only because DINO has no text encoder.

The first fusion policy is unweighted reciprocal-rank fusion. Human retrieval
judgments can later tune weighted RRF or a learned fusion policy without
rewriting vectors.

## Local Setup

Install the client and start the pinned Qdrant service:

```bash
.venv/bin/python -m pip install -e '.[qdrant]'
docker compose -f orchestration/airflow/compose.yaml up -d qdrant
```

The service binds REST to `127.0.0.1:6333` and persists its rebuildable state
under ignored `data/qdrant/`.

Inspect, sync, and query:

```bash
.venv/bin/artsearch-qdrant config
.venv/bin/artsearch-qdrant status
.venv/bin/artsearch-qdrant sync

.venv/bin/artsearch-qdrant search-artwork ARTWORK_ID --top-k 20
.venv/bin/artsearch-qdrant search-text "orange character painting" --top-k 20
```

`search-text` loads the pinned local CLIP encoder. The query text is sent to
Qdrant only as a vector.

## Qdrant Cloud

Create an ignored runtime configuration and export secrets:

```bash
cp configs/production.qdrant-cloud.example.toml \
  configs/production.qdrant-cloud.local.toml
export QDRANT_URL='https://YOUR-CLUSTER.cloud.qdrant.io:6333'
export QDRANT_API_KEY='...'

.venv/bin/artsearch-qdrant sync \
  --production-config configs/production.qdrant-cloud.local.toml
```

The endpoint may be committed only as an example. API keys remain in the
runtime environment. Qdrant payloads intentionally omit post text, alt text,
source URLs, local paths, private notes, and image bytes. Search returns
`artwork_id`; SQLite hydrates private/display metadata afterward.

## Idempotency And Repair

`vector_index_points` records the successful physical collection, artwork ID,
point UUID, and a hash of both global vectors plus serving payload. A normal
sync upserts only new or changed hashes.

Sync also:

- deletes points that are no longer safe or demo eligible;
- notices remote/local count drift and re-upserts canonical points;
- deletes remote points not present in the eligible set;
- verifies the final exact point count;
- promotes the read alias only after reconciliation succeeds.

Use `--force` for a complete vector refresh, `--no-prune` for diagnostics, and
`--no-promote` while preparing a new collection version.

## HNSW Evaluation

Before promoting meaningful corpus growth, compare approximate results with
exact search for each named vector:

```bash
.venv/bin/artsearch-qdrant evaluate-ann \
  --sample-size 50 \
  --top-k 20 \
  --seed 20260813
```

The JSON report includes mean/minimum recall@K plus approximate and exact
p50/p95 latency for CLIP and DINO separately. Tune `hnsw_ef` from measured
recall and latency; do not tune it from a generic benchmark.

## Airflow Integration

The intake DAG now executes:

```text
collect + SigLIP filter
    -> publish accepted originals
    -> generate current CLIP/DINO/patch embeddings
    -> reconcile Qdrant and promote the alias
    -> build Polars manifests and metrics
    -> publish the immutable run bundle
```

The run bundle records embedding and Qdrant summaries. A Qdrant failure stops
the downstream run publication rather than advertising an incomplete serving
index. Candidate-level collection errors retain the existing bounded failure
policy.

## Current Boundaries

- The local/Cloud index is single-node and has no availability guarantee.
- Sparse lexical retrieval is not present yet.
- Alpha query/session/click telemetry is not present yet.
- Patch artifacts still use the current full SQLite matrices; compact patch
  storage remains the next storage optimization.
- HNSW quality has a measurement command, but no large-corpus benchmark should
  be claimed until it is run against representative filters and judgments.

## Primary References

- [Qdrant collections and aliases](https://qdrant.tech/documentation/manage-data/collections/)
- [Qdrant named vectors and points](https://qdrant.tech/documentation/manage-data/points/)
- [Qdrant hybrid and multi-stage queries](https://qdrant.tech/documentation/search/hybrid-queries/)
- [Qdrant vector datatypes](https://qdrant.tech/documentation/manage-data/vectors/)
- [Qdrant payload indexes](https://qdrant.tech/documentation/search/text-search/text-filtering/)
- [Qdrant capacity planning](https://qdrant.tech/documentation/operations/capacity-planning/)
