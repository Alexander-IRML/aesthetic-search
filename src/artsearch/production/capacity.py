from __future__ import annotations

from dataclasses import asdict, dataclass

from artsearch.production.config import FaissConfig, ScaleBudgetConfig


CAPACITY_PLAN_VERSION = "artsearch-capacity-plan-v1"
BYTES_PER_GIB = 1024**3
BYTES_PER_BILLING_GB = 1000**3


@dataclass(frozen=True)
class CapacityPlan:
    target_points: int
    monthly_budget_usd: float
    projected_monthly_usd: float
    monthly_headroom_usd: float
    required_headroom_usd: float
    promotion_ready: bool
    vector_snapshot_gib: float
    retained_vector_snapshot_gib: float
    hot_image_cache_count: int
    hot_image_cache_gib: float
    projected_object_storage_gib: float
    projected_object_storage_monthly_usd: float
    full_original_baseline_gib: float
    full_original_baseline_monthly_usd: float
    monthly_components_usd: dict[str, float]
    bytes_per_point: dict[str, int]
    assumptions: dict[str, object]
    schema_version: str = CAPACITY_PLAN_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def build_capacity_plan(
    faiss: FaissConfig,
    budget: ScaleBudgetConfig,
    *,
    target_points: int | None = None,
    average_cached_image_bytes: int | None = None,
    average_original_bytes: int = 723_000,
) -> CapacityPlan:
    """Estimate the configured steady-state bill and enforce reserved headroom."""

    points = budget.target_points if target_points is None else target_points
    cached_bytes = (
        budget.average_cached_image_bytes
        if average_cached_image_bytes is None
        else average_cached_image_bytes
    )
    if points <= 0 or cached_bytes <= 0 or average_original_bytes <= 0:
        raise ValueError("capacity point and byte assumptions must be positive")

    pq_code_bytes = _pq_code_bytes(faiss.clip_pq_subquantizers, faiss.pq_bits) + _pq_code_bytes(
        faiss.csd_pq_subquantizers,
        faiss.pq_bits,
    )
    vector_id_bytes = 16  # one int64 identifier in each independent ANN index
    rerank_bytes = (
        2 * (faiss.clip_dimension + faiss.csd_dimension)
        if budget.store_exact_fp16_rerank_vectors
        else 0
    )
    per_point = {
        "pq_codes": pq_code_bytes,
        "ann_ids": vector_id_bytes,
        "fp16_rerank": rerank_bytes,
        "compact_metadata": budget.metadata_bytes_per_point,
    }
    snapshot_bytes = points * sum(per_point.values())
    retained_snapshot_bytes = snapshot_bytes * budget.retained_snapshot_copies
    hot_cache_count = round(points * budget.hot_image_cache_fraction)
    hot_cache_bytes = hot_cache_count * cached_bytes
    object_storage_bytes = retained_snapshot_bytes + hot_cache_bytes
    object_storage_gib = object_storage_bytes / BYTES_PER_GIB
    object_storage_billing_gb = object_storage_bytes / BYTES_PER_BILLING_GB
    storage_monthly = object_storage_billing_gb * budget.object_storage_gb_monthly_usd

    components = {
        "vector_host": budget.vector_host_monthly_usd,
        "edge_compute": budget.edge_compute_monthly_usd,
        "metadata_database": budget.metadata_database_monthly_usd,
        "object_storage": storage_monthly,
        "observability": budget.observability_monthly_usd,
        "contingency": budget.contingency_monthly_usd,
    }
    projected = sum(components.values())
    headroom = budget.monthly_budget_usd - projected
    baseline_gib = points * average_original_bytes / BYTES_PER_GIB
    baseline_billing_gb = points * average_original_bytes / BYTES_PER_BILLING_GB
    return CapacityPlan(
        target_points=points,
        monthly_budget_usd=budget.monthly_budget_usd,
        projected_monthly_usd=round(projected, 2),
        monthly_headroom_usd=round(headroom, 2),
        required_headroom_usd=budget.minimum_headroom_usd,
        promotion_ready=headroom >= budget.minimum_headroom_usd,
        vector_snapshot_gib=round(snapshot_bytes / BYTES_PER_GIB, 3),
        retained_vector_snapshot_gib=round(retained_snapshot_bytes / BYTES_PER_GIB, 3),
        hot_image_cache_count=hot_cache_count,
        hot_image_cache_gib=round(hot_cache_bytes / BYTES_PER_GIB, 3),
        projected_object_storage_gib=round(object_storage_gib, 3),
        projected_object_storage_monthly_usd=round(storage_monthly, 2),
        full_original_baseline_gib=round(baseline_gib, 3),
        full_original_baseline_monthly_usd=round(
            baseline_billing_gb * budget.object_storage_gb_monthly_usd,
            2,
        ),
        monthly_components_usd={key: round(value, 2) for key, value in components.items()},
        bytes_per_point=per_point,
        assumptions={
            "average_cached_image_bytes": cached_bytes,
            "average_original_bytes": average_original_bytes,
            "hot_image_cache_fraction": budget.hot_image_cache_fraction,
            "retained_snapshot_copies": budget.retained_snapshot_copies,
            "object_storage_billing_gb": round(object_storage_billing_gb, 3),
            "storage_rate_usd_per_gb_month": budget.object_storage_gb_monthly_usd,
            "exact_fp16_rerank_vectors": budget.store_exact_fp16_rerank_vectors,
            "object_storage_free_tier_applied": False,
            "object_storage_operation_charges_included": False,
            "provider_billing_rounding_included": False,
            "excludes_usage_overages": True,
        },
    )


def _pq_code_bytes(subquantizers: int, bits: int) -> int:
    total_bits = subquantizers * bits
    return (total_bits + 7) // 8
