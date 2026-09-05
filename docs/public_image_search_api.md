# Public Image Search API

Status: deployed public resume-demo vertical slice

## Responsibility

The public browser sends one reference artwork and receives at most ten similar
corpus artworks. It never receives a Qdrant key, PostgreSQL URL, object-store
credential, model token, raw vector, or private filesystem path.

The initial deployment is deliberately one application service. It owns the
HTTP API and exact query encoder in one process, then calls hosted Qdrant and
PostgreSQL. The encoder can move behind a private service boundary later without
changing the frontend contract.

## Request Path

1. Cloudflare terminates TLS, serves the frontend from edge assets, and applies
   Worker rate-limit bindings before model compute can start.
2. `POST /api/search-by-image` accepts one multipart `image`, an opaque browser
   `session_key`, and an optional stable `artist_id` scope.
3. The API streams with a hard byte cap, verifies JPEG/PNG/WebP MIME against the
   decoded format, rejects animation and decompression bombs, applies EXIF
   orientation, and converts to RGB in memory.
4. A bounded model owner produces normalized CLIP subject and CSD style vectors
   with the exact model revisions used by the active corpus collection.
5. Qdrant independently retrieves each named vector, then combines the recall
   lists with server-side reciprocal-rank fusion and catalog policy filters. Up
   to 100 fused candidates are retained as reproducible ranking evidence.
6. The API enriches result IDs from SQLite in development or PostgreSQL in
   production, converting object keys into CDN URLs and attaching bounded
   Bluesky post and alt text when available.
7. The API starts the query telemetry record concurrently with inference, then
   joins it only when the stable query/session IDs are needed. It queues the
   ranking-stage, top-100 candidate, top-10 impression, version, and completed
   timing writes on the same ordered worker after the response is built.
   Feedback uses that worker so it cannot overtake its search record.
8. The decoded query and original upload bytes leave scope without being stored.
9. After the results render, the browser can send only the ten compact result
   records to the edge-only retrieval assistant for a grounded summary.
10. If a follow-up names one displayed artist, deterministic agent routing
    resubmits the same browser-held image with that artist's stable ID. Qdrant
    applies an exact metadata filter before CLIP/CSD fusion.

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
```

The result count is server-controlled and fixed at ten for this version:

```json
{
  "api_version": "1.0.0",
  "query_id": "019...",
  "session_id": "019...",
  "query_modality": "image",
  "result_count": 10,
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
    "index_alias": "artworks_active",
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

Container-routed responses include:

- `X-ArtSearch-Container-State`: state observed before starting the request.
- `X-ArtSearch-Container-Wait-Ms`: time spent obtaining a ready Container.
- `X-ArtSearch-Container-Fetch-Ms`: time to receive backend response headers.
- `X-ArtSearch-Edge-Total-Ms`: complete Worker time for the routed request.
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

Defaults live in `configs/search_api.default.toml`:

- 10 MB and 40 million decoded pixels.
- JPEG, PNG, and WebP only; single-frame only.
- One CPU inference owner and a two-second admission queue.
- 120-second local model deadline.
- Four searches per source IP and eight searches per Cloudflare edge location
  per minute before Container wake.
- Sixty feedback writes per source IP per minute at the edge.
- Twelve assistant requests per source IP and 24 per edge location per minute.
- Twelve searches and 120 feedback writes per source IP per minute in the
  Container as defense in depth.
- Ten results, at most two from one artist through the Qdrant policy.

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

The alpha deployment uses a Cloudflare Worker in front of one scale-to-zero
Cloudflare Container. The Worker serves the public frontend without waking the
Container, rate-limits API requests, serves approved media through a read-only
R2 binding, and keeps Qdrant and PostgreSQL credentials inside managed runtime
secrets. The Container bakes the pinned model weights and starts with Hugging
Face network access disabled.

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
rolling window and reports total searches, search success/error rates, and p50
and p95 total latency. Feedback maps `great` to relevance 2, `good` to relevance
1, and `bad` to relevance 0. Great and Good therefore count as hits.

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
