# ADR 0003: Budget-Constrained Ten-Million Retrieval

Status: Accepted for staged rollout

Recorded: 2026-09-08

## Context

ArtSearch needs a credible path from the current Qdrant-backed alpha to ten
million CLIP and CSD artwork vectors under a hard recurring infrastructure
budget of USD 50/month. Keeping two uncompressed named vectors in a managed
vector database would make vector memory and managed-service capacity the
dominant bill. Keeping every source original in object storage would also
exceed the complete budget under the current measured average image size.

The alpha has low traffic. The design should optimize for bounded recurring
cost, reproducible index releases, and measurable retrieval quality instead of
high availability or continuous per-item index mutation.

## Decision

1. Keep Qdrant as the live alpha backend while a separate FAISS backend is
   built and measured. No automatic fallback silently changes ranking behavior.
2. Build independent normalized CLIP-subject and CSD-style FAISS indexes. Do
   not concatenate the representations or compare their raw scores directly.
3. Use OPQ + IVF + PQ for large snapshots and exact `IndexFlatIP` for small
   fixtures. The initial 10M profile uses 32 CLIP and 48 CSD subquantizers at
   eight bits, `nlist=4096`, and `nprobe=32`.
4. Union candidates from both ANN indexes, use reciprocal-rank fusion for the
   shortlist, exactly rescore the bounded union from memory-mapped FP16 CLIP
   and CSD matrices, then apply exact RRF and artist diversity.
5. Treat indexes as immutable, checksummed snapshots. Promotion changes one
   atomic active pointer only after both a quality report and a capacity report
   pass their configured gates.
6. Publish a compact PostgreSQL control plane for the scale profile. FAISS owns
   serving vectors and Parquet run bundles own historical evidence, so
   PostgreSQL omits embeddings, filter history, and run history in that profile.
7. Keep accepted source references and model-ready vectors durable. Do not
   publish every corpus image to R2 in the 10M profile. The result catalog may
   use HTTPS media URLs from the trusted Bluesky CDN when no local or R2 object
   exists.
8. Delete local raw and processed Bluesky files only after accepted status,
   exact current CLIP/CSD versions, durable run publication, compact metadata
   publication, and a recoverable source reference are present. Cleanup is
   disabled in the normal alpha configuration and dry-run by default in the
   CLI.
9. Disable per-image SigLIP feature caching in the scale filter profile. Prompt
   embeddings remain cached, while accept/review/reject evidence remains in the
   immutable run bundle.

## Capacity Envelope

The checked-in 10M example projects USD 39.99/month and USD 10.01 of headroom:

| Component | Monthly assumption |
| --- | ---: |
| Single persistent vector host | $24.00 |
| Edge/API compute | $5.00 |
| Compact metadata database | $5.00 |
| Snapshot and hot-cache object storage | $0.99 |
| Contingency | $5.00 |
| Observability | $0.00 |

One 10M snapshot is estimated at 27.716 GiB. Two retained snapshots plus a
100,000-image hot cache are estimated at 61.253 GiB in object storage. The
estimate uses 2,976 bytes per point: 80 bytes of PQ codes, 16 bytes of ANN IDs,
2,560 bytes of exact FP16 rerank vectors, and 320 bytes of compact metadata.

These are editable assumptions and a promotion gate, not a provider invoice.
Usage charges, taxes, egress, backups, and price changes are explicitly outside
the arithmetic. At the current 723 KB source-image assumption, retaining all
10M originals would add about 7.23 decimal TB and $108.45/month in object
storage alone, so it is not part of this budget profile.

The R2 Standard storage rate was checked against Cloudflare's official pricing
page on 2026-09-08: <https://developers.cloudflare.com/r2/pricing/>. The plan is
conservative about the 10 GB free storage allowance, but does not model request
charges or Cloudflare's billing-unit rounding. Those must be checked from real
usage before promotion.

## Consequences

- Two visual intents remain independently inspectable through ANN and exact
  component ranks and scores.
- Exact FP16 rerank matrices dominate snapshot size, but preserve ranking
  quality on a bounded candidate set without a managed-vector memory bill.
- A single host is an accepted availability tradeoff for early low traffic.
- Updates are snapshot-oriented. A delta index and automatic rebuild cadence
  are still required before frequent corpus mutations at 10M.
- Depending on source CDN media saves substantial storage cost but does not
  provide durable image ownership. Source deletion propagation, stale-result
  removal, and a bounded display-image cache remain required rollout work.
- A 10M snapshot must not be promoted based only on fixture tests. Representative
  100k and 1M shadow builds must establish recall, p95 latency, build time, RAM,
  disk, and recovery behavior on the intended host.

## Rejected Alternatives

- **Two managed, uncompressed 10M-vector collections:** simplest operationally,
  but incompatible with the hard budget at the expected dimensions.
- **One concatenated CLIP+CSD vector:** cheaper retrieval logic, but destroys
  independent subject/style evidence and makes weighting difficult to audit.
- **PQ-only final ranking:** smaller snapshots, but accepts avoidable ranking
  loss when exact FP16 rescoring of a few hundred candidates is affordable.
- **Ten million retained source originals:** operationally comfortable, but the
  measured storage assumption exceeds the entire monthly budget by itself.
