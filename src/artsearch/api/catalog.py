from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Protocol, Sequence
from urllib.parse import quote, urlsplit

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from artsearch.api.config import SearchApiConfig
from artsearch.ingest.config import AppConfig
from artsearch.ingest.db import connect, init_db
from artsearch.postgres.schema import artists, artwork_objects, artworks


@dataclass(frozen=True)
class CatalogArtwork:
    artwork_id: str
    artist_id: str
    artist_name: str
    image_url: str
    source_url: str | None = None
    post_text: str | None = None
    alt_text: str | None = None


class ArtworkCatalog(Protocol):
    async def get_many(self, artwork_ids: Sequence[str]) -> dict[str, CatalogArtwork]: ...

    async def local_image_path(self, artwork_id: str) -> Path | None: ...


class SQLiteArtworkCatalog:
    """Development catalog with controlled image delivery through the API."""

    def __init__(self, app_config: AppConfig, api_config: SearchApiConfig) -> None:
        self.app_config = app_config
        self.api_config = api_config

    async def get_many(self, artwork_ids: Sequence[str]) -> dict[str, CatalogArtwork]:
        return await asyncio.to_thread(self._get_many, artwork_ids)

    def _get_many(self, artwork_ids: Sequence[str]) -> dict[str, CatalogArtwork]:
        unique_ids = list(dict.fromkeys(artwork_ids))
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        with connect(self.app_config.database_path) as connection:
            init_db(connection)
            rows = connection.execute(
                f"""
                SELECT a.artwork_id, a.artist_id, r.display_name, r.source_url,
                       a.processed_path,
                       COALESCE(t.object_key, d.object_key, o.object_key) AS object_key,
                       COALESCE(t.object_uri, d.object_uri, o.object_uri) AS object_uri,
                       (
                           SELECT fd.candidate_json
                             FROM artwork_filter_decisions AS fd
                            WHERE fd.candidate_id = a.source_id
                            ORDER BY fd.processed_at DESC, fd.decision_key DESC
                            LIMIT 1
                       ) AS source_candidate_json
                  FROM artworks AS a
                  JOIN artists AS r ON r.artist_id = a.artist_id
                  LEFT JOIN artwork_objects AS o
                    ON o.artwork_id = a.artwork_id AND o.role = 'original'
                  LEFT JOIN artwork_objects AS d
                    ON d.artwork_id = a.artwork_id AND d.role = 'display'
                  LEFT JOIN artwork_objects AS t
                    ON t.artwork_id = a.artwork_id AND t.role = 'thumbnail'
                 WHERE a.artwork_id IN ({placeholders})
                """,
                unique_ids,
            ).fetchall()
        return {
            str(row["artwork_id"]): CatalogArtwork(
                artwork_id=str(row["artwork_id"]),
                artist_id=str(row["artist_id"]),
                artist_name=str(row["display_name"]),
                image_url=_public_image_url(
                    self.api_config,
                    artwork_id=str(row["artwork_id"]),
                    object_key=row["object_key"],
                    object_uri=row["object_uri"],
                    source_media_url=_source_media_url(row["source_candidate_json"]),
                ),
                source_url=_optional_text(row["source_url"]),
            )
            for row in rows
        }

    async def local_image_path(self, artwork_id: str) -> Path | None:
        return await asyncio.to_thread(self._local_image_path, artwork_id)

    def _local_image_path(self, artwork_id: str) -> Path | None:
        if not self.api_config.serve_local_images:
            return None
        with connect(self.app_config.database_path) as connection:
            init_db(connection)
            row = connection.execute(
                """
                SELECT processed_path
                  FROM artworks
                 WHERE artwork_id = ?
                   AND validated = 1
                   AND demo_eligible = 1
                   AND is_sfw = 1
                   AND processed_path IS NOT NULL
                """,
                (artwork_id,),
            ).fetchone()
        if row is None:
            return None
        candidate = Path(str(row["processed_path"]))
        if not candidate.is_absolute():
            candidate = self.app_config.root_dir / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.app_config.root_dir.resolve()):
            return None
        return resolved if resolved.is_file() else None


class PostgresArtworkCatalog:
    """Production metadata lookup that emits CDN URLs, never object credentials."""

    def __init__(self, engine: AsyncEngine, api_config: SearchApiConfig) -> None:
        self.engine = engine
        self.api_config = api_config

    async def get_many(self, artwork_ids: Sequence[str]) -> dict[str, CatalogArtwork]:
        unique_ids = list(dict.fromkeys(artwork_ids))
        if not unique_ids:
            return {}
        thumbnail = artwork_objects.alias("thumbnail")
        display = artwork_objects.alias("display")
        original = artwork_objects.alias("original")
        statement = (
            select(
                artworks.c.artwork_id,
                artworks.c.artist_id,
                artworks.c.post_text,
                artworks.c.alt_text,
                artworks.c.metadata,
                artists.c.display_name,
                artists.c.source_url,
                func.coalesce(
                    thumbnail.c.object_key,
                    display.c.object_key,
                    original.c.object_key,
                ).label("object_key"),
                func.coalesce(
                    thumbnail.c.object_uri,
                    display.c.object_uri,
                    original.c.object_uri,
                ).label("object_uri"),
            )
            .join(artists, artists.c.artist_id == artworks.c.artist_id)
            .outerjoin(
                thumbnail,
                and_(
                    thumbnail.c.artwork_id == artworks.c.artwork_id,
                    thumbnail.c.role == "thumbnail",
                ),
            )
            .outerjoin(
                display,
                and_(
                    display.c.artwork_id == artworks.c.artwork_id,
                    display.c.role == "display",
                ),
            )
            .outerjoin(
                original,
                and_(
                    original.c.artwork_id == artworks.c.artwork_id,
                    original.c.role == "original",
                ),
            )
            .where(artworks.c.artwork_id.in_(unique_ids))
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).mappings().all()
        return {
            str(row["artwork_id"]): CatalogArtwork(
                artwork_id=str(row["artwork_id"]),
                artist_id=str(row["artist_id"]),
                artist_name=str(row["display_name"]),
                image_url=_public_image_url(
                    self.api_config,
                    artwork_id=str(row["artwork_id"]),
                    object_key=row["object_key"],
                    object_uri=row["object_uri"],
                    source_media_url=_source_media_url(row["metadata"]),
                ),
                source_url=_optional_text(row["source_url"]),
                post_text=_bounded_metadata_text(row["post_text"]),
                alt_text=_bounded_metadata_text(row["alt_text"]),
            )
            for row in rows
        }

    async def local_image_path(self, artwork_id: str) -> Path | None:
        return None


def _public_image_url(
    config: SearchApiConfig,
    *,
    artwork_id: str,
    object_key: object,
    object_uri: object,
    source_media_url: str | None = None,
) -> str:
    key = _optional_text(object_key)
    if config.asset_base_url and key:
        encoded_key = "/".join(quote(part, safe="") for part in key.split("/"))
        return f"{config.asset_base_url}/{encoded_key}"
    uri = _optional_text(object_uri)
    if uri and urlsplit(uri).scheme in {"http", "https"}:
        return uri
    if config.serve_local_images:
        return f"/api/artworks/{quote(artwork_id, safe='')}/image"
    return source_media_url or ""


def _source_media_url(value: object) -> str | None:
    payload: Mapping[str, object]
    if isinstance(value, Mapping):
        payload = value
    elif isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, Mapping):
            return None
        payload = decoded
    else:
        return None
    for field in ("fullsize_url", "thumbnail_url"):
        candidate = _optional_text(payload.get(field))
        if candidate is None:
            continue
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower()
        try:
            port = parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and port in {None, 443}
            and (hostname == "cdn.bsky.app" or hostname.endswith(".bsky.app"))
        ):
            return candidate
    return None


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _bounded_metadata_text(value: object, *, max_length: int = 700) -> str | None:
    normalized = " ".join(str(value or "").split())
    return normalized[:max_length] or None
