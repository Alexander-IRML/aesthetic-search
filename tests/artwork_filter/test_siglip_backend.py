from types import SimpleNamespace

import numpy as np
import torch

from artsearch.artwork_filter.config import ModelConfig
from artsearch.artwork_filter.siglip_backend import Siglip2Backend


class FakeProcessor:
    def __call__(self, *, text=None, images=None, **kwargs):
        if text is not None:
            return {
                "input_ids": torch.ones((len(text), 2), dtype=torch.long),
                "attention_mask": torch.ones((len(text), 2), dtype=torch.long),
            }
        return {"pixel_values": torch.ones((len(images), 3, 2, 2))}


class FakeModel:
    def __init__(self):
        self.config = SimpleNamespace(
            _commit_hash="resolved-revision",
            text_config=SimpleNamespace(projection_size=2),
        )
        self.logit_scale = torch.tensor(np.log(2.0), dtype=torch.float32)
        self.logit_bias = torch.tensor(0.5, dtype=torch.float32)
        self.dtype = torch.float32

    def eval(self):
        return self

    def to(self, device):
        return self

    def get_image_features(self, pixel_values):
        return SimpleNamespace(pooler_output=torch.tensor([[3.0, 4.0]] * len(pixel_values)))

    def get_text_features(self, input_ids, attention_mask):
        return SimpleNamespace(pooler_output=torch.tensor([[0.0, 2.0]] * len(input_ids)))


def test_siglip_backend_loads_once_and_returns_normalized_cpu_arrays(monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(
        "transformers.AutoProcessor.from_pretrained",
        lambda *args, **kwargs: FakeProcessor(),
    )
    monkeypatch.setattr(
        "transformers.AutoModel.from_pretrained",
        lambda *args, **kwargs: model,
    )
    backend = Siglip2Backend(ModelConfig(device="cpu", dtype="float32", batch_size=2))

    images = backend.encode_images([object()])
    texts = backend.encode_texts(["art"])
    scores = backend.score(images, texts)

    assert np.allclose(images, [[0.6, 0.8]])
    assert np.allclose(texts, [[0.0, 1.0]])
    assert np.allclose(scores, [[2.1]])
    assert backend.model_revision == "resolved-revision"
