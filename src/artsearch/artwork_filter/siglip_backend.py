from __future__ import annotations

from collections.abc import Sequence
import logging
from typing import Any, Protocol

import numpy as np
from PIL import Image

from artsearch.artwork_filter.config import ModelConfig
from artsearch.artwork_filter.errors import ModelArtifactError, ModelInferenceError


LOGGER = logging.getLogger(__name__)


class VisionLanguageBackend(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def model_revision(self) -> str | None: ...

    @property
    def embedding_dimension(self) -> int: ...

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray: ...

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray: ...

    def score(
        self,
        image_embeddings: np.ndarray,
        text_embeddings: np.ndarray,
    ) -> np.ndarray: ...


class Siglip2Backend:
    """Lazy, single-owner SigLIP 2 backend returning normalized CPU arrays."""

    def __init__(self, config: ModelConfig) -> None:
        self.config = config
        self._processor: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None
        self._device = ""
        self._dtype_name = ""
        self._resolved_revision: str | None = config.revision or None
        self._embedding_dimension: int | None = None
        self._logit_scale = 1.0
        self._logit_bias = 0.0

    @property
    def model_id(self) -> str:
        return self.config.model_id

    @property
    def model_revision(self) -> str | None:
        self._ensure_loaded()
        return self._resolved_revision

    @property
    def embedding_dimension(self) -> int:
        self._ensure_loaded()
        assert self._embedding_dimension is not None
        return self._embedding_dimension

    @property
    def device(self) -> str:
        self._ensure_loaded()
        return self._device

    @property
    def dtype(self) -> str:
        self._ensure_loaded()
        return self._dtype_name

    def encode_images(self, images: Sequence[Image.Image]) -> np.ndarray:
        if not images:
            return np.empty((0, self.embedding_dimension), dtype=np.float32)
        self._ensure_loaded()
        batches = []
        for start in range(0, len(images), self.config.batch_size):
            batch = list(images[start : start + self.config.batch_size])
            batches.append(self._encode_image_batch_with_oom_retry(batch))
        return np.concatenate(batches, axis=0)

    def encode_texts(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.embedding_dimension), dtype=np.float32)
        self._ensure_loaded()
        batches = []
        for start in range(0, len(texts), self.config.batch_size):
            batch = list(texts[start : start + self.config.batch_size])
            try:
                inputs = self._processor(
                    text=batch,
                    padding="max_length",
                    truncation=True,
                    max_length=64,
                    return_tensors="pt",
                )
                inputs = self._move_inputs(inputs)
                with self._torch.inference_mode():
                    output = self._model.get_text_features(**inputs)
                batches.append(self._to_numpy_features(output))
            except (RuntimeError, ValueError, TypeError) as exc:
                raise ModelInferenceError(f"SigLIP 2 text inference failed: {exc}") from exc
        return np.concatenate(batches, axis=0)

    def score(
        self,
        image_embeddings: np.ndarray,
        text_embeddings: np.ndarray,
    ) -> np.ndarray:
        self._ensure_loaded()
        images = _two_dimensional(image_embeddings, "image")
        texts = _two_dimensional(text_embeddings, "text")
        if images.shape[1] != texts.shape[1]:
            raise ModelInferenceError("image and text embedding dimensions do not match")
        return (images @ texts.T) * self._logit_scale + self._logit_bias

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            import torch
            from transformers import AutoModel, AutoProcessor
        except ImportError as exc:
            raise ModelArtifactError(
                "SigLIP 2 requires the artwork-filter ML dependencies; "
                "install the project with .[filter]"
            ) from exc

        device = _select_device(torch, self.config.device)
        dtype, dtype_name = _select_dtype(torch, self.config.dtype, device)
        model_kwargs: dict[str, object] = {"torch_dtype": dtype}
        if self.config.revision:
            model_kwargs["revision"] = self.config.revision
        try:
            processor = AutoProcessor.from_pretrained(
                self.config.model_id,
                revision=self.config.revision or "main",
            )
            model = AutoModel.from_pretrained(self.config.model_id, **model_kwargs)
            model.eval()
            model.to(device)
            if self.config.compile_model and hasattr(torch, "compile"):
                model = torch.compile(model)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ModelArtifactError(f"could not load SigLIP 2 model: {exc}") from exc

        self._torch = torch
        self._processor = processor
        self._model = model
        self._device = device
        self._dtype_name = dtype_name
        model_config = getattr(model, "config", None)
        self._resolved_revision = (
            getattr(model_config, "_commit_hash", None) or self.config.revision or "main"
        )
        self._embedding_dimension = _projection_dimension(model_config)
        self._logit_scale = _scalar_parameter(model, "logit_scale", exponentiate=True)
        self._logit_bias = _scalar_parameter(model, "logit_bias", exponentiate=False)
        LOGGER.info(
            "artwork_filter.started model_id=%s model_revision=%s device=%s dtype=%s "
            "embedding_dimension=%s",
            self.model_id,
            self._resolved_revision,
            device,
            dtype_name,
            self._embedding_dimension,
        )

    def _encode_image_batch_with_oom_retry(
        self,
        images: list[Image.Image],
    ) -> np.ndarray:
        try:
            return self._encode_image_batch(images)
        except self._torch.cuda.OutOfMemoryError as exc:
            if len(images) == 1:
                raise ModelInferenceError("SigLIP 2 image inference exhausted GPU memory") from exc
            self._torch.cuda.empty_cache()
            midpoint = len(images) // 2
            left = self._encode_image_batch_with_oom_retry(images[:midpoint])
            right = self._encode_image_batch_with_oom_retry(images[midpoint:])
            return np.concatenate([left, right], axis=0)

    def _encode_image_batch(self, images: list[Image.Image]) -> np.ndarray:
        try:
            inputs = self._processor(images=images, return_tensors="pt")
            inputs = self._move_inputs(inputs)
            with self._torch.inference_mode():
                output = self._model.get_image_features(**inputs)
            return self._to_numpy_features(output)
        except self._torch.cuda.OutOfMemoryError:
            raise
        except (RuntimeError, ValueError, TypeError) as exc:
            raise ModelInferenceError(f"SigLIP 2 image inference failed: {exc}") from exc

    def _move_inputs(self, inputs: Any) -> dict[str, Any]:
        return {
            key: value.to(self._device) if hasattr(value, "to") else value
            for key, value in inputs.items()
        }

    def _to_numpy_features(self, output: Any) -> np.ndarray:
        features = _extract_features(output)
        if self.config.normalize_embeddings:
            features = features / features.norm(p=2, dim=-1, keepdim=True).clamp_min(1e-12)
        value = features.detach().to(device="cpu", dtype=self._torch.float32).numpy()
        if value.ndim != 2 or not np.isfinite(value).all():
            raise ModelInferenceError("SigLIP 2 returned invalid embedding data")
        if self._embedding_dimension is None:
            self._embedding_dimension = int(value.shape[1])
        return value


def _select_device(torch: Any, configured: str) -> str:
    if configured != "auto":
        if configured == "cuda" and not torch.cuda.is_available():
            raise ModelArtifactError("CUDA was requested but is not available")
        return configured
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _select_dtype(torch: Any, configured: str, device: str) -> tuple[Any, str]:
    dtype_name = configured
    if configured == "auto":
        dtype_name = "float16" if device == "cuda" else "float32"
    known = {
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }
    if dtype_name not in known:
        raise ModelArtifactError(f"unsupported model dtype: {configured}")
    if device == "cpu" and dtype_name == "float16":
        raise ModelArtifactError("float16 inference is not supported on CPU")
    return known[dtype_name], dtype_name


def _extract_features(output: Any) -> Any:
    if hasattr(output, "pooler_output"):
        return output.pooler_output
    if hasattr(output, "ndim"):
        return output
    if isinstance(output, tuple) and len(output) > 1:
        return output[1]
    raise ModelInferenceError("SigLIP 2 feature output has an unsupported shape")


def _projection_dimension(model_config: Any) -> int | None:
    text_config = getattr(model_config, "text_config", None)
    for source in (text_config, model_config):
        for name in ("projection_size", "hidden_size"):
            value = getattr(source, name, None)
            if isinstance(value, int) and value > 0:
                return value
    return None


def _scalar_parameter(model: Any, name: str, *, exponentiate: bool) -> float:
    value = getattr(model, name, None)
    if value is None:
        return 0.0 if name == "logit_bias" else 1.0
    scalar = float(value.detach().to(device="cpu", dtype=value.dtype).item())
    if exponentiate:
        return float(np.exp(scalar))
    return scalar


def _two_dimensional(value: np.ndarray, label: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float32)
    if array.ndim != 2 or not np.isfinite(array).all():
        raise ModelInferenceError(f"{label} embeddings must be a finite two-dimensional array")
    return array
