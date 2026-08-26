from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence
from urllib.parse import quote, urlsplit

from sqlalchemy import Engine, and_, func, select

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


class ArtworkCatalog(Protocol):
    def get_many(self, artwork_ids: Sequence[str]) -> dict[str, CatalogArtwork]: ...

    def local_image_path(self, artwork_id: str) -> Path | None: ...


class SQLiteArtworkCatalog:
    """Development catalog with controlled image delivery through the API."""

    def __init__(self, app_config: AppConfig, api_config: SearchApiConfig) -> None:
        self.app_config = app_config
        self.api_config = api_config

    def get_many(self, artwork_ids: Sequence[str]) -> dict[str, CatalogArtwork]:
        unique_ids = list(dict.fromkeys(artwork_ids))
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        with connect(self.app_config.database_path) as connection:
            init_db(connection)
            rows = connection.execute(
                f"""
                SELECT a.artwork_id, a.artist_id, r.display_name, r.source_url,
                       a.processed_path, o.object_key, o.object_uri
                  FROM artworks AS a
                  JOIN artists AS r ON r.artist_id = a.artist_id
                  LEFT JOIN artwork_objects AS o
                    ON o.artwork_id = a.artwork_id AND o.role = 'original'
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
                ),
                source_url=_optional_text(row["source_url"]),
            )
            for row in rows
        }

    def local_image_path(self, artwork_id: str) -> Path | None:
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

    def __init__(self, engine: Engine, api_config: SearchApiConfig) -> None:
        self.engine = engine
        self.api_config = api_config

    def get_many(self, artwork_ids: Sequence[str]) -> dict[str, CatalogArtwork]:
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
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
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
                ),
                source_url=_optional_text(row["source_url"]),
            )
            for row in rows
        }

    def local_image_path(self, artwork_id: str) -> Path | None:
        return None


def _public_image_url(
    config: SearchApiConfig,
    *,
    artwork_id: str,
    object_key: object,
    object_uri: object,
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
    return ""


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
