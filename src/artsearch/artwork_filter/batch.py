from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Callable

from pydantic import ValidationError

from artsearch.artwork_filter.errors import CandidateInputError
from artsearch.artwork_filter.schemas import ImageCandidate
from artsearch.artwork_filter.service import ArtworkFilterService


def iter_candidate_batches(
    path: str | Path,
    *,
    batch_size: int,
    skip_candidate_ids: set[str] | None = None,
    skip_candidate_keys: set[tuple[str, str | None]] | None = None,
    on_skip: Callable[[ImageCandidate], None] | None = None,
) -> Iterator[list[ImageCandidate]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    skipped = skip_candidate_ids or set()
    skipped_versions = skip_candidate_keys or set()
    batch: list[ImageCandidate] = []
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
                if candidate.candidate_id in skipped or (
                    candidate.candidate_id,
                    candidate.post_cid,
                ) in skipped_versions:
                    if on_skip is not None:
                        on_skip(candidate)
                    continue
                batch.append(candidate)
                if len(batch) == batch_size:
                    yield batch
                    batch = []
    except OSError as exc:
        raise CandidateInputError(str(exc)) from exc
    if batch:
        yield batch


async def classify_candidate_jsonl(
    service: ArtworkFilterService,
    input_path: str | Path,
    *,
    resume_decisions_path: str | Path | None = None,
) -> dict[str, int]:
    skip_keys = (
        completed_candidate_keys(
            resume_decisions_path,
            config_hash=service.config.config_hash,
            model_id=service.config.model.model_id,
            model_revision=(
                service.visual_classifier.model_revision
                if service.visual_classifier is not None
                else None
            ),
            prompt_version=(
                service.visual_classifier.prompt_version
                if service.visual_classifier is not None
                else None
            ),
        )
        if resume_decisions_path is not None
        else set()
    )
    counts = {
        "processed": 0,
        "accepted": 0,
        "review": 0,
        "rejected": 0,
        "errors": 0,
        "skipped": 0,
    }

    def record_skip(candidate: ImageCandidate) -> None:
        counts["skipped"] += 1

    for batch in iter_candidate_batches(
        input_path,
        batch_size=service.config.model.batch_size,
        skip_candidate_keys=skip_keys,
        on_skip=record_skip,
    ):
        results = await service.classify_many(batch)
        counts["processed"] += len(results)
        for result in results:
            if result.decision.value == "accept":
                counts["accepted"] += 1
            elif result.decision.value == "review":
                counts["review"] += 1
            elif result.decision.value == "reject":
                counts["rejected"] += 1
            else:
                counts["errors"] += 1
    return counts


def completed_candidate_ids(
    path: str | Path,
    *,
    config_hash: str,
    model_id: str,
    model_revision: str | None,
    prompt_version: str | None,
) -> set[str]:
    return {
        candidate_id
        for candidate_id, _ in completed_candidate_keys(
            path,
            config_hash=config_hash,
            model_id=model_id,
            model_revision=model_revision,
            prompt_version=prompt_version,
        )
    }


def completed_candidate_keys(
    path: str | Path,
    *,
    config_hash: str,
    model_id: str,
    model_revision: str | None,
    prompt_version: str | None,
) -> set[tuple[str, str | None]]:
    decision_path = Path(path)
    if not decision_path.exists():
        return set()
    completed: set[tuple[str, str | None]] = set()
    try:
        with decision_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                visual = row.get("visual_scores")
                row_revision = visual.get("model_revision") if isinstance(visual, dict) else None
                if (
                    row.get("config_hash") == config_hash
                    and row.get("model_version") == model_id
                    and row_revision == model_revision
                    and row.get("prompt_version") == prompt_version
                    and row.get("decision") in {"accept", "review", "reject"}
                    and isinstance(row.get("candidate_id"), str)
                ):
                    source_cid = row.get("source_cid")
                    if source_cid is None or isinstance(source_cid, str):
                        completed.add((row["candidate_id"], source_cid))
    except OSError as exc:
        raise CandidateInputError(str(exc)) from exc
    return completed
