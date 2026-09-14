from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import inspect
import logging
from pathlib import Path

from artsearch.api.catalog import PostgresArtworkCatalog, SQLiteArtworkCatalog
from artsearch.api.config import load_search_api_config
from artsearch.api.encoder import PinnedQueryEncoder
from artsearch.api.service import ImageSearchService
from artsearch.api.telemetry import NoopSearchTelemetry, PostgresSearchTelemetry
from artsearch.ingest.config import load_config
from artsearch.postgres.database import build_async_engine
from artsearch.postgres.repositories import AsyncTelemetryRepository
from artsearch.production.config import load_production_config
from artsearch.retrieval.backend import ThreadedVectorSearchBackend
from artsearch.retrieval.qdrant import (
    AsyncQdrantSearchService,
    QdrantIntegrationError,
    build_async_qdrant_client,
    validate_qdrant_read_alias_async,
)
from artsearch.retrieval.faiss_index import (
    FaissIndexError,
    FaissSearchService,
    faiss_model_bundle_version,
)


LOGGER = logging.getLogger(__name__)


async def build_image_search_service(
    *,
    app_config_path: str | Path = "config/config.yaml",
    production_config_path: str | Path = "configs/production.default.toml",
    api_config_path: str | Path = "configs/search_api.default.toml",
) -> ImageSearchService:
    app_config = load_config(app_config_path)
    production = load_production_config(production_config_path)
    api = load_search_api_config(api_config_path)

    close_callbacks: list[Callable[[], Awaitable[None] | None]] = []
    try:
        if api.vector_backend == "qdrant":
            if not production.qdrant.enabled:
                raise QdrantIntegrationError(
                    "search_api.vector_backend is qdrant but qdrant.enabled is false"
                )
            client = build_async_qdrant_client(production.qdrant)
            close_callbacks.append(client.close)
            await validate_qdrant_read_alias_async(
                client,
                production.qdrant,
                app_config.models,
            )
            search = AsyncQdrantSearchService(client, production.qdrant)
            encoder_config = production.qdrant
            retrieval_contract = None
        else:
            if not production.faiss.enabled:
                raise FaissIndexError(
                    "search_api.vector_backend is faiss but faiss.enabled is false"
                )
            faiss_search = await asyncio.to_thread(
                FaissSearchService.from_active,
                production.faiss,
            )
            close_callbacks.append(faiss_search.close)
            expected_bundle = faiss_model_bundle_version(
                app_config.models,
                production.faiss,
            )
            if faiss_search.retrieval_contract.model_bundle_version != expected_bundle:
                raise FaissIndexError(
                    "active FAISS snapshot was built with a different CLIP/CSD model contract"
                )
            search = ThreadedVectorSearchBackend(faiss_search)
            encoder_config = production.faiss
            retrieval_contract = faiss_search.retrieval_contract

        engine = None
        if api.catalog_backend == "postgres" or (
            api.telemetry_enabled and production.postgres.enabled
        ):
            engine = build_async_engine(production.postgres)
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
            telemetry = PostgresSearchTelemetry(AsyncTelemetryRepository(engine))
        else:
            telemetry = NoopSearchTelemetry()

        encoder = await asyncio.to_thread(PinnedQueryEncoder, app_config, encoder_config)
        return ImageSearchService(
            api_config=api,
            model_config=app_config.models,
            qdrant_config=production.qdrant if api.vector_backend == "qdrant" else None,
            retrieval_contract=retrieval_contract,
            encoder=encoder,
            search=search,
            catalog=catalog,
            telemetry=telemetry,
            close_callbacks=close_callbacks,
        )
    except BaseException:
        await _close_resources(close_callbacks)
        raise


async def _close_resources(
    callbacks: list[Callable[[], Awaitable[None] | None]],
) -> None:
    for callback in reversed(callbacks):
        try:
            result = callback()
            if inspect.isawaitable(result):
                await result
        except Exception as cleanup_error:
            # Preserve the startup exception that triggered cleanup.
            LOGGER.warning(
                "search startup resource cleanup failed: %s",
                type(cleanup_error).__name__,
            )
