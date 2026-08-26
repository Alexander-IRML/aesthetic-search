# PostgreSQL Control Plane

Status: implemented foundation; live Neon canary pending credentials

## Ownership Boundary

ArtSearch now has an explicit production data boundary:

| System | Owns |
| --- | --- |
| SQLite | Worker-local intake staging, resumable Bluesky crawl state, local dashboards, and offline repair |
| Neon PostgreSQL | Canonical catalog metadata, filter/workflow evidence, model lineage, publication state, query traces, impressions, and feedback |
| Qdrant | Rebuildable CLIP-subject and CSD-style ANN serving vectors plus compact filter payloads |
| R2/S3 | Accepted originals, derivatives, manifests, reports, and temporary query objects |

The cloud path is staged publication, not dual-write business logic. An intake
worker finishes its local SQLite transaction and embedding job, then publishes a
candidate-scoped, consistent SQLite snapshot to PostgreSQL in one transaction.
PostgreSQL commits the canonical rows and Qdrant outbox events together. Qdrant
is updated afterward with idempotent retries.

## PostgreSQL Layout

- `catalog`: artists, artworks, object references, searchable text, source and
  review metadata.
- `ml`: pinned CLIP/CSD archival `halfvec` values, model bundles, and Qdrant
  point publication state. PostgreSQL has no HNSW or IVFFlat index in this
  release; Qdrant remains the ANN engine.
- `ingest`: runs, events, immutable filter evidence, routes, Bluesky cursor
  state, SQLite import verification, and the transactional outbox.
- `product`: users, sessions, queries, ranking stages, result impressions,
  interaction events, saves, and followed artists.

The initial Alembic revision enables `vector`, `pg_trgm`, and
`pg_stat_statements`. Product tables have RLS policies keyed by transaction-local
`app.user_id` and `app.session_id`. The table owner/service worker can perform
trusted maintenance; a future public API role should receive only the narrow
schema/table privileges it requires.

## Connections

Install the optional stack:

```bash
python -m pip install -e '.[postgres]'
```

Two Neon URLs are intentionally separate:

```text
DATABASE_URL         pooled endpoint for API and ordinary workers
DATABASE_DIRECT_URL  direct endpoint for Alembic and bulk snapshot publication
```

Both values belong in `deploy/alpha/.env` or a provider secret store, never in
TOML or Git. Migrations must use the direct endpoint. Ordinary runtime queries
use the pooled endpoint with bounded SQLAlchemy pools and `pool_pre_ping`. The
direct endpoint receives `statement_timeout` as a startup option; pooled
transactions apply it with `SET LOCAL` because Neon PgBouncer rejects arbitrary
startup options. The
cloud config requires an explicit `sslmode=require`, `verify-ca`, or
`verify-full` query parameter on both URLs.
The setup helper accepts the single connection string copied from Neon's
Connect dialog. It derives the pooled and direct variants using Neon's
documented `-pooler` hostname convention and stores both without placing either
one in shell history:

```bash
artsearch-deploy configure-postgres
```

## First Cutover

Create an ignored alpha config, load its private environment, then run:

```bash
set -a
source deploy/alpha/.env
set +a

artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml upgrade

artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml status --direct

artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml publish-sqlite \
  --sqlite data/artsearch.db

artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml drain-qdrant \
  --app-config config/config.yaml
```

The full cutover refuses to merge into a non-empty, untracked catalog. It also
refuses a different second full snapshot unless the operator explicitly passes
`--allow-new-full-snapshot`. Every import records the consistent SQLite backup
hash, scope hash, Alembic revision, per-table counts, and verification result.
CLIP and CSD vectors must match the production dimensions of 512 and 768.

## Scheduled Intake

When `[postgres].enabled=true`, `artsearch_bluesky_intake` now performs:

```text
collect/filter locally
  -> publish accepted bytes to R2
  -> compute CLIP/CSD in local SQLite staging
  -> publish only this run's candidate IDs to PostgreSQL
  -> commit Qdrant reconciliation events in the same transaction
  -> drain outbox into Qdrant
  -> publish immutable run artifacts
```

When PostgreSQL is disabled, local development retains the existing direct
SQLite-to-local-Qdrant flow. This is a deliberate development fallback, not the
cloud authority model.

## Outbox Semantics

Workers claim events with `FOR UPDATE SKIP LOCKED`. Qdrant point IDs and event
idempotency keys are deterministic. Remote writes happen before PostgreSQL
settlement, so a crash after the Qdrant write produces a harmless repeat. Failed
events use bounded retry state; stale processing leases can be released. The
physical Qdrant collection is versioned and promoted through the stable read
alias only after successful work.

## Product Telemetry

The schema can record the alpha evidence selected in the architecture plan:

- query and session IDs, modality, parent reformulation, positive/negative
  examples, filters, and explicit retrieval weights;
- candidate count, top-100 ranked candidate evidence, and per-stage model/index
  version and latency;
- displayed artwork, artist, rank, fusion/CLIP/style scores;
- click, like, dislike, save, follow, reformulation, and graded relevance events;
- exact timestamps and JSONB extension fields for bounded future evidence.

Detailed telemetry defaults to 180 days. Run
`artsearch-postgres prune-telemetry` from a scheduled maintenance task. At the
current scale, ordinary B-tree indexes are sufficient; add monthly partitions
only after measured event volume makes retention deletes or index maintenance a
problem.

## Verification

Offline validation currently covers SQL compilation for all tables, Alembic SQL
generation, configuration secrecy, UUIDv7 IDs, migration scopes, vector shape
and finiteness checks, and outbox/schema contracts. A live integration test still
requires a disposable Neon branch or another explicit PostgreSQL test URL. Do
not run a first migration against the production branch without first exercising
upgrade, cutover, counts, Qdrant drain, and restore on that disposable branch.
