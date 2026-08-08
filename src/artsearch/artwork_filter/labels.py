from __future__ import annotations

from collections.abc import Iterable, Sequence
import csv
from datetime import datetime
import json
import os
from pathlib import Path

from pydantic import ValidationError

from artsearch.artwork_filter.enums import (
    ContentClass,
    CorpusInclusionLabel,
    HumanContentLabel,
    OriginalWorkLabel,
)
from artsearch.artwork_filter.errors import CandidateInputError, PersistenceError
from artsearch.artwork_filter.hashing import stable_json_hash
from artsearch.artwork_filter.schemas import ArtworkLabel


LABEL_COLUMNS = (
    "label_content_class",
    "label_is_original_artist_work",
    "label_include_in_main_corpus",
    "label_annotator_note",
)


class JSONLLabelStore:
    """Append immutable annotations while suppressing exact repeated imports."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, label: ArtworkLabel) -> bool:
        return self.append_many([label]) == 1

    def append_many(self, labels: Sequence[ArtworkLabel]) -> int:
        known_hashes = {label.annotation_hash for label in iter_label_history(self.path)}
        pending: list[ArtworkLabel] = []
        for label in labels:
            prepared = with_annotation_hash(label)
            if prepared.annotation_hash in known_hashes:
                continue
            known_hashes.add(prepared.annotation_hash)
            pending.append(prepared)
        if not pending:
            return 0

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                for label in pending:
                    handle.write(json.dumps(label.model_dump(mode="json"), sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PersistenceError(str(exc)) from exc
        return len(pending)


def with_annotation_hash(label: ArtworkLabel) -> ArtworkLabel:
    payload = label.model_dump(
        mode="json",
        exclude={"label_id", "labeled_at", "annotation_hash"},
    )
    return label.model_copy(update={"annotation_hash": stable_json_hash(payload)})


def iter_label_history(path: str | Path) -> Iterable[ArtworkLabel]:
    label_path = Path(path)
    if not label_path.exists():
        return
    try:
        with label_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    label = ArtworkLabel.model_validate_json(line)
                except (ValidationError, ValueError) as exc:
                    raise CandidateInputError(
                        f"invalid label JSONL at {label_path}:{line_number}: {exc}"
                    ) from exc
                yield with_annotation_hash(label)
    except OSError as exc:
        raise CandidateInputError(str(exc)) from exc


def latest_labels(path: str | Path) -> dict[str, ArtworkLabel]:
    latest: dict[str, ArtworkLabel] = {}
    for label in iter_label_history(path):
        current = latest.get(label.candidate_id)
        if current is None or label.labeled_at >= current.labeled_at:
            latest[label.candidate_id] = label
    return latest


def import_review_csv(
    input_path: str | Path,
    output_path: str | Path,
    *,
    annotator: str,
) -> dict[str, int]:
    if not annotator.strip():
        raise ValueError("annotator must not be empty")
    rows_seen = 0
    incomplete = 0
    labels: list[ArtworkLabel] = []
    review_path = Path(input_path)
    try:
        with review_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            _validate_review_columns(reader.fieldnames)
            for row_number, row in enumerate(reader, start=2):
                rows_seen += 1
                required = [
                    row.get("label_content_class", "").strip(),
                    row.get("label_is_original_artist_work", "").strip(),
                    row.get("label_include_in_main_corpus", "").strip(),
                ]
                if not any(required):
                    incomplete += 1
                    continue
                if not all(required):
                    raise CandidateInputError(
                        f"partially completed annotation at {review_path}:{row_number}"
                    )
                try:
                    labels.append(_label_from_review_row(row, annotator=annotator))
                except (ValidationError, ValueError) as exc:
                    raise CandidateInputError(
                        f"invalid annotation at {review_path}:{row_number}: {exc}"
                    ) from exc
    except OSError as exc:
        raise CandidateInputError(str(exc)) from exc

    appended = JSONLLabelStore(output_path).append_many(labels)
    return {
        "rows_seen": rows_seen,
        "complete": len(labels),
        "incomplete": incomplete,
        "appended": appended,
        "duplicates_skipped": len(labels) - appended,
    }


def _validate_review_columns(fieldnames: list[str] | None) -> None:
    available = set(fieldnames or [])
    required = {"candidate_id", *LABEL_COLUMNS[:3]}
    missing = sorted(required - available)
    if missing:
        raise CandidateInputError(f"review CSV is missing columns: {', '.join(missing)}")


def _label_from_review_row(row: dict[str, str], *, annotator: str) -> ArtworkLabel:
    prediction = row.get("predicted_class", "").strip()
    return ArtworkLabel(
        candidate_id=row["candidate_id"].strip(),
        content_class=HumanContentLabel(row["label_content_class"].strip()),
        is_original_artist_work=OriginalWorkLabel(
            row["label_is_original_artist_work"].strip()
        ),
        include_in_main_corpus=CorpusInclusionLabel(
            row["label_include_in_main_corpus"].strip()
        ),
        annotator=annotator,
        annotator_note=row.get("label_annotator_note", ""),
        source_decision_processed_at=_parse_datetime(row.get("processed_at")),
        source_model_version=row.get("model_version") or None,
        source_config_hash=row.get("config_hash") or None,
        previous_model_prediction=ContentClass(prediction) if prediction else None,
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
