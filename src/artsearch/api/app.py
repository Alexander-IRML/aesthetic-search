from __future__ import annotations

from contextlib import asynccontextmanager
from collections import defaultdict, deque
from importlib.resources import files
import logging
from pathlib import Path
import re
from threading import Lock
import time
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response

from artsearch.api.config import SearchApiConfig, load_search_api_config
from artsearch.api.errors import (
    CatalogConsistencyError,
    FeedbackTargetNotFoundError,
    FeedbackUnavailableError,
    InvalidUploadError,
    SearchBusyError,
    SearchTimeoutError,
    UnsupportedUploadError,
    UploadTooLargeError,
)
from artsearch.api.factory import build_image_search_service
from artsearch.api.image_input import read_validated_image
from artsearch.api.schemas import (
    ErrorDetail,
    ErrorResponse,
    ImageSearchResponse,
    ResultFeedbackRequest,
    ResultFeedbackResponse,
)
from artsearch.api.service import ImageSearchService
from artsearch.postgres.ids import uuid7
from artsearch.retrieval.qdrant import QdrantIntegrationError


LOGGER = logging.getLogger(__name__)
SESSION_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")


class FixedWindowRateLimiter:
    def __init__(self, *, requests: int, window_seconds: int) -> None:
        self.requests = requests
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> int | None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.requests:
                return max(1, int(self.window_seconds - (now - events[0])))
            events.append(now)
        return None


def create_app(
    *,
    api_config: SearchApiConfig | None = None,
    service: ImageSearchService | None = None,
    initialize_search_service: bool = True,
    app_config_path: str | Path = "config/config.yaml",
    production_config_path: str | Path = "configs/production.default.toml",
    api_config_path: str | Path = "configs/search_api.default.toml",
) -> FastAPI:
    settings = api_config or load_search_api_config(api_config_path)
    static_dir = Path(str(files("artsearch.api.static")))
    frontend_html = (static_dir / "index.html").read_text(encoding="utf-8")
    frontend_css = (static_dir / "app.css").read_text(encoding="utf-8")
    frontend_js = (static_dir / "app.js").read_text(encoding="utf-8")

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        owns_service = service is None and initialize_search_service
        if owns_service:
            application.state.search_service = build_image_search_service(
                app_config_path=app_config_path,
                production_config_path=production_config_path,
                api_config_path=api_config_path,
            )
        else:
            application.state.search_service = service
        try:
            yield
        finally:
            if owns_service and application.state.search_service is not None:
                application.state.search_service.close()

    application = FastAPI(
        title="ArtSearch Image Search API",
        version=settings.version,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    application.state.search_service = service
    application.state.api_config = settings
    limiter = FixedWindowRateLimiter(
        requests=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )
    feedback_limiter = FixedWindowRateLimiter(
        requests=settings.feedback_rate_limit_requests,
        window_seconds=settings.feedback_rate_limit_window_seconds,
    )

    @application.middleware("http")
    async def public_boundary(request: Request, call_next):
        request.state.request_id = str(uuid7())
        if request.method == "POST" and request.url.path == "/api/search-by-image":
            client_ip = _client_ip(request, settings)
            retry_after = limiter.check(client_ip)
            if retry_after is not None:
                response = _error_response(
                    request,
                    429,
                    "rate_limited",
                    "Too many image searches. Try again shortly.",
                    headers={"Retry-After": str(retry_after)},
                )
                return _security_headers(response)
            content_length = request.headers.get("content-length")
            if content_length and content_length.isdigit():
                multipart_allowance = 2 * 1024 * 1024
                if int(content_length) > settings.max_upload_bytes + multipart_allowance:
                    response = _error_response(
                        request,
                        413,
                        "upload_too_large",
                        "The request exceeds the image upload limit.",
                    )
                    return _security_headers(response)
        if request.method == "POST" and request.url.path == "/api/result-feedback":
            retry_after = feedback_limiter.check(_client_ip(request, settings))
            if retry_after is not None:
                response = _error_response(
                    request,
                    429,
                    "rate_limited",
                    "Too many feedback events. Try again shortly.",
                    headers={"Retry-After": str(retry_after)},
                )
                return _security_headers(response)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return _security_headers(response)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        message = (
            "Provide a valid query, result, session key, and feedback grade."
            if request.url.path == "/api/result-feedback"
            else "Provide one image file and a valid session key."
        )
        return _error_response(
            request,
            400,
            "invalid_request",
            message,
        )

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        LOGGER.error(
            "unexpected API error request_id=%s error=%s",
            getattr(request.state, "request_id", "unknown"),
            type(exc).__name__,
        )
        return _error_response(
            request,
            500,
            "internal_error",
            "An unexpected error occurred.",
        )

    @application.get("/health/live", include_in_schema=False)
    async def liveness() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", include_in_schema=False)
    async def readiness(request: Request):
        active = getattr(request.app.state, "search_service", None)
        if active is None or not active.ready:
            return JSONResponse({"status": "starting"}, status_code=503)
        return {
            "status": "ready",
            "model_bundle_version": active.model_bundle,
            "index_alias": active.qdrant_config.alias_name,
        }

    @application.post(
        "/api/search-by-image",
        response_model=ImageSearchResponse,
        responses={
            400: {"model": ErrorResponse},
            413: {"model": ErrorResponse},
            415: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            504: {"model": ErrorResponse},
        },
    )
    async def search_by_image(
        request: Request,
        image: Annotated[UploadFile, File(description="JPEG, PNG, or WebP query image")],
        session_key: Annotated[str, Form(min_length=8, max_length=128)],
    ):
        if not SESSION_KEY.fullmatch(session_key):
            return _error_response(
                request,
                400,
                "invalid_session",
                "The browser session key is invalid.",
            )
        decode_started = time.perf_counter()
        try:
            active = request.app.state.search_service
            if active is None or not active.ready:
                return _error_response(
                    request,
                    503,
                    "service_starting",
                    "The image search model is still starting.",
                    headers={"Retry-After": "10"},
                )
            validated = await read_validated_image(image, settings)
            decode_ms = (time.perf_counter() - decode_started) * 1000.0
            return await active.search_image(
                validated.image,
                client_session_key=session_key,
                upload_decode_ms=decode_ms,
            )
        except UploadTooLargeError as exc:
            return _error_response(request, 413, "upload_too_large", str(exc))
        except UnsupportedUploadError as exc:
            return _error_response(request, 415, "unsupported_image", str(exc))
        except InvalidUploadError as exc:
            return _error_response(request, 400, "invalid_image", str(exc))
        except SearchBusyError as exc:
            return _error_response(
                request,
                503,
                "encoder_busy",
                str(exc),
                headers={"Retry-After": "3"},
            )
        except SearchTimeoutError as exc:
            return _error_response(request, 504, "encoder_timeout", str(exc))
        except (CatalogConsistencyError, QdrantIntegrationError, RuntimeError) as exc:
            LOGGER.error(
                "image search failed request_id=%s error=%s",
                request.state.request_id,
                type(exc).__name__,
            )
            return _error_response(
                request,
                503,
                "search_unavailable",
                "Image search is temporarily unavailable.",
                headers={"Retry-After": "10"},
            )
        finally:
            await image.close()

    @application.post(
        "/api/result-feedback",
        response_model=ResultFeedbackResponse,
        responses={
            400: {"model": ErrorResponse},
            404: {"model": ErrorResponse},
            429: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
        },
    )
    async def result_feedback(request: Request, payload: ResultFeedbackRequest):
        if not SESSION_KEY.fullmatch(payload.session_key):
            return _error_response(
                request,
                400,
                "invalid_session",
                "The browser session key is invalid.",
            )
        active = request.app.state.search_service
        if active is None or not active.ready:
            return _error_response(
                request,
                503,
                "service_starting",
                "Result feedback is temporarily unavailable.",
                headers={"Retry-After": "10"},
            )
        try:
            receipt = await active.submit_feedback(
                client_session_key=payload.session_key,
                query_id=payload.query_id,
                artwork_id=payload.artwork_id,
                grade=payload.grade,
            )
        except FeedbackUnavailableError as exc:
            return _error_response(request, 503, "feedback_unavailable", str(exc))
        except FeedbackTargetNotFoundError as exc:
            return _error_response(request, 404, "feedback_target_not_found", str(exc))
        relevance = {"bad": 0, "good": 1, "great": 2}[payload.grade]
        return ResultFeedbackResponse(
            interaction_id=str(receipt.interaction_id),
            query_id=str(payload.query_id),
            artwork_id=payload.artwork_id,
            result_rank=receipt.result_rank,
            grade=payload.grade,
            relevance=relevance,
        )

    @application.get("/api/artworks/{artwork_id}/image", include_in_schema=False)
    async def artwork_image(request: Request, artwork_id: str):
        active = request.app.state.search_service
        path = None if active is None else active.catalog.local_image_path(artwork_id)
        if path is None:
            return _error_response(request, 404, "image_not_found", "Artwork image not found.")
        try:
            content = path.read_bytes()
        except OSError:
            return _error_response(request, 404, "image_not_found", "Artwork image not found.")
        media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        return Response(
            content,
            media_type=media_type,
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @application.get("/", include_in_schema=False)
    @application.get("/search", include_in_schema=False)
    async def frontend() -> HTMLResponse:
        return HTMLResponse(frontend_html)

    @application.get("/assets/app.css", include_in_schema=False)
    async def frontend_css_asset() -> Response:
        return Response(frontend_css, media_type="text/css")

    @application.get("/assets/app.js", include_in_schema=False)
    async def frontend_js_asset() -> Response:
        return Response(frontend_js, media_type="text/javascript")

    return application


def _client_ip(request: Request, config: SearchApiConfig) -> str:
    if config.trust_proxy_headers:
        cloudflare_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cloudflare_ip:
            return cloudflare_ip
    return request.client.host if request.client is not None else "unknown"


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid7()))
    payload = ErrorResponse(
        error=ErrorDetail(code=code, message=message, request_id=request_id)
    ).model_dump(mode="json")
    return JSONResponse(payload, status_code=status_code, headers=headers)


def _security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data: https:; "
        "style-src 'self'; script-src 'self'; connect-src 'self'"
    )
    return response
