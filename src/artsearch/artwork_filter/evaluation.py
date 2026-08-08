from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
import csv
import json
from math import sqrt
from pathlib import Path
from typing import Any

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.ensemble import ACCEPTED_ROUTES, REJECTED_CLASSES, ROUTING_ROUTES
from artsearch.artwork_filter.enums import (
    ContentClass,
    CorpusInclusionLabel,
    FilterDecision,
    HumanContentLabel,
    RuleDisposition,
)
from artsearch.artwork_filter.errors import PersistenceError
from artsearch.artwork_filter.labels import latest_labels
from artsearch.artwork_filter.review_export import (
    load_decision_history,
    load_latest_candidates,
)
from artsearch.artwork_filter.schemas import ArtworkLabel, FilterResult, ImageCandidate


DEFAULT_SWEEP = tuple(index / 100 for index in range(101))


def evaluate_filter(
    candidates_path: str | Path,
    decisions_path: str | Path,
    labels_path: str | Path,
) -> dict[str, Any]:
    samples, accounting = load_evaluation_samples(
        candidates_path,
        decisions_path,
        labels_path,
    )
    binary = [sample for sample in samples if _binary_truth(sample.label) is not None]
    multiclass = [
        sample
        for sample in samples
        if sample.label.content_class != HumanContentLabel.UNCERTAIN
    ]

    report: dict[str, Any] = {
        "dataset": {
            **accounting,
            "binary_evaluable": len(binary),
            "multiclass_evaluable": len(multiclass),
            "artist_groups": len({_artist_group(sample) for sample in samples}),
        },
        "versions": _versions(samples),
        "decision_metrics": _decision_metrics(binary),
        "multiclass_metrics": _multiclass_metrics(multiclass),
        "artist_macro_metrics": _artist_macro_metrics(binary),
        "false_acceptance_by_negative_subtype": _false_acceptance_by_subtype(binary),
    }
    return report


def calibrate_thresholds(
    candidates_path: str | Path,
    decisions_path: str | Path,
    labels_path: str | Path,
    config: ArtworkFilterConfig,
    *,
    target_accept_precision: float = 0.95,
    target_reject_precision: float = 0.95,
    minimum_decisions: int = 30,
    thresholds: Sequence[float] = DEFAULT_SWEEP,
) -> dict[str, Any]:
    _validate_calibration_args(
        target_accept_precision,
        target_reject_precision,
        minimum_decisions,
        thresholds,
    )
    samples, accounting = load_evaluation_samples(
        candidates_path,
        decisions_path,
        labels_path,
    )
    binary = [sample for sample in samples if _binary_truth(sample.label) is not None]
    _validate_calibration_cohort(binary, config)
    accept_sweep = [
        _accept_threshold_row(binary, config, threshold=float(threshold))
        for threshold in sorted(set(thresholds))
    ]
    reject_sweep = [
        _reject_threshold_row(binary, config, threshold=float(threshold))
        for threshold in sorted(set(thresholds))
    ]
    accept = _recommend_accept(
        accept_sweep,
        target_precision=target_accept_precision,
        minimum_decisions=minimum_decisions,
    )
    reject = _recommend_reject(
        reject_sweep,
        target_precision=target_reject_precision,
        minimum_decisions=minimum_decisions,
        maximum_threshold=(
            accept["threshold"] if accept is not None else config.thresholds.accept_score
        ),
    )
    return {
        "dataset": {
            **accounting,
            "binary_evaluable": len(binary),
        },
        "targets": {
            "accept_precision_lower_bound": target_accept_precision,
            "reject_precision_lower_bound": target_reject_precision,
            "minimum_decisions": minimum_decisions,
            "confidence_level": 0.95,
        },
        "current_thresholds": config.thresholds.model_dump(mode="json"),
        "current_policy_projection": _policy_projection(
            binary,
            config,
            accept_score=config.thresholds.accept_score,
            reject_score=config.thresholds.reject_score,
        ),
        "recommendation": {
            "accept_score": accept["threshold"] if accept is not None else None,
            "reject_score": reject["threshold"] if reject is not None else None,
            "accept_metrics": accept,
            "reject_metrics": reject,
            "ready": accept is not None and reject is not None,
            "projected_policy": (
                _policy_projection(
                    binary,
                    config,
                    accept_score=accept["threshold"],
                    reject_score=reject["threshold"],
                )
                if accept is not None and reject is not None
                else None
            ),
        },
        "sweep": {
            "accept": accept_sweep,
            "reject": reject_sweep,
        },
    }


class EvaluationSample:
    def __init__(
        self,
        candidate: ImageCandidate,
        decision: FilterResult,
        label: ArtworkLabel,
    ) -> None:
        self.candidate = candidate
        self.decision = decision
        self.label = label


def load_evaluation_samples(
    candidates_path: str | Path,
    decisions_path: str | Path,
    labels_path: str | Path,
) -> tuple[list[EvaluationSample], dict[str, int]]:
    candidates = load_latest_candidates(candidates_path)
    histories = load_decision_history(decisions_path)
    labels = latest_labels(labels_path)
    samples: list[EvaluationSample] = []
    missing_candidates = 0
    missing_decisions = 0
    missing_decision_snapshots = 0
    for label in labels.values():
        candidate = candidates.get(label.candidate_id)
        if candidate is None:
            missing_candidates += 1
            continue
        history = histories.get(label.candidate_id, [])
        decision = _decision_for_label(history, label)
        if decision is None:
            if history:
                missing_decision_snapshots += 1
            else:
                missing_decisions += 1
            continue
        samples.append(EvaluationSample(candidate, decision, label))
    return samples, {
        "latest_labels": len(labels),
        "matched": len(samples),
        "missing_candidates": missing_candidates,
        "missing_decisions": missing_decisions,
        "missing_decision_snapshots": missing_decision_snapshots,
    }


def write_json_report(report: dict[str, Any], path: str | Path) -> None:
    destination = Path(path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise PersistenceError(str(exc)) from exc


def write_threshold_sweep_csv(report: dict[str, Any], path: str | Path) -> None:
    sweep = report.get("sweep", {})
    rows = [
        {"kind": kind, **row}
        for kind in ("accept", "reject")
        for row in sweep.get(kind, [])
    ]
    destination = Path(path)
    fieldnames = (
        "kind",
        "threshold",
        "decision_count",
        "precision",
        "precision_low",
        "recall",
        "coverage",
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        raise PersistenceError(str(exc)) from exc


def _decision_for_label(
    history: Sequence[FilterResult],
    label: ArtworkLabel,
) -> FilterResult | None:
    if not history:
        return None
    if label.source_decision_processed_at is None:
        return history[-1]
    for decision in reversed(history):
        if decision.processed_at != label.source_decision_processed_at:
            continue
        if label.source_config_hash and decision.config_hash != label.source_config_hash:
            continue
        if label.source_model_version and decision.model_version != label.source_model_version:
            continue
        return decision
    return None


def _decision_metrics(samples: Sequence[EvaluationSample]) -> dict[str, Any]:
    total = len(samples)
    accepts = [sample for sample in samples if sample.decision.decision == FilterDecision.ACCEPT]
    reviews = [sample for sample in samples if sample.decision.decision == FilterDecision.REVIEW]
    rejects = [sample for sample in samples if sample.decision.decision == FilterDecision.REJECT]
    errors = [sample for sample in samples if sample.decision.decision == FilterDecision.ERROR]
    true_positive = sum(_binary_truth(sample.label) is True for sample in accepts)
    positive_total = sum(_binary_truth(sample.label) is True for sample in samples)
    precision = _rate(true_positive, len(accepts))
    precision_interval = _wilson_interval(true_positive, len(accepts))
    recall = _rate(true_positive, positive_total)
    return {
        "automatic_accept_count": len(accepts),
        "automatic_accept_precision": precision,
        "automatic_accept_precision_95ci": precision_interval,
        "automatic_accept_recall": recall,
        "automatic_accept_coverage": _rate(len(accepts), total),
        "review_rate": _rate(len(reviews), total),
        "reject_rate": _rate(len(rejects), total),
        "error_rate": _rate(len(errors), total),
    }


def _multiclass_metrics(samples: Sequence[EvaluationSample]) -> dict[str, Any]:
    pairs = [
        (ContentClass(sample.label.content_class.value), sample.decision.predicted_class)
        for sample in samples
    ]
    classes = sorted(
        {truth for truth, _ in pairs} | {prediction for _, prediction in pairs},
        key=lambda value: value.value,
    )
    per_class: dict[str, Any] = {}
    confusion: dict[str, dict[str, int]] = {}
    for content_class in classes:
        true_positive = sum(
            truth == content_class and prediction == content_class
            for truth, prediction in pairs
        )
        predicted = sum(prediction == content_class for _, prediction in pairs)
        actual = sum(truth == content_class for truth, _ in pairs)
        precision = _rate(true_positive, predicted)
        recall = _rate(true_positive, actual)
        per_class[content_class.value] = {
            "support": actual,
            "precision": precision,
            "recall": recall,
            "f1": _f1(precision, recall),
        }
        confusion[content_class.value] = {
            predicted_class.value: sum(
                truth == content_class and prediction == predicted_class
                for truth, prediction in pairs
            )
            for predicted_class in classes
        }
    accuracy = _rate(sum(truth == prediction for truth, prediction in pairs), len(pairs))
    f1_values = [metrics["f1"] for metrics in per_class.values() if metrics["f1"] is not None]
    weighted_f1_numerator = sum(
        metrics["f1"] * metrics["support"]
        for metrics in per_class.values()
        if metrics["f1"] is not None
    )
    weighted_support = sum(
        metrics["support"]
        for metrics in per_class.values()
        if metrics["f1"] is not None
    )
    return {
        "top1_accuracy": accuracy,
        "macro_f1": _mean(f1_values),
        "weighted_f1": _rate(weighted_f1_numerator, weighted_support),
        "per_class": per_class,
        "confusion_matrix": confusion,
    }


def _artist_macro_metrics(samples: Sequence[EvaluationSample]) -> dict[str, Any]:
    groups: dict[str, list[EvaluationSample]] = defaultdict(list)
    for sample in samples:
        groups[_artist_group(sample)].append(sample)
    metrics = [_binary_group_metrics(group) for group in groups.values()]
    return {
        "artist_count": len(groups),
        "accuracy": _mean_defined(metrics, "accuracy"),
        "automatic_accept_precision": _mean_defined(metrics, "precision"),
        "automatic_accept_recall": _mean_defined(metrics, "recall"),
        "automatic_accept_f1": _mean_defined(metrics, "f1"),
    }


def _binary_group_metrics(samples: Sequence[EvaluationSample]) -> dict[str, float | None]:
    truths = [_binary_truth(sample.label) for sample in samples]
    predictions = [sample.decision.decision == FilterDecision.ACCEPT for sample in samples]
    true_positive = sum(truth is True and prediction for truth, prediction in zip(truths, predictions))
    predicted_positive = sum(predictions)
    actual_positive = sum(truth is True for truth in truths)
    precision = _rate(true_positive, predicted_positive)
    recall = _rate(true_positive, actual_positive)
    return {
        "accuracy": _rate(
            sum(bool(truth) == prediction for truth, prediction in zip(truths, predictions)),
            len(samples),
        ),
        "precision": precision,
        "recall": recall,
        "f1": _f1(precision, recall),
    }


def _false_acceptance_by_subtype(samples: Sequence[EvaluationSample]) -> dict[str, Any]:
    groups: dict[str, list[EvaluationSample]] = defaultdict(list)
    for sample in samples:
        if _binary_truth(sample.label) is False:
            groups[sample.label.content_class.value].append(sample)
    return {
        label: {
            "support": len(group),
            "accepted": sum(
                sample.decision.decision == FilterDecision.ACCEPT for sample in group
            ),
            "rate": _rate(
                sum(sample.decision.decision == FilterDecision.ACCEPT for sample in group),
                len(group),
            ),
        }
        for label, group in sorted(groups.items())
    }


def _accept_threshold_row(
    samples: Sequence[EvaluationSample],
    config: ArtworkFilterConfig,
    *,
    threshold: float,
) -> dict[str, Any]:
    accepts = [
        sample
        for sample in samples
        if _accept_eligible(sample.decision, config)
        and sample.decision.final_score >= threshold
    ]
    correct = sum(_binary_truth(sample.label) is True for sample in accepts)
    positive_total = sum(_binary_truth(sample.label) is True for sample in samples)
    interval = _wilson_interval(correct, len(accepts))
    return {
        "threshold": threshold,
        "decision_count": len(accepts),
        "precision": _rate(correct, len(accepts)),
        "precision_low": interval[0],
        "recall": _rate(correct, positive_total),
        "coverage": _rate(len(accepts), len(samples)),
    }


def _reject_threshold_row(
    samples: Sequence[EvaluationSample],
    config: ArtworkFilterConfig,
    *,
    threshold: float,
) -> dict[str, Any]:
    rejects = [
        sample
        for sample in samples
        if _would_reject(sample.decision, config, threshold=threshold)
    ]
    correct = sum(_binary_truth(sample.label) is False for sample in rejects)
    negative_total = sum(_binary_truth(sample.label) is False for sample in samples)
    interval = _wilson_interval(correct, len(rejects))
    return {
        "threshold": threshold,
        "decision_count": len(rejects),
        "precision": _rate(correct, len(rejects)),
        "precision_low": interval[0],
        "recall": _rate(correct, negative_total),
        "coverage": _rate(len(rejects), len(samples)),
    }


def _policy_projection(
    samples: Sequence[EvaluationSample],
    config: ArtworkFilterConfig,
    *,
    accept_score: float,
    reject_score: float,
) -> dict[str, Any]:
    accepts = [
        sample
        for sample in samples
        if _accept_eligible(sample.decision, config)
        and sample.decision.final_score >= accept_score
    ]
    rejects = [
        sample
        for sample in samples
        if _would_reject(sample.decision, config, threshold=reject_score)
    ]
    decided = {id(sample) for sample in accepts} | {id(sample) for sample in rejects}
    accept_correct = sum(_binary_truth(sample.label) is True for sample in accepts)
    reject_correct = sum(_binary_truth(sample.label) is False for sample in rejects)
    return {
        "accept_score": accept_score,
        "reject_score": reject_score,
        "accept_count": len(accepts),
        "accept_precision": _rate(accept_correct, len(accepts)),
        "accept_coverage": _rate(len(accepts), len(samples)),
        "reject_count": len(rejects),
        "reject_precision": _rate(reject_correct, len(rejects)),
        "reject_coverage": _rate(len(rejects), len(samples)),
        "review_count": len(samples) - len(decided),
        "review_rate": _rate(len(samples) - len(decided), len(samples)),
    }


def _accept_eligible(result: FilterResult, config: ArtworkFilterConfig) -> bool:
    if result.visual_scores is None or result.predicted_class not in ACCEPTED_ROUTES:
        return False
    if not bool(getattr(config.policy, f"accept_{result.predicted_class.value}", False)):
        return False
    if result.confidence < config.thresholds.force_review_below_confidence:
        return False
    if result.visual_scores.confidence_margin < config.thresholds.minimum_margin:
        return False
    return not _has_rule_disposition(
        result,
        {RuleDisposition.FORCE_REJECT, RuleDisposition.FORCE_REVIEW},
    )


def _would_reject(
    result: FilterResult,
    config: ArtworkFilterConfig,
    *,
    threshold: float,
) -> bool:
    if _has_rule_disposition(result, {RuleDisposition.FORCE_REJECT}):
        return True
    if _has_rule_disposition(result, {RuleDisposition.FORCE_REVIEW}):
        return False
    if result.predicted_class in REJECTED_CLASSES:
        return True
    if result.predicted_class in ROUTING_ROUTES:
        return False
    if result.visual_scores is None:
        return False
    if not bool(getattr(config.policy, f"accept_{result.predicted_class.value}", False)):
        return False
    if result.confidence < config.thresholds.force_review_below_confidence:
        return False
    if result.visual_scores.confidence_margin < config.thresholds.minimum_margin:
        return False
    return result.final_score <= threshold


def _has_rule_disposition(
    result: FilterResult,
    dispositions: set[RuleDisposition],
) -> bool:
    if result.rule_result is None:
        return False
    return result.rule_result.disposition in dispositions or any(
        hit.disposition in dispositions for hit in result.rule_result.hits
    )


def _recommend_accept(
    sweep: Sequence[dict[str, Any]],
    *,
    target_precision: float,
    minimum_decisions: int,
) -> dict[str, Any] | None:
    eligible = [
        row
        for row in sweep
        if row["decision_count"] >= minimum_decisions
        and row["precision_low"] is not None
        and row["precision_low"] >= target_precision
    ]
    return min(eligible, key=lambda row: row["threshold"], default=None)


def _recommend_reject(
    sweep: Sequence[dict[str, Any]],
    *,
    target_precision: float,
    minimum_decisions: int,
    maximum_threshold: float,
) -> dict[str, Any] | None:
    eligible = [
        row
        for row in sweep
        if row["decision_count"] >= minimum_decisions
        and row["threshold"] < maximum_threshold
        and row["precision_low"] is not None
        and row["precision_low"] >= target_precision
    ]
    return max(eligible, key=lambda row: row["threshold"], default=None)


def _binary_truth(label: ArtworkLabel) -> bool | None:
    if label.include_in_main_corpus == CorpusInclusionLabel.YES:
        return True
    if label.include_in_main_corpus == CorpusInclusionLabel.NO:
        return False
    return None


def _artist_group(sample: EvaluationSample) -> str:
    if sample.candidate.author_did:
        return f"did:{sample.candidate.author_did}"
    if sample.candidate.author_handle:
        return f"handle:{sample.candidate.author_handle.strip().lower()}"
    return f"candidate:{sample.candidate.candidate_id}"


def _versions(samples: Iterable[EvaluationSample]) -> dict[str, list[str]]:
    sample_list = list(samples)
    return {
        "model_versions": sorted({sample.decision.model_version for sample in sample_list}),
        "model_revisions": sorted(
            {
                sample.decision.visual_scores.model_revision
                for sample in sample_list
                if sample.decision.visual_scores is not None
                and sample.decision.visual_scores.model_revision is not None
            }
        ),
        "classifier_versions": sorted(
            {
                sample.decision.classifier_version
                for sample in sample_list
                if sample.decision.classifier_version is not None
            }
        ),
        "config_hashes": sorted({sample.decision.config_hash for sample in sample_list}),
        "prompt_versions": sorted(
            {
                sample.decision.prompt_version
                for sample in sample_list
                if sample.decision.prompt_version is not None
            }
        ),
    }


def _validate_calibration_args(
    accept_precision: float,
    reject_precision: float,
    minimum_decisions: int,
    thresholds: Sequence[float],
) -> None:
    if not 0.0 < accept_precision <= 1.0 or not 0.0 < reject_precision <= 1.0:
        raise ValueError("calibration precision targets must be in (0, 1]")
    if minimum_decisions <= 0:
        raise ValueError("minimum_decisions must be positive")
    if not thresholds or any(value < 0.0 or value > 1.0 for value in thresholds):
        raise ValueError("calibration thresholds must be non-empty and in [0, 1]")


def _validate_calibration_cohort(
    samples: Sequence[EvaluationSample],
    config: ArtworkFilterConfig,
) -> None:
    versions = _versions(samples)
    for field_name, values in versions.items():
        if len(values) > 1:
            raise ValueError(
                f"calibration requires one decision cohort; found mixed {field_name}"
            )
    if versions["config_hashes"] and versions["config_hashes"][0] != config.config_hash:
        raise ValueError("calibration config does not match the labeled decision cohort")
    if versions["model_versions"] and versions["model_versions"][0] != config.model.model_id:
        raise ValueError("calibration model does not match the labeled decision cohort")
    if (
        config.model.revision
        and versions["model_revisions"]
        and versions["model_revisions"][0] != config.model.revision
    ):
        raise ValueError("calibration model revision does not match the labeled decision cohort")


def _wilson_interval(successes: int, total: int) -> list[float | None]:
    if total == 0:
        return [None, None]
    z = 1.959963984540054
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    radius = (
        z
        * sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total))
        / denominator
    )
    return [max(0.0, center - radius), min(1.0, center + radius)]


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _rate(numerator: float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _mean_defined(
    rows: Sequence[dict[str, float | None]],
    key: str,
) -> float | None:
    values = [row[key] for row in rows if row[key] is not None]
    return _mean(values)  # type: ignore[arg-type]
