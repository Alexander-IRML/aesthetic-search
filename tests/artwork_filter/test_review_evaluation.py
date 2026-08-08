from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from artsearch.artwork_filter.config import load_artwork_filter_config
from artsearch.artwork_filter.enums import (
    ContentClass,
    CorpusInclusionLabel,
    FilterDecision,
    HumanContentLabel,
    ModelMode,
    OriginalWorkLabel,
    RuleDisposition,
)
from artsearch.artwork_filter.evaluation import calibrate_thresholds, evaluate_filter
from artsearch.artwork_filter.labels import JSONLLabelStore, import_review_csv, latest_labels
from artsearch.artwork_filter.review_export import export_review_queue
from artsearch.artwork_filter.schemas import (
    ArtworkLabel,
    ClassScore,
    FilterResult,
    ImageCandidate,
    RuleResult,
    VisualScores,
)


NOW = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
CONFIG = load_artwork_filter_config()


def test_human_content_labels_track_model_classes_plus_uncertain():
    model_values = {content_class.value for content_class in ContentClass}
    human_values = {label.value for label in HumanContentLabel}

    assert human_values - {HumanContentLabel.UNCERTAIN.value} <= model_values
    assert ContentClass.UNKNOWN.value not in human_values


def test_review_export_prioritizes_boundary_and_imports_append_only_labels(tmp_path):
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    review_path = tmp_path / "review.csv"
    labels_path = tmp_path / "labels.jsonl"
    candidates = [
        _candidate("near", created_at=NOW),
        _candidate("far", created_at=NOW + timedelta(minutes=1)),
    ]
    decisions = [
        _result("near", decision=FilterDecision.REVIEW, score=0.77, processed_at=NOW),
        _result("far", decision=FilterDecision.REVIEW, score=0.50, processed_at=NOW),
    ]
    _write_jsonl(candidates_path, candidates)
    _write_jsonl(decisions_path, decisions)

    counts = export_review_queue(
        candidates_path,
        decisions_path,
        review_path,
        accept_score=0.78,
        reject_score=0.35,
    )

    assert counts["exported"] == 2
    with review_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["candidate_id"] for row in rows] == ["near", "far"]
    assert rows[0]["label_content_class"] == ""

    rows[0]["label_content_class"] = "finished_illustration"
    rows[0]["label_is_original_artist_work"] = "yes"
    rows[0]["label_include_in_main_corpus"] = "yes"
    with review_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    first = import_review_csv(review_path, labels_path, annotator="tester")
    second = import_review_csv(review_path, labels_path, annotator="tester")

    assert first == {
        "rows_seen": 2,
        "complete": 1,
        "incomplete": 1,
        "appended": 1,
        "duplicates_skipped": 0,
    }
    assert second["appended"] == 0
    assert second["duplicates_skipped"] == 1
    label = latest_labels(labels_path)["near"]
    assert label.content_class == HumanContentLabel.FINISHED_ILLUSTRATION
    assert label.source_decision_processed_at == NOW


def test_evaluation_uses_the_decision_snapshot_recorded_by_the_label(tmp_path):
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    labels_path = tmp_path / "labels.jsonl"
    _write_jsonl(candidates_path, [_candidate("one"), _candidate("two")])
    old = _result("one", decision=FilterDecision.REVIEW, score=0.7, processed_at=NOW)
    new = _result(
        "one",
        decision=FilterDecision.REJECT,
        score=0.2,
        processed_at=NOW + timedelta(hours=1),
    )
    false_accept = _result(
        "two",
        decision=FilterDecision.ACCEPT,
        score=0.9,
        processed_at=NOW,
    )
    _write_jsonl(decisions_path, [old, new, false_accept])
    store = JSONLLabelStore(labels_path)
    store.append_many(
        [
            _label(
                "one",
                content_class=HumanContentLabel.FINISHED_ILLUSTRATION,
                inclusion=CorpusInclusionLabel.YES,
                decision=old,
            ),
            _label(
                "two",
                content_class=HumanContentLabel.CASUAL_PHOTO,
                inclusion=CorpusInclusionLabel.NO,
                decision=false_accept,
            ),
        ]
    )

    report = evaluate_filter(candidates_path, decisions_path, labels_path)

    assert report["dataset"]["matched"] == 2
    assert report["decision_metrics"]["automatic_accept_count"] == 1
    assert report["decision_metrics"]["automatic_accept_precision"] == 0.0
    assert report["decision_metrics"]["review_rate"] == 0.5


def test_calibration_recommends_non_overlapping_thresholds(tmp_path):
    candidates_path = tmp_path / "candidates.jsonl"
    decisions_path = tmp_path / "decisions.jsonl"
    labels_path = tmp_path / "labels.jsonl"
    _write_jsonl(candidates_path, [_candidate("art"), _candidate("noise")])
    art = _result("art", decision=FilterDecision.ACCEPT, score=0.9, processed_at=NOW)
    noise = _result("noise", decision=FilterDecision.REVIEW, score=0.4, processed_at=NOW)
    _write_jsonl(decisions_path, [art, noise])
    JSONLLabelStore(labels_path).append_many(
        [
            _label(
                "art",
                content_class=HumanContentLabel.FINISHED_ILLUSTRATION,
                inclusion=CorpusInclusionLabel.YES,
                decision=art,
            ),
            _label(
                "noise",
                content_class=HumanContentLabel.CASUAL_PHOTO,
                inclusion=CorpusInclusionLabel.NO,
                decision=noise,
            ),
        ]
    )

    report = calibrate_thresholds(
        candidates_path,
        decisions_path,
        labels_path,
        CONFIG,
        target_accept_precision=0.2,
        target_reject_precision=0.2,
        minimum_decisions=1,
    )

    recommendation = report["recommendation"]
    assert recommendation["accept_score"] == 0.41
    assert recommendation["reject_score"] == 0.4
    assert recommendation["ready"] is True


def _candidate(candidate_id: str, *, created_at: datetime = NOW) -> ImageCandidate:
    return ImageCandidate(
        candidate_id=candidate_id,
        author_did=f"did:example:{candidate_id}",
        author_handle=f"{candidate_id}.example",
        post_uri=f"at://did:example:{candidate_id}/app.bsky.feed.post/1",
        image_index=0,
        local_path=Path(f"{candidate_id}.jpg"),
        post_text="test post",
        alt_text="test image",
        created_at=created_at,
        source="test",
    )


def _result(
    candidate_id: str,
    *,
    decision: FilterDecision,
    score: float,
    processed_at: datetime,
) -> FilterResult:
    return FilterResult(
        candidate_id=candidate_id,
        decision=decision,
        predicted_class=ContentClass.FINISHED_ILLUSTRATION,
        accepted_for_main_corpus=decision == FilterDecision.ACCEPT,
        route="main_art" if decision == FilterDecision.ACCEPT else decision.value,
        final_score=score,
        confidence=0.9,
        reason_codes=[f"test.{decision.value}"],
        visual_scores=VisualScores(
            backend="fake",
            model_id="fake/model",
            model_revision="revision",
            mode=ModelMode.ZERO_SHOT,
            class_scores=[
                ClassScore(content_class=ContentClass.FINISHED_ILLUSTRATION, score=0.9),
                ClassScore(content_class=ContentClass.CASUAL_PHOTO, score=0.1),
            ],
            art_utility_score=score,
            noise_score=1.0 - score,
            confidence_margin=0.8,
            embedding_dimension=2,
        ),
        rule_result=RuleResult(disposition=RuleDisposition.CONTINUE),
        model_version=CONFIG.model.model_id,
        config_version="1.0.0",
        prompt_version="prompts-v1",
        processed_at=processed_at,
        duration_ms=1.0,
        source_uri=f"at://did:example:{candidate_id}/app.bsky.feed.post/1",
        author_did=f"did:example:{candidate_id}",
        image_index=0,
        config_hash=CONFIG.config_hash,
        software_version="test",
    )


def _label(
    candidate_id: str,
    *,
    content_class: HumanContentLabel,
    inclusion: CorpusInclusionLabel,
    decision: FilterResult,
) -> ArtworkLabel:
    return ArtworkLabel(
        candidate_id=candidate_id,
        content_class=content_class,
        is_original_artist_work=OriginalWorkLabel.YES,
        include_in_main_corpus=inclusion,
        annotator="tester",
        source_decision_processed_at=decision.processed_at,
        source_model_version=decision.model_version,
        source_config_hash=decision.config_hash,
        previous_model_prediction=decision.predicted_class,
    )


def _write_jsonl(path: Path, rows: list[object]) -> None:
    path.write_text(
        "".join(
            json.dumps(row.model_dump(mode="json"), sort_keys=True) + "\n"  # type: ignore[attr-defined]
            for row in rows
        ),
        encoding="utf-8",
    )
