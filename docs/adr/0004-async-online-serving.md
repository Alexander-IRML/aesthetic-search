# ADR 0004: Async Online Search With A Bounded Model Owner

Status: accepted

Date: 2026-09-13

## Context

The public search process performs two different kinds of work:

- CPU-heavy Pillow, CLIP, and CSD operations.
- Wait-heavy Qdrant, PostgreSQL, upload, and telemetry operations.

Running every step synchronously prevents the server from servicing health
checks or another user's I/O while one request waits. Running several model
copies in generic ASGI workers is also a poor fit for the current small
container: CSD is memory-heavy, alpha traffic is low, and overlapping CPU model
jobs usually increase contention rather than useful throughput.

## Decision

Use one asynchronous FastAPI coordinator with an explicitly bounded model
execution lane.

1. Multipart upload reads are asynchronous and byte-limited.
2. Pillow decode and validation run off the event loop.
3. CLIP and CSD share a dedicated `ThreadPoolExecutor`. Production defaults to
   one active inference and at most two queued requests.
4. Queue admission has a timeout and returns `503 encoder_busy` when saturated.
5. The model slot is released immediately after both query vectors exist.
6. Qdrant search uses `AsyncQdrantClient`.
7. Production catalog and telemetry operations use SQLAlchemy's async psycopg
   dialect and bounded connection pool.
8. Telemetry completion runs after the response path. Feedback waits for the
   completion task belonging to its own query, without serializing other users.
9. Lifespan shutdown drains telemetry and closes async clients and engines.

The request path is therefore:

```text
async upload -> off-loop decode -> bounded CLIP/CSD owner
             -> release model slot
             -> async Qdrant -> async PostgreSQL catalog -> response
                                      |
                                      +-> per-query telemetry completion
```

## Operational Contract

- Run one Uvicorn worker per loaded CLIP/CSD pair.
- Treat `max_concurrent_inferences` as model capacity, not ordinary HTTP
  concurrency.
- Keep `/health/live`, `/health/ready`, and `/api/runtime-status` on the event
  loop and free of model work.
- Observe active and pending model counts through `/api/runtime-status`.
- Scale later by adding complete service replicas or by moving the encoder
  behind a private inference service. Do not increase ASGI worker count blindly.

## Consequences

Remote I/O for one search can overlap query encoding for another, and health
traffic remains responsive during inference. Memory use and overload behavior
are predictable. The architecture still has a deliberate single-model
bottleneck, so sustained concurrency above alpha traffic will queue or receive
backpressure instead of scaling automatically.

The local FAISS and SQLite development adapters use worker threads because
their libraries are synchronous. Production Qdrant and PostgreSQL calls are
native async and do not consume the model executor.

## Verification

Tests cover:

- a second encoding beginning while the first request waits on vector search;
- queue saturation returning `SearchBusyError`;
- one query's slow telemetry completion not blocking another query;
- feedback waiting for its own impressions;
- async Qdrant fusion retaining CLIP, CSD, and RRF evidence;
- async PostgreSQL engine construction;
- API upload, health, result, and feedback contracts.
