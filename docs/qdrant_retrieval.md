# ArtSearch Qdrant Retrieval

Status: implemented local/Cloud-ready vertical slice

Updated for CSD: 2026-08-20

Qdrant is a derived online serving index. It does not replace the canonical
SQLite metadata database, immutable accepted images in object storage, or the
CLIP/CSD embedding workers. Losing a Qdrant collection must be recoverable by
creating a new physical collection and syncing current canonical rows.

## Retrieval Contract

One Qdrant point represents one explicitly demo-approved artwork:

| Field | Purpose |
| --- | --- |
| Point ID | Deterministic UUIDv5 derived from `artwork_id` |
| `clip_subject` | 512-dimensional normalized CLIP cosine vector |
| `csd_style` | 768-dimensional normalized Contrastive Style Descriptor cosine vector |
| Payload | Compact filtering and model-lineage fields only |

The current physical collection is `artworks_clip3_csd1_v2`; reads use the
stable `artworks_active` alias. The complete 8,279-artwork corpus was built and
validated there before the alias moved from v1; v1 remains the rollback target.
Future model or corpus migrations follow the same immutable-collection process.

The Cloud configuration uses float16 storage for both vectors. The previous
DINO patch matrices and late-interaction stage are not part of the current
serving contract.

## Eligibility Policy

An artwork is indexable only when all applicable conditions hold:

```text
validated = true
processed image exists in canonical metadata
not an exact-duplicate child
demo_eligible = true
is_sfw = true
current configured CLIP embedding
current configured CSD embedding
latest Bluesky filter decision = ACCEPT
```

`is_sfw` and `demo_eligible` remain independent from SigLIP's artwork decision.
SigLIP answers whether an image is useful artwork; it is not an NSFW detector.
For Bluesky intake, a separate versioned source gate removes configured adult
labels and text signals before candidate creation. An accepted image carrying
evidence that it passed that gate is marked SFW and demo-eligible by the current
policy. Local, legacy, and source-unverified rows remain unknown.

Existing databases migrate `demo_eligible` to false and are not retroactively
assumed safe. Inspect the state with:

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

The command accepts repeated `--artwork-id` values. Source moderation is
auditable per candidate and does not create a bulk safety assumption for older
rows.

## Query Funnel

An image-to-image request performs:

```text
CLIP subject HNSW prefetch, top 200 ──┐
                                      ├─ Qdrant RRF, top 100
CSD style HNSW prefetch, top 200 ─────┘
                                      |
                                      v
                     max two results per artist, top 20
```

CLIP and CSD are never concatenated. Qdrant records each retriever's raw score
and rank plus the RRF score and rank. Text queries use CLIP only because CSD is
an image style descriptor, not a text encoder.

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
  --top-k 100 \
  --seed 20260813
```

The JSON report includes mean/minimum Recall@100 plus approximate and exact
p50/p95 latency for CLIP and CSD separately. Tune `hnsw_ef` from measured
recall and latency; do not tune it from a generic benchmark.

## Airflow Integration

The intake DAG now executes:

```text
collect + SigLIP filter
    -> publish accepted originals
    -> generate current CLIP/CSD embeddings
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
- Alpha query/session telemetry, top-100 ranking evidence, top-10 impressions,
  and Great/Good/Bad relevance feedback are persisted in PostgreSQL.
- The CSD checkpoint is a converted safetensors artifact pinned by repository
  revision and SHA-256; its quality must be measured on ArtSearch judgments.
- Local-detail late interaction is intentionally absent after retiring DINO
  patches. Add a replacement only when evaluation demonstrates the need.
- HNSW quality has a measurement command, but no large-corpus benchmark should
  be claimed until it is run against representative filters and judgments.

## Primary References

- [Qdrant collections and aliases](https://qdrant.tech/documentation/manage-data/collections/)
- [Qdrant named vectors and points](https://qdrant.tech/documentation/manage-data/points/)
- [Qdrant hybrid and multi-stage queries](https://qdrant.tech/documentation/search/hybrid-queries/)
- [Qdrant vector datatypes](https://qdrant.tech/documentation/manage-data/vectors/)
- [Qdrant payload indexes](https://qdrant.tech/documentation/search/text-search/text-filtering/)
- [Qdrant capacity planning](https://qdrant.tech/documentation/operations/capacity-planning/)
- [Contrastive Style Descriptors paper](https://arxiv.org/abs/2404.01292)
- [CSD authors' implementation](https://github.com/learn2phoenix/CSD)
