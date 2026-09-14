from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RetrievalWeights(BaseModel):
    """Experimental coordinate-half weights applied before cosine retrieval."""

    model_config = ConfigDict(extra="forbid")

    clip_first_half: float = Field(default=1.0, ge=0.1, le=2.0)
    clip_second_half: float = Field(default=1.0, ge=0.1, le=2.0)
    csd_first_half: float = Field(default=1.0, ge=0.1, le=2.0)
    csd_second_half: float = Field(default=1.0, ge=0.1, le=2.0)


class MatchEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fusion_score: float | None = None
    subject_score: float | None = None
    subject_rank: int | None = Field(default=None, gt=0)
    style_score: float | None = None
    style_rank: int | None = Field(default=None, gt=0)


class ResultSourceMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_text: str | None = Field(default=None, max_length=700)
    alt_text: str | None = Field(default=None, max_length=700)


class ArtworkSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artwork_id: str
    artist_id: str
    artist_name: str
    rank: int = Field(gt=0)
    image_url: str
    source_url: str | None = None
    content_class: str = "unknown"
    source_metadata: ResultSourceMetadata = Field(default_factory=ResultSourceMetadata)
    match: MatchEvidence


class SearchTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upload_decode_ms: float = Field(ge=0)
    queue_ms: float = Field(ge=0)
    telemetry_start_ms: float = Field(ge=0)
    telemetry_wait_ms: float = Field(ge=0)
    encode_ms: float = Field(ge=0)
    clip_encode_ms: float = Field(ge=0)
    csd_encode_ms: float = Field(ge=0)
    csd_preprocess_ms: float = Field(ge=0)
    csd_tensor_transfer_ms: float = Field(ge=0)
    csd_backbone_ms: float = Field(ge=0)
    csd_projection_ms: float = Field(ge=0)
    csd_output_ms: float = Field(ge=0)
    embedding_normalize_ms: float = Field(ge=0)
    vector_search_ms: float = Field(ge=0)
    catalog_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)


class ModelContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_version: str
    vector_backend: Literal["qdrant", "faiss"]
    index_alias: str
    index_version: str
    clip_model: str
    clip_revision: str
    csd_model: str
    csd_revision: str


class ImageSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_version: str
    query_id: str
    session_id: str
    query_modality: Literal["image"] = "image"
    result_count: int = Field(ge=0)
    results: list[ArtworkSearchResult]
    retrieval_weights: RetrievalWeights = Field(default_factory=RetrievalWeights)
    model: ModelContract
    timing: SearchTiming


class SiteVisitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_key: str = Field(min_length=8, max_length=128)


class SiteVisitResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["recorded", "disabled"]


class ResultFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_id: UUID
    artwork_id: str = Field(min_length=1, max_length=256)
    session_key: str = Field(min_length=8, max_length=128)
    grade: Literal["great", "good", "bad"]


class ResultFeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["recorded"] = "recorded"
    interaction_id: str
    query_id: str
    artwork_id: str
    result_rank: int = Field(gt=0)
    grade: Literal["great", "good", "bad"]
    relevance: int = Field(ge=0, le=2)


class ErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail
