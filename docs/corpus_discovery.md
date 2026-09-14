# Corpus Discovery

ArtSearch corpus discovery is a bounded, provenance-preserving system for finding
independent Bluesky creators, sampling their media, and sending useful work through
the existing artwork intake pipeline. PostgreSQL is the canonical discovery control
plane. SQLite remains the worker-local corpus staging database, and Qdrant remains a
derived serving index.

The implementation follows one invariant:

> Discovery, evidence, creator qualification, artwork acceptance, and indexing are
> separate decisions.

A search result or social edge is a lead, not proof that an account is an artist. A
qualified creator does not make every attachment acceptable. Social similarity is
not presented as visual similarity.

## Implemented Flow

One bounded campaign performs the following work:

1. Query enabled discovery oracles: manual seeds, actor search, post search, Starter
   Pack search/list hydration, outbound follows, and suggested follows.
2. Hydrate each account to a stable DID and current profile.
3. Upsert one creator while retaining each independent source observation and graph
   edge as provenance.
4. Apply profile-label and text safety policy independently from art relevance.
5. Lease creator-media tasks from the PostgreSQL frontier with
   `FOR UPDATE SKIP LOCKED`.
6. Sample a small media cohort and run it through the existing Bluesky moderation,
   SigLIP gate, router, and SQLite corpus importer.
7. Deepen only creators that qualify from the initial sample.
8. Persist post/media identities, versioned model evidence, creator score components,
   state-transition events, and a coverage snapshot.
9. Queue bounded graph expansion for qualified creators.
10. On later runs, skip fresh creator scores, reconsider stale or model-version-changed
    scores, and recover due frontier tasks left by interrupted campaigns.

Accepted media follows the normal corpus path. It is downloaded at full size,
metadata-stripped, standardized, and upserted into local SQLite. The normal Airflow
jobs then create current CLIP subject and CSD style vectors, publish durable objects
and canonical metadata, and reconcile the derived serving index.

Rejected image bytes are not routed or retained. Their decision evidence remains so
thresholds can be audited and replayed.

## Storage Model

Alembic revision `20260909_0004` adds the `discovery` PostgreSQL schema:

- `campaigns`: versioned run configuration, budgets, outcomes, and state.
- `graph_entities`: heterogeneous creator, post, media, pack, list, feed, query, and
  source nodes.
- `creator_candidates`: current profile, safety state, score, and review projection.
- `sources` and `observations`: independent discovery provenance.
- `graph_edges` and `graph_edge_evidence`: idempotent relationships with multiple
  supporting observations.
- `posts` and `media`: canonical AT URI/post CID and stable post-position media
  identity; blob CID is retained as version evidence.
- `classification_evidence`: append-only, model/config-versioned work and creator
  scores.
- `creator_profiles`: latest creator-level score projection.
- `frontier_tasks`: leased, retryable, depth- and budget-bounded work.
- `pipeline_events`: append-only state transitions.
- `exclusions` and `removal_requests`: discovery/index policy and removal workflow.
- `coverage_snapshots`: creator state, safety, graph depth, popularity, source, and
  media-state distributions.

CDN URLs are retrieval conveniences. Creator DID, post AT URI/CID, candidate ID, and
blob CID provide stable identity and version information.

## Configuration

[`configs/discovery.default.toml`](../configs/discovery.default.toml) owns:

- enabled sources and query vocabulary;
- crawl concurrency, graph depth, and creator/media/task budgets;
- a hard AppView HTTP-attempt budget that counts retries;
- initial/deep media sample sizes;
- a shared per-creator expansion fan-out cap across all social discovery sources;
- the per-batch exploration fraction used to lease a small low-priority slice;
- lease duration and bounded exponential retry policy;
- qualification thresholds and resampling intervals;
- versioned creator-score weights and long-tail bias;
- profile moderation and retention policy.

The resolved configuration receives a deterministic SHA-256 hash. CLI and Airflow
budget overrides are revalidated and rehashed before a campaign starts.

Defaults are intentionally bounded to 200 creators, 5,000 candidate media objects,
500 frontier tasks, 1,000 AppView attempts, and depth two per scheduled run. A
high-follower account can contribute at most 25 unique child accounts across
outbound-follow and suggested-follow expansion. Repeated DIDs retain independent
edge provenance, but are counted in campaign duplicate metrics and are not sampled
twice while an equivalent task or fresh qualification is present.

## Setup And Run

Install the PostgreSQL and model extras, configure pooled/direct database URLs, and
apply the migration:

```bash
python -m pip install -e '.[filter,postgres]'
artsearch-postgres --config configs/production.alpha-cloud.local.toml upgrade
artsearch-postgres --config configs/production.alpha-cloud.local.toml status
```

Run a small campaign before enabling the scheduled path:

```bash
artsearch-discovery run \
  --production-config configs/production.alpha-cloud.local.toml \
  --creator-budget 20 \
  --media-budget 200 \
  --max-tasks 50 \
  --request-budget 100 \
  --max-depth 1 \
  --seed known-artist.bsky.social \
  --campaign-name discovery-pilot-001

artsearch-discovery status \
  --production-config configs/production.alpha-cloud.local.toml
```

The first model-backed run may acquire the pinned Hugging Face checkpoint. Use
`--deterministic-only` only for plumbing tests; that mode cannot silently accept
artwork.

Other operator commands are:

```bash
artsearch-discovery review-creator --did did:plc:... --decision approve
artsearch-discovery exclude --subject-type creator --subject-key did:plc:... \
  --reason-code operator.block
artsearch-discovery request-removal --subject-type creator \
  --subject-key did:plc:... --request-type artist_opt_out
artsearch-discovery approve-removal --request-id 019...
```

Each subcommand also accepts `--production-config`.

For an existing trusted corpus roster, `--trusted-seeds --manual-seeds-only`
records the seed provenance and immediately schedules its outbound/suggested-account
neighborhood. It does not spend model work requalifying the seed before taking the
first graph step. Profile safety policy still applies, discovered accounts still
require normal media qualification, and the HTTP request budget remains absolute.

## Airflow And Dashboard

The existing `artsearch_bluesky_intake` DAG supports both intake strategies. Keep
`discovery_enabled=false` for the fixed actor roster. Set it to `true` to run
`artsearch-discovery` first while retaining the same downstream object publication,
embedding, PostgreSQL, and Qdrant stages.

The scheduler passes these explicit DAG controls:

- `discovery_config`;
- `discovery_creator_budget`;
- `candidate_target` as the campaign media budget;
- `discovery_max_tasks`.

The local model-operations application has a **Discovery** tab for campaign,
frontier, source-yield, state, safety, and coverage metrics. Its corpus map derives
artist and post keys from canonical SQLite records, overlays PostgreSQL creator
relationships, and includes bounded background controls to refresh missing posts or
expand one to three graph steps from rotating existing-corpus seeds. The displayed
post keys are compact hashes; the canonical AT URIs are not duplicated into a new
store. Job state and logs live under ignored `data/control/` paths.

Under **Review & feedback**, the **Creator discovery** workspace shows score
components, source observations, and sampled media. Human decisions are approve,
defer, reject, or block; block also writes an exclusion and cancels pending work for
that creator.

## Failure And Removal Semantics

- API and task failures do not convert into rejections.
- Frontier leases expire and can be reclaimed after worker interruption.
- Retry delay is bounded and exponential; exhausted work becomes `dead` and raises a
  dashboard warning.
- New campaigns can claim due work from older campaigns, preventing stranded retries.
- Replaying the same observation or edge evidence is idempotent.
- A removal approval marks canonical discovery state deleted, blocks future work,
  and emits a transactional `corpus.remove_subject` outbox event.
- Vector and object stores are derived. A consumer must process the removal outbox
  before the deletion is complete across those stores.

## Current Boundary

This release implements the high-precision MVP through bounded graph expansion and
cheap coverage reporting. The following are explicit later phases:

- custom-feed discovery and feed co-occurrence;
- Jetstream create/update/delete ingestion and historical replay;
- reverse list-membership reconstruction;
- CLIP/CSD creator centroids, visual clusters, and measured novelty bonuses;
- taxonomy-driven aesthetic, subject, and community coverage;
- graph embeddings and community detection.

Until creator visual vectors exist, novelty is a neutral `0.5` component rather than
an invented measurement. Coverage snapshots are observational and do not yet alter
frontier priority beyond the explicit exploration slice. Classification does not
grant copyright permission, prove
attribution, or supersede artist opt-outs and upstream deletion policy.
