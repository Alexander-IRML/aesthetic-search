from __future__ import annotations

import json
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from threading import Lock
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import webbrowser

from sqlalchemy.exc import SQLAlchemyError

from artsearch.artwork_filter.errors import ArtworkFilterError
from artsearch.monitoring.config import load_monitoring_config
from artsearch.monitoring.corpus_control import (
    CorpusControlConflictError,
    CorpusControlService,
)
from artsearch.monitoring.dashboard import render_monitoring_html
from artsearch.monitoring.review import HumanReviewService
from artsearch.monitoring.snapshot import MonitoringInputs, build_monitoring_snapshot


class SnapshotProvider:
    def __init__(self, inputs: MonitoringInputs, *, cache_seconds: int) -> None:
        self.inputs = inputs
        self.cache_seconds = cache_seconds
        self._snapshot: dict[str, Any] | None = None
        self._built_at = 0.0
        self._lock = Lock()

    def get(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if force or self._snapshot is None or now - self._built_at >= self.cache_seconds:
                self._snapshot = build_monitoring_snapshot(self.inputs)
                self._built_at = now
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None
            self._built_at = 0.0


class MonitoringHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    review_token = ""


def create_monitoring_server(
    inputs: MonitoringInputs,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    allow_remote_review: bool = False,
) -> MonitoringHTTPServer:
    monitoring = load_monitoring_config(inputs.monitoring_config_path)
    provider = SnapshotProvider(inputs, cache_seconds=monitoring.snapshot_cache_seconds)
    review = None
    control = None
    if monitoring.review.enabled and (_loopback_host(host) or allow_remote_review):
        review = HumanReviewService(
            app_config_path=inputs.app_config_path,
            filter_config_path=inputs.filter_config_path,
            production_config_path=inputs.production_config_path,
            review_config=monitoring.review,
            filter_label_path=inputs.filter_label_path,
            retrieval_judgment_path=inputs.review_judgment_path,
            retrieval_judgment_sources=inputs.retrieval_judgment_paths,
            annotator=inputs.review_annotator,
            on_write=provider.invalidate,
        )
        control = CorpusControlService(
            app_config_path=inputs.app_config_path,
            filter_config_path=inputs.filter_config_path,
            prompt_config_path=inputs.prompt_config_path,
            production_config_path=inputs.production_config_path,
            bluesky_config_path=inputs.bluesky_config_path,
            discovery_config_path=inputs.discovery_config_path,
            on_write=provider.invalidate,
        )
    handler = _handler(provider, review, control)
    server = MonitoringHTTPServer((host, port), handler)
    server.review_token = review.token if review is not None else ""
    return server


def serve_monitoring_dashboard(
    inputs: MonitoringInputs,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    allow_remote_review: bool = False,
) -> str:
    server = _bind_available(
        inputs,
        host=host,
        preferred_port=port,
        allow_remote_review=allow_remote_review,
    )
    actual_port = int(server.server_address[1])
    url = f"http://{host}:{actual_port}/"
    print(f"ArtSearch model dashboard: {url}", flush=True)
    print("Close this window or press Ctrl+C to stop it.", flush=True)
    if open_browser:
        _open_url(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nStopping ArtSearch model dashboard.", flush=True)
    finally:
        server.server_close()
    return url


def _bind_available(
    inputs: MonitoringInputs,
    *,
    host: str,
    preferred_port: int,
    allow_remote_review: bool = False,
) -> MonitoringHTTPServer:
    if preferred_port == 0:
        return create_monitoring_server(
            inputs,
            host=host,
            port=0,
            allow_remote_review=allow_remote_review,
        )
    last_error: OSError | None = None
    for candidate in range(preferred_port, preferred_port + 10):
        try:
            return create_monitoring_server(
                inputs,
                host=host,
                port=candidate,
                allow_remote_review=allow_remote_review,
            )
        except OSError as exc:
            last_error = exc
    raise OSError(
        f"could not bind ArtSearch dashboard on ports {preferred_port}-{preferred_port + 9}: "
        f"{last_error}"
    )


def _handler(
    provider: SnapshotProvider,
    review: HumanReviewService | None,
    control: CorpusControlService | None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ArtSearchMonitoring/1.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                token = review.token if review is not None else ""
                self._write(
                    render_monitoring_html(review_token=token).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if parsed.path.startswith("/api/review/"):
                self._review_get(parsed)
                return
            if parsed.path.startswith("/api/control/"):
                self._control_get(parsed)
                return
            if parsed.path == "/api/snapshot":
                query = parse_qs(parsed.query)
                try:
                    payload = provider.get(force=query.get("refresh") == ["1"])
                except (OSError, RuntimeError, ValueError) as exc:
                    self._json(
                        {"error": type(exc).__name__, "message": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )
                    return
                headers = {}
                if query.get("download") == ["1"]:
                    headers["Content-Disposition"] = (
                        "attachment; filename=artsearch-model-health.json"
                    )
                self._json(payload, headers=headers)
                return
            if parsed.path == "/api/health":
                try:
                    payload = provider.get()
                except (OSError, RuntimeError, ValueError) as exc:
                    self._json(
                        {"status": "error", "message": str(exc)},
                        status=HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                level = payload["monitoring"]["overall_level"]
                self._json({"status": level, "generated_at": payload["generated_at"]})
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            is_review = parsed.path.startswith("/api/review/")
            is_control = parsed.path.startswith("/api/control/")
            if not is_review and not is_control:
                self._json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
                return
            if (is_review and review is None) or (is_control and control is None):
                self._json(
                    {"error": "operator_controls_disabled"},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            assert review is not None
            token = self.headers.get("X-ArtSearch-Review-Token", "")
            if not hmac.compare_digest(token, review.token):
                self._json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            try:
                payload = self._read_json()
                if parsed.path == "/api/review/filter-label":
                    self._json(review.submit_filter_label(payload))
                    return
                if parsed.path == "/api/review/retrieval-judgment":
                    self._json(review.submit_retrieval_judgment(payload))
                    return
                if parsed.path == "/api/review/discovery-decision":
                    self._json(review.submit_discovery_review(payload))
                    return
                if parsed.path == "/api/control/refresh":
                    assert control is not None
                    self._json(control.start_refresh(payload), status=HTTPStatus.ACCEPTED)
                    return
                if parsed.path == "/api/control/expand":
                    assert control is not None
                    self._json(control.start_expand(payload), status=HTTPStatus.ACCEPTED)
                    return
                self._json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            except KeyError as exc:
                self._json(
                    {"error": "not_found", "message": str(exc)},
                    status=HTTPStatus.NOT_FOUND,
                )
            except ValueError as exc:
                self._json(
                    {"error": "invalid_request", "message": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            except CorpusControlConflictError as exc:
                self._json(
                    {"error": "job_already_running", "message": str(exc)},
                    status=HTTPStatus.CONFLICT,
                )
            except (ArtworkFilterError, SQLAlchemyError, OSError, RuntimeError) as exc:
                self._json(
                    {"error": type(exc).__name__, "message": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _control_get(self, parsed) -> None:
            if review is None or control is None:
                self._json(
                    {"error": "operator_controls_disabled"},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            query = parse_qs(parsed.query)
            token = query.get("token", [""])[0]
            if not hmac.compare_digest(token, review.token):
                self._json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            try:
                if parsed.path == "/api/control/graph":
                    self._json(control.graph())
                    return
                if parsed.path == "/api/control/job":
                    self._json(control.status())
                    return
                self._json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            except (SQLAlchemyError, OSError, RuntimeError, ValueError) as exc:
                self._json(
                    {"error": type(exc).__name__, "message": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _review_get(self, parsed) -> None:
            if review is None:
                self._json(
                    {"error": "review_disabled"},
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            query = parse_qs(parsed.query)
            token = query.get("token", [""])[0]
            if not hmac.compare_digest(token, review.token):
                self._json({"error": "forbidden"}, status=HTTPStatus.FORBIDDEN)
                return
            try:
                if parsed.path == "/api/review/summary":
                    self._json(review.summary())
                    return
                if parsed.path == "/api/review/filter":
                    self._json(
                        review.filter_item(
                            decision=query.get("decision", ["all"])[0],
                            label_status=query.get("status", ["unlabeled"])[0],
                            offset=int(query.get("offset", ["0"])[0]),
                        )
                    )
                    return
                if parsed.path == "/api/review/retrieval":
                    self._json(review.new_retrieval_session(query.get("mode", ["ensemble"])[0]))
                    return
                if parsed.path == "/api/review/discovery":
                    self._json(
                        review.discovery_item(
                            state=query.get("state", ["needs_review"])[0],
                            offset=int(query.get("offset", ["0"])[0]),
                        )
                    )
                    return
                if parsed.path == "/api/review/media/filter":
                    media = review.filter_media(query.get("candidate_id", [""])[0])
                    self._write(media.data, media.content_type)
                    return
                if parsed.path == "/api/review/media/artwork":
                    media = review.artwork_media(query.get("artwork_id", [""])[0])
                    self._write(media.data, media.content_type)
                    return
                if parsed.path == "/api/review/media/discovery":
                    media = review.discovery_media(query.get("candidate_id", [""])[0])
                    self._write(media.data, media.content_type)
                    return
                self._json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            except (KeyError, FileNotFoundError) as exc:
                self._json(
                    {"error": "not_found", "message": str(exc)},
                    status=HTTPStatus.NOT_FOUND,
                )
            except ValueError as exc:
                self._json(
                    {"error": "invalid_review", "message": str(exc)},
                    status=HTTPStatus.BAD_REQUEST,
                )
            except (ArtworkFilterError, SQLAlchemyError, OSError, RuntimeError) as exc:
                self._json(
                    {"error": type(exc).__name__, "message": str(exc)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def _read_json(self) -> dict[str, Any]:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type != "application/json":
                raise ValueError("dashboard writes require application/json")
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("invalid Content-Length") from exc
            if not 0 < content_length <= 65_536:
                raise ValueError("dashboard payload must be between 1 byte and 64 KiB")
            payload = json.loads(self.rfile.read(content_length))
            if not isinstance(payload, dict):
                raise ValueError("dashboard payload must be a JSON object")
            return payload

        def log_message(self, format: str, *args: object) -> None:
            return

        def _json(
            self,
            value: object,
            *,
            status: HTTPStatus = HTTPStatus.OK,
            headers: dict[str, str] | None = None,
        ) -> None:
            data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self._write(data, "application/json; charset=utf-8", status=status, headers=headers)

        def _write(
            self,
            data: bytes,
            content_type: str,
            *,
            status: HTTPStatus = HTTPStatus.OK,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def _open_url(url: str) -> None:
    if _running_under_wsl():
        subprocess.Popen(
            ["explorer.exe", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    webbrowser.open(url)


def _running_under_wsl() -> bool:
    try:
        release = Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8")
    except OSError:
        return False
    return "microsoft" in release.lower() or "wsl" in release.lower()


def _loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}
