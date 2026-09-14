from __future__ import annotations

import pytest

from artsearch.postgres.metrics import calculate_feedback_metrics, empty_product_metrics


def test_feedback_metrics_require_complete_top_ten_for_precision_and_ndcg() -> None:
    rows = []
    for rank in range(1, 11):
        rows.append(
            _row(
                "query-a",
                rank,
                "great" if rank == 1 else "good" if rank == 2 else "bad",
            )
        )
        rows.append(
            _row(
                "query-b",
                rank,
                "great" if rank == 10 else "good" if rank == 9 else "bad",
            )
        )
    rows.append(_row("query-partial", 1, "good"))

    metrics = calculate_feedback_metrics(rows, top_ten_impressions=30)

    assert metrics["fully_judged_queries"] == 2
    assert metrics["judged_queries"] == 3
    assert metrics["great"] == 2
    assert metrics["good"] == 3
    assert metrics["bad"] == 16
    assert metrics["hits"] == 5
    assert metrics["misses"] == 16
    assert metrics["judged_hit_rate"] == pytest.approx(5 / 21)
    assert metrics["precision_at_10"] == pytest.approx(0.2)
    assert 0.5 < metrics["ndcg_at_10"] < 1.0
    assert metrics["annotation_coverage_at_10"] == pytest.approx(0.7)


def test_feedback_metrics_use_the_latest_grade_per_query_result() -> None:
    rows = [
        _row("query-a", 1, "great"),
        _row("query-a", 1, "bad"),
    ]

    metrics = calculate_feedback_metrics(rows, top_ten_impressions=10)

    assert metrics["latest_judgments"] == 1
    assert metrics["great"] == 0
    assert metrics["bad"] == 1
    assert metrics["precision_at_10"] is None
    assert metrics["ndcg_at_10"] is None


def test_empty_product_metrics_include_privacy_preserving_visitor_counts() -> None:
    metrics = empty_product_metrics(status="disabled", window_days=7)

    assert metrics["visitors"] == {
        "total_unique_ips": 0,
        "unique_ips_last_24h": 0,
        "window_unique_ips": 0,
        "latest_visit_at": None,
        "identifier": "hmac_sha256_ip",
    }


def _row(query_id: str, rank: int, grade: str) -> dict[str, object]:
    return {
        "query_id": query_id,
        "artwork_id": f"{query_id}-art-{rank}",
        "result_rank": rank,
        "result_count": 10,
        "grade": grade,
    }
