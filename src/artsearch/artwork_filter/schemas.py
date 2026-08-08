from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from artsearch.artwork_filter.enums import (
    ContentClass,
    CorpusInclusionLabel,
    FilterDecision,
    HumanContentLabel,
    ModelMode,
    OriginalWorkLabel,
    RuleDisposition,
)


class ImageCandidate(BaseModel):
    candidate_id: str = Field(min_length=1)
    author_did: str | None = None
    author_handle: str | None = None
    post_uri: str | None = None
    post_cid: str | None = None
    image_index: int | None = None
    thumbnail_url: str | None = None
    fullsize_url: str | None = None
    local_path: Path | None = None
    post_text: str = ""
    alt_text: str = ""
    created_at: datetime | None = None
    langs: list[str] = Field(default_factory=list)
    content_labels: list[str] = Field(default_factory=list)
    author_labels: list[str] = Field(default_factory=list)
    is_repost: bool = False
    is_quote_post: bool = False
    quoted_author_did: str | None = None
    declared_width: int | None = None
    declared_height: int | None = None
    mime_type: str | None = None
    source: Literal["bluesky", "local", "test"] = "bluesky"

    @field_validator("post_text", "alt_text", mode="before")
    @classmethod
    def _empty_text(cls, value: object) -> str:
        return "" if value is None else str(value)

    @field_validator("image_index")
    @classmethod
    def _non_negative_index(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("image_index must be non-negative")
        return value

    @field_validator("candidate_id")
    @classmethod
    def _candidate_id_is_not_url(cls, value: str) -> str:
        if value.startswith(("http://", "https://")):
            raise ValueError("candidate_id must not be a URL")
        return value

    @model_validator(mode="after")
    def _has_image_source(self) -> "ImageCandidate":
        if self.local_path is None and not self.thumbnail_url and not self.fullsize_url:
            raise ValueError("one of local_path, thumbnail_url, or fullsize_url is required")
        return self


class LoadedImage(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    candidate_id: str
    rgb_image: Image.Image = Field(exclude=True)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    format: str | None = None
    mime_type: str | None = None
    byte_size: int = Field(ge=0)
    sha256: str
    perceptual_hash: str | None = None
    source_url: str | None = None
    is_animated: bool = False


class RuleHit(BaseModel):
    rule_id: str
    disposition: RuleDisposition
    reason_code: str
    message: str
    numeric_value: float | None = None


class RuleResult(BaseModel):
    disposition: RuleDisposition
    hits: list[RuleHit] = Field(default_factory=list)


class ClassScore(BaseModel):
    content_class: ContentClass
    score: float = Field(ge=0.0, le=1.0)


class VisualScores(BaseModel):
    backend: str
    model_id: str
    model_revision: str | None = None
    mode: ModelMode
    class_scores: list[ClassScore]
    art_utility_score: float = Field(ge=0.0, le=1.0)
    noise_score: float = Field(ge=0.0, le=1.0)
    confidence_margin: float = Field(ge=0.0, le=1.0)
    embedding_dimension: int | None = None
    embedding_cache_key: str | None = None


class TextScores(BaseModel):
    positive_score: float = Field(ge=0.0, le=1.0)
    negative_score: float = Field(ge=0.0, le=1.0)
    net_score: float = Field(ge=-1.0, le=1.0)
    matched_positive_terms: list[str] = Field(default_factory=list)
    matched_negative_terms: list[str] = Field(default_factory=list)
    matched_patterns: list[str] = Field(default_factory=list)


class FilterResult(BaseModel):
    candidate_id: str
    decision: FilterDecision
    predicted_class: ContentClass
    accepted_for_main_corpus: bool
    route: str
    final_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[str]
    image_sha256: str | None = None
    width: int | None = None
    height: int | None = None
    visual_scores: VisualScores | None = None
    text_scores: TextScores | None = None
    rule_result: RuleResult | None = None
    model_version: str
    config_version: str
    prompt_version: str | None = None
    classifier_version: str | None = None
    processed_at: datetime
    duration_ms: float = Field(ge=0.0)
    error_type: str | None = None
    error_message: str | None = None
    source_uri: str | None = None
    source_cid: str | None = None
    author_did: str | None = None
    image_index: int | None = None
    config_hash: str = ""
    software_version: str = ""


class ArtworkLabel(BaseModel):
    """One immutable human annotation event for a candidate decision."""

    label_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    candidate_id: str = Field(min_length=1)
    content_class: HumanContentLabel
    is_original_artist_work: OriginalWorkLabel
    include_in_main_corpus: CorpusInclusionLabel
    annotator: str = Field(min_length=1)
    annotator_note: str = ""
    labeled_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_decision_processed_at: datetime | None = None
    source_model_version: str | None = None
    source_config_hash: str | None = None
    previous_model_prediction: ContentClass | None = None
    annotation_hash: str = ""

    @field_validator("annotator", mode="before")
    @classmethod
    def _trim_annotator(cls, value: object) -> str:
        return str(value).strip()

    @field_validator("annotator_note", mode="before")
    @classmethod
    def _normalize_note(cls, value: object) -> str:
        return "" if value is None else str(value).strip()
