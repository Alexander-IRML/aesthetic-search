from __future__ import annotations

import sqlite3

from artsearch.ingest.config import ModelConfig


ELIGIBLE_EMBEDDING_QUERY = """
    WITH decision_links AS (
        SELECT r.artwork_id,
               d.decision_key,
               d.decision,
               d.accepted_for_main_corpus,
               d.predicted_class,
               d.author_did,
               d.post_uri,
               d.image_index,
               d.processed_at,
               0 AS link_priority
          FROM artwork_filter_routes AS r
          JOIN artwork_filter_decisions AS d
            ON d.decision_key = r.decision_key
         WHERE r.artwork_id IS NOT NULL
        UNION ALL
        SELECT a.artwork_id,
               d.decision_key,
               d.decision,
               d.accepted_for_main_corpus,
               d.predicted_class,
               d.author_did,
               d.post_uri,
               d.image_index,
               d.processed_at,
               1 AS link_priority
          FROM artworks AS a
          JOIN artwork_filter_decisions AS d
            ON d.candidate_id = a.source_id
         WHERE a.source_platform = 'bluesky'
    ),
    ranked_decisions AS (
        SELECT decision_links.*,
               ROW_NUMBER() OVER (
                   PARTITION BY artwork_id
                   ORDER BY processed_at DESC, decision_key DESC, link_priority
               ) AS decision_rank
          FROM decision_links
    )
    SELECT a.artwork_id,
           a.artist_id,
           ar.display_name AS artist_display_name,
           ar.source_url AS artist_source_url,
           a.source_platform,
           a.source_id,
           a.review_status,
           a.is_sfw,
           a.demo_eligible,
           a.orig_width,
           a.orig_height,
           a.date_added,
           e.clip_vector,
           e.clip_dim,
           e.csd_style,
           e.csd_style_dim,
           e.date_computed,
           COALESCE(d.predicted_class, 'unknown') AS content_class,
           d.decision AS filter_decision,
           d.author_did,
           d.post_uri,
           d.image_index,
           COALESCE(thumb.object_key, display.object_key, original.object_key) AS object_key,
           COALESCE(thumb.object_uri, display.object_uri, original.object_uri) AS object_uri,
           COALESCE(thumb.role, display.role, original.role) AS object_role,
           state.content_hash AS indexed_content_hash
      FROM artworks AS a
      JOIN artists AS ar ON ar.artist_id = a.artist_id
      JOIN embeddings AS e ON e.artwork_id = a.artwork_id
      LEFT JOIN ranked_decisions AS d
        ON d.artwork_id = a.artwork_id AND d.decision_rank = 1
      LEFT JOIN artwork_objects AS thumb
        ON thumb.artwork_id = a.artwork_id AND thumb.role = 'thumbnail'
      LEFT JOIN artwork_objects AS display
        ON display.artwork_id = a.artwork_id AND display.role = 'display'
      LEFT JOIN artwork_objects AS original
        ON original.artwork_id = a.artwork_id AND original.role = 'original'
      LEFT JOIN vector_index_points AS state
        ON state.artwork_id = a.artwork_id AND state.collection_name = :state_collection
     WHERE a.validated = 1
       AND a.processed_path IS NOT NULL
       AND a.duplicate_of IS NULL
       AND a.demo_eligible = 1
       AND (:require_sfw = 0 OR a.is_sfw = 1)
       AND (
           :require_bluesky_accept = 0
           OR a.source_platform != 'bluesky'
           OR (d.decision = 'accept' AND d.accepted_for_main_corpus = 1)
       )
       AND e.clip_vector IS NOT NULL
       AND e.csd_style IS NOT NULL
       AND e.clip_dim = :clip_dimension
       AND e.csd_style_dim = :csd_dimension
       AND e.model_name_clip = :clip_model_name
       AND e.model_version_clip = :clip_model_version
       AND e.model_name_csd = :csd_model_name
       AND e.model_version_csd = :csd_model_version
"""


def eligible_embedding_rows(
    connection: sqlite3.Connection,
    models: ModelConfig,
    *,
    clip_dimension: int,
    csd_dimension: int,
    require_sfw: bool,
    require_bluesky_accept: bool,
    state_collection: str | None = None,
) -> sqlite3.Cursor:
    """Stream the exact policy corpus shared by all derived vector indexes."""

    return connection.execute(
        f"{ELIGIBLE_EMBEDDING_QUERY} ORDER BY a.artwork_id",
        _parameters(
            models,
            clip_dimension=clip_dimension,
            csd_dimension=csd_dimension,
            require_sfw=require_sfw,
            require_bluesky_accept=require_bluesky_accept,
            state_collection=state_collection,
        ),
    )


def eligible_embedding_count(
    connection: sqlite3.Connection,
    models: ModelConfig,
    *,
    clip_dimension: int,
    csd_dimension: int,
    require_sfw: bool,
    require_bluesky_accept: bool,
) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) FROM ({ELIGIBLE_EMBEDDING_QUERY}) AS eligible",
        _parameters(
            models,
            clip_dimension=clip_dimension,
            csd_dimension=csd_dimension,
            require_sfw=require_sfw,
            require_bluesky_accept=require_bluesky_accept,
            state_collection=None,
        ),
    ).fetchone()
    return int(row[0])


def _parameters(
    models: ModelConfig,
    *,
    clip_dimension: int,
    csd_dimension: int,
    require_sfw: bool,
    require_bluesky_accept: bool,
    state_collection: str | None,
) -> dict[str, object]:
    return {
        "state_collection": state_collection,
        "require_sfw": int(require_sfw),
        "require_bluesky_accept": int(require_bluesky_accept),
        "clip_dimension": clip_dimension,
        "csd_dimension": csd_dimension,
        "clip_model_name": models.clip_model_name,
        "clip_model_version": models.clip_model_version,
        "csd_model_name": models.csd_model_name,
        "csd_model_version": models.csd_model_version,
    }
