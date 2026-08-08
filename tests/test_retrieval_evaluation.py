from datetime import datetime, timedelta, timezone
import json

from artsearch.retrieval.evaluation import (
    RetrievalJudgment,
    evaluate_retrieval_judgments,
    latest_retrieval_judgments,
    load_retrieval_judgments,
    write_retrieval_evaluation,
)
from artsearch.retrieval.search import RetrievalMode


def _judgment(
    candidate: str,
    *,
    relevant: bool,
    rank: int,
    mode: RetrievalMode = RetrievalMode.CLIP_SUBJECT,
    labeled_at: datetime | None = None,
) -> RetrievalJudgment:
    task = {
        RetrievalMode.ENSEMBLE: "ensemble",
        RetrievalMode.CLIP_SUBJECT: "subject",
        RetrievalMode.DINO_POOLED: "style",
        RetrievalMode.DINO_PATCH_MAXSIM: "local_detail",
    }[mode]
    return RetrievalJudgment(
        judgment_id=f"{candidate}-{rank}-{relevant}-{labeled_at}",
        dashboard_id="dashboard-1",
        review_session_id="review-01",
        review_seed="repeatable-review",
        corpus_fingerprint="corpus-1",
        query_artwork_id="query",
        candidate_artwork_id=candidate,
        retrieval_mode=mode,
        task=task,
        relevant=relevant,
        rank=rank,
        result_count=3,
        score=1.0 - rank / 10,
        model_id="model",
        model_revision="revision",
        labeled_at=labeled_at or datetime(2026, 7, 26, tzinfo=timezone.utc),
    )


def test_latest_retrieval_judgments_uses_newest_event():
    initial = datetime(2026, 7, 26, tzinfo=timezone.utc)
    latest = latest_retrieval_judgments(
        [
            _judgment("candidate", relevant=False, rank=1, labeled_at=initial),
            _judgment(
                "candidate",
                relevant=True,
                rank=1,
                labeled_at=initial + timedelta(minutes=1),
            ),
        ]
    )

    assert len(latest) == 1
    assert latest[0].relevant is True


def test_evaluate_retrieval_judgments_reports_rank_metrics():
    report = evaluate_retrieval_judgments(
        [
            _judgment("relevant-first", relevant=True, rank=1),
            _judgment("negative", relevant=False, rank=2),
            _judgment("relevant-third", relevant=True, rank=3),
        ]
    )

    metrics = report["per_mode"]["clip_subject"]
    assert metrics["annotation_coverage"] == 1.0
    assert metrics["judged_precision"] == 2 / 3
    assert metrics["mrr"] == 1.0
    assert metrics["at_k"]["1"]["judged_precision"] == 1.0
    assert metrics["at_k"]["5"]["judged_pool_recall"] == 1.0
    assert metrics["retrieved_confusion"] == {
        "true_positive": 2,
        "false_positive": 1,
        "true_negative": None,
        "false_negative": None,
    }
    assert report["dataset"]["review_session_ids"] == ["review-01"]
    assert report["dataset"]["review_seeds"] == ["repeatable-review"]


def test_retrieval_judgment_jsonl_round_trip(tmp_path):
    source = tmp_path / "judgments.jsonl"
    source.write_text(_judgment("candidate", relevant=True, rank=1).model_dump_json() + "\n")

    loaded = load_retrieval_judgments(source)
    report = evaluate_retrieval_judgments(loaded)
    output = write_retrieval_evaluation(report, tmp_path / "report.json")

    assert len(loaded) == 1
    assert loaded[0].review_session_id == "review-01"
    assert loaded[0].review_seed == "repeatable-review"
    assert json.loads(output.read_text())["dataset"]["latest_judgments"] == 1
