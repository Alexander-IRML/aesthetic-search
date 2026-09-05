import numpy as np
import pytest

from artsearch.embed.csd import CSDEncodingTiming
from artsearch.embed.csd_benchmark import evaluate_embedding_agreement, summarize_timings


def test_embedding_agreement_accepts_effectively_identical_vectors() -> None:
    reference = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    candidate = np.array([[1.0, 0.0], [0.001, 0.9999995]], dtype=np.float32)

    agreement = evaluate_embedding_agreement(reference, candidate)

    assert agreement.passed is True
    assert agreement.minimum_cosine > 0.999


def test_embedding_agreement_rejects_materially_changed_vectors() -> None:
    reference = np.array([[1.0, 0.0]], dtype=np.float32)
    candidate = np.array([[0.9, 0.4358899]], dtype=np.float32)

    agreement = evaluate_embedding_agreement(reference, candidate)

    assert agreement.passed is False
    assert agreement.minimum_cosine == pytest.approx(0.9)


def test_timing_summary_reports_stage_percentiles() -> None:
    samples = [
        CSDEncodingTiming(1, 2, 10, 3, 4, 20),
        CSDEncodingTiming(2, 3, 20, 4, 5, 34),
    ]

    summary = summarize_timings(samples)

    assert summary["backbone"]["mean_ms"] == 15
    assert summary["total"]["p50_ms"] == 27


def test_timing_summary_rejects_empty_input() -> None:
    with pytest.raises(ValueError, match="at least one"):
        summarize_timings([])
