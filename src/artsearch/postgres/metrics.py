from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from math import log2
from typing import Any

from sqlalchemy import Engine, text


FEEDBACK_RELEVANCE = {"bad": 0, "good": 1, "great": 2}


def calculate_feedback_metrics(
    rows: Iterable[Mapping[str, Any]],
    *,
    top_ten_impressions: int,
) -> dict[str, Any]:
    """Calculate complete-query ranking metrics without filling missing labels as misses."""

    latest_by_result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        query_id = str(row["query_id"])
        artwork_id = str(row["artwork_id"])
        grade = str(row.get("grade") or "").lower()
        if grade not in FEEDBACK_RELEVANCE:
            continue
        rank = int(row["result_rank"])
        if rank <= 0:
            continue
        latest_by_result[(query_id, artwork_id)] = {
            "query_id": query_id,
            "artwork_id": artwork_id,
            "result_rank": rank,
            "result_count": max(0, int(row.get("result_count") or 0)),
            "grade": grade,
            "relevance": FEEDBACK_RELEVANCE[grade],
        }

    judgments = list(latest_by_result.values())
    grades = Counter(str(item["grade"]) for item in judgments)
    top_ten = [item for item in judgments if int(item["result_rank"]) <= 10]
    hit_count = sum(int(item["relevance"]) > 0 for item in top_ten)
    miss_count = sum(int(item["relevance"]) == 0 for item in top_ten)

    by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in top_ten:
        by_query[str(item["query_id"])].append(item)

    precision_values: list[float] = []
    ndcg_values: list[float] = []
    fully_judged_results = 0
    for query_rows in by_query.values():
        result_count = max(int(item["result_count"]) for item in query_rows)
        expected_count = min(10, result_count)
        if expected_count <= 0:
            continue
        relevance_by_rank = {
            int(item["result_rank"]): int(item["relevance"]) for item in query_rows
        }
        if set(relevance_by_rank) != set(range(1, expected_count + 1)):
            continue
        relevance = [relevance_by_rank.get(rank, 0) for rank in range(1, 11)]
        precision_values.append(sum(value > 0 for value in relevance) / 10.0)
        ndcg_values.append(_ndcg_at_ten(relevance))
        fully_judged_results += expected_count

    judgment_count = len(top_ten)
    coverage = min(judgment_count / top_ten_impressions, 1.0) if top_ten_impressions > 0 else None
    return {
        "latest_judgments": len(judgments),
        "top_ten_judgments": judgment_count,
        "judged_queries": len(by_query),
        "fully_judged_queries": len(precision_values),
        "fully_judged_results": fully_judged_results,
        "great": grades["great"],
        "good": grades["good"],
        "bad": grades["bad"],
        "hits": hit_count,
        "misses": miss_count,
        "judged_hit_rate": _ratio(hit_count, judgment_count),
        "precision_at_10": _mean(precision_values),
        "ndcg_at_10": _mean(ndcg_values),
        "annotation_coverage_at_10": coverage,
        "top_ten_impressions": top_ten_impressions,
    }


class ProductMetricsRepository:
    """Read aggregate online-search and ingestion health without exposing user data."""

    def __init__(self, engine: Engine, *, window_days: int = 30) -> None:
        if window_days <= 0:
            raise ValueError("window_days must be positive")
        self.engine = engine
        self.window_days = window_days

    def snapshot(self) -> dict[str, Any]:
        window_start = datetime.now(timezone.utc) - timedelta(days=self.window_days)
        with self.engine.connect() as connection:
            search_row = (
                connection.execute(
                    text(
                        """
                    SELECT COUNT(*) AS total_searches,
                           COUNT(*) FILTER (WHERE status = 'completed') AS total_completed,
                           COUNT(*) FILTER (WHERE status = 'error') AS total_errors,
                           COUNT(*) FILTER (WHERE status = 'started') AS total_in_flight,
                           COUNT(*) FILTER (
                               WHERE created_at >= now() - interval '24 hours'
                           ) AS searches_last_24h,
                           COUNT(*) FILTER (
                               WHERE created_at >= :window_start
                           ) AS window_searches,
                           COUNT(*) FILTER (
                               WHERE created_at >= :window_start AND status = 'completed'
                           ) AS window_completed,
                           COUNT(*) FILTER (
                               WHERE created_at >= :window_start AND status = 'error'
                           ) AS window_errors,
                           percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms)
                               FILTER (
                                   WHERE created_at >= :window_start
                                     AND status = 'completed'
                                     AND latency_ms IS NOT NULL
                               ) AS latency_p50_ms,
                           percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                               FILTER (
                                   WHERE created_at >= :window_start
                                     AND status = 'completed'
                                     AND latency_ms IS NOT NULL
                               ) AS latency_p95_ms,
                           MAX(completed_at) FILTER (WHERE status = 'completed')
                               AS latest_completed_at
                      FROM product.queries
                    """
                    ),
                    {"window_start": window_start},
                )
                .mappings()
                .one()
            )
            impression_count = int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                          FROM product.result_impressions i
                          JOIN product.queries q ON q.query_id = i.query_id
                         WHERE q.created_at >= :window_start
                           AND q.status = 'completed'
                           AND i.result_rank <= 10
                        """
                    ),
                    {"window_start": window_start},
                ).scalar_one()
                or 0
            )
            feedback_rows = connection.execute(
                text(
                    """
                    SELECT query_id, artwork_id, result_rank, result_count, grade
                      FROM (
                        SELECT DISTINCT ON (e.query_id, e.artwork_id)
                               e.query_id,
                               e.artwork_id,
                               e.result_rank,
                               q.result_count,
                               e.metadata ->> 'grade' AS grade
                          FROM product.interaction_events e
                          JOIN product.queries q ON q.query_id = e.query_id
                         WHERE e.interaction_type = 'relevance_feedback'
                           AND q.created_at >= :window_start
                           AND q.status = 'completed'
                         ORDER BY e.query_id, e.artwork_id,
                                  e.created_at DESC, e.interaction_id DESC
                      ) AS latest_feedback
                     ORDER BY query_id, result_rank
                    """
                ),
                {"window_start": window_start},
            ).mappings()
            feedback = calculate_feedback_metrics(
                feedback_rows,
                top_ten_impressions=impression_count,
            )
            pipeline = _pipeline_snapshot(connection, window_start=window_start)

        completed_or_error = int(search_row["window_completed"] or 0) + int(
            search_row["window_errors"] or 0
        )
        return {
            "status": "online",
            "window_days": self.window_days,
            "search": {
                "total_searches": int(search_row["total_searches"] or 0),
                "total_completed": int(search_row["total_completed"] or 0),
                "total_errors": int(search_row["total_errors"] or 0),
                "total_in_flight": int(search_row["total_in_flight"] or 0),
                "searches_last_24h": int(search_row["searches_last_24h"] or 0),
                "window_searches": int(search_row["window_searches"] or 0),
                "window_completed": int(search_row["window_completed"] or 0),
                "window_errors": int(search_row["window_errors"] or 0),
                "success_rate": _ratio(
                    int(search_row["window_completed"] or 0), completed_or_error
                ),
                "error_rate": _ratio(int(search_row["window_errors"] or 0), completed_or_error),
                "latency_p50_ms": _optional_float(search_row["latency_p50_ms"]),
                "latency_p95_ms": _optional_float(search_row["latency_p95_ms"]),
                "latest_completed_at": _iso(search_row["latest_completed_at"]),
            },
            "feedback": feedback,
            "pipeline": pipeline,
        }


def empty_product_metrics(
    *, status: str, window_days: int, error: str | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "window_days": window_days,
        "error": error,
        "search": {
            "total_searches": 0,
            "total_completed": 0,
            "total_errors": 0,
            "total_in_flight": 0,
            "searches_last_24h": 0,
            "window_searches": 0,
            "window_completed": 0,
            "window_errors": 0,
            "success_rate": None,
            "error_rate": None,
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "latest_completed_at": None,
        },
        "feedback": calculate_feedback_metrics((), top_ten_impressions=0),
        "pipeline": {
            "decisions": 0,
            "decision_errors": 0,
            "decision_success_rate": None,
            "routes": 0,
            "route_errors": 0,
            "route_success_rate": None,
            "runs": 0,
            "finished_runs": 0,
            "run_errors": 0,
            "run_success_rate": None,
            "latest_run_at": None,
        },
    }


def _pipeline_snapshot(connection: Any, *, window_start: datetime) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                """
            SELECT
              (SELECT COUNT(*) FROM ingest.artwork_filter_decisions
                WHERE processed_at >= :window_start) AS decisions,
              (SELECT COUNT(*) FROM ingest.artwork_filter_decisions
                WHERE processed_at >= :window_start AND decision = 'error')
                AS decision_errors,
              (SELECT COUNT(*) FROM ingest.artwork_filter_routes
                WHERE routed_at >= :window_start) AS routes,
              (SELECT COUNT(*) FROM ingest.artwork_filter_routes
                WHERE routed_at >= :window_start AND status = 'error') AS route_errors,
              (SELECT COUNT(*) FROM ingest.runs
                WHERE started_at >= :window_start) AS runs,
              (SELECT COUNT(*) FROM ingest.runs
                WHERE started_at >= :window_start AND finished_at IS NOT NULL)
                AS finished_runs,
              (SELECT COALESCE(SUM(errors_count), 0) FROM ingest.runs
                WHERE started_at >= :window_start) AS run_errors,
              (SELECT COUNT(*) FROM ingest.runs
                WHERE started_at >= :window_start AND finished_at IS NOT NULL
                  AND errors_count = 0) AS successful_runs,
              (SELECT MAX(started_at) FROM ingest.runs) AS latest_run_at
            """
            ),
            {"window_start": window_start},
        )
        .mappings()
        .one()
    )
    decisions = int(row["decisions"] or 0)
    decision_errors = int(row["decision_errors"] or 0)
    routes = int(row["routes"] or 0)
    route_errors = int(row["route_errors"] or 0)
    runs = int(row["runs"] or 0)
    finished_runs = int(row["finished_runs"] or 0)
    run_errors = int(row["run_errors"] or 0)
    successful_runs = int(row["successful_runs"] or 0)
    return {
        "decisions": decisions,
        "decision_errors": decision_errors,
        "decision_success_rate": _ratio(decisions - decision_errors, decisions),
        "routes": routes,
        "route_errors": route_errors,
        "route_success_rate": _ratio(routes - route_errors, routes),
        "runs": runs,
        "finished_runs": finished_runs,
        "run_errors": run_errors,
        "run_success_rate": _ratio(successful_runs, finished_runs),
        "latest_run_at": _iso(row["latest_run_at"]),
    }


def _ndcg_at_ten(relevance: list[int]) -> float:
    values = (relevance + [0] * 10)[:10]
    dcg = sum((2**value - 1) / log2(rank + 1) for rank, value in enumerate(values, 1))
    ideal = sorted(values, reverse=True)
    ideal_dcg = sum((2**value - 1) / log2(rank + 1) for rank, value in enumerate(ideal, 1))
    return dcg / ideal_dcg if ideal_dcg > 0 else 0.0


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
