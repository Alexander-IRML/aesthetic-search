from __future__ import annotations

from pathlib import Path

from artsearch.api.catalog import PostgresArtworkCatalog, SQLiteArtworkCatalog
from artsearch.api.config import load_search_api_config
from artsearch.api.encoder import PinnedQueryEncoder
from artsearch.api.service import ImageSearchService
from artsearch.api.telemetry import NoopSearchTelemetry, PostgresSearchTelemetry
from artsearch.ingest.config import load_config
from artsearch.postgres.database import build_engine
from artsearch.postgres.repositories import TelemetryRepository
from artsearch.production.config import load_production_config
from artsearch.retrieval.qdrant import (
    QdrantSearchService,
    build_qdrant_client,
    validate_qdrant_read_alias,
)


def build_image_search_service(
    *,
    app_config_path: str | Path = "config/config.yaml",
    production_config_path: str | Path = "configs/production.default.toml",
    api_config_path: str | Path = "configs/search_api.default.toml",
) -> ImageSearchService:
    app_config = load_config(app_config_path)
    production = load_production_config(production_config_path)
    api = load_search_api_config(api_config_path)

    client = build_qdrant_client(production.qdrant)
    validate_qdrant_read_alias(client, production.qdrant, app_config.models)
    close_callbacks = [client.close]

    engine = None
    if api.catalog_backend == "postgres" or (api.telemetry_enabled and production.postgres.enabled):
        engine = build_engine(production.postgres)
        close_callbacks.append(engine.dispose)

    if api.catalog_backend == "postgres":
        if engine is None or not production.postgres.enabled:
            raise RuntimeError("the PostgreSQL catalog requires postgres.enabled=true")
        catalog = PostgresArtworkCatalog(engine, api)
    else:
        catalog = SQLiteArtworkCatalog(app_config, api)

    if api.telemetry_enabled and production.postgres.enabled:
        if engine is None:
            raise RuntimeError("telemetry requires a PostgreSQL engine")
        telemetry = PostgresSearchTelemetry(TelemetryRepository(engine))
    else:
        telemetry = NoopSearchTelemetry()

    return ImageSearchService(
        api_config=api,
        model_config=app_config.models,
        qdrant_config=production.qdrant,
        encoder=PinnedQueryEncoder(app_config, production.qdrant),
        search=QdrantSearchService(client, production.qdrant),
        catalog=catalog,
        telemetry=telemetry,
        close_callbacks=close_callbacks,
    )
