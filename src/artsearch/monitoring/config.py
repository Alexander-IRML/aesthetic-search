from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tomllib

from pydantic import BaseModel, Field, field_validator, model_validator


class MonitoringTargetConfig(BaseModel):
    embedding_coverage_warning: float = 0.99
    embedding_coverage_critical: float = 0.95
    filter_error_rate_warning: float = 0.02
    filter_error_rate_critical: float = 0.05
    filter_accept_precision: float = 0.95
    minimum_filter_labels: int = 100
    minimum_retrieval_judgments: int = 100
    minimum_retrieval_queries: int = 20
    retrieval_precision_at_5: float = 0.70
    retrieval_ndcg: float = 0.75
    ann_mean_recall_at_k: float = 0.95
    ann_minimum_recall_at_k: float = 0.90
    filter_p95_latency_ms: float = 15_000
    search_p95_latency_ms: float = 5_000
    search_error_rate_warning: float = 0.05
    search_error_rate_critical: float = 0.15
    feedback_precision_at_10: float = 0.50
    feedback_ndcg_at_10: float = 0.65
    minimum_complete_feedback_queries: int = 10
    pipeline_success_rate_warning: float = 0.95
    decision_freshness_days: int = 7
    minimum_drift_cohort: int = 50
    class_distribution_js_warning: float = 0.10
    class_distribution_js_critical: float = 0.20
    maximum_top_artist_share: float = 0.25

    @model_validator(mode="after")
    def _validate_targets(self) -> "MonitoringTargetConfig":
        ratios = {
            "embedding_coverage_warning": self.embedding_coverage_warning,
            "embedding_coverage_critical": self.embedding_coverage_critical,
            "filter_error_rate_warning": self.filter_error_rate_warning,
            "filter_error_rate_critical": self.filter_error_rate_critical,
            "filter_accept_precision": self.filter_accept_precision,
            "retrieval_precision_at_5": self.retrieval_precision_at_5,
            "retrieval_ndcg": self.retrieval_ndcg,
            "ann_mean_recall_at_k": self.ann_mean_recall_at_k,
            "ann_minimum_recall_at_k": self.ann_minimum_recall_at_k,
            "search_error_rate_warning": self.search_error_rate_warning,
            "search_error_rate_critical": self.search_error_rate_critical,
            "feedback_precision_at_10": self.feedback_precision_at_10,
            "feedback_ndcg_at_10": self.feedback_ndcg_at_10,
            "pipeline_success_rate_warning": self.pipeline_success_rate_warning,
            "class_distribution_js_warning": self.class_distribution_js_warning,
            "class_distribution_js_critical": self.class_distribution_js_critical,
            "maximum_top_artist_share": self.maximum_top_artist_share,
        }
        if any(not 0.0 <= value <= 1.0 for value in ratios.values()):
            raise ValueError("monitoring ratio targets must be within [0, 1]")
        if self.embedding_coverage_critical > self.embedding_coverage_warning:
            raise ValueError("critical embedding coverage must not exceed warning coverage")
        if self.filter_error_rate_warning > self.filter_error_rate_critical:
            raise ValueError("warning filter error rate must not exceed critical rate")
        if self.search_error_rate_warning > self.search_error_rate_critical:
            raise ValueError("warning search error rate must not exceed critical rate")
        if self.class_distribution_js_warning > self.class_distribution_js_critical:
            raise ValueError("warning class drift must not exceed critical class drift")
        counts = (
            self.minimum_filter_labels,
            self.minimum_retrieval_judgments,
            self.minimum_retrieval_queries,
            self.minimum_complete_feedback_queries,
            self.decision_freshness_days,
            self.minimum_drift_cohort,
        )
        if any(value <= 0 for value in counts):
            raise ValueError("monitoring count and freshness targets must be positive")
        if self.filter_p95_latency_ms <= 0 or self.search_p95_latency_ms <= 0:
            raise ValueError("filter and search p95 latency targets must be positive")
        return self


class HumanReviewConfig(BaseModel):
    enabled: bool = True
    filter_queue_limit: int = 250
    retrieval_result_count: int = 8
    media_jpeg_quality: int = 88
    conceal_media_by_default: bool = False

    @model_validator(mode="after")
    def _validate_review(self) -> "HumanReviewConfig":
        if self.filter_queue_limit <= 0:
            raise ValueError("review.filter_queue_limit must be positive")
        if self.retrieval_result_count <= 0:
            raise ValueError("review.retrieval_result_count must be positive")
        if not 1 <= self.media_jpeg_quality <= 100:
            raise ValueError("review.media_jpeg_quality must be between 1 and 100")
        return self


class MonitoringConfig(BaseModel):
    version: str = "1.0.0"
    refresh_seconds: int = 30
    snapshot_cache_seconds: int = 5
    qdrant_probe_timeout_seconds: float = 1.0
    drift_recent_days: int = 7
    drift_baseline_days: int = 30
    product_metrics_window_days: int = 30
    targets: MonitoringTargetConfig = Field(default_factory=MonitoringTargetConfig)
    review: HumanReviewConfig = Field(default_factory=HumanReviewConfig)
    config_hash: str = ""

    @field_validator("version")
    @classmethod
    def _version_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("monitoring config version is required")
        return value.strip()

    @model_validator(mode="after")
    def _validate_runtime(self) -> "MonitoringConfig":
        if self.refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        if self.snapshot_cache_seconds < 0:
            raise ValueError("snapshot_cache_seconds must be non-negative")
        if self.qdrant_probe_timeout_seconds <= 0:
            raise ValueError("qdrant_probe_timeout_seconds must be positive")
        if self.drift_recent_days <= 0 or self.drift_baseline_days <= 0:
            raise ValueError("drift cohort windows must be positive")
        if self.product_metrics_window_days <= 0:
            raise ValueError("product_metrics_window_days must be positive")
        return self


def load_monitoring_config(
    path: str | Path = "configs/model_dashboard.default.toml",
) -> MonitoringConfig:
    source = Path(path)
    with source.open("rb") as handle:
        raw = tomllib.load(handle)
    config = MonitoringConfig.model_validate(raw)
    payload = config.model_dump(mode="json", exclude={"config_hash"})
    config.config_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return config
