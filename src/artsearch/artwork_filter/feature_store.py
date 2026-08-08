from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
from typing import Protocol
import uuid

import numpy as np

from artsearch.artwork_filter.errors import PersistenceError
from artsearch.artwork_filter.hashing import stable_json_hash


class FeatureStore(Protocol):
    def get(self, key: str) -> np.ndarray | None: ...

    def put(self, key: str, array: np.ndarray, metadata: Mapping[str, object]) -> None: ...


class LocalNumpyFeatureStore:
    """Store finite numeric arrays under content-addressed keys using atomic renames."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def get(self, key: str) -> np.ndarray | None:
        array_path, metadata_path = self._paths(key)
        if not array_path.exists() or not metadata_path.exists():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            with array_path.open("rb") as handle:
                array = np.load(handle, allow_pickle=False)
            if list(array.shape) != metadata.get("shape"):
                raise ValueError("cached feature shape does not match metadata")
            if str(array.dtype) != metadata.get("dtype"):
                raise ValueError("cached feature dtype does not match metadata")
            if array.ndim not in {1, 2} or not np.issubdtype(array.dtype, np.floating):
                raise ValueError("cached feature has an unsupported shape or dtype")
            if not np.isfinite(array).all():
                raise ValueError("cached feature contains non-finite values")
            return np.asarray(array, dtype=np.float32)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._discard(array_path, metadata_path)
            return None

    def put(self, key: str, array: np.ndarray, metadata: Mapping[str, object]) -> None:
        array_path, metadata_path = self._paths(key)
        value = np.asarray(array, dtype=np.float32)
        if value.ndim not in {1, 2} or not np.isfinite(value).all():
            raise PersistenceError("feature arrays must be finite one- or two-dimensional data")

        payload = dict(metadata)
        payload.update({"key": key, "shape": list(value.shape), "dtype": str(value.dtype)})
        array_path.parent.mkdir(parents=True, exist_ok=True)
        suffix = uuid.uuid4().hex
        array_tmp = array_path.with_name(f".{array_path.name}.{suffix}.tmp")
        metadata_tmp = metadata_path.with_name(f".{metadata_path.name}.{suffix}.tmp")
        try:
            with array_tmp.open("wb") as handle:
                np.save(handle, value, allow_pickle=False)
                handle.flush()
                os.fsync(handle.fileno())
            with metadata_tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True, default=str)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(array_tmp, array_path)
            os.replace(metadata_tmp, metadata_path)
        except OSError as exc:
            self._discard(array_tmp, metadata_tmp)
            raise PersistenceError(str(exc)) from exc

    def _paths(self, key: str) -> tuple[Path, Path]:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise ValueError("feature cache keys must be lowercase SHA-256 hex")
        directory = self.root / key[:2]
        return directory / f"{key}.npy", directory / f"{key}.json"

    @staticmethod
    def _discard(*paths: Path) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def image_feature_cache_key(
    *,
    image_sha256: str,
    model_id: str,
    model_revision: str | None,
    preprocessing_version: str,
    normalize_embeddings: bool,
) -> str:
    return stable_json_hash(
        {
            "kind": "image_embedding",
            "image_sha256": image_sha256,
            "model_id": model_id,
            "model_revision": model_revision or "",
            "preprocessing_version": preprocessing_version,
            "normalize_embeddings": normalize_embeddings,
        }
    )


def prompt_feature_cache_key(
    *,
    model_id: str,
    model_revision: str | None,
    prompt_version: str,
    prompt_hash: str,
    normalize_embeddings: bool,
) -> str:
    return stable_json_hash(
        {
            "kind": "prompt_embeddings",
            "model_id": model_id,
            "model_revision": model_revision or "",
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "normalize_embeddings": normalize_embeddings,
        }
    )
