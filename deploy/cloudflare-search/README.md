# Cloudflare Search Deployment

This package deploys the public ArtSearch frontend and image-search API as one
scale-to-zero Cloudflare Container behind a small Worker. The Worker also
streams accepted corpus objects from a read-only R2 binding at `/media/`.

The container image bakes the exact pinned CLIP and CSD artifacts during its
Docker build. Runtime starts are offline with respect to Hugging Face and use
the same model revisions and vector dimensions as the Qdrant collection.

## Runtime Shape

- One named container instance, capped at one active instance.
- 1 vCPU, 4 GiB memory, and 8 GB ephemeral disk.
- Western North America placement, aligned with the alpha Qdrant cluster.
- Fifteen-minute idle timeout, then scale to zero.
- Extended three-minute model startup allowance.
- Read-only R2 binding for accepted corpus media.
- Outbound TLS to managed PostgreSQL and Qdrant Cloud.
- Worker observability enabled at full sampling for the closed alpha.

One instance is intentional for the first five testers. The Python service owns
one inference queue, so adding random replicas before measuring demand would
increase cold starts and model memory without improving ranking quality.

## Prerequisites

1. Docker is running and can build `linux/amd64` images.
2. Node.js 20 or newer is installed.
3. The Cloudflare account is on Workers Paid, which is required for Containers.
4. The R2 bucket `artsearch-artifacts-alpha` exists and contains published
   objects beneath `artsearch/corpus/`.
5. The Qdrant `artworks_active` alias and PostgreSQL catalog contain the same
   eligible artwork set.

Create the managed PostgreSQL project in a western US region as well. Moving a
few browser milliseconds to the edge is less important than avoiding a
cross-country hop between the query container, catalog, and vector database.

## Configure

From this directory:

```bash
npm ci
npx wrangler login
npm run access:init
node --env-file=../alpha/.env scripts/put-secrets.mjs
npm run check
```

The secret helper sends values over Wrangler standard input and prints only
their names. It requires `ALPHA_ACCESS_CODE`, `ALPHA_SESSION_KEY`,
`DATABASE_URL`, `QDRANT_URL`, and `QDRANT_API_KEY` in the ignored
`deploy/alpha/.env` file. `npm run access:init` creates the two alpha values
without printing them and leaves existing values unchanged.

For local Worker development, copy `.dev.vars.example` to `.dev.vars` and fill
it with disposable development credentials. `.dev.vars` is ignored by Git.

## Deploy And Verify

```bash
npm run deploy
npx wrangler containers list
npx wrangler containers images list
```

The first deployment builds and uploads the large CPU model image and can take
several minutes. After Cloudflare finishes provisioning, verify:

```bash
curl -fsS https://artsearch-alpha-search.<workers-subdomain>.workers.dev/health/edge
```

Then open `/search`, enter the access code stored locally in
`deploy/alpha/.env`, check `/health/ready` in that authenticated browser
session, and upload one ordinary test image. A cold request may take noticeably
longer; subsequent requests should reuse the warm model until the fifteen-minute
idle timeout.

## Protect The Closed Alpha

Use the generated `workers.dev` hostname for the first release; a custom domain
is optional. The Worker-level alpha gate protects the frontend, API, readiness
probe, and R2 media route before paid container work begins. Only
`/health/edge` remains public. A successful code entry creates a signed,
HttpOnly, Secure, same-site cookie that expires after twelve hours.

Share only the raw `ALPHA_ACCESS_CODE`, never the environment file or session
signing key. Rotate access by changing both alpha values in the ignored file,
rerunning the secret helper, and deploying the Worker.

Cloudflare Access can be added later as defense in depth or to replace the
shared code with per-tester identity:

1. In **Workers & Pages**, open `artsearch-alpha-search`, then open **Access**.
2. Select **Protect this Worker behind Access** and protect **All traffic**.
3. Enable Cloudflare's **One-time PIN** identity provider.
4. Create one Allow policy whose Include rules list the exact tester email
   addresses. Include the operator's email as well.
5. Use a short alpha session duration, then verify one allowed address and one
   address that is not on the list.

Do not create an Allow rule using `Login Methods = One-time PIN`. That selector
accepts every email address that can complete an OTP challenge; it is not an
invite list. Worker-level Access protects the `workers.dev` URL, preview URLs,
and any later custom domain attached to this Worker.

Do not put `DATABASE_DIRECT_URL`, R2 S3 write credentials, Bluesky credentials,
or local corpus files in Worker secrets. The query deployment does not need
them.
