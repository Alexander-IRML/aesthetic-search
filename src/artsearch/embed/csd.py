from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

import numpy as np
from PIL import Image

from artsearch.ingest.config import ModelConfig


CSD_STYLE_DIMENSION = 768


@dataclass(frozen=True)
class CSDEncodingTiming:
    """Wall-clock stages for one CSD batch; all values are milliseconds."""

    preprocess_ms: float
    tensor_transfer_ms: float
    backbone_ms: float
    projection_ms: float
    output_ms: float
    total_ms: float


class CSDStyleEncoder:
    """Load pinned CSD weights without remote code or device-bound tracing."""

    def __init__(self, models: ModelConfig, device: Any) -> None:
        try:
            import torch
            from huggingface_hub import hf_hub_download
            from huggingface_hub.errors import LocalEntryNotFoundError
            from safetensors.torch import load_file
            from transformers import CLIPImageProcessor
        except ImportError as exc:
            raise RuntimeError(
                "CSD embedding generation requires the optional ML dependencies. "
                "Install them with: python -m pip install -e '.[embed]'"
            ) from exc

        checkpoint = _pinned_hf_file(
            hf_hub_download,
            LocalEntryNotFoundError,
            repo_id=models.csd_model_name,
            filename=models.csd_model_filename,
            revision=models.csd_model_version,
        )
        actual_sha256 = _sha256_file(checkpoint)
        if actual_sha256 != models.csd_model_sha256.lower():
            raise RuntimeError(
                "CSD artifact checksum mismatch: "
                f"expected={models.csd_model_sha256.lower()}, actual={actual_sha256}"
            )

        state = load_file(str(checkpoint), device="cpu")
        backbone_state, style_projection = _split_state_dict(state)
        self._torch = torch
        self.device = device
        processor_config = _pinned_hf_file(
            hf_hub_download,
            LocalEntryNotFoundError,
            repo_id=models.csd_processor_name,
            filename="preprocessor_config.json",
            revision=models.csd_processor_version,
        )
        self.processor = CLIPImageProcessor.from_pretrained(
            processor_config.parent,
            local_files_only=True,
        )
        self.model = _build_visual_transformer(torch, backbone_state)
        self.style_projection = style_projection.float()
        self.dtype = torch.float32
        if getattr(device, "type", str(device)) == "cuda":
            self.model.half()
            self.style_projection = self.style_projection.half()
            self.dtype = torch.float16
        self.model.to(device).eval()
        self.style_projection = self.style_projection.to(device)

    def encode(self, images: Sequence[Image.Image]) -> np.ndarray:
        vectors, _ = self.encode_timed(images)
        return vectors

    def encode_timed(
        self,
        images: Sequence[Image.Image],
    ) -> tuple[np.ndarray, CSDEncodingTiming]:
        """Encode images and expose the CPU-serving stages that dominate latency."""

        total_started = time.perf_counter()
        if not images:
            return (
                np.empty((0, CSD_STYLE_DIMENSION), dtype=np.float32),
                CSDEncodingTiming(
                    preprocess_ms=0.0,
                    tensor_transfer_ms=0.0,
                    backbone_ms=0.0,
                    projection_ms=0.0,
                    output_ms=0.0,
                    total_ms=0.0,
                ),
            )

        preprocess_started = time.perf_counter()
        inputs = self.processor(images=list(images), return_tensors="pt")
        preprocess_ms = _elapsed_ms(preprocess_started)

        transfer_started = time.perf_counter()
        pixel_values = inputs["pixel_values"].to(device=self.device, dtype=self.dtype)
        tensor_transfer_ms = _elapsed_ms(transfer_started)

        with self._torch.inference_mode():
            backbone_started = time.perf_counter()
            features = self.model(pixel_values)
            backbone_ms = _elapsed_ms(backbone_started)

            projection_started = time.perf_counter()
            style = features @ self.style_projection
            style = self._torch.nn.functional.normalize(style, dim=1, p=2)
            projection_ms = _elapsed_ms(projection_started)

        output_started = time.perf_counter()
        vectors = style.detach().cpu().float().numpy().astype(np.float32)
        if vectors.ndim != 2 or vectors.shape[1] != CSD_STYLE_DIMENSION:
            raise RuntimeError(
                "CSD produced an unexpected style embedding shape: "
                f"{vectors.shape}, expected=(*, {CSD_STYLE_DIMENSION})"
            )
        if not np.isfinite(vectors).all():
            raise RuntimeError("CSD produced non-finite style embeddings")
        norms = np.linalg.norm(vectors, axis=1)
        if np.any(norms == 0):
            raise RuntimeError("CSD produced a zero-length style embedding")
        output_ms = _elapsed_ms(output_started)
        return (
            vectors,
            CSDEncodingTiming(
                preprocess_ms=preprocess_ms,
                tensor_transfer_ms=tensor_transfer_ms,
                backbone_ms=backbone_ms,
                projection_ms=projection_ms,
                output_ms=output_ms,
                total_ms=_elapsed_ms(total_started),
            ),
        )


def _split_state_dict(state: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    style_projection = state.get("last_layer_style")
    if style_projection is None:
        raise RuntimeError("CSD state dict is missing last_layer_style")
    if tuple(style_projection.shape) != (1024, CSD_STYLE_DIMENSION):
        raise RuntimeError(
            f"CSD style projection has an unexpected shape: {tuple(style_projection.shape)}"
        )
    backbone = {
        key.removeprefix("backbone."): value
        for key, value in state.items()
        if key.startswith("backbone.")
    }
    if not backbone:
        raise RuntimeError("CSD state dict has no backbone weights")
    unexpected = {
        key
        for key in state
        if not key.startswith("backbone.") and key not in {"last_layer_style", "last_layer_content"}
    }
    if unexpected:
        raise RuntimeError(f"CSD state dict has unexpected keys: {sorted(unexpected)}")
    return backbone, style_projection


def _pinned_hf_file(
    downloader: Any,
    local_missing_error: type[Exception],
    *,
    repo_id: str,
    filename: str,
    revision: str,
) -> Path:
    try:
        path = downloader(
            repo_id=repo_id,
            filename=filename,
            revision=revision,
            local_files_only=True,
        )
    except local_missing_error:
        path = downloader(repo_id=repo_id, filename=filename, revision=revision)
    return Path(path)


def _build_visual_transformer(torch: Any, state: Mapping[str, Any]) -> Any:
    """Rebuild the OpenAI CLIP visual backbone used by the CSD authors."""

    nn = torch.nn

    class QuickGELU(nn.Module):
        def forward(self, values: Any) -> Any:
            return values * torch.sigmoid(1.702 * values)

    class ResidualAttentionBlock(nn.Module):
        def __init__(self, width: int, heads: int) -> None:
            super().__init__()
            self.attn = nn.MultiheadAttention(width, heads)
            self.ln_1 = nn.LayerNorm(width)
            self.mlp = nn.Sequential(
                OrderedDict(
                    [
                        ("c_fc", nn.Linear(width, width * 4)),
                        ("gelu", QuickGELU()),
                        ("c_proj", nn.Linear(width * 4, width)),
                    ]
                )
            )
            self.ln_2 = nn.LayerNorm(width)

        def forward(self, values: Any) -> Any:
            normalized = self.ln_1(values)
            attention = self.attn(normalized, normalized, normalized, need_weights=False)[0]
            values = values + attention
            return values + self.mlp(self.ln_2(values))

    class Transformer(nn.Module):
        def __init__(self, width: int, layers: int, heads: int) -> None:
            super().__init__()
            self.resblocks = nn.Sequential(
                *[ResidualAttentionBlock(width, heads) for _ in range(layers)]
            )

        def forward(self, values: Any) -> Any:
            return self.resblocks(values)

    class VisionTransformer(nn.Module):
        def __init__(
            self,
            input_resolution: int,
            patch_size: int,
            width: int,
            layers: int,
            heads: int,
        ) -> None:
            super().__init__()
            self.conv1 = nn.Conv2d(
                3,
                width,
                kernel_size=patch_size,
                stride=patch_size,
                bias=False,
            )
            scale = width**-0.5
            self.class_embedding = nn.Parameter(scale * torch.randn(width))
            token_count = (input_resolution // patch_size) ** 2 + 1
            self.positional_embedding = nn.Parameter(scale * torch.randn(token_count, width))
            self.ln_pre = nn.LayerNorm(width)
            self.transformer = Transformer(width, layers, heads)
            self.ln_post = nn.LayerNorm(width)

        def forward(self, images: Any) -> Any:
            values = self.conv1(images)
            values = values.reshape(values.shape[0], values.shape[1], -1)
            values = values.permute(0, 2, 1)
            class_token = self.class_embedding.to(values.dtype).expand(values.shape[0], 1, -1)
            values = torch.cat([class_token, values], dim=1)
            values = values + self.positional_embedding.to(values.dtype)
            values = self.ln_pre(values)
            values = values.permute(1, 0, 2)
            values = self.transformer(values)
            values = values.permute(1, 0, 2)
            return self.ln_post(values[:, 0, :])

    conv = state.get("conv1.weight")
    positions = state.get("positional_embedding")
    if conv is None or positions is None:
        raise RuntimeError("CSD backbone is missing convolution or positional weights")
    width = int(conv.shape[0])
    patch_size = int(conv.shape[-1])
    grid_size = math.isqrt(int(positions.shape[0]) - 1)
    if grid_size * grid_size + 1 != int(positions.shape[0]):
        raise RuntimeError("CSD positional embedding does not describe a square patch grid")
    layer_indices = {
        int(key.split(".")[2]) for key in state if key.startswith("transformer.resblocks.")
    }
    layers = max(layer_indices, default=-1) + 1
    if (width, patch_size, grid_size, layers) != (1024, 14, 16, 24):
        raise RuntimeError(
            "CSD backbone does not match the pinned ViT-L/14 contract: "
            f"width={width}, patch={patch_size}, grid={grid_size}, layers={layers}"
        )
    model = VisionTransformer(
        input_resolution=grid_size * patch_size,
        patch_size=patch_size,
        width=width,
        layers=layers,
        heads=width // 64,
    )
    try:
        model.load_state_dict(dict(state), strict=True)
    except RuntimeError as exc:
        raise RuntimeError(f"CSD backbone state is incompatible: {exc}") from exc
    return model


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
