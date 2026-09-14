# Cloudflare Search Deployment

This package deploys the public ArtSearch resume demo. A Cloudflare Worker
serves the static search workbench from edge assets, applies request limits,
streams approved corpus objects from a read-only R2 binding at `/media/`, and
wakes one of three scale-to-zero Container shards for model-backed API requests.
A post-retrieval assistant runs separately at the edge through the native
Workers AI binding.

The Container image bakes the exact pinned CLIP and CSD artifacts during its
Docker build. Runtime starts are offline with respect to Hugging Face and use
the same model revisions and vector dimensions as the Qdrant collection.

## Runtime Shape

- Public, anonymous frontend served from Cloudflare static assets.
- Three named Container shards, capped at three active instances total.
- One model inference owner and 24 bounded waiters inside each Python service.
- 2 vCPU, 6 GiB memory, and 8 GB ephemeral disk.
- Western North America placement, aligned with the alpha Qdrant cluster.
- Fifteen-minute idle timeout, then scale to zero.
- Extended three-minute model startup allowance.
- Read-only R2 binding for approved corpus media.
- Outbound TLS to managed PostgreSQL and Qdrant Cloud.
- Twelve searches per client IP and 120 searches per edge location per minute.
- Sixty feedback events per client IP per minute.
- Twelve assistant calls per client IP and 24 per edge location per minute.
- Cloudflare Workers AI `@cf/meta/llama-3.2-1b-instruct` for metadata-grounded
  summaries after results render.

The edge-served frontend stays responsive while model Containers sleep. A
first search after an idle period can still take about a minute while CLIP and
CSD load. A best-effort page-load visit beacon warms the visitor's deterministic
shard and records only a keyed hash of the canonical IP in PostgreSQL.
Assistant requests never wake that Container and use no additional API secret.
The workbench checks Container state once on load, shows a red/green warm-state
light, and displays browser, edge, Container-start, API, CLIP, detailed CSD,
Qdrant, catalog, and telemetry timing after each search. It does not poll or
send keep-warm traffic.

## Prerequisites

1. Docker can build `linux/amd64` images.
2. Node.js 20 or newer is installed.
3. The Cloudflare account is on Workers Paid, which Containers require.
4. The R2 bucket `artsearch-artifacts-alpha` contains approved objects beneath
   `artsearch/corpus/`.
5. The Qdrant `artworks_active` alias and PostgreSQL catalog contain the same
   eligible artwork set.

## Configure

From this directory:

```bash
npm ci
npx wrangler login
artsearch-deploy generate-visitor-secret
node --env-file=../alpha/.env scripts/put-secrets.mjs
npm run check
```

The helper sends `DATABASE_URL`, `QDRANT_URL`, `QDRANT_API_KEY`, and
`ARTSEARCH_VISITOR_HASH_SECRET` to Wrangler over standard input and prints only
their names. The generator creates a stable random value of at least 32 bytes;
do not rotate it casually because old and new hashes cannot be deduplicated.
Those values
live in the ignored `deploy/alpha/.env` file. The Worker does not use a browser
passcode or session-signing secret.

For local Worker development, copy `.dev.vars.example` to `.dev.vars` and fill
it with disposable development credentials. `.dev.vars` is ignored by Git.

The `AI` binding is declared in `wrangler.jsonc`; Cloudflare supplies it at
runtime. The assistant receives at most ten result records with capped post and
alt text. It does not receive the uploaded image. Artist follow-ups are routed
by deterministic code to the existing image-search API, not by granting the
language model arbitrary tools.

## Deploy And Verify

Release tags and manual GitHub dispatches use the protected pipeline documented
in [`docs/ci_cd.md`](../../docs/ci_cd.md). CI type-checks and bundles this Worker
without touching Cloudflare; the deployment job applies database migrations,
builds the Container, deploys with the lockfile-pinned Wrangler version, and
checks the edge routes. Provider values live only in the GitHub `alpha`
environment or existing Worker secrets.

For an operator-driven deployment from this directory:

```bash
npm run deploy
npx wrangler containers list
npx wrangler containers images list
```

Verify the edge before waking the model:

```bash
curl -fsS https://artsearch-alpha-search.<workers-subdomain>.workers.dev/health/edge
curl -fsS https://artsearch-alpha-search.<workers-subdomain>.workers.dev/api/runtime-status
curl -fsS -o /dev/null -w '%{http_code} %{time_total}\n' \
  https://artsearch-alpha-search.<workers-subdomain>.workers.dev/search
```

`/search` should return immediately without credentials. Upload one ordinary
test image next, then confirm `/health/ready` while the Container is warm. A
cold query is expected to be slower than later requests.

For an edge/backend split, inspect a search response with `curl -D -` or browser
developer tools. `X-ArtSearch-Container-Wait-Ms`,
`X-ArtSearch-Container-Fetch-Ms`, `X-ArtSearch-Edge-Total-Ms`, and
`X-ArtSearch-Container-Shard` distinguish startup, routing, and backend
inference. The JSON response
then separates CLIP, CSD preprocessing/backbone/projection/output,
normalization, Qdrant, catalog, and telemetry time inside the Python service.
Telemetry initialization overlaps model inference; `telemetry_wait_ms` is the
small residual that remained on the response's critical path.

## Public Demo Boundaries

The generated `workers.dev` hostname is a public URL suitable for a resume. It
is not private or unfindable. Cost and abuse controls come from the
three-Container ceiling, one-at-a-time inference per shard, edge rate limits,
upload validation, and the idle shutdown. Provider budget alerts should still
remain enabled.

## Stream-Day Check

The profile can admit 75 image searches across all shards: three active model
owners plus 72 bounded waiters. It does not run 75 inferences concurrently. At
the observed warm CSD speed, a roughly even 20-to-50-person one-query burst
should drain over about one or two minutes; a single NAT or a slower model host
can concentrate or extend that queue. Treat this as an engineering estimate
until a real hosted load test confirms p95 and error rate.

Before sharing the link, deploy the profile, open the page from a few distinct
networks to warm all likely shards, confirm `/api/runtime-status` is green, and
watch Container, Neon, and Qdrant usage. After the event, set `max_instances`
back to `1` if the larger burst envelope is no longer needed.

The Worker exposes only the frontend, approved R2 media, health endpoints, API
documentation, image search, result feedback, and the bounded retrieval
assistant. Unknown routes return at the edge without waking the Container. Do
not put `DATABASE_DIRECT_URL`, R2 S3 write credentials, Bluesky credentials,
local corpus files, or model tokens in Worker secrets.
