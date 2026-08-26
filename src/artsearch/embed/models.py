from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

import numpy as np
from PIL import Image

from artsearch.embed.csd import CSDStyleEncoder
from artsearch.embed.storage import ImageEmbeddings
from artsearch.ingest.config import AppConfig


class EmbeddingProvider(Protocol):
    def embed_images(self, image_paths: Sequence[Path]) -> list[ImageEmbeddings]:
        """Return one embedding payload per image path, preserving input order."""


class TextEmbeddingProvider(Protocol):
    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        """Return normalized CLIP text vectors, preserving input order."""


class HuggingFaceEmbeddingProvider:
    def __init__(self, config: AppConfig) -> None:
        try:
            import torch
            from transformers import AutoProcessor, CLIPModel
        except ImportError as exc:
            raise RuntimeError(
                "Embedding generation requires the optional ML dependencies. "
                "Install them with: python -m pip install -e '.[embed]'"
            ) from exc

        self._torch = torch
        self.device = torch.device(config.embeddings.device)
        self.clip_processor = _from_pretrained_cache_first(
            AutoProcessor.from_pretrained,
            config.models.clip_model_name,
            revision=config.models.clip_model_version,
        )
        self.clip_model = _from_pretrained_cache_first(
            CLIPModel.from_pretrained,
            config.models.clip_model_name,
            revision=config.models.clip_model_version,
        ).to(self.device)
        self.csd_encoder = CSDStyleEncoder(config.models, self.device)
        self.clip_model.eval()

    def embed_images(self, image_paths: Sequence[Path]) -> list[ImageEmbeddings]:
        images = [_load_rgb_image(path) for path in image_paths]
        return self.embed_pil_images(images)

    def embed_pil_images(self, images: Sequence[Image.Image]) -> list[ImageEmbeddings]:
        """Encode already-decoded RGB images without writing query uploads to disk."""

        if not images:
            return []
        with self._torch.inference_mode():
            clip_vectors = self._embed_clip(images)
            csd_style = self.csd_encoder.encode(images)

        return [
            ImageEmbeddings(
                clip_vector=clip_vectors[index],
                csd_style=csd_style[index],
            )
            for index in range(len(images))
        ]

    def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            projection_dim = int(getattr(self.clip_model.config, "projection_dim", 0))
            return np.empty((0, projection_dim), dtype=np.float32)
        inputs = self.clip_processor(
            text=list(texts),
            padding=True,
            truncation=True,
            return_tensors="pt",
        )
        inputs = _to_device(inputs, self.device)
        with self._torch.no_grad():
            features = self.clip_model.get_text_features(**inputs)
        vectors = features.detach().cpu().float().numpy().astype(np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        if np.any(norms == 0):
            raise RuntimeError("CLIP produced a zero-length text embedding")
        return vectors / norms

    def _embed_clip(self, images: Sequence[Image.Image]) -> np.ndarray:
        inputs = self.clip_processor(images=list(images), return_tensors="pt")
        inputs = _to_device(inputs, self.device)
        vision_outputs = self.clip_model.vision_model(**inputs)
        features = _pooler_tensor(vision_outputs)
        if hasattr(self.clip_model, "visual_projection"):
            features = self.clip_model.visual_projection(features)
        return features.detach().cpu().float().numpy().astype(np.float32)


def _load_rgb_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB").copy()


def _to_device(inputs, device):
    return {key: value.to(device) for key, value in inputs.items()}


def _pooler_tensor(outputs):
    pooler_output = getattr(outputs, "pooler_output", None)
    if pooler_output is not None:
        return pooler_output
    return outputs.last_hidden_state[:, 0, :]


def _from_pretrained_cache_first(loader, model_name: str, *, revision: str):
    """Avoid network metadata checks when an exact pinned revision is cached."""

    try:
        return loader(model_name, revision=revision, local_files_only=True)
    except OSError:
        return loader(model_name, revision=revision)
