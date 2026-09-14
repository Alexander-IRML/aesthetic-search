# Public Image Search API

Status: deployed public resume-demo vertical slice

Serving concurrency is governed by
[ADR 0004](adr/0004-async-online-serving.md).

## Responsibility

The public browser sends one reference artwork and receives at most ten similar
corpus artworks. It never receives a Qdrant key, PostgreSQL URL, object-store
credential, model token, raw vector, or private filesystem path.

The hosted stream-day profile uses three application-service shards. Each owns
its HTTP API and exact query encoder in one process, then calls hosted Qdrant and
PostgreSQL. The Worker deterministically assigns a source IP to a shard, keeping
warm-state checks and searches on the same model process. The encoder can move
behind a private service boundary later without changing the frontend contract.

## Request Path

1. Cloudflare terminates TLS, serves the frontend from edge assets, and applies
   Worker rate-limit bindings before model compute can start.
2. `POST /api/search-by-image` accepts one multipart `image`, an opaque browser
   `session_key`, four optional coordinate-half weights, and an optional stable
   `artist_id` scope.
3. The API streams with a hard byte cap. Pillow verification, EXIF orientation,
   and RGB conversion run outside the ASGI event loop; JPEG/PNG/WebP MIME,
   animation, pixel-count, and decompression-bomb checks still apply.
4. A bounded model executor produces normalized CLIP subject and CSD style
   vectors with the exact model revisions used by the active corpus collection.
   Each hosted shard has one model owner and a 24-request bounded waiting room.
5. The model slot is released before the API asynchronously asks Qdrant to
   retrieve each named vector and combine the recall
   lists with server-side reciprocal-rank fusion and catalog policy filters. Up
   to 100 fused candidates are retained as reproducible ranking evidence.
6. The API asynchronously enriches result IDs from PostgreSQL in production,
   converting object keys into CDN URLs and attaching bounded Bluesky post and
   alt text when available. Development SQLite calls run in a thread adapter.
7. The API starts the query telemetry record concurrently with inference, then
   joins it only when the stable query/session IDs are needed. Ranking-stage,
   top-100 candidate, top-10 impression, version, and completed timing writes
   continue asynchronously after the response is built. Feedback waits only for
   its own query's impressions; slow telemetry for one user cannot serialize
   unrelated searches.
8. The decoded query and original upload bytes leave scope without being stored.
9. After the results render, the browser can send only the ten compact result
   records to the edge-only retrieval assistant for a grounded summary.
10. If a follow-up names one displayed artist, deterministic agent routing
    resubmits the same browser-held image with that artist's stable ID. Qdrant
    applies an exact metadata filter before CLIP/CSD fusion.
11. At page load, a best-effort visit beacon records an HMAC-SHA256 digest of
    the canonical source IP in session metadata. Raw IP addresses are not
    persisted. The beacon also starts that visitor's assigned model shard,
    giving it time to warm before an image is selected.

Qdrant recommends named-vector prefetch plus top-level RRF for
[multi-representation hybrid queries](https://qdrant.tech/documentation/search/hybrid-queries/).
FastAPI's `UploadFile` uses a spooled upload and requires the streaming
[`python-multipart` parser](https://fastapi.tiangolo.com/tutorial/request-files/).

## Model Compatibility

The first API does not substitute a smaller arbitrary image model. Embeddings
from another model are coordinates in another space and cannot query the current
collection correctly. The current query encoder therefore loads:

- `openai/clip-vit-base-patch32` at the pinned corpus revision for subject recall.
- `yuxi-liu-wired/CSD` at the pinned corpus revision for artistic-style recall.

The service validates the immutable physical collection metadata and confirms
that the stable read alias points at that collection before becoming ready.
Every successful response includes the model-bundle hash and index alias.

A genuinely small query encoder is a later optimization. It requires training a
student or projection into both current vector spaces, then measuring top-k
neighbor overlap and end-to-end nDCG against the exact encoder before promotion.

## Public Contract

```http
POST /api/search-by-image
Content-Type: multipart/form-data

image=<jpeg|png|webp>
session_key=<opaque browser identifier>
artist_id=<optional stable artist scope>
clip_first_half=<0.1..2.0, default 1.0>
clip_second_half=<0.1..2.0, default 1.0>
csd_first_half=<0.1..2.0, default 1.0>
csd_second_half=<0.1..2.0, default 1.0>
```

The result count is server-controlled and fixed at ten for this version:

```json
{
  "api_version": "1.0.0",
  "query_id": "019...",
  "session_id": "019...",
  "query_modality": "image",
  "result_count": 10,
  "retrieval_weights": {
    "clip_first_half": 1.0,
    "clip_second_half": 1.0,
    "csd_first_half": 1.0,
    "csd_second_half": 1.0
  },
  "results": [
    {
      "artwork_id": "...",
      "artist_id": "...",
      "artist_name": "...",
      "rank": 1,
      "image_url": "https://images.example.com/...",
      "source_url": "https://bsky.app/...",
      "content_class": "finished_illustration",
      "source_metadata": {
        "post_text": "A finished commission...",
        "alt_text": "An illustrated character..."
      },
      "match": {
        "fusion_score": 0.83,
        "subject_score": 0.31,
        "subject_rank": 2,
        "style_score": 0.42,
        "style_rank": 1
      }
    }
  ],
  "model": {
    "bundle_version": "sha256...",
    "vector_backend": "qdrant",
    "index_alias": "artworks_active",
    "index_version": "artworks_clip3_csd1_v2",
    "clip_model": "...",
    "clip_revision": "...",
    "csd_model": "...",
    "csd_revision": "..."
  },
  "timing": {
    "upload_decode_ms": 8.1,
    "queue_ms": 0.1,
    "telemetry_start_ms": 3.2,
    "telemetry_wait_ms": 0.1,
    "encode_ms": 840.0,
    "clip_encode_ms": 115.0,
    "csd_encode_ms": 724.4,
    "csd_preprocess_ms": 4.0,
    "csd_tensor_transfer_ms": 0.1,
    "csd_backbone_ms": 719.5,
    "csd_projection_ms": 0.4,
    "csd_output_ms": 0.4,
    "embedding_normalize_ms": 0.6,
    "vector_search_ms": 14.0,
    "catalog_ms": 4.0,
    "total_ms": 869.4
  }
}
```

The experimental controls multiply the first and second halves of the query
embedding, then L2-normalize it before retrieval. CLIP is currently 512
dimensions, split 256/256; CSD is 768 dimensions, split 384/384. Stored corpus
vectors do not change. These coordinate ranges are arbitrary learned latent
axes, not known semantic regions, so a half is not guaranteed to correspond to
a human concept. Equal A/B weights cancel during normalization and preserve the
baseline query direction; only their ratio changes ranking.

`encode_ms` is the aggregate encoder wall time. Its CLIP, CSD, and vector
normalization children make model cost directly attributable; they must not be
added to `encode_ms` a second time. The five `csd_*` stage values are children
of `csd_encode_ms`. `telemetry_start_ms` is total database work that normally
overlaps inference; only `telemetry_wait_ms` was on the critical path. The
individual timing values therefore do not form one additive waterfall.
`total_ms` is measured inside the Python API and does not include a
scale-to-zero Container start or browser transit.

## Warm State And Edge Timing

`GET /api/runtime-status` reports `ready`, `warming`, `cold`, or `unknown`.
Cloudflare answers this by reading Container state without forwarding a health
request to the Python process, so checking the light does not start the model.
The browser checks once at page load rather than polling continuously. A local
FastAPI run exposes the same contract from its in-process service state.
The hosted response also reports the source-IP-assigned shard and total shard
count. It describes that visitor's backend, not every shard in the pool.

Container-routed responses include:

- `X-ArtSearch-Container-State`: state observed before starting the request.
- `X-ArtSearch-Container-Wait-Ms`: time spent obtaining a ready Container.
- `X-ArtSearch-Container-Fetch-Ms`: time to receive backend response headers.
- `X-ArtSearch-Edge-Total-Ms`: complete Worker time for the routed request.
- `X-ArtSearch-Container-Shard`: assigned shard number and configured count.
- `Server-Timing`: the same edge phases for browser developer tools.

The public workbench combines those values with browser wall time and API timing
to show whether a slow request came from cold start, CLIP, the CSD backbone,
Qdrant, catalog, residual telemetry wait, or network/browser overhead. The
indicator turns green after
a successful search and conservatively returns to red before the configured
15-minute idle shutdown; it does not run a keep-warm loop.

Errors use one stable envelope with a safe message and request ID. The API uses
400 for malformed images, 413 for size limits, 415 for unsupported media, 429
for request-rate limits, 503 for busy or unavailable dependencies, and 504 for
the inference deadline.

Each result card can append a relevance grade against an actually displayed
query result:

```http
POST /api/result-feedback
Content-Type: application/json

{
  "query_id": "019...",
  "artwork_id": "...",
  "session_key": "<same opaque browser identifier>",
  "grade": "great"
}
```

Allowed grades are `great`, `good`, and `bad`. The API verifies that the query
completed in the same browser session and that the artwork was one of its
displayed impressions. Events are immutable; when a grade changes, evaluation
uses the newest event for that query and artwork.

## Post-Retrieval Assistant

The assistant is deliberately downstream of retrieval. The top ten display
without waiting for generation, then the browser sends a compact copy of those
result records to the Worker:

```http
POST /api/retrieval-assistant
Content-Type: application/json

{
  "mode": "summary",
  "message": null,
  "query_id": "019...",
  "results": [
    {
      "artwork_id": "...",
      "artist_id": "...",
      "artist_name": "Artist Name",
      "rank": 1,
      "content_class": "finished_illustration",
      "source_metadata": {
        "post_text": "...",
        "alt_text": "..."
      }
    }
  ]
}
```

Cloudflare Workers AI runs `@cf/meta/llama-3.2-1b-instruct` with a short,
low-temperature prompt. Metadata is treated as untrusted data, field and body
sizes are capped, and the model is told explicitly that it has not inspected
the pixels. A model failure returns a deterministic metadata summary and does
not affect retrieval.

For follow-ups, `mode` is `message`. Code outside the model recognizes an
unambiguous displayed artist plus search intent and returns one typed action:

```json
{
  "route": "tool",
  "action": {
    "type": "artist_search",
    "artist_id": "bsky_artist_...",
    "artist_name": "Artist Name"
  }
}
```

The browser executes that action by resubmitting its original in-memory image
to `/api/search-by-image` with `artist_id`. The language model has no Qdrant
credential, no image bytes, no network tool, and no authority to construct an
arbitrary filter. The scoped query is recorded in PostgreSQL telemetry.

## Runtime Limits

Hosted stream-day values live in `configs/search_api.cloudflare.toml` and
`deploy/cloudflare-search/wrangler.jsonc`:

- 10 MB and 40 million decoded pixels.
- JPEG, PNG, and WebP only; single-frame only.
- Three deterministic Container shards, each with one CPU inference owner.
- Twenty-four waiting requests per shard and a 120-second admission deadline.
- 120-second local model deadline.
- Twelve searches per source IP and 120 searches per Cloudflare edge location
  per minute before Container wake.
- Sixty feedback writes per source IP per minute at the edge.
- Ten visit beacons per source IP and 300 per edge location per minute.
- Twelve assistant requests per source IP and 24 per edge location per minute.
- Twelve searches and 120 feedback writes per source IP per minute in the
  Container as defense in depth.
- Ten results, at most two from one artist through the Qdrant policy.

## Async Serving And Backpressure

The ASGI event loop owns request coordination, health checks, Qdrant calls,
PostgreSQL catalog reads, and telemetry. CPU-bound image decode and CLIP/CSD
inference never execute on that loop. The query encoder has a dedicated bounded
executor because overlapping copies of the pinned models would exceed the small
container's useful memory budget.

This creates a deliberate pipeline: an upload can wait for the model owner, use
it once, release it, and then await remote retrieval independently. While that
request is waiting on Qdrant or PostgreSQL, the model owner can encode the next
query and `/health/live`, `/health/ready`, and `/api/runtime-status` remain
responsive. A full admission queue returns `503 encoder_busy` with `Retry-After`
instead of creating an unbounded task backlog. Runtime status exposes active and
pending inference counts for operations and the demo's warm-state display.

The deployment runs one Uvicorn worker per loaded model copy. Horizontal
Container shards are the scaling unit; increasing generic ASGI worker count
would load another CLIP/CSD pair into the same container. Three shards admit up
to 75 active-or-waiting encodes, but still execute only three model inferences
at once. That is a burst buffer, not a 50-way inference promise.

Only Cloudflare's trusted `CF-Connecting-IP` is used when
`trust_proxy_headers=true`. The hosted origin must be reachable only through the
configured edge or another trusted proxy before enabling that option.

## Local Run

The local Qdrant collection and read alias must already contain the current
CLIP/CSD corpus:

```bash
artsearch-qdrant sync
docker compose --profile search up -d api
```

Open `http://127.0.0.1:8000/search`. The local catalog exposes approved processed
images only through `/api/artworks/{artwork_id}/image`; user input never selects
a path. API documentation is at `/api/docs`, liveness at `/health/live`, and
model/index readiness at `/health/ready`. The same page exposes its latest
request trace and reads local warm state from `/api/runtime-status`.

The retrieval assistant is a Cloudflare Worker route rather than a FastAPI
route. Direct local FastAPI runs retain image search but show the assistant as
offline unless the frontend is served through local Wrangler development.

## Hosted Deployment

The alpha deployment uses a Cloudflare Worker in front of three scale-to-zero
Cloudflare Container shards. The Worker serves the public frontend, rate-limits
API requests, serves approved media through a read-only R2 binding, and keeps
Qdrant and PostgreSQL credentials inside managed runtime secrets. A page-load
visit beacon wakes only the visitor's assigned shard. Each Container bakes the
pinned model weights and starts with Hugging Face network access disabled.

Before traffic expands beyond the trusted alpha:

1. Add provider spend alerts and an external edge-health monitor.
2. Add an artist report/opt-out and cross-store removal procedure.
3. Add per-user identity only if individual revocation or private features
   become necessary.
4. Revisit edge throttles and add bot verification if public traffic grows.
5. Benchmark p50/p95 encode and total latency with one and several concurrent
   alpha users before increasing the inference concurrency.
6. Verify that PostgreSQL query rows, ranked candidates, result impressions,
   feedback events, model lineage, and latency breakdowns are arriving.

## Online Quality Metrics

The control dashboard reads aggregate PostgreSQL telemetry over a configurable
rolling window and reports total searches, search success/error rates, p50 and
p95 total latency, and approximate unique demo IPs. The unique count uses only
keyed hashes; shared NATs can undercount people and changing addresses can
overcount them. Feedback maps `great` to relevance 2, `good` to relevance 1,
and `bad` to relevance 0. Great and Good therefore count as hits.

- **Judged hit rate** is available immediately from partial feedback, but is not
  labeled Precision@10.
- **Precision@10** is calculated only for searches whose entire displayed top
  ten has been judged. Missing judgments are not silently converted to misses.
- **nDCG@10** uses graded relevance, rewarding Great results above Good results,
  and also requires a completely judged displayed top ten.
- **Recall@100** cannot be derived from user feedback because users cannot label
  relevant corpus items they were never shown. It comes from Qdrant's
  exact-versus-HNSW evaluator at K=100.

The laptop, local SQLite image route, and private operations dashboard are not
part of the hosted request path.
