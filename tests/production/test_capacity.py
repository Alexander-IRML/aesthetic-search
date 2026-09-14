import pytest

from artsearch.production.capacity import build_capacity_plan
from artsearch.production.config import FaissConfig, ScaleBudgetConfig


def test_ten_million_capacity_plan_fits_hard_budget_with_headroom() -> None:
    plan = build_capacity_plan(FaissConfig(), ScaleBudgetConfig())

    assert plan.target_points == 10_000_000
    assert plan.monthly_budget_usd == 50.0
    assert plan.projected_monthly_usd == 39.99
    assert plan.monthly_headroom_usd == 10.01
    assert plan.promotion_ready is True
    assert plan.monthly_headroom_usd >= 5.0
    assert plan.hot_image_cache_count == 100_000
    assert plan.vector_snapshot_gib == 27.716
    assert plan.projected_object_storage_gib == 61.253
    assert plan.full_original_baseline_monthly_usd > plan.projected_monthly_usd
    assert plan.bytes_per_point == {
        "pq_codes": 80,
        "ann_ids": 16,
        "fp16_rerank": 2560,
        "compact_metadata": 320,
    }


def test_capacity_gate_fails_when_fixed_costs_consume_budget() -> None:
    budget = ScaleBudgetConfig(vector_host_monthly_usd=45.0)
    plan = build_capacity_plan(FaissConfig(), budget)

    assert plan.promotion_ready is False
    assert plan.monthly_headroom_usd < budget.minimum_headroom_usd


def test_capacity_plan_rejects_zero_override() -> None:
    with pytest.raises(ValueError, match="positive"):
        build_capacity_plan(FaissConfig(), ScaleBudgetConfig(), target_points=0)
