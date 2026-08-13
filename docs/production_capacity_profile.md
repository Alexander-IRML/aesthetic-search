# ArtSearch Production Capacity and Cost Profile

Status: recorded operating decisions for the production intake build

Recorded: 2026-08-12

This profile turns the production roadmap into a capacity and spending envelope.
It supersedes generic managed-cloud recommendations for the first alpha. The
corpus milestones below mean accepted, searchable artworks rather than source
candidates.

## Recorded Targets

| Input | Decision |
| --- | --- |
| Initial corpus | 5,000 accepted artworks |
| Three-month corpus | 100,000 accepted artworks |
| Eight-month corpus | 500,000 accepted artworks |
| Twelve-month corpus | 1,000,000 accepted artworks |
| First-year ordinary intake | About 2,000 source candidates/day plus backfills |
| Mature maintenance intake | About 500 source candidates/day |
| Design capacity | 10,000 candidates/day and 5,000 accepted artworks/day |
| Infrastructure budget | USD 20-50/month, excluding existing hardware and electricity |
| Rejected bytes | Delete immediately after durable decision evidence commits |
| Alpha audience | About five invited users |
| Availability | Best effort; single-node downtime is acceptable |
| Artwork-gate quality | Target at least 95% measured automatic-ACCEPT precision |

## Intake Arithmetic

The milestones are more aggressive than a flat 2,000 accepted images/day:

| Interval | Net accepted images | Required accepted/day |
| --- | ---: | ---: |
| Initial to month 3 | 95,000 | 1,056 |
| Month 3 to month 8 | 400,000 | 2,667 |
| Month 8 to month 12 | 500,000 | 4,167 |
| Full year after initial 5,000 | 995,000 | 2,726 average |

A flat 2,000 accepted images/day reaches approximately 735,000 images after one
year, and 2,000 *candidates* per day reaches less after rejects. The system will
therefore support 5,000 accepted images/day and at least 10,000 candidates/day,
while normal operation can remain near 2,000 candidates/day and use scheduled
backfills to meet the milestones.

Even 10,000 candidates/day is only 0.116 candidates/second averaged across a
day. This is a durability and storage problem, not a distributed-throughput
problem. One bounded local worker pool and nightly GPU batches are sufficient
until measurement proves otherwise.

## Frugal Alpha Architecture

```text
Bluesky Tap/AppView + manual imports
    -> PostgreSQL source records and durable job leases
    -> bounded local acquisition/quarantine
    -> decode, hashes, provenance, safety, and SigLIP gate
       -> REJECT: decision evidence only; delete bytes
       -> REVIEW: compact preview with a short TTL
       -> ACCEPT: one sanitized canonical image in Backblaze B2
    -> local CLIP/DINO batch workers
    -> named global vectors in Qdrant OSS
    -> compact DINO patch artifact in B2
    -> local disk LRU fetch + MaxSim over the shortlist
    -> private alpha API/dashboard
```

### Component choices

| Concern | Initial choice | Upgrade trigger |
| --- | --- | --- |
| Compute | Existing workstation; sequential pinned model workers | Measured backlog cannot recover inside the batch window |
| Metadata | Local PostgreSQL; SQLite retained for tests and migration input | Move PostgreSQL to the alpha VPS when remote operation is needed |
| Job dispatch | PostgreSQL leases with `FOR UPDATE SKIP LOCKED` | Add SQS/Temporal only for multi-host complexity that jobs cannot express |
| Object storage | Backblaze B2 through a narrow S3-compatible adapter | Reconsider R2 if public egress or edge delivery dominates cost |
| Vector search | Single-node Qdrant OSS, on disk where appropriate | Larger single node first; replicas only when availability becomes a product need |
| Qdrant Cloud | Optional free shadow/prototype only | Paid Cloud must win a measured operations/cost comparison |
| Patch features | Compressed `16x16 x 768` artifacts in B2 plus local LRU | Quantize or prune only after exact quality comparison |
| Deployment | Docker Compose plus service units | A small single VPS for the private alpha; no Kubernetes |
| Backups | Daily PostgreSQL dump and periodic Qdrant snapshot to B2 | Increase retention after restore drills expose the need |

Qdrant remains a rebuildable serving projection. PostgreSQL and immutable
feature manifests define what should be in an index. A lost Qdrant node is an
outage and rebuild, not corpus loss.

## Byte Retention Policy

### Rejected

1. Download into bounded local quarantine.
2. Decode, hash, classify, and commit decision evidence.
3. Delete the source bytes immediately after that transaction succeeds.
4. Retain only source identity, hashes, dimensions, policy/model versions,
   scores, reason codes, and timestamps.

### Review

Keep only a small review derivative, not the full source. Start with a 14-day
TTL. Promotion to ACCEPT creates the accepted canonical object; expiry deletes
the preview. This gives the dashboard a useful review lane without quietly
building a second corpus.

### Accepted

Keep one metadata-stripped, sRGB canonical image. Benchmark `1024`, `1280`, and
`1600` pixel longest-edge WebP variants over at least 1,000 representative
artworks. The starting target is `1280` pixels and at most 250 KiB average.
Delete the downloaded original after the canonical object checksum is verified.
Generate UI thumbnails into a bounded cache instead of retaining another
permanent derivative.

This policy trades preservation of the exact source file for lower storage. It
still retains the source URI/CID, raw SHA-256, decoded-pixel hash, transform
fingerprint, and provenance evidence.

## Feature Storage

The current SQLite file demonstrates why patch matrices need a different home:
117 embedding rows occupy about 370 MB, of which about 368 MB, or 99.5%, is the
current DINO patch BLOB column.

At the starred compact patch setting:

```text
256 patches x 768 dimensions x 2 bytes float16 = 384 KiB/image
1,000,000 images                                = about 366 GiB
```

One 768-dimensional float16 global vector is only about 1.43 GiB per million
images. Store only `semantic` and `visual_global` named global vectors in the
initial Qdrant collection. The future learned `style` vector is added as a new
versioned collection after it has evaluation evidence.

Patch artifacts do not belong in PostgreSQL and do not belong in the first
Qdrant deployment. Store them as checksummed objects keyed by artwork version
and feature fingerprint. Fetch only the final rerank shortlist. A top-50 query
reads at most about 18.75 MiB of raw float16 patches before compression; a local
disk LRU should absorb repeated results.

Before 500,000 images, benchmark:

- float16 versus int8 patch values;
- direct low-resolution DINO inference versus pooled `32x32` features;
- zstd/NPZ storage size and decode latency;
- exact rank agreement and nDCG against the current full patch baseline;
- Qdrant float16, scalar quantization, and on-disk global-vector settings.

## Cost Envelope

Backblaze B2 currently starts at USD 6.95/TB-month, includes the first 10 GB,
and includes egress up to three times average stored data. Cloudflare R2
Standard is USD 0.015/GB-month with free egress. With five alpha users and a
read-through local cache, B2 is the cheaper initial choice.

Using a 250 KiB canonical image plus a raw 384 KiB compact patch artifact gives
this conservative base estimate before compression:

| Corpus | Base artifacts | Approximate B2 storage/month |
| ---: | ---: | ---: |
| 5,000 | 3.25 GB | Free tier |
| 100,000 | 64.9 GB | USD 0.38 |
| 500,000 | 324.6 GB | USD 2.19 |
| 1,000,000 | 649.2 GB | USD 4.44 |

Allow USD 6-10/month at one million images for manifests, reports, database
backups, snapshots, temporary feature versions, and growth headroom. Keep only
the active feature version plus one rollback version during migrations, then
purge stale artifacts.

The Qdrant Cloud free tier is a single node with 1 GB RAM and 4 GB disk. Its
documentation says that can serve about one million 768-dimensional vectors,
but ArtSearch uses multiple named vectors, payload indexes, and HNSW overhead.
It is suitable for the 5,000-image prototype and perhaps the 100,000-image
shadow index, not a promise for the million-image target.

If remote alpha serving moves off the workstation, current low-cost European
VPS prices put a single shared node around USD 18-35/month before tax and IPv4.
Together with B2, the intended steady bill is approximately USD 25-45/month.
Cloud GPUs, managed PostgreSQL, paid Qdrant Cloud, and high availability are
outside this budget and require an explicit spending decision.

## Quality and Release Gates

Artwork qualification and adult-content safety are separate classifiers and
must have separate reports. SigLIP artwork confidence alone cannot make a
public-safe claim.

Start with these internal promotion gates:

| Gate | Initial requirement |
| --- | --- |
| Automatic ACCEPT precision | At least 95% on a held-out labeled set |
| Evaluation support | At least 500 representative labels spanning artists and hard negatives |
| Borderline decisions | REVIEW, never silently ACCEPT |
| Accepted audit | Randomly inspect 2-5% per source/policy version |
| Reject audit | Sample enough rejects to estimate lost-art recall |
| Adult-content safety | Separate source-label and visual-safety evaluation before alpha exposure |
| Index promotion | Exact-vs-HNSW recall and latency report plus rollback alias |
| Data integrity | Restore PostgreSQL and rebuild Qdrant from manifests in a drill |

The dashboard judgments are valuable evidence, but sampling must be randomized
and grouped by artist so the evaluation does not only measure memorable or easy
examples.

## Milestone Gates

### 5,000 images

- PostgreSQL identities, stage attempts, jobs, and deletion state exist.
- B2 adapter, quarantine purge, and checksum verification work end to end.
- Qdrant OSS shadows exact search with two named global vectors.
- Rejected bytes are proven absent after completed jobs.

### 100,000 images

- Measured intake throughput and per-stage cost are stable.
- Compact patch artifacts and local-cache reranking pass quality tests.
- HNSW parameters are selected against exact ground truth.
- Backup restore, Qdrant rebuild, and source-delete drills pass.

### 500,000 images

- Quantization and on-disk settings have measured recall/resource reports.
- Remote alpha hosting is moved to one VPS only if workstation serving is a
  practical problem.
- Object, vector, database, and cache growth remain inside warning budgets.

### 1,000,000 images

- The active index is versioned, reproducible, and rebuildable.
- The system sustains 5,000 accepted images/day during backfills.
- Alpha query traces contain model/index versions, stage scores, latency,
  impressions, rank, and consented interaction events.
- The USD 50/month hard warning threshold has not been crossed silently.

## Sources

- [Backblaze B2 pricing](https://www.backblaze.com/cloud-storage/pricing)
- [Backblaze B2 transaction pricing](https://www.backblaze.com/cloud-storage/transaction-pricing)
- [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/)
- [Qdrant Cloud pricing](https://qdrant.tech/pricing/)
- [Qdrant free-cluster capacity](https://qdrant.tech/documentation/cloud/create-cluster/)
- [Qdrant capacity planning](https://qdrant.tech/documentation/operations/capacity-planning/)
- [Hetzner 2026 price adjustment](https://docs.hetzner.com/general/infrastructure-and-availability/price-adjustment/)
