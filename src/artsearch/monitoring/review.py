from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path
import secrets
import sqlite3
from threading import Lock
from typing import Any, Iterator

from PIL import Image

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import (
    CorpusInclusionLabel,
    HumanContentLabel,
    OriginalWorkLabel,
)
from artsearch.artwork_filter.evaluation import (
    EvaluationSample,
    evaluate_filter_samples,
)
from artsearch.artwork_filter.image_io import HttpOrLocalImageLoader
from artsearch.artwork_filter.labels import JSONLLabelStore, iter_label_history, latest_labels
from artsearch.artwork_filter.schemas import ArtworkLabel, FilterResult, ImageCandidate
from artsearch.ingest.config import load_config
from artsearch.monitoring.config import HumanReviewConfig
from artsearch.retrieval.evaluation import (
    JSONLRetrievalJudgmentStore,
    RetrievalJudgment,
    latest_retrieval_judgments,
    load_retrieval_judgments,
)
from artsearch.retrieval.search import (
    SUPPORTED_RETRIEVAL_MODES,
    RetrievalMode,
    SearchFilters,
    SearchResult,
    get_artwork_for_demo,
    load_search_index,
    search_similar,
)


_TASKS = {
    RetrievalMode.ENSEMBLE: "ensemble",
    RetrievalMode.CLIP_SUBJECT: "subject",
    RetrievalMode.CSD_STYLE: "style",
}
_QUESTIONS = {
    RetrievalMode.ENSEMBLE: "Is this a useful overall visual match?",
    RetrievalMode.CLIP_SUBJECT: "Is this a useful subject or semantic match?",
    RetrievalMode.CSD_STYLE: "Is this a useful artistic-style match?",
}


@dataclass(frozen=True)
class ReviewMedia:
    data: bytes
    content_type: str = "image/jpeg"


@dataclass(frozen=True)
class _RetrievalSession:
    session_id: str
    seed: str
    corpus_fingerprint: str
    mode: RetrievalMode
    query: SearchResult
    results: tuple[SearchResult, ...]
    model_id: str
    model_revision: str


class HumanReviewService:
    """Serve local review work while preserving immutable annotation evidence."""

    def __init__(
        self,
        *,
        app_config_path: str | Path,
        filter_config_path: str | Path,
        review_config: HumanReviewConfig,
        filter_label_path: str | Path,
        retrieval_judgment_path: str | Path,
        retrieval_judgment_sources: Sequence[str | Path] = (),
        annotator: str = "local-reviewer",
        on_write: Callable[[], None] | None = None,
    ) -> None:
        self.app = load_config(app_config_path)
        self.filter_config = load_artwork_filter_config(filter_config_path)
        self.review_config = review_config
        self.filter_label_path = _resolve(self.app.root_dir, filter_label_path)
        self.retrieval_judgment_path = _resolve(
            self.app.root_dir,
            retrieval_judgment_path,
        )
        sources = [_resolve(self.app.root_dir, value) for value in retrieval_judgment_sources]
        if not sources:
            sources = sorted(
                {
                    *self.app.root_dir.glob("data/retrieval/judgments*.jsonl"),
                    *self.app.root_dir.glob("data/retrieval/**/judgments*.jsonl"),
                }
            )
        self.retrieval_judgment_sources = tuple(
            dict.fromkeys([*sources, self.retrieval_judgment_path])
        )
        self.annotator = annotator.strip() or "local-reviewer"
        self.on_write = on_write
        self.token = secrets.token_urlsafe(32)
        self.dashboard_id = f"model-ops-{secrets.token_hex(8)}"
        self._write_lock = Lock()
        self._session_lock = Lock()
        self._sessions: OrderedDict[str, _RetrievalSession] = OrderedDict()
        self._recent_queries: dict[RetrievalMode, deque[str]] = {
            mode: deque(maxlen=20) for mode in SUPPORTED_RETRIEVAL_MODES
        }

    def summary(self) -> dict[str, Any]:
        labels = latest_labels(self.filter_label_path)
        label_history = sum(1 for _ in iter_label_history(self.filter_label_path))
        rows = self._latest_filter_rows()
        retrieval_history = self._retrieval_history()
        return {
            "filter": {
                "decisions": len(rows),
                "labeled": len(set(labels) & {row["candidate_id"] for row in rows}),
                "pending": sum(row["candidate_id"] not in labels for row in rows),
                "history_events": label_history,
                "output": _relative_label(self.app.root_dir, self.filter_label_path),
            },
            "retrieval": {
                "history_events": len(retrieval_history),
                "latest_judgments": len(latest_retrieval_judgments(retrieval_history)),
                "output": _relative_label(
                    self.app.root_dir,
                    self.retrieval_judgment_path,
                ),
            },
            "annotator": self.annotator,
            "conceal_media": self.review_config.conceal_media_by_default,
            "content_labels": [item.value for item in HumanContentLabel],
            "original_work_labels": [item.value for item in OriginalWorkLabel],
            "corpus_inclusion_labels": [item.value for item in CorpusInclusionLabel],
            "retrieval_modes": [
                {"value": mode.value, "label": mode.label, "question": _QUESTIONS[mode]}
                for mode in SUPPORTED_RETRIEVAL_MODES
            ],
        }

    def filter_item(
        self,
        *,
        decision: str = "all",
        label_status: str = "unlabeled",
        offset: int = 0,
    ) -> dict[str, Any]:
        if decision not in {"all", "accept", "review", "reject", "error"}:
            raise ValueError("unsupported filter decision")
        if label_status not in {"unlabeled", "labeled", "all"}:
            raise ValueError("unsupported label status")
        if offset < 0:
            raise ValueError("offset must be non-negative")

        labels = latest_labels(self.filter_label_path)
        rows = self._latest_filter_rows(decision=decision)
        if label_status == "unlabeled":
            rows = [row for row in rows if row["candidate_id"] not in labels]
        elif label_status == "labeled":
            rows = [row for row in rows if row["candidate_id"] in labels]
        rows = rows[: self.review_config.filter_queue_limit]
        if not rows:
            return {"item": None, "offset": 0, "total": 0}
        resolved_offset = min(offset, len(rows) - 1)
        row = rows[resolved_offset]
        return {
            "item": self._filter_payload(row, labels.get(row["candidate_id"])),
            "offset": resolved_offset,
            "total": len(rows),
        }

    def submit_filter_label(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(payload.get("candidate_id", "")).strip()
        if not candidate_id:
            raise ValueError("candidate_id is required")
        row = self._filter_row(candidate_id)
        evidence = _json_object(row["evidence_json"])
        result = FilterResult.model_validate(evidence)
        label = ArtworkLabel(
            candidate_id=candidate_id,
            content_class=HumanContentLabel(payload.get("content_class")),
            is_original_artist_work=OriginalWorkLabel(payload.get("is_original_artist_work")),
            include_in_main_corpus=CorpusInclusionLabel(payload.get("include_in_main_corpus")),
            annotator=self.annotator,
            annotator_note=payload.get("annotator_note", ""),
            source_decision_processed_at=result.processed_at,
            source_model_version=result.model_version,
            source_config_hash=result.config_hash,
            previous_model_prediction=result.predicted_class,
        )
        with self._write_lock:
            appended = JSONLLabelStore(self.filter_label_path).append(label)
        self._written()
        return {
            "appended": appended,
            "label": label.model_dump(mode="json"),
            "summary": self.summary(),
        }

    def filter_media(self, candidate_id: str) -> ReviewMedia:
        row = self._filter_row(candidate_id)
        candidate = ImageCandidate.model_validate(_json_object(row["candidate_json"]))
        local = self._first_safe_path(
            row["processed_path"],
            row["route_local_path"],
            candidate.local_path,
        )
        if local is not None:
            candidate = candidate.model_copy(
                update={
                    "local_path": local,
                    "thumbnail_url": None,
                    "fullsize_url": None,
                }
            )
        loaded = asyncio.run(self._load_image(candidate))
        return self._encode_media(loaded.rgb_image)

    def new_retrieval_session(self, mode_value: str) -> dict[str, Any]:
        mode = RetrievalMode.coerce(mode_value)
        with self._database() as connection:
            index = load_search_index(connection, self.app, mode=mode)
            if len(index.artwork_ids) < 2:
                raise ValueError(f"not enough searchable artworks for {mode.value}")
            recent = set(self._recent_queries[mode])
            available = [item for item in index.artwork_ids if item not in recent]
            pool = available or list(index.artwork_ids)
            query_id = secrets.choice(pool)
            self._recent_queries[mode].append(query_id)
            query = get_artwork_for_demo(connection, self.app, query_id)
            results = search_similar(
                connection,
                self.app,
                query_id,
                mode=mode,
                filters=SearchFilters(
                    top_k=self.review_config.retrieval_result_count,
                    include_same_artist=False,
                ),
            )
            if not results:
                results = search_similar(
                    connection,
                    self.app,
                    query_id,
                    mode=mode,
                    filters=SearchFilters(
                        top_k=self.review_config.retrieval_result_count,
                        include_same_artist=True,
                    ),
                )
            fingerprint = _corpus_fingerprint(connection)

        seed = secrets.token_hex(8)
        session = _RetrievalSession(
            session_id=f"review-{secrets.token_hex(10)}",
            seed=seed,
            corpus_fingerprint=fingerprint,
            mode=mode,
            query=query,
            results=tuple(results),
            model_id=self._model_identity(mode)[0],
            model_revision=self._model_identity(mode)[1],
        )
        with self._session_lock:
            self._sessions[session.session_id] = session
            while len(self._sessions) > 50:
                self._sessions.popitem(last=False)
        return self._retrieval_payload(session)

    def submit_retrieval_judgment(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id", ""))
        candidate_id = str(payload.get("candidate_artwork_id", ""))
        relevant = payload.get("relevant")
        if not isinstance(relevant, bool):
            raise ValueError("relevant must be true or false")
        with self._session_lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ValueError("review session expired; generate a new query")
        result_by_id = {item.artwork_id: item for item in session.results}
        result = result_by_id.get(candidate_id)
        if result is None:
            raise ValueError("candidate does not belong to this review session")
        rank = next(
            index
            for index, item in enumerate(session.results, start=1)
            if item.artwork_id == candidate_id
        )
        judgment = RetrievalJudgment(
            dashboard_id=self.dashboard_id,
            review_session_id=session.session_id,
            review_seed=session.seed,
            corpus_fingerprint=session.corpus_fingerprint,
            query_artwork_id=session.query.artwork_id,
            candidate_artwork_id=result.artwork_id,
            retrieval_mode=session.mode,
            task=_TASKS[session.mode],
            relevant=relevant,
            rank=rank,
            result_count=len(session.results),
            score=result.score,
            fusion_score=result.fusion_score,
            fusion_rank=result.fusion_rank,
            clip_score=result.clip_score,
            clip_rank=result.clip_rank,
            style_score=result.style_score,
            style_rank=result.style_rank,
            shortlist_size=result.shortlist_size,
            candidate_count=result.candidate_count,
            query_artist_id=session.query.artist_id,
            candidate_artist_id=result.artist_id,
            model_id=session.model_id,
            model_revision=session.model_revision,
            annotator=self.annotator,
            note=payload.get("note", ""),
        )
        with self._write_lock:
            JSONLRetrievalJudgmentStore(self.retrieval_judgment_path).append(judgment)
        self._written()
        return {
            "judgment": judgment.model_dump(mode="json"),
            "summary": self.summary(),
        }

    def artwork_media(self, artwork_id: str) -> ReviewMedia:
        with self._database() as connection:
            row = connection.execute(
                """
                SELECT processed_path
                  FROM artworks
                 WHERE artwork_id = ?
                   AND validated = 1
                   AND processed_path IS NOT NULL
                """,
                (artwork_id,),
            ).fetchone()
        if row is None:
            raise KeyError("artwork not found")
        local = self._first_safe_path(row["processed_path"])
        if local is None:
            raise FileNotFoundError("artwork image is not available")
        candidate = ImageCandidate(
            candidate_id=f"review:{artwork_id}",
            local_path=local,
            source="local",
        )
        loaded = asyncio.run(self._load_image(candidate))
        return self._encode_media(loaded.rgb_image)

    def _filter_payload(
        self,
        row: sqlite3.Row,
        previous_label: ArtworkLabel | None,
    ) -> dict[str, Any]:
        candidate = _json_object(row["candidate_json"])
        evidence = _json_object(row["evidence_json"])
        visual = evidence.get("visual_scores") or {}
        class_scores = sorted(
            visual.get("class_scores") or [],
            key=lambda item: float(item.get("score", 0.0)),
            reverse=True,
        )
        text_scores = evidence.get("text_scores") or {}
        rule_result = evidence.get("rule_result") or {}
        media_available = self._first_safe_path(
            row["processed_path"],
            row["route_local_path"],
            candidate.get("local_path"),
        ) is not None or bool(candidate.get("thumbnail_url") or candidate.get("fullsize_url"))
        return {
            "candidate_id": row["candidate_id"],
            "author_handle": candidate.get("author_handle"),
            "post_uri": candidate.get("post_uri"),
            "post_text": candidate.get("post_text", ""),
            "alt_text": candidate.get("alt_text", ""),
            "created_at": candidate.get("created_at"),
            "decision": row["decision"],
            "predicted_class": row["predicted_class"],
            "route": row["route"],
            "final_score": row["final_score"],
            "confidence": row["confidence"],
            "confidence_margin": visual.get("confidence_margin"),
            "art_utility_score": visual.get("art_utility_score"),
            "noise_score": visual.get("noise_score"),
            "class_scores": class_scores,
            "reason_codes": evidence.get("reason_codes") or [],
            "rule_hits": rule_result.get("hits") or [],
            "text_scores": text_scores,
            "model_version": row["model_id"],
            "model_revision": row["model_revision"],
            "prompt_version": row["prompt_version"],
            "processed_at": row["processed_at"],
            "media_available": media_available,
            "previous_label": (
                previous_label.model_dump(mode="json") if previous_label is not None else None
            ),
        }

    def _retrieval_payload(self, session: _RetrievalSession) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "corpus_fingerprint": session.corpus_fingerprint,
            "mode": {
                "value": session.mode.value,
                "label": session.mode.label,
                "task": _TASKS[session.mode],
                "question": _QUESTIONS[session.mode],
            },
            "model_id": session.model_id,
            "model_revision": session.model_revision,
            "query": _retrieval_item(session.query),
            "results": [
                {**_retrieval_item(result), "rank": rank}
                for rank, result in enumerate(session.results, start=1)
            ],
        }

    def _latest_filter_rows(self, *, decision: str = "all") -> list[sqlite3.Row]:
        where = "" if decision == "all" else "WHERE decisions.decision = ?"
        parameters: tuple[object, ...] = () if decision == "all" else (decision,)
        with self._database() as connection:
            return connection.execute(
                f"""
                WITH decisions AS (
                    SELECT source.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY source.candidate_id
                               ORDER BY source.processed_at DESC, source.date_added DESC
                           ) AS latest_rank
                      FROM artwork_filter_decisions AS source
                )
                SELECT decisions.*,
                       routes.local_path AS route_local_path,
                       routes.artwork_id,
                       artworks.processed_path
                  FROM decisions
                  LEFT JOIN artwork_filter_routes AS routes
                    ON routes.decision_key = decisions.decision_key
                  LEFT JOIN artworks
                    ON artworks.artwork_id = routes.artwork_id
                 {where}
                   {"AND" if where else "WHERE"} decisions.latest_rank = 1
                 ORDER BY CASE decisions.decision
                            WHEN 'review' THEN 0 WHEN 'accept' THEN 1
                            WHEN 'reject' THEN 2 ELSE 3 END,
                          decisions.confidence ASC,
                          decisions.processed_at DESC
                """,
                parameters,
            ).fetchall()

    def _filter_row(self, candidate_id: str) -> sqlite3.Row:
        rows = [row for row in self._latest_filter_rows() if row["candidate_id"] == candidate_id]
        if not rows:
            raise KeyError("filter decision not found")
        return rows[0]

    def _retrieval_history(self) -> list[RetrievalJudgment]:
        history: list[RetrievalJudgment] = []
        for path in self.retrieval_judgment_sources:
            if path.exists():
                history.extend(load_retrieval_judgments(path))
        return history

    async def _load_image(self, candidate: ImageCandidate):
        loader = HttpOrLocalImageLoader(self.filter_config)
        try:
            return await loader.load(candidate)
        finally:
            await loader.aclose()

    def _encode_media(self, image: Image.Image) -> ReviewMedia:
        normalized = image.copy()
        normalized.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        output = BytesIO()
        normalized.save(
            output,
            format="JPEG",
            quality=self.review_config.media_jpeg_quality,
            optimize=True,
        )
        return ReviewMedia(output.getvalue())

    def _first_safe_path(self, *values: object) -> Path | None:
        root = self.app.root_dir.resolve()
        for value in values:
            if not value:
                continue
            path = Path(str(value))
            if not path.is_absolute():
                path = root / path
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (FileNotFoundError, OSError, ValueError):
                continue
            if resolved.is_file():
                return resolved
        return None

    def _model_identity(self, mode: RetrievalMode) -> tuple[str, str]:
        if mode == RetrievalMode.CLIP_SUBJECT:
            return self.app.models.clip_model_name, self.app.models.clip_model_version
        if mode == RetrievalMode.CSD_STYLE:
            return self.app.models.csd_model_name, self.app.models.csd_model_version
        return (
            f"{self.app.models.clip_model_name}+{self.app.models.csd_model_name}",
            f"{self.app.models.clip_model_version}+{self.app.models.csd_model_version}",
        )

    @contextmanager
    def _database(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            f"file:{self.app.database_path}?mode=ro",
            uri=True,
            timeout=3.0,
        )
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def _written(self) -> None:
        if self.on_write is not None:
            self.on_write()


def evaluate_filter_labels_from_database(
    database_path: str | Path,
    labels_path: str | Path,
) -> dict[str, Any] | None:
    """Build current filter metrics directly from canonical decision evidence."""

    labels = latest_labels(labels_path)
    if not labels:
        return None
    connection = sqlite3.connect(
        f"file:{Path(database_path)}?mode=ro",
        uri=True,
        timeout=3.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT candidate_id, candidate_json, evidence_json
              FROM artwork_filter_decisions
             ORDER BY processed_at
            """
        ).fetchall()
    finally:
        connection.close()

    candidates: dict[str, ImageCandidate] = {}
    histories: dict[str, list[FilterResult]] = {}
    for row in rows:
        candidate = ImageCandidate.model_validate(_json_object(row["candidate_json"]))
        result = FilterResult.model_validate(_json_object(row["evidence_json"]))
        candidates[candidate.candidate_id] = candidate
        histories.setdefault(result.candidate_id, []).append(result)

    samples: list[EvaluationSample] = []
    missing_candidates = 0
    missing_decisions = 0
    missing_snapshots = 0
    for candidate_id, label in labels.items():
        candidate = candidates.get(candidate_id)
        if candidate is None:
            missing_candidates += 1
            continue
        history = histories.get(candidate_id, [])
        result = _decision_for_label(history, label)
        if result is None:
            if history:
                missing_snapshots += 1
            else:
                missing_decisions += 1
            continue
        samples.append(EvaluationSample(candidate, result, label))
    return evaluate_filter_samples(
        samples,
        accounting={
            "latest_labels": len(labels),
            "matched": len(samples),
            "missing_candidates": missing_candidates,
            "missing_decisions": missing_decisions,
            "missing_decision_snapshots": missing_snapshots,
        },
    )


def _retrieval_item(result: SearchResult) -> dict[str, Any]:
    return {
        "artwork_id": result.artwork_id,
        "artist_id": result.artist_id,
        "artist_name": result.artist_display_name,
        "score": result.score,
        "review_status": result.review_status,
        "is_sfw": result.is_sfw,
        "fusion_score": result.fusion_score,
        "fusion_rank": result.fusion_rank,
        "clip_score": result.clip_score,
        "clip_rank": result.clip_rank,
        "style_score": result.style_score,
        "style_rank": result.style_rank,
        "shortlist_size": result.shortlist_size,
        "candidate_count": result.candidate_count,
    }


def _corpus_fingerprint(connection: sqlite3.Connection) -> str:
    import hashlib

    rows = connection.execute(
        """
        SELECT artworks.artwork_id, artworks.file_hash,
               embeddings.model_name_clip, embeddings.model_version_clip,
               embeddings.model_name_csd, embeddings.model_version_csd
          FROM artworks
          JOIN embeddings ON embeddings.artwork_id = artworks.artwork_id
         WHERE artworks.validated = 1
         ORDER BY artworks.artwork_id
        """
    ).fetchall()
    payload = [dict(row) for row in rows]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _decision_for_label(
    history: Sequence[FilterResult],
    label: ArtworkLabel,
) -> FilterResult | None:
    if not history:
        return None
    if label.source_decision_processed_at is None:
        return history[-1]
    for result in reversed(history):
        if result.processed_at != label.source_decision_processed_at:
            continue
        if label.source_model_version and result.model_version != label.source_model_version:
            continue
        if label.source_config_hash and result.config_hash != label.source_config_hash:
            continue
        return result
    return None


def _json_object(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("review evidence must be a JSON object")
    return parsed


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _relative_label(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
