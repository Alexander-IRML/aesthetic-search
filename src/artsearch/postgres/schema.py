from __future__ import annotations

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID


SCHEMAS = ("catalog", "ml", "ingest", "product")

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


artists = Table(
    "artists",
    metadata,
    Column("artist_id", Text, primary_key=True),
    Column("display_name", Text, nullable=False),
    Column("folder_name", Text, nullable=False, unique=True),
    Column("handle", Text),
    Column("source_platform", Text, nullable=False, server_default=text("'manual'")),
    Column("source_url", Text),
    Column("notes", Text),
    Column("date_added", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema="catalog",
)
Index(
    "ix_artists_display_name_trgm",
    artists.c.display_name,
    postgresql_using="gin",
    postgresql_ops={"display_name": "gin_trgm_ops"},
)
Index(
    "ix_artists_handle_trgm",
    artists.c.handle,
    postgresql_using="gin",
    postgresql_ops={"handle": "gin_trgm_ops"},
)


artworks = Table(
    "artworks",
    metadata,
    Column("artwork_id", Text, primary_key=True),
    Column(
        "artist_id",
        Text,
        ForeignKey("catalog.artists.artist_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("raw_path", Text, nullable=False),
    Column("processed_path", Text),
    Column("object_uri", Text),
    Column("source_platform", Text, nullable=False, server_default=text("'manual'")),
    Column("source_id", Text),
    Column("title", Text),
    Column("description", Text),
    Column("post_text", Text),
    Column("alt_text", Text),
    Column("tags", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("orig_width", Integer),
    Column("orig_height", Integer),
    Column("file_hash", Text),
    Column("phash", Text),
    Column("is_sfw", Boolean),
    Column("demo_eligible", Boolean, nullable=False, server_default=text("false")),
    Column("validated", Boolean, nullable=False, server_default=text("false")),
    Column("review_status", Text, nullable=False, server_default=text("'unreviewed'")),
    Column(
        "duplicate_of",
        Text,
        ForeignKey("catalog.artworks.artwork_id", ondelete="SET NULL"),
    ),
    Column("scale_factor", Float),
    Column("pad_left", Integer, nullable=False, server_default=text("0")),
    Column("pad_top", Integer, nullable=False, server_default=text("0")),
    Column("pad_right", Integer, nullable=False, server_default=text("0")),
    Column("pad_bottom", Integer, nullable=False, server_default=text("0")),
    Column("crop_left", Integer, nullable=False, server_default=text("0")),
    Column("crop_top", Integer, nullable=False, server_default=text("0")),
    Column("crop_right", Integer, nullable=False, server_default=text("0")),
    Column("crop_bottom", Integer, nullable=False, server_default=text("0")),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("date_added", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("notes", Text),
    Column(
        "search_document",
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('simple', coalesce(description, '')), 'B') || "
            "setweight(to_tsvector('simple', coalesce(post_text, '')), 'C') || "
            "setweight(to_tsvector('simple', coalesce(alt_text, '')), 'C')",
            persisted=True,
        ),
    ),
    CheckConstraint(
        "review_status IN ('unreviewed', 'confirmed_unique', 'confirmed_duplicate', "
        "'confirmed_variant')",
        name="artworks_review_status",
    ),
    UniqueConstraint("source_platform", "source_id", name="uq_artworks_source"),
    schema="catalog",
)
Index("ix_artworks_artist", artworks.c.artist_id)
Index("ix_artworks_file_hash", artworks.c.file_hash)
Index("ix_artworks_phash", artworks.c.phash)
Index("ix_artworks_review_status", artworks.c.review_status)
Index("ix_artworks_demo_policy", artworks.c.demo_eligible, artworks.c.is_sfw, artworks.c.validated)
Index("ix_artworks_search_document", artworks.c.search_document, postgresql_using="gin")
Index("ix_artworks_metadata", artworks.c.metadata, postgresql_using="gin")


artwork_objects = Table(
    "artwork_objects",
    metadata,
    Column(
        "artwork_id",
        Text,
        ForeignKey("catalog.artworks.artwork_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role", Text, nullable=False),
    Column("object_key", Text, nullable=False),
    Column("object_uri", Text, nullable=False),
    Column("content_sha256", Text, nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("etag", Text),
    Column("published_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("verified_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("artwork_id", "role"),
    CheckConstraint("role IN ('original', 'display', 'thumbnail')", name="artwork_objects_role"),
    schema="catalog",
)
Index("ix_artwork_objects_hash", artwork_objects.c.content_sha256)


model_versions = Table(
    "model_versions",
    metadata,
    Column("model_bundle_version", Text, primary_key=True),
    Column("clip_model_name", Text, nullable=False),
    Column("clip_model_revision", Text, nullable=False),
    Column("csd_model_name", Text, nullable=False),
    Column("csd_model_revision", Text, nullable=False),
    Column("clip_dimension", Integer, nullable=False),
    Column("csd_dimension", Integer, nullable=False),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('active', 'retired', 'failed')", name="model_versions_status"),
    schema="ml",
)


embeddings = Table(
    "embeddings",
    metadata,
    Column(
        "artwork_id",
        Text,
        ForeignKey("catalog.artworks.artwork_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("clip_vector", HALFVEC(512)),
    Column("clip_dim", Integer),
    Column("csd_style", HALFVEC(768)),
    Column("csd_style_dim", Integer),
    Column("model_name_clip", Text),
    Column("model_version_clip", Text),
    Column("model_name_csd", Text),
    Column("model_version_csd", Text),
    Column(
        "model_bundle_version",
        Text,
        ForeignKey("ml.model_versions.model_bundle_version", ondelete="SET NULL"),
    ),
    Column("date_computed", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("clip_dim IS NULL OR clip_dim = 512", name="embeddings_clip_dimension"),
    CheckConstraint(
        "csd_style_dim IS NULL OR csd_style_dim = 768", name="embeddings_csd_dimension"
    ),
    schema="ml",
)
Index(
    "ix_embeddings_models",
    embeddings.c.model_name_clip,
    embeddings.c.model_version_clip,
    embeddings.c.model_name_csd,
    embeddings.c.model_version_csd,
)


vector_index_points = Table(
    "vector_index_points",
    metadata,
    Column("collection_name", Text, nullable=False),
    Column(
        "artwork_id",
        Text,
        ForeignKey("catalog.artworks.artwork_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("point_id", UUID(as_uuid=True), nullable=False),
    Column("content_hash", Text, nullable=False),
    Column("indexed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("collection_name", "artwork_id"),
    UniqueConstraint("collection_name", "point_id", name="uq_vector_index_points_point"),
    schema="ml",
)
Index("ix_vector_index_points_artwork", vector_index_points.c.artwork_id)


runs = Table(
    "runs",
    metadata,
    Column("run_id", BigInteger, Identity(), primary_key=True),
    Column("script_name", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True)),
    Column("images_processed", Integer, nullable=False, server_default=text("0")),
    Column("images_skipped", Integer, nullable=False, server_default=text("0")),
    Column("errors_count", Integer, nullable=False, server_default=text("0")),
    Column("notes", Text),
    Column("metrics", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    schema="ingest",
)


run_events = Table(
    "run_events",
    metadata,
    Column("event_id", BigInteger, Identity(), primary_key=True),
    Column("run_id", BigInteger, ForeignKey("ingest.runs.run_id", ondelete="CASCADE")),
    Column("level", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("raw_path", Text),
    Column("artwork_id", Text, ForeignKey("catalog.artworks.artwork_id", ondelete="SET NULL")),
    Column("message", Text, nullable=False),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("level IN ('info', 'warning', 'error')", name="run_events_level"),
    schema="ingest",
)
Index("ix_run_events_run", run_events.c.run_id)
Index("ix_run_events_created", run_events.c.created_at)


filter_decisions = Table(
    "artwork_filter_decisions",
    metadata,
    Column("decision_key", Text, primary_key=True),
    Column("candidate_id", Text, nullable=False),
    Column("author_did", Text),
    Column("post_uri", Text),
    Column("image_index", Integer),
    Column("image_sha256", Text),
    Column("decision", Text, nullable=False),
    Column("predicted_class", Text, nullable=False),
    Column("accepted_for_main_corpus", Boolean, nullable=False),
    Column("route", Text, nullable=False),
    Column("final_score", Float, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("reason_codes", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")),
    Column("candidate", JSONB, nullable=False),
    Column("evidence", JSONB, nullable=False),
    Column("model_id", Text, nullable=False),
    Column("model_revision", Text),
    Column("config_version", Text, nullable=False),
    Column("config_hash", Text, nullable=False),
    Column("prompt_version", Text),
    Column("classifier_version", Text),
    Column("software_version", Text, nullable=False),
    Column("processed_at", DateTime(timezone=True), nullable=False),
    Column("duration_ms", Float, nullable=False),
    Column("error_type", Text),
    Column("error_message", Text),
    Column("date_added", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "decision IN ('accept', 'review', 'reject', 'error')",
        name="filter_decisions_decision",
    ),
    schema="ingest",
)
Index(
    "ix_filter_decisions_candidate",
    filter_decisions.c.candidate_id,
    filter_decisions.c.processed_at,
)
Index(
    "ix_filter_decisions_outcome", filter_decisions.c.decision, filter_decisions.c.predicted_class
)


filter_routes = Table(
    "artwork_filter_routes",
    metadata,
    Column("route_key", Text, primary_key=True),
    Column(
        "decision_key",
        Text,
        ForeignKey("ingest.artwork_filter_decisions.decision_key", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    ),
    Column("candidate_id", Text, nullable=False),
    Column("target", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("local_path", Text),
    Column("image_sha256", Text),
    Column("perceptual_hash", Text),
    Column("width", Integer),
    Column("height", Integer),
    Column("artwork_id", Text, ForeignKey("catalog.artworks.artwork_id", ondelete="SET NULL")),
    Column("error_type", Text),
    Column("error_message", Text),
    Column("routed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("target IN ('corpus', 'review')", name="filter_routes_target"),
    CheckConstraint("status IN ('stored', 'duplicate', 'error')", name="filter_routes_status"),
    schema="ingest",
)
Index(
    "ix_filter_routes_candidate",
    filter_routes.c.candidate_id,
    filter_routes.c.target,
    filter_routes.c.status,
)


actor_intake_state = Table(
    "bluesky_actor_intake_state",
    metadata,
    Column("actor_key", Text, primary_key=True),
    Column("actor", Text, nullable=False),
    Column("high_water_post_uri", Text),
    Column("pending_high_water_post_uri", Text),
    Column("resume_cursor", Text),
    Column("last_scan_complete", Boolean, nullable=False, server_default=text("false")),
    Column("last_candidate_count", Integer, nullable=False, server_default=text("0")),
    Column("last_error", Text),
    Column("last_attempt_at", DateTime(timezone=True), nullable=False),
    Column("last_completed_at", DateTime(timezone=True)),
    schema="ingest",
)
Index("ix_actor_intake_completed", actor_intake_state.c.last_completed_at)


roster_intake_state = Table(
    "bluesky_roster_intake_state",
    metadata,
    Column("roster_key", Text, primary_key=True),
    Column("actor_count", Integer, nullable=False),
    Column("next_actor_index", Integer, nullable=False, server_default=text("0")),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    schema="ingest",
)


sqlite_imports = Table(
    "sqlite_imports",
    metadata,
    Column("import_id", UUID(as_uuid=True), primary_key=True),
    Column("import_key", Text, nullable=False, unique=True),
    Column("source_name", Text, nullable=False),
    Column("source_sha256", Text, nullable=False),
    Column("source_size", BigInteger, nullable=False),
    Column("scope", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("schema_revision", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    Column("row_counts", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("verification", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("error_message", Text),
    CheckConstraint("status IN ('running', 'completed', 'failed')", name="sqlite_imports_status"),
    schema="ingest",
)
Index("ix_sqlite_imports_source", sqlite_imports.c.source_sha256)


outbox_events = Table(
    "outbox_events",
    metadata,
    Column("event_id", UUID(as_uuid=True), primary_key=True),
    Column("aggregate_type", Text, nullable=False),
    Column("aggregate_id", Text, nullable=False),
    Column("event_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("status", Text, nullable=False, server_default=text("'pending'")),
    Column("attempts", Integer, nullable=False, server_default=text("0")),
    Column("available_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("locked_at", DateTime(timezone=True)),
    Column("locked_by", Text),
    Column("published_at", DateTime(timezone=True)),
    Column("last_error", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "status IN ('pending', 'processing', 'published', 'dead')",
        name="outbox_events_status",
    ),
    schema="ingest",
)
Index(
    "ix_outbox_claim",
    outbox_events.c.status,
    outbox_events.c.available_at,
    outbox_events.c.created_at,
)


users = Table(
    "users",
    metadata,
    Column("user_id", UUID(as_uuid=True), primary_key=True),
    Column("external_subject", Text, nullable=False, unique=True),
    Column("display_name", Text),
    Column("status", Text, nullable=False, server_default=text("'active'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True)),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    CheckConstraint("status IN ('invited', 'active', 'disabled')", name="users_status"),
    schema="product",
)


sessions = Table(
    "sessions",
    metadata,
    Column("session_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("product.users.user_id", ondelete="SET NULL"),
    ),
    Column("client_session_key", Text, unique=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("ended_at", DateTime(timezone=True)),
    Column("last_seen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("client_metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    schema="product",
)
Index("ix_sessions_user_started", sessions.c.user_id, sessions.c.started_at)


queries = Table(
    "queries",
    metadata,
    Column("query_id", UUID(as_uuid=True), primary_key=True),
    Column("request_id", UUID(as_uuid=True), nullable=False, unique=True),
    Column(
        "session_id",
        UUID(as_uuid=True),
        ForeignKey("product.sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("product.users.user_id", ondelete="SET NULL"),
    ),
    Column(
        "parent_query_id",
        UUID(as_uuid=True),
        ForeignKey("product.queries.query_id", ondelete="SET NULL"),
    ),
    Column("query_modality", Text, nullable=False),
    Column("query_text", Text),
    Column("query_image_uri", Text),
    Column(
        "positive_artwork_ids", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    ),
    Column(
        "negative_artwork_ids", ARRAY(Text), nullable=False, server_default=text("'{}'::text[]")
    ),
    Column("filters", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("retrieval_weights", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("model_bundle_version", Text),
    Column("index_version", Text),
    Column("candidate_count", Integer),
    Column("result_count", Integer),
    Column("latency_ms", Float),
    Column("latency_breakdown", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("status", Text, nullable=False, server_default=text("'started'")),
    Column("error_type", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("completed_at", DateTime(timezone=True)),
    CheckConstraint(
        "query_modality IN ('text', 'image', 'hybrid', 'refinement')",
        name="queries_modality",
    ),
    CheckConstraint("status IN ('started', 'completed', 'error')", name="queries_status"),
    schema="product",
)
Index("ix_queries_session_created", queries.c.session_id, queries.c.created_at)
Index("ix_queries_created", queries.c.created_at)


ranking_runs = Table(
    "ranking_runs",
    metadata,
    Column("ranking_run_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "query_id",
        UUID(as_uuid=True),
        ForeignKey("product.queries.query_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("stage", Text, nullable=False),
    Column("candidate_count", Integer, nullable=False),
    Column("model_version", Text),
    Column("index_version", Text),
    Column("latency_ms", Float, nullable=False),
    Column("candidate_artifact_uri", Text),
    Column("score_summary", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    schema="product",
)
Index("ix_ranking_runs_query", ranking_runs.c.query_id, ranking_runs.c.created_at)


retrieval_candidates = Table(
    "retrieval_candidates",
    metadata,
    Column(
        "query_id",
        UUID(as_uuid=True),
        ForeignKey("product.queries.query_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "artwork_id",
        Text,
        ForeignKey("catalog.artworks.artwork_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "artist_id",
        Text,
        ForeignKey("catalog.artists.artist_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("candidate_rank", Integer, nullable=False),
    Column("fusion_score", Float),
    Column("clip_score", Float),
    Column("clip_rank", Integer),
    Column("style_score", Float),
    Column("style_rank", Integer),
    Column("scores", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("model_bundle_version", Text),
    Column("index_version", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("query_id", "candidate_rank"),
    UniqueConstraint("query_id", "artwork_id", name="uq_retrieval_candidates_query_artwork"),
    CheckConstraint("candidate_rank > 0", name="retrieval_candidates_rank"),
    schema="product",
)
Index("ix_retrieval_candidates_artwork", retrieval_candidates.c.artwork_id)


result_impressions = Table(
    "result_impressions",
    metadata,
    Column("impression_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "query_id",
        UUID(as_uuid=True),
        ForeignKey("product.queries.query_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "session_id",
        UUID(as_uuid=True),
        ForeignKey("product.sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "artwork_id",
        Text,
        ForeignKey("catalog.artworks.artwork_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "artist_id",
        Text,
        ForeignKey("catalog.artists.artist_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("result_rank", Integer, nullable=False),
    Column("fusion_score", Float),
    Column("clip_score", Float),
    Column("style_score", Float),
    Column("scores", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("model_bundle_version", Text),
    Column("index_version", Text),
    Column("shown_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("query_id", "result_rank", name="uq_impressions_query_rank"),
    UniqueConstraint("query_id", "artwork_id", name="uq_impressions_query_artwork"),
    CheckConstraint("result_rank > 0", name="result_impressions_rank"),
    schema="product",
)
Index("ix_impressions_artwork", result_impressions.c.artwork_id, result_impressions.c.shown_at)


interaction_events = Table(
    "interaction_events",
    metadata,
    Column("interaction_id", UUID(as_uuid=True), primary_key=True),
    Column(
        "query_id",
        UUID(as_uuid=True),
        ForeignKey("product.queries.query_id", ondelete="SET NULL"),
    ),
    Column(
        "session_id",
        UUID(as_uuid=True),
        ForeignKey("product.sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("product.users.user_id", ondelete="SET NULL"),
    ),
    Column("artwork_id", Text, ForeignKey("catalog.artworks.artwork_id", ondelete="SET NULL")),
    Column("artist_id", Text, ForeignKey("catalog.artists.artist_id", ondelete="SET NULL")),
    Column("interaction_type", Text, nullable=False),
    Column("result_rank", Integer),
    Column("value", Boolean),
    Column("metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "interaction_type IN ('click', 'like', 'dislike', 'save', 'unsave', "
        "'follow_artist', 'unfollow_artist', 'reformulation', 'relevance_feedback')",
        name="interaction_events_type",
    ),
    schema="product",
)
Index(
    "ix_interactions_session_created",
    interaction_events.c.session_id,
    interaction_events.c.created_at,
)
Index("ix_interactions_query", interaction_events.c.query_id)
Index(
    "ix_interactions_feedback_created",
    interaction_events.c.interaction_type,
    interaction_events.c.created_at,
)


saved_artworks = Table(
    "saved_artworks",
    metadata,
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("product.users.user_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "artwork_id",
        Text,
        ForeignKey("catalog.artworks.artwork_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("saved_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("user_id", "artwork_id"),
    schema="product",
)


followed_artists = Table(
    "followed_artists",
    metadata,
    Column(
        "user_id",
        UUID(as_uuid=True),
        ForeignKey("product.users.user_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "artist_id",
        Text,
        ForeignKey("catalog.artists.artist_id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("followed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("user_id", "artist_id"),
    schema="product",
)


ALL_TABLES = {table.name: table for table in metadata.tables.values()}
