# ArtSearch Production Capacity and Cost Profile

Status: recorded operating decisions for the production intake build

Recorded: 2026-08-12

This profile turns the production roadmap into a capacity and spending envelope.
It superseded generic managed-cloud recommendations for the first alpha until
the cloud-serving decision recorded on 2026-08-19. The current deployment
topology is now governed by `docs/adr/0001-alpha-cloud-topology.md`; the capacity
arithmetic and quality gates in this document remain active. The
corpus milestones below mean accepted, searchable artworks rather than source
candidates.

> **Representation update (2026-08-20):** ADR 0002 removed DINO global and
> patch features from the active system. CSD is now the single 768-dimensional
> style representation. Historical patch-storage calculations below have been
> replaced with the current CLIP/CSD footprint.

> **Ten-million update (2026-09-08):** the hard ceiling is now USD 50/month and
> the long-range target is 10,000,000 searchable artworks. ADR 0003 and
> `docs/ten_million_scale_runbook.md` supersede this document for vector serving,
> image retention, and 10M cost arithmetic. The milestone and intake analysis
> below remains useful historical context for the path through the first 1M.

## Recorded Targets

| Input | Decision |
| --- | --- |
| Initial corpus | 5,000 accepted artworks |
| Three-month corpus | 100,000 accepted artworks |
| Eight-month corpus | 500,000 accepted artworks |
| Twelve-month corpus | 1,000,000 accepted artworks |
| First-year ordinary intake | Target 5,000 source candidates/day plus backfills |
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
year, and 2,000 *candidates* per day reaches less after rejects. Scheduled local
operation now targets 5,000 candidates/day. The system remains designed for
10,000-candidate backfills and 5,000 accepted images/day; measured acceptance
coverage determines whether the million-image milestone needs additional
backfill days.

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
    -> local CLIP/CSD batch workers
    -> named global vectors in Qdrant OSS
    -> reciprocal-rank fusion of subject and style recall
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
| Style features | One normalized 768D CSD vector in Qdrant | Replace only after held-out style judgments show a gain |
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

The active Qdrant point has a 512-dimensional CLIP vector and a 768-dimensional
CSD vector. At float16, their raw values total 2,560 bytes per artwork, or about
2.38 GiB for one million artworks before HNSW and payload overhead. The
canonical SQLite float32 copy totals 5,120 bytes per artwork, or about 4.77 GiB
for one million rows before database overhead.

This is a major reduction from the retired patch-matrix design. Before 500,000
images, benchmark Qdrant float16, scalar quantization, and on-disk vector/index
settings against exact CLIP and CSD search. Keep model artifacts and manifests
in object storage; do not create a per-artwork patch artifact tier.

## Cost Envelope

Backblaze B2 currently starts at USD 6.95/TB-month, includes the first 10 GB,
and includes egress up to three times average stored data. Cloudflare R2
Standard is USD 0.015/GB-month with free egress. With five alpha users and a
read-through local cache, B2 is the cheaper initial choice.

Using a 250 KiB canonical image gives this conservative object-storage estimate
before manifests, reports, and backups:

| Corpus | Base artifacts | Approximate B2 storage/month |
| ---: | ---: | ---: |
| 5,000 | 1.28 GB | Free tier |
| 100,000 | 25.6 GB | About USD 0.18 |
| 500,000 | 128 GB | About USD 0.89 |
| 1,000,000 | 256 GB | About USD 1.78 |

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
- CLIP/CSD fusion beats both single-vector baselines on held-out judgments.
- HNSW parameters are selected against exact ground truth.
- Backup restore, Qdrant rebuild, and source-delete drills pass.

### 500,000 images

- Quantization and on-disk settings have measured recall/resource reports.
- Remote alpha hosting follows ADR 0001 and does not depend on the workstation.
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
