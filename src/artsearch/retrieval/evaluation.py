from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
import json
from math import log2
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from artsearch.retrieval.search import RetrievalMode


class RetrievalJudgment(BaseModel):
    """One immutable relevance judgment tied to an exact ranked model output."""

    schema_version: Literal["1.0"] = "1.0"
    judgment_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    dashboard_id: str = Field(min_length=1)
    review_session_id: str | None = None
    review_seed: str | None = None
    corpus_fingerprint: str = Field(min_length=1)
    query_artwork_id: str = Field(min_length=1)
    candidate_artwork_id: str = Field(min_length=1)
    retrieval_mode: RetrievalMode
    task: Literal["ensemble", "subject", "style", "local_detail"]
    relevant: bool
    rank: int = Field(gt=0)
    result_count: int = Field(gt=0)
    score: float
    pooled_score: float | None = None
    pooled_rank: int | None = Field(default=None, gt=0)
    patch_score: float | None = None
    patch_rank: int | None = Field(default=None, gt=0)
    clip_score: float | None = None
    clip_rank: int | None = Field(default=None, gt=0)
    shortlist_size: int | None = Field(default=None, gt=0)
    candidate_count: int | None = Field(default=None, gt=0)
    patch_match_top_n: int | None = Field(default=None, gt=0)
    query_artist_id: str = ""
    candidate_artist_id: str = ""
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    annotator: str = Field(default="local-reviewer", min_length=1)
    note: str = ""
    labeled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("score", "pooled_score", "patch_score", "clip_score")
    @classmethod
    def _finite_score(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("score must be finite")
        return value

    @field_validator("annotator", "note", mode="before")
    @classmethod
    def _trim_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()


def load_retrieval_judgments(path: str | Path) -> list[RetrievalJudgment]:
    source = Path(path)
    judgments: list[RetrievalJudgment] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                judgments.append(RetrievalJudgment.model_validate_json(line))
            except ValueError as exc:
                raise ValueError(
                    f"invalid retrieval judgment at {source}:{line_number}: {exc}"
                ) from exc
    return judgments


def latest_retrieval_judgments(
    judgments: Iterable[RetrievalJudgment],
) -> list[RetrievalJudgment]:
    latest: dict[tuple[str, RetrievalMode, str, str], RetrievalJudgment] = {}
    for judgment in judgments:
        key = (
            judgment.corpus_fingerprint,
            judgment.retrieval_mode,
            judgment.query_artwork_id,
            judgment.candidate_artwork_id,
        )
        previous = latest.get(key)
        if previous is None or judgment.labeled_at >= previous.labeled_at:
            latest[key] = judgment
    return sorted(
        latest.values(),
        key=lambda item: (
            item.retrieval_mode.value,
            item.query_artwork_id,
            item.rank,
            item.candidate_artwork_id,
        ),
    )


def evaluate_retrieval_judgments(
    judgments_or_path: Sequence[RetrievalJudgment] | str | Path,
) -> dict[str, object]:
    if isinstance(judgments_or_path, (str, Path)):
        history = load_retrieval_judgments(judgments_or_path)
    else:
        history = list(judgments_or_path)
    judgments = latest_retrieval_judgments(history)
    fingerprints = sorted({item.corpus_fingerprint for item in judgments})
    review_session_ids = sorted(
        {item.review_session_id for item in judgments if item.review_session_id}
    )
    review_seeds = sorted({item.review_seed for item in judgments if item.review_seed})
    per_mode = {
        mode.value: _mode_metrics(
            [item for item in judgments if item.retrieval_mode == mode],
        )
        for mode in RetrievalMode
    }
    return {
        "schema_version": "1.0",
        "dataset": {
            "history_events": len(history),
            "latest_judgments": len(judgments),
            "corpus_fingerprints": fingerprints,
            "mixed_corpora": len(fingerprints) > 1,
            "query_count": len({item.query_artwork_id for item in judgments}),
            "review_session_ids": review_session_ids,
            "review_seeds": review_seeds,
        },
        "overall": _mode_metrics(judgments),
        "per_mode": per_mode,
    }


def write_retrieval_evaluation(report: dict[str, object], path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination


def _mode_metrics(judgments: Sequence[RetrievalJudgment]) -> dict[str, object]:
    by_query: dict[str, list[RetrievalJudgment]] = defaultdict(list)
    for judgment in judgments:
        by_query[judgment.query_artwork_id].append(judgment)
    for query_judgments in by_query.values():
        query_judgments.sort(key=lambda item: item.rank)

    relevant = sum(item.relevant for item in judgments)
    nonrelevant = len(judgments) - relevant
    expected = sum(
        max(item.result_count for item in query_judgments)
        for query_judgments in by_query.values()
    )
    positive_scores = [item.score for item in judgments if item.relevant]
    negative_scores = [item.score for item in judgments if not item.relevant]
    prefix_metrics = {
        str(k): _prefix_metrics(by_query, k)
        for k in (1, 5, 10)
    }
    reciprocal_ranks = [_reciprocal_rank(items) for items in by_query.values()]
    average_precisions = [_average_precision(items) for items in by_query.values()]
    ndcgs = [_ndcg(items) for items in by_query.values()]
    return {
        "judgments": len(judgments),
        "query_count": len(by_query),
        "relevant": relevant,
        "not_relevant": nonrelevant,
        "annotation_coverage": _ratio(len(judgments), expected),
        "judged_precision": _ratio(relevant, len(judgments)),
        "mean_relevant_score": _mean(positive_scores),
        "mean_not_relevant_score": _mean(negative_scores),
        "mrr": _mean(reciprocal_ranks),
        "map": _mean(average_precisions),
        "ndcg": _mean(ndcgs),
        "at_k": prefix_metrics,
        "retrieved_confusion": {
            "true_positive": relevant,
            "false_positive": nonrelevant,
            "true_negative": None,
            "false_negative": None,
        },
        "score_calibration": _score_calibration(judgments),
    }


def _prefix_metrics(
    by_query: dict[str, list[RetrievalJudgment]],
    k: int,
) -> dict[str, float | int | None]:
    prefix = [
        item
        for query_judgments in by_query.values()
        for item in query_judgments
        if item.rank <= k
    ]
    relevant = sum(item.relevant for item in prefix)
    expected = sum(
        min(k, max(item.result_count for item in query_judgments))
        for query_judgments in by_query.values()
    )
    query_hits = sum(
        any(item.relevant and item.rank <= k for item in query_judgments)
        for query_judgments in by_query.values()
    )
    pool_recalls = [
        _pool_recall(query_judgments, k)
        for query_judgments in by_query.values()
        if any(item.relevant for item in query_judgments)
    ]
    return {
        "judgments": len(prefix),
        "relevant": relevant,
        "coverage": _ratio(len(prefix), expected),
        "judged_precision": _ratio(relevant, len(prefix)),
        "judged_query_hit_rate": _ratio(query_hits, len(by_query)),
        "judged_pool_recall": _mean(pool_recalls),
    }


def _reciprocal_rank(judgments: Sequence[RetrievalJudgment]) -> float:
    relevant_ranks = [item.rank for item in judgments if item.relevant]
    return 0.0 if not relevant_ranks else 1.0 / min(relevant_ranks)


def _average_precision(judgments: Sequence[RetrievalJudgment]) -> float:
    relevant_total = sum(item.relevant for item in judgments)
    if not relevant_total:
        return 0.0
    relevant_seen = 0
    precision_sum = 0.0
    for item in judgments:
        if not item.relevant:
            continue
        relevant_seen += 1
        precision_sum += relevant_seen / item.rank
    return precision_sum / relevant_total


def _ndcg(judgments: Sequence[RetrievalJudgment]) -> float:
    relevant_total = sum(item.relevant for item in judgments)
    if not relevant_total:
        return 0.0
    dcg = sum(1.0 / log2(item.rank + 1) for item in judgments if item.relevant)
    ideal = sum(1.0 / log2(rank + 1) for rank in range(1, relevant_total + 1))
    return dcg / ideal


def _pool_recall(judgments: Sequence[RetrievalJudgment], k: int) -> float:
    relevant_total = sum(item.relevant for item in judgments)
    if not relevant_total:
        return 0.0
    return sum(item.relevant and item.rank <= k for item in judgments) / relevant_total


def _score_calibration(
    judgments: Sequence[RetrievalJudgment],
) -> list[dict[str, float | int | None]]:
    rows = []
    for index in range(5):
        lower = -1.0 + index * 0.4
        upper = lower + 0.4
        bucket = [
            item
            for item in judgments
            if lower <= item.score < upper or (index == 4 and item.score == upper)
        ]
        rows.append(
            {
                "score_min": lower,
                "score_max": upper,
                "judgments": len(bucket),
                "relevant": sum(item.relevant for item in bucket),
                "precision": _ratio(sum(item.relevant for item in bucket), len(bucket)),
            }
        )
    return rows


def _ratio(numerator: float, denominator: float) -> float | None:
    return None if not denominator else numerator / denominator


def _mean(values: Sequence[float]) -> float | None:
    return None if not values else sum(values) / len(values)
