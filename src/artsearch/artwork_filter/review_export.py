from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from artsearch.artwork_filter.enums import FilterDecision
from artsearch.artwork_filter.errors import CandidateInputError, PersistenceError
from artsearch.artwork_filter.labels import LABEL_COLUMNS
from artsearch.artwork_filter.schemas import FilterResult, ImageCandidate


REVIEW_COLUMNS = (
    "candidate_id",
    "author_did",
    "author_handle",
    "post_uri",
    "image_index",
    "thumbnail_url",
    "fullsize_url",
    "local_path",
    "predicted_class",
    "decision",
    "route",
    "final_score",
    "confidence",
    "confidence_margin",
    "distance_to_boundary",
    "reason_codes",
    "post_text",
    "alt_text",
    "created_at",
    "image_sha256",
    "model_version",
    "model_revision",
    "config_version",
    "config_hash",
    "prompt_version",
    "processed_at",
    *LABEL_COLUMNS,
)


def export_review_queue(
    candidates_path: str | Path,
    decisions_path: str | Path,
    output_path: str | Path,
    *,
    accept_score: float,
    reject_score: float,
    decisions: set[FilterDecision] | None = None,
) -> dict[str, int]:
    if not 0.0 <= reject_score < accept_score <= 1.0:
        raise ValueError("review boundaries must satisfy 0 <= reject < accept <= 1")
    candidates = load_latest_candidates(candidates_path)
    latest_decisions = load_latest_decisions(decisions_path)
    included = decisions or {FilterDecision.REVIEW}

    rows = []
    missing_candidates = 0
    for result in latest_decisions.values():
        if result.decision not in included:
            continue
        candidate = candidates.get(result.candidate_id)
        if candidate is None:
            missing_candidates += 1
            continue
        rows.append(
            _review_row(
                candidate,
                result,
                accept_score=accept_score,
                reject_score=reject_score,
            )
        )

    rows.sort(key=_review_priority)
    destination = Path(output_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=REVIEW_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        raise PersistenceError(str(exc)) from exc
    return {
        "candidates": len(candidates),
        "decisions": len(latest_decisions),
        "exported": len(rows),
        "missing_candidates": missing_candidates,
    }


def load_latest_candidates(path: str | Path) -> dict[str, ImageCandidate]:
    candidates: dict[str, ImageCandidate] = {}
    input_path = Path(path)
    try:
        with input_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    candidate = ImageCandidate.model_validate_json(line)
                except (ValidationError, ValueError) as exc:
                    raise CandidateInputError(
                        f"invalid candidate JSONL at {input_path}:{line_number}: {exc}"
                    ) from exc
                candidates[candidate.candidate_id] = candidate
    except OSError as exc:
        raise CandidateInputError(str(exc)) from exc
    return candidates


def load_decision_history(path: str | Path) -> dict[str, list[FilterResult]]:
    decisions: dict[str, list[FilterResult]] = {}
    input_path = Path(path)
    try:
        with input_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    result = FilterResult.model_validate_json(line)
                except (ValidationError, ValueError) as exc:
                    raise CandidateInputError(
                        f"invalid decision JSONL at {input_path}:{line_number}: {exc}"
                    ) from exc
                decisions.setdefault(result.candidate_id, []).append(result)
    except OSError as exc:
        raise CandidateInputError(str(exc)) from exc
    for history in decisions.values():
        history.sort(key=lambda result: result.processed_at)
    return decisions


def load_latest_decisions(path: str | Path) -> dict[str, FilterResult]:
    return {
        candidate_id: history[-1]
        for candidate_id, history in load_decision_history(path).items()
        if history
    }


def _review_row(
    candidate: ImageCandidate,
    result: FilterResult,
    *,
    accept_score: float,
    reject_score: float,
) -> dict[str, Any]:
    visual = result.visual_scores
    margin = visual.confidence_margin if visual is not None else 0.0
    revision = visual.model_revision if visual is not None else None
    boundary_distance = min(
        abs(result.final_score - accept_score),
        abs(result.final_score - reject_score),
    )
    return {
        "candidate_id": candidate.candidate_id,
        "author_did": candidate.author_did or "",
        "author_handle": _spreadsheet_safe(candidate.author_handle or ""),
        "post_uri": candidate.post_uri or "",
        "image_index": candidate.image_index if candidate.image_index is not None else "",
        "thumbnail_url": candidate.thumbnail_url or "",
        "fullsize_url": candidate.fullsize_url or "",
        "local_path": str(candidate.local_path) if candidate.local_path is not None else "",
        "predicted_class": result.predicted_class.value,
        "decision": result.decision.value,
        "route": result.route,
        "final_score": f"{result.final_score:.8f}",
        "confidence": f"{result.confidence:.8f}",
        "confidence_margin": f"{margin:.8f}",
        "distance_to_boundary": f"{boundary_distance:.8f}",
        "reason_codes": "|".join(result.reason_codes),
        "post_text": _spreadsheet_safe(candidate.post_text),
        "alt_text": _spreadsheet_safe(candidate.alt_text),
        "created_at": candidate.created_at.isoformat() if candidate.created_at else "",
        "image_sha256": result.image_sha256 or "",
        "model_version": result.model_version,
        "model_revision": revision or "",
        "config_version": result.config_version,
        "config_hash": result.config_hash,
        "prompt_version": result.prompt_version or "",
        "processed_at": result.processed_at.isoformat(),
        "label_content_class": "",
        "label_is_original_artist_work": "",
        "label_include_in_main_corpus": "",
        "label_annotator_note": "",
    }


def _review_priority(row: dict[str, Any]) -> tuple[float, float, int, float]:
    routing_rank = 0 if row["route"] != "review" else 1
    created_at = _timestamp(row["created_at"])
    return (
        float(row["distance_to_boundary"]),
        float(row["confidence_margin"]),
        routing_rank,
        -created_at,
    )


def _timestamp(value: str) -> float:
    if not value:
        return 0.0
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _spreadsheet_safe(value: str) -> str:
    if value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
