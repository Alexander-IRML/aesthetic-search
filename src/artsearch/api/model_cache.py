from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
from typing import Literal

from artsearch.ingest.config import load_config


CacheComponent = Literal["all", "clip", "csd"]


def cache_search_models(
    config_path: str | Path = "config/config.yaml",
    *,
    component: CacheComponent = "all",
) -> dict[str, object]:
    """Populate and validate pinned query artifacts, optionally one layer at a time."""

    try:
        from huggingface_hub import hf_hub_download
        from transformers import AutoProcessor, CLIPImageProcessor, CLIPModel
    except ImportError as exc:
        raise RuntimeError("model caching requires the optional embed dependencies") from exc

    config = load_config(config_path)
    models = config.models

    report: dict[str, object] = {
        "component": component,
        "offline_ready": True,
    }
    if component in {"all", "clip"}:
        processor = _from_pretrained_cache_first(
            AutoProcessor.from_pretrained,
            models.clip_model_name,
            models.clip_model_version,
        )
        # Prefer the checkpoint shipped by the pinned revision instead of selecting
        # a separately converted safetensors artifact at runtime.
        clip_model = _from_pretrained_cache_first(
            CLIPModel.from_pretrained,
            models.clip_model_name,
            models.clip_model_version,
            use_safetensors=False,
        )
        projection_dimension = int(getattr(clip_model.config, "projection_dim", 0))
        if projection_dimension != 512:
            raise RuntimeError(
                f"pinned CLIP model has projection dimension {projection_dimension}, expected 512"
            )
        report.update(
            clip_model=models.clip_model_name,
            clip_revision=models.clip_model_version,
            clip_dimension=projection_dimension,
        )
        del processor, clip_model
        gc.collect()

    if component in {"all", "csd"}:
        processor_path = Path(
            hf_hub_download(
                repo_id=models.csd_processor_name,
                filename="preprocessor_config.json",
                revision=models.csd_processor_version,
            )
        )
        CLIPImageProcessor.from_pretrained(processor_path.parent, local_files_only=True)

        checkpoint = Path(
            hf_hub_download(
                repo_id=models.csd_model_name,
                filename=models.csd_model_filename,
                revision=models.csd_model_version,
            )
        )
        actual_sha256 = _sha256_file(checkpoint)
        if actual_sha256 != models.csd_model_sha256.lower():
            raise RuntimeError(
                "CSD artifact checksum mismatch: "
                f"expected={models.csd_model_sha256.lower()}, actual={actual_sha256}"
            )
        report.update(
            csd_model=models.csd_model_name,
            csd_revision=models.csd_model_version,
            csd_sha256=actual_sha256,
        )

    return report


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _from_pretrained_cache_first(loader, model_name: str, revision: str, **kwargs):
    try:
        return loader(model_name, revision=revision, local_files_only=True, **kwargs)
    except OSError:
        return loader(model_name, revision=revision, **kwargs)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Cache and validate the pinned ArtSearch query encoders."
    )
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--component", choices=("all", "clip", "csd"), default="all")
    args = parser.parse_args(argv)
    print(json.dumps(cache_search_models(args.config, component=args.component), sort_keys=True))


if __name__ == "__main__":
    main()
