import numpy as np
import pytest

from artsearch.embed.csd import _pinned_hf_file, _sha256_file, _split_state_dict


def test_split_state_dict_extracts_backbone_and_style_head() -> None:
    style = np.zeros((1024, 768), dtype=np.float32)
    weight = np.zeros((2, 2), dtype=np.float32)

    backbone, extracted_style = _split_state_dict(
        {
            "last_layer_style": style,
            "last_layer_content": style,
            "backbone.example.weight": weight,
        }
    )

    assert set(backbone) == {"example.weight"}
    assert backbone["example.weight"] is weight
    assert extracted_style is style


def test_split_state_dict_rejects_unknown_artifact_contract() -> None:
    with pytest.raises(RuntimeError, match="unexpected keys"):
        _split_state_dict(
            {
                "last_layer_style": np.zeros((1024, 768), dtype=np.float32),
                "backbone.example.weight": np.zeros((2, 2), dtype=np.float32),
                "surprise": np.zeros(1, dtype=np.float32),
            }
        )


def test_csd_artifact_hash_is_stable(tmp_path) -> None:
    artifact = tmp_path / "model.pt"
    artifact.write_bytes(b"pinned-csd-artifact")

    assert _sha256_file(artifact) == (
        "ae4d0ca0ff944529de78a5d58d6ae85a90bfc7f4a5c880b32ce5c4118a2025ff"
    )


def test_pinned_file_prefers_offline_cache_then_downloads(tmp_path) -> None:
    class MissingLocalFile(Exception):
        pass

    calls: list[bool] = []

    def downloader(**kwargs):
        local_only = bool(kwargs.get("local_files_only"))
        calls.append(local_only)
        if local_only:
            raise MissingLocalFile
        return str(tmp_path / "downloaded.safetensors")

    path = _pinned_hf_file(
        downloader,
        MissingLocalFile,
        repo_id="example/csd",
        filename="model.safetensors",
        revision="revision",
    )

    assert path == tmp_path / "downloaded.safetensors"
    assert calls == [True, False]
