import json

import numpy as np

from artsearch.artwork_filter.feature_store import (
    LocalNumpyFeatureStore,
    image_feature_cache_key,
)


def test_local_feature_store_round_trips_and_ignores_corruption(tmp_path):
    store = LocalNumpyFeatureStore(tmp_path / "features")
    key = image_feature_cache_key(
        image_sha256="a" * 64,
        model_id="model",
        model_revision="revision",
        preprocessing_version="v1",
        normalize_embeddings=True,
    )
    vector = np.asarray([0.25, 0.75], dtype=np.float32)

    store.put(key, vector, {"kind": "image_embedding"})

    assert np.allclose(store.get(key), vector)
    metadata_path = tmp_path / "features" / key[:2] / f"{key}.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["shape"] = [3]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert store.get(key) is None
    assert not metadata_path.exists()
