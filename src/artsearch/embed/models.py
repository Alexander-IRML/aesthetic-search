from __future__ import annotations

import math
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np
from PIL import Image

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
            from transformers import AutoImageProcessor, AutoModel, AutoProcessor, CLIPModel
        except ImportError as exc:
            raise RuntimeError(
                "Embedding generation requires the optional ML dependencies. "
                "Install them with: python -m pip install -e '.[embed]'"
            ) from exc

        self._torch = torch
        self.device = torch.device(config.embeddings.device)
        self.clip_processor = AutoProcessor.from_pretrained(
            config.models.clip_model_name,
            revision=config.models.clip_model_version,
        )
        self.clip_model = CLIPModel.from_pretrained(
            config.models.clip_model_name,
            revision=config.models.clip_model_version,
        ).to(self.device)
        self.dino_processor = AutoImageProcessor.from_pretrained(
            config.models.dino_model_name,
            revision=config.models.dino_model_version,
        )
        self.dino_model = AutoModel.from_pretrained(
            config.models.dino_model_name,
            revision=config.models.dino_model_version,
        ).to(self.device)
        self.clip_model.eval()
        self.dino_model.eval()

    def embed_images(self, image_paths: Sequence[Path]) -> list[ImageEmbeddings]:
        images = [_load_rgb_image(path) for path in image_paths]
        with self._torch.no_grad():
            clip_vectors = self._embed_clip(images)
            dino_pooled, dino_patches, dino_grid_size = self._embed_dino(images)

        return [
            ImageEmbeddings(
                clip_vector=clip_vectors[index],
                dino_pooled=dino_pooled[index],
                dino_patches=dino_patches[index],
                dino_patch_grid_size=dino_grid_size,
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

    def _embed_dino(self, images: Sequence[Image.Image]) -> tuple[np.ndarray, np.ndarray, int]:
        inputs = self.dino_processor(
            images=list(images),
            return_tensors="pt",
            do_resize=False,
            do_center_crop=False,
        )
        inputs = _to_device(inputs, self.device)
        outputs = _call_dino_model(self.dino_model, inputs)
        hidden_states = outputs.last_hidden_state.detach().cpu().float().numpy()
        pooled = _pooler_tensor(outputs).detach().cpu().float().numpy()

        register_tokens = int(getattr(self.dino_model.config, "num_register_tokens", 0))
        patch_start = 1 + register_tokens
        patches = hidden_states[:, patch_start:, :]
        patch_count = patches.shape[1]
        grid_size = int(math.sqrt(patch_count))
        if grid_size * grid_size != patch_count:
            raise RuntimeError(
                f"DINO patch token count {patch_count} does not form a square grid"
            )

        return pooled.astype(np.float32), patches.astype(np.float32), grid_size


def _load_rgb_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return image.convert("RGB").copy()


def _to_device(inputs, device):
    return {key: value.to(device) for key, value in inputs.items()}


def _call_dino_model(model, inputs):
    try:
        return model(**inputs, interpolate_pos_encoding=True)
    except TypeError:
        return model(**inputs)


def _pooler_tensor(outputs):
    pooler_output = getattr(outputs, "pooler_output", None)
    if pooler_output is not None:
        return pooler_output
    return outputs.last_hidden_state[:, 0, :]
