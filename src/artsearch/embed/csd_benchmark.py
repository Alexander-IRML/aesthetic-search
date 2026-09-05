from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import statistics
from typing import Sequence

import numpy as np
from PIL import Image, ImageOps

from artsearch.embed.csd import CSDEncodingTiming, CSDStyleEncoder
from artsearch.ingest.config import load_config


@dataclass(frozen=True)
class EmbeddingAgreement:
    """Quality gate for a candidate runtime against the canonical FP32 encoder."""

    minimum_cosine: float
    mean_cosine: float
    maximum_absolute_error: float
    passed: bool


def evaluate_embedding_agreement(
    reference: np.ndarray,
    candidate: np.ndarray,
    *,
    minimum_cosine: float = 0.995,
    maximum_absolute_error: float = 0.02,
) -> EmbeddingAgreement:
    """Compare row-aligned vectors without treating speed as sufficient evidence."""

    reference = np.asarray(reference, dtype=np.float32)
    candidate = np.asarray(candidate, dtype=np.float32)
    if reference.shape != candidate.shape or reference.ndim != 2 or not reference.size:
        raise ValueError("reference and candidate must be non-empty matrices with equal shapes")
    if not np.isfinite(reference).all() or not np.isfinite(candidate).all():
        raise ValueError("embedding matrices must contain only finite values")

    reference_norms = np.linalg.norm(reference, axis=1)
    candidate_norms = np.linalg.norm(candidate, axis=1)
    if np.any(reference_norms == 0) or np.any(candidate_norms == 0):
        raise ValueError("embedding matrices must not contain zero-length rows")
    cosine = np.sum(reference * candidate, axis=1) / (reference_norms * candidate_norms)
    minimum = float(np.min(cosine))
    mean = float(np.mean(cosine))
    max_error = float(np.max(np.abs(reference - candidate)))
    return EmbeddingAgreement(
        minimum_cosine=minimum,
        mean_cosine=mean,
        maximum_absolute_error=max_error,
        passed=minimum >= minimum_cosine and max_error <= maximum_absolute_error,
    )


def summarize_timings(samples: Sequence[CSDEncodingTiming]) -> dict[str, dict[str, float]]:
    if not samples:
        raise ValueError("at least one timing sample is required")
    stages = {
        "preprocess": [sample.preprocess_ms for sample in samples],
        "tensor_transfer": [sample.tensor_transfer_ms for sample in samples],
        "backbone": [sample.backbone_ms for sample in samples],
        "projection": [sample.projection_ms for sample in samples],
        "output": [sample.output_ms for sample in samples],
        "total": [sample.total_ms for sample in samples],
    }
    return {
        stage: {
            "mean_ms": statistics.fmean(values),
            "p50_ms": float(np.percentile(values, 50)),
            "p95_ms": float(np.percentile(values, 95)),
        }
        for stage, values in stages.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark the canonical CSD FP32 encoder across CPU thread counts."
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--path", action="append", default=[], type=Path)
    parser.add_argument("--threads", action="append", type=_positive_int)
    parser.add_argument("--warmup", type=_nonnegative_int, default=1)
    parser.add_argument("--repeats", type=_positive_int, default=3)
    args = parser.parse_args()

    config = load_config(args.config)
    if config.embeddings.device != "cpu":
        parser.error("the thread-scaling benchmark requires embeddings.device=cpu")

    try:
        import torch
    except ImportError as exc:
        parser.error(f"the embed dependencies are required: {exc}")

    images = _load_images(args.path)
    thread_counts = tuple(dict.fromkeys(args.threads or [1, 2]))
    encoder = CSDStyleEncoder(config.models, torch.device("cpu"))
    thread_runs: list[dict[str, object]] = []
    report: dict[str, object] = {
        "model": config.models.csd_model_name,
        "revision": config.models.csd_model_version,
        "device": "cpu",
        "torch_version": torch.__version__,
        "image_count": len(images),
        "warmup_runs": args.warmup,
        "measured_runs": args.repeats,
        "quality_gate": {
            "minimum_cosine": 0.995,
            "maximum_absolute_error": 0.02,
        },
        "thread_runs": thread_runs,
    }
    reference: np.ndarray | None = None
    try:
        for thread_count in thread_counts:
            torch.set_num_threads(thread_count)
            for _ in range(args.warmup):
                encoder.encode(images)
            timings: list[CSDEncodingTiming] = []
            vectors: np.ndarray | None = None
            for _ in range(args.repeats):
                vectors, timing = encoder.encode_timed(images)
                timings.append(timing)
            assert vectors is not None
            if reference is None:
                reference = vectors
                agreement = evaluate_embedding_agreement(reference, vectors)
            else:
                agreement = evaluate_embedding_agreement(reference, vectors)
            thread_runs.append(
                {
                    "threads": thread_count,
                    "timing": summarize_timings(timings),
                    "agreement_with_first_run": asdict(agreement),
                }
            )
    finally:
        for image in images:
            image.close()

    print(json.dumps(report, indent=2, sort_keys=True))


def _load_images(paths: Sequence[Path]) -> list[Image.Image]:
    if not paths:
        return _synthetic_images()
    images: list[Image.Image] = []
    try:
        for path in paths:
            with Image.open(path) as image:
                images.append(ImageOps.exif_transpose(image).convert("RGB").copy())
    except Exception:
        for image in images:
            image.close()
        raise
    return images


def _synthetic_images() -> list[Image.Image]:
    rng = np.random.default_rng(20260905)
    noise = rng.integers(0, 256, size=(448, 448, 3), dtype=np.uint8)
    horizontal = np.linspace(0, 255, 448, dtype=np.uint8)
    gradient = np.zeros((448, 448, 3), dtype=np.uint8)
    gradient[:, :, 0] = horizontal[np.newaxis, :]
    gradient[:, :, 1] = horizontal[:, np.newaxis]
    gradient[:, :, 2] = 128
    return [
        Image.fromarray(noise),
        Image.fromarray(gradient),
    ]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


if __name__ == "__main__":
    main()
