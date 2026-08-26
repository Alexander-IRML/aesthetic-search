# ADR 0001: Closed-Alpha Cloud Topology

- Status: accepted
- Date: 2026-08-19
- Decision owners: ArtSearch
- Supersedes: the workstation-hosted alpha recommendation in the 2026-08-12
  capacity and intake research documents

## Context

ArtSearch has a functioning local supply and retrieval pipeline: Bluesky intake,
SigLIP artwork qualification, accepted-object publication, CLIP and CSD feature
generation, Qdrant named-vector fusion, evaluation, and human
review. The next product milestone is an invite-only alpha for approximately five
users with a burst target of five requests per second.

The public service must remain available when the development laptop is powered
off and must not consume workstation CPU, GPU, memory, or network capacity. The
initial infrastructure budget remains approximately USD 20-50 per month. High
availability is desirable later but is not an alpha requirement.

## Decision

Use a small cloud control-plane VM plus managed, scale-to-zero data services and
ephemeral GPU workers.

```text
Browser
  -> Cloudflare Pages
  -> public ArtSearch API on one small VM
       -> Neon PostgreSQL
       -> Qdrant Cloud
       -> Cloudflare R2
       -> Modal query encoder

Airflow on the VM
  -> scheduled Modal batch workers
       -> R2 accepted objects and artifacts
       -> PostgreSQL metadata and traces
       -> Qdrant versioned named-vector collections

Developer laptop
  -> source control, deployment, review, and administration only
```

The initial VM target is a shared 4-vCPU/8-GB instance. The API, Airflow
scheduler/webserver, and reverse proxy run in separate containers with resource
limits. Airflow is an offline control plane and never handles a user request.
Its UI binds privately and is reached through an authenticated tunnel.

### Service Ownership

| Service | Owns | Explicitly does not own |
| --- | --- | --- |
| Cloudflare Pages | Versioned static public client | Corpus bytes, secrets, or ranking |
| Public API | Authentication, query orchestration, result assembly, feedback validation | Model training or scheduled ingestion |
| Neon PostgreSQL | Users, sessions, query/impression/interaction events, canonical workflow metadata | Image bytes or vector search |
| Qdrant Cloud | Rebuildable named-vector serving index and filter payload | Canonical metadata or audit truth |
| Cloudflare R2 | Accepted originals, display derivatives, bounded query uploads, backups | Workflow state or authorization |
| Modal | Ephemeral SigLIP/CLIP/CSD batch and query inference | Persistent metadata or public routing |
| Airflow | Schedules, dependencies, retries, and run evidence | Per-item idempotency or interactive requests |

### Query Path

1. The browser obtains a short-lived signed upload URL when a query contains an
   image. Uploads go to a private temporary R2 prefix rather than through the API.
2. The API creates a request, query, and pseudonymous session record.
3. Text-only CLIP encoding may run on CPU after benchmarking. Image CLIP/CSD
   encoding runs in an ephemeral GPU worker.
4. Qdrant performs parallel named-vector recall with metadata filters and returns
   independent component evidence.
5. The API unions candidates, applies reciprocal-rank fusion, then applies
   diversity and policy rules.
6. PostgreSQL receives the query event, displayed impressions, latency breakdown,
   exact model/index versions, and later interaction events.
7. The client receives derivative URLs, attribution, ranks, and public-safe result
   metadata. Internal scores are returned only to authorized evaluators.

Five QPS is treated as a burst target. A sustained five QPS would be 432,000
queries per day and requires a separate capacity and cost review.

### Object Boundaries

Use one R2 account with separate buckets or equivalently isolated prefixes:

| Boundary | Access | Retention |
| --- | --- | --- |
| `artsearch-originals` | Private service credentials | Accepted corpus policy |
| `artsearch-derivatives` | CDN-readable display assets | While artwork is eligible |
| `artsearch-query-temp` | Signed upload/read only | Automatic deletion after 24 hours |
| `artsearch-artifacts` | Private workers and operators | Versioned manifests, model artifacts, reports, backups |

Rejected intake bytes are never uploaded. Public derivatives are sanitized,
metadata-stripped, bounded-resolution outputs rather than source originals.

### Security Boundary

- The public API is the only public dynamic origin.
- Airflow, the model-operations dashboard, SSH, PostgreSQL, and Qdrant credentials
  are never exposed through the public client.
- Invite authentication and per-user/IP limits are required before alpha access.
- Query uploads use strict byte, pixel, media-type, and decode limits. SVG is not
  accepted.
- Runtime secrets come from provider secret stores or an ignored deployment
  environment file; no credential enters TOML, source control, an image, or logs.
- Original objects and user query objects remain private. Public access is limited
  to generated derivatives selected for the alpha corpus.
- Artist deletion, opt-out, source deletion, or policy revocation must remove the
  derivative, Qdrant point, and public metadata while preserving bounded audit
  evidence according to policy.

### Reliability and Observability

The alpha is deliberately single-region and best-effort. It still requires:

- health, readiness, and dependency checks;
- request IDs and query IDs across API, inference, Qdrant, and PostgreSQL;
- p50/p95/p99 total and stage latency, error rate, QPS, queue depth, GPU cold-start,
  object-store traffic, and provider cost;
- daily PostgreSQL backups and tested restores;
- versioned Qdrant collections, snapshots, exact-count reconciliation, and alias
  promotion only after validation;
- hard monthly provider spend limits or alerts wherever offered;
- deploy rollback to the prior application and Qdrant alias version.

## Cost Guardrails

The expected alpha shape is one low-cost VM, free or entry managed tiers for
Pages/PostgreSQL/Qdrant, low-volume R2 storage, and scale-to-zero GPU usage. GPU
work and Qdrant memory are the first components expected to exceed free tiers.

No paid service is upgraded based only on a corpus-size estimate. Upgrade gates
are measured p95 latency, error rate, vector memory/disk pressure, queue age,
throughput, and monthly cost.

## Alternatives Considered

### Continue serving from the laptop

Rejected. It couples availability and latency to a personal workstation, exposes
the home network to unnecessary risk, and makes user traffic compete with local
workloads.

### Put every service on one VM

Rejected as the default. It is inexpensive but makes image delivery, vector
memory, metadata durability, interactive latency, and batch jobs contend for one
failure domain. It remains an emergency fallback, not the intended alpha shape.

### Kubernetes and always-on GPU instances

Rejected for the alpha. Five invited users and bursty daily intake do not justify
the operational or idle-compute cost.

### Full managed high availability

Deferred. Multi-node Qdrant, replicated API instances, and multi-zone databases
become appropriate before a public availability promise, not before evidence from
the closed alpha.

## Consequences

- The laptop can be completely offline while users search.
- The public product needs a real API and PostgreSQL event schema; the existing
  local static gallery is not deployed as the product.
- SQLite remains the deterministic local development and migration source until
  the PostgreSQL adapter lands.
- Existing S3-compatible publication and Qdrant adapters remain useful; cloud
  adoption primarily changes endpoints, credentials, and operating boundaries.
- Public search cannot launch until authorization, content policy, attribution,
  opt-out/deletion propagation, and query-retention behavior are tested.
