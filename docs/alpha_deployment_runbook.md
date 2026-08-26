# Closed-Alpha Deployment Runbook

Status: closed-alpha query plane deployed; scheduled corpus refresh remains an
operator-controlled workflow

This runbook starts where the local pipeline ends. PostgreSQL schema, cutover,
telemetry, Qdrant outbox support, private R2 delivery, and a scale-to-zero
Cloudflare Container query deployment now exist. Its purpose is to reproduce or
repair that environment without placing credentials in the repository.

## Accounts to Create

Create these accounts under one project email and enable multi-factor
authentication before creating resources.

| Required for first cloud canary | Account and resource |
| --- | --- |
| Yes | Cloudflare Workers Paid account and one private R2 bucket |
| Yes | Qdrant Cloud account and one free cluster |
| Yes | Neon account and one PostgreSQL 17 project |
| Optional | Hetzner or another VM for unattended scheduled intake |
| Optional | Modal or another GPU service after CPU latency warrants it |
| Recommended | Error/uptime service for edge health and alert routing |

Do not buy a paid Qdrant, Neon, or GPU plan yet. The free/usage tiers are enough
to run the canaries and collect sizing evidence. Set spend alerts immediately.

## Resource Names

Use stable names so runtime configuration, documentation, and deletion tooling
agree:

```text
Cloudflare Worker: artsearch-alpha-search
Cloudflare Container: artsearch-alpha-search-api
R2 accepted/artifact bucket: artsearch-artifacts-alpha
Qdrant cluster: artsearch-alpha
Qdrant physical collection: artworks_clip3_csd1_v2
Qdrant read alias: artworks_active
Neon project/database: artsearch-alpha / artsearch
Optional intake VM: artsearch-alpha-01
Optional GPU app: artsearch-inference-alpha
```

## Credentials and Runtime Variables

Create separate least-privilege credentials for each workload. Never reuse the
Cloudflare account token as an application object-store key.

| Variable | Source | Consumer |
| --- | --- | --- |
| `AWS_ACCESS_KEY_ID` | R2 S3 API token | Airflow and corpus publisher |
| `AWS_SECRET_ACCESS_KEY` | R2 S3 API token | Airflow and corpus publisher |
| `AWS_DEFAULT_REGION` | `auto` for R2 S3 clients | Airflow and corpus publisher |
| `ARTSEARCH_R2_ACCOUNT_ID` | Cloudflare account overview | Config rendering/operator tooling |
| `ARTSEARCH_R2_BUCKET` | Accepted-object/artifact bucket choice | Corpus publisher |
| `QDRANT_URL` | Qdrant cluster endpoint | Sync, evaluation, and query API |
| `QDRANT_API_KEY` | Qdrant database API key | Sync, evaluation, and query API |
| `DATABASE_URL` | Neon pooled connection string | API, telemetry, and outbox workers |
| `DATABASE_DIRECT_URL` | Neon direct connection string | Alembic and verified SQLite cutover |
| `ALPHA_ACCESS_CODE` | Generated high-entropy invite code | Cloudflare Worker access gate |
| `ALPHA_SESSION_KEY` | Separate generated HMAC key | Cloudflare Worker session signing |
| `MODAL_TOKEN_ID` | Modal token | Deployment/worker launcher |
| `MODAL_TOKEN_SECRET` | Modal token | Deployment/worker launcher |
| `ARTSEARCH_SESSION_SECRET` | Generated 32-byte secret | Future public API |

The application reads R2 through the standard AWS credential variables, Qdrant
through `QDRANT_URL` and `QDRANT_API_KEY`, and PostgreSQL through separate pooled
and direct URLs. Modal variables remain reserved for the inference deployment.

## Provider Setup Order

1. Enable Workers Paid and create the private `artsearch-artifacts-alpha` R2
   bucket.
2. Create one R2 S3 token restricted to that bucket for intake publication. The
   public query Container does not receive these credentials.
3. Create the Qdrant cluster, database API key, and copy its HTTPS endpoint.
4. Copy `configs/production.alpha-cloud.example.toml` to the ignored
   `configs/production.alpha-cloud.local.toml`, then replace the account ID and
   bucket placeholders.
5. Export credentials in the shell and run `artsearch-deploy preflight`.
6. Publish a tiny accepted-object canary, initialize Qdrant, sync a small corpus,
   and run ANN evaluation before provisioning the VM.
7. Create the Neon PostgreSQL 17 project and record both connection strings in
   `deploy/alpha/.env`. Run Alembic and the verified cutover on a disposable
   branch before repeating them on the production branch.
8. Publish the approved PostgreSQL corpus to Qdrant, verify count parity and ANN
   recall, and move the stable alias only after evaluation succeeds.
9. Follow `deploy/cloudflare-search/README.md` to generate alpha access values,
   upload Worker secrets,
   build the pinned model image, and deploy the alpha search link.
10. Provision a private intake VM or GPU worker only after measured throughput,
    uptime, or latency justifies the additional provider.

## First Canary Commands

```bash
.venv/bin/artsearch-deploy configure-qdrant

cp configs/production.alpha-cloud.example.toml \
  configs/production.alpha-cloud.local.toml

export ARTSEARCH_R2_ACCOUNT_ID='replace-me'
export ARTSEARCH_R2_BUCKET='artsearch-artifacts-alpha'
export AWS_ACCESS_KEY_ID='replace-me'
export AWS_SECRET_ACCESS_KEY='replace-me'
export AWS_DEFAULT_REGION='auto'
export QDRANT_URL='https://replace-me.cloud.qdrant.io:6333'
export QDRANT_API_KEY='replace-me'
export DATABASE_URL='postgresql://pooled-runtime-url'
export DATABASE_DIRECT_URL='postgresql://direct-migration-url'

.venv/bin/artsearch-deploy preflight \
  --production-config configs/production.alpha-cloud.local.toml

.venv/bin/artsearch-data publish-corpus \
  --config configs/production.alpha-cloud.local.toml \
  --decisions data/filter/decisions.jsonl

.venv/bin/artsearch-qdrant init \
  --production-config configs/production.alpha-cloud.local.toml
.venv/bin/artsearch-qdrant sync \
  --production-config configs/production.alpha-cloud.local.toml
.venv/bin/artsearch-qdrant evaluate-ann \
  --production-config configs/production.alpha-cloud.local.toml \
  --sample-size 50 --top-k 20

.venv/bin/artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml upgrade
.venv/bin/artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml publish-sqlite \
  --sqlite data/artsearch.db
.venv/bin/artsearch-postgres \
  --config configs/production.alpha-cloud.local.toml drain-qdrant
```

The canary should use a tiny explicitly reviewed corpus. Never point the first
cloud test at the entire local store.

The PostgreSQL procedure, refusal guards, RLS model, and retention behavior are
documented in [postgresql_control_plane.md](postgresql_control_plane.md).

## Launch Gates

- The VM can be rebooted and reconstruct all services from checked-in images and
  ignored/provider-managed secrets.
- No public port exposes Airflow, the review dashboard, PostgreSQL, or Qdrant.
- R2 rejects anonymous reads from private buckets and automatically expires
  temporary queries.
- PostgreSQL restore and Qdrant rebuild/alias rollback have been exercised.
- Five-QPS burst tests report p50/p95/p99 latency and zero silent ranking-recipe
  fallbacks.
- Every result has attribution/source links and every eligible artwork can be
  removed across metadata, vectors, and derivatives.
- Invite auth, request/upload limits, consent, telemetry retention, and abuse
  reporting are visible and tested.
