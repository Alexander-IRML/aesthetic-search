from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite

from artsearch import __version__

from artsearch.artwork_filter.config import ArtworkFilterConfig
from artsearch.artwork_filter.enums import ContentClass, FilterDecision, RuleDisposition
from artsearch.artwork_filter.errors import (
    DownloadError,
    ImageDecodeError,
    ImageValidationError,
    ModelInferenceError,
    PersistenceError,
    UnsupportedMediaError,
)
from artsearch.artwork_filter.schemas import (
    FilterResult,
    ImageCandidate,
    LoadedImage,
    RuleResult,
    TextScores,
    VisualScores,
)


ACCEPTED_ROUTES = {
    ContentClass.FINISHED_ILLUSTRATION: "main_art",
    ContentClass.TRADITIONAL_ART: "main_art",
    ContentClass.COMIC: "comics",
    ContentClass.CHARACTER_SHEET: "reference_sheets",
    ContentClass.SKETCH_OR_WIP: "sketches",
    ContentClass.THREE_D_RENDER: "main_art",
}
ROUTING_ROUTES = {
    ContentClass.COMMISSION_SHEET: "commission_material",
    ContentClass.ADOPTABLE_SHEET: "commission_material",
    ContentClass.ART_MERCH_PHOTO: "art_photos",
    ContentClass.PHOTO_OF_ART: "art_photos",
}
REJECTED_CLASSES = {
    ContentClass.CASUAL_PHOTO,
    ContentClass.SELFIE,
    ContentClass.FOOD_PHOTO,
    ContentClass.PET_PHOTO,
    ContentClass.SCREENSHOT,
    ContentClass.MEME,
    ContentClass.TEXT_ANNOUNCEMENT,
    ContentClass.OTHER,
}


class DecisionEngine:
    def __init__(self, config: ArtworkFilterConfig) -> None:
        self.config = config

    def decide(
        self,
        candidate: ImageCandidate,
        loaded_image: LoadedImage,
        rule_result: RuleResult,
        visual_scores: VisualScores | None,
        text_scores: TextScores,
        *,
        duration_ms: float = 0.0,
        prompt_version: str | None = None,
    ) -> FilterResult:
        if rule_result.disposition == RuleDisposition.FORCE_REJECT:
            return self._result(
                candidate,
                loaded_image,
                FilterDecision.REJECT,
                ContentClass.UNKNOWN,
                "rejected",
                0.0,
                1.0,
                [
                    hit.reason_code
                    for hit in rule_result.hits
                    if hit.disposition == rule_result.disposition
                ],
                rule_result,
                visual_scores,
                text_scores,
                duration_ms,
                prompt_version,
            )

        predicted_class = _predicted_class(visual_scores)
        if visual_scores is None:
            decision = FilterDecision.REVIEW
            route = "review"
            reason_codes = ["review.no_visual_model"]
            final_score = _text_only_review_score(text_scores)
            confidence = 0.0
        else:
            final_score = _final_score(visual_scores, text_scores, self.config)
            confidence = _top_class_confidence(visual_scores)
            decision, route, reason_codes = self._visual_decision(
                predicted_class,
                final_score,
                confidence,
                visual_scores.confidence_margin,
            )

        if rule_result.disposition == RuleDisposition.FORCE_REVIEW:
            decision = FilterDecision.REVIEW
            route = "review"
            reason_codes.extend(
                hit.reason_code
                for hit in rule_result.hits
                if hit.disposition == RuleDisposition.FORCE_REVIEW
            )

        return self._result(
            candidate,
            loaded_image,
            decision,
            predicted_class,
            route,
            final_score,
            confidence,
            _dedupe(reason_codes),
            rule_result,
            visual_scores,
            text_scores,
            duration_ms,
            prompt_version,
        )

    def error_result(
        self,
        candidate: ImageCandidate,
        error: Exception,
        *,
        duration_ms: float,
    ) -> FilterResult:
        return FilterResult(
            candidate_id=candidate.candidate_id,
            decision=FilterDecision.ERROR,
            predicted_class=ContentClass.UNKNOWN,
            accepted_for_main_corpus=False,
            route="error",
            final_score=0.0,
            confidence=0.0,
            reason_codes=[_error_reason_code(error)],
            image_sha256=None,
            width=None,
            height=None,
            visual_scores=None,
            text_scores=None,
            rule_result=None,
            model_version=self.config.model.model_id,
            config_version=self.config.version,
            prompt_version=None,
            classifier_version=None,
            processed_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
            error_type=type(error).__name__,
            error_message=str(error),
            source_uri=candidate.post_uri,
            source_cid=candidate.post_cid,
            author_did=candidate.author_did,
            image_index=candidate.image_index,
            config_hash=self.config.config_hash,
            software_version=__version__,
        )

    def _visual_decision(
        self,
        predicted_class: ContentClass,
        final_score: float,
        confidence: float,
        margin: float,
    ) -> tuple[FilterDecision, str, list[str]]:
        if predicted_class in REJECTED_CLASSES:
            return FilterDecision.REJECT, "rejected", [f"reject.{predicted_class.value}"]

        if predicted_class in ROUTING_ROUTES or not self._policy_accepts(predicted_class):
            return (
                FilterDecision.REVIEW,
                ROUTING_ROUTES.get(predicted_class, "review"),
                [f"review.{predicted_class.value}"],
            )

        if confidence < self.config.thresholds.force_review_below_confidence:
            return FilterDecision.REVIEW, "review", ["review.low_confidence"]
        if margin < self.config.thresholds.minimum_margin:
            return FilterDecision.REVIEW, "review", ["review.low_margin"]
        if final_score >= self.config.thresholds.accept_score:
            if not self.config.policy.automatic_accept_enabled:
                return (
                    FilterDecision.REVIEW,
                    "review",
                    ["review.automatic_accept_disabled"],
                )
            return (
                FilterDecision.ACCEPT,
                ACCEPTED_ROUTES[predicted_class],
                [f"accept.high_confidence_{predicted_class.value}"],
            )
        if final_score <= self.config.thresholds.reject_score:
            return FilterDecision.REJECT, "rejected", ["reject.low_score"]
        return FilterDecision.REVIEW, "review", ["review.score_band"]

    def _policy_accepts(self, content_class: ContentClass) -> bool:
        field_name = f"accept_{content_class.value}"
        return bool(getattr(self.config.policy, field_name, False))

    def _result(
        self,
        candidate: ImageCandidate,
        loaded_image: LoadedImage,
        decision: FilterDecision,
        predicted_class: ContentClass,
        route: str,
        final_score: float,
        confidence: float,
        reason_codes: list[str],
        rule_result: RuleResult,
        visual_scores: VisualScores | None,
        text_scores: TextScores,
        duration_ms: float,
        prompt_version: str | None,
    ) -> FilterResult:
        return FilterResult(
            candidate_id=candidate.candidate_id,
            decision=decision,
            predicted_class=predicted_class,
            accepted_for_main_corpus=decision == FilterDecision.ACCEPT,
            route=route,
            final_score=_finite_score(final_score),
            confidence=_finite_score(confidence),
            reason_codes=reason_codes,
            image_sha256=loaded_image.sha256,
            width=loaded_image.width,
            height=loaded_image.height,
            visual_scores=visual_scores,
            text_scores=text_scores,
            rule_result=rule_result,
            model_version=self.config.model.model_id,
            config_version=self.config.version,
            prompt_version=prompt_version,
            classifier_version=None,
            processed_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
            error_type=None,
            error_message=None,
            source_uri=candidate.post_uri,
            source_cid=candidate.post_cid,
            author_did=candidate.author_did,
            image_index=candidate.image_index,
            config_hash=self.config.config_hash,
            software_version=__version__,
        )


def _predicted_class(visual_scores: VisualScores | None) -> ContentClass:
    if visual_scores is None or not visual_scores.class_scores:
        return ContentClass.UNKNOWN
    return max(visual_scores.class_scores, key=lambda score: score.score).content_class


def _top_class_confidence(visual_scores: VisualScores) -> float:
    if not visual_scores.class_scores:
        return 0.0
    return _finite_score(max(item.score for item in visual_scores.class_scores))


def _final_score(
    visual_scores: VisualScores,
    text_scores: TextScores,
    config: ArtworkFilterConfig,
) -> float:
    text_component = max(0.0, min(1.0, 0.5 + 0.5 * text_scores.net_score))
    return (
        config.ensemble.visual_weight * visual_scores.art_utility_score
        + config.ensemble.text_weight * text_component
        + config.ensemble.rule_adjustment_weight * 0.5
    )


def _text_only_review_score(text_scores: TextScores) -> float:
    return max(0.0, min(1.0, 0.5 + 0.5 * text_scores.net_score))


def _finite_score(value: float) -> float:
    if not isfinite(value):
        return 0.0
    return max(0.0, min(1.0, float(value)))


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _error_reason_code(error: Exception) -> str:
    if isinstance(error, ModelInferenceError):
        return "error.model_inference"
    if isinstance(error, ImageDecodeError):
        return "error.image_decode"
    if isinstance(error, (ImageValidationError, UnsupportedMediaError)):
        return "error.image_validation"
    if isinstance(error, DownloadError):
        return "error.download"
    if isinstance(error, PersistenceError):
        return "error.persistence"
    return "error.classification"
