from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from artsearch.production.config import ObjectStoreConfig
from artsearch.production.object_store import (
    LocalObjectStore,
    ObjectCollisionError,
    S3ObjectStore,
    content_addressed_key,
    file_identity,
    normalize_object_key,
)


def test_local_store_is_idempotent_and_detects_collisions(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"first")
    store = LocalObjectStore(tmp_path / "objects")

    created = store.put_file(source, "accepted/example.bin")
    repeated = store.put_file(source, "accepted/example.bin")

    assert created.created is True
    assert repeated.created is False
    assert repeated.sha256 == created.sha256

    source.write_bytes(b"other")
    with pytest.raises(ObjectCollisionError):
        store.put_file(source, "accepted/example.bin")


def test_local_store_fetches_and_deletes_exact_key(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    store = LocalObjectStore(tmp_path / "objects")
    stored = store.put_file(source, "one/two.bin")

    destination = tmp_path / "download" / "two.bin"
    fetched = store.get_file(stored.key, destination)

    assert destination.read_bytes() == b"payload"
    assert fetched.sha256 == stored.sha256
    assert store.delete(stored.key) is True
    assert store.delete(stored.key) is False


def test_content_addressed_keys_are_partitioned_and_safe() -> None:
    digest = "a" * 64
    assert content_addressed_key("corpus/originals", digest, suffix=".jpg") == (
        f"corpus/originals/sha256/aa/aa/{digest}.jpg"
    )
    with pytest.raises(ValueError):
        normalize_object_key("../private")


def test_s3_store_uses_prefix_and_verifies_existing_bytes(tmp_path: Path) -> None:
    filesystem = MemoryS3FileSystem()
    config = ObjectStoreConfig(
        provider="s3",
        bucket="artsearch-test",
        prefix="v1",
        endpoint_url="https://s3.example.test",
        region="test-1",
    )
    store = S3ObjectStore(config, filesystem=filesystem)
    source = tmp_path / "image.jpg"
    source.write_bytes(b"image bytes")

    created = store.put_file(source, "corpus/image.jpg")
    repeated = store.put_file(source, "corpus/image.jpg")
    destination = tmp_path / "copy.jpg"
    fetched = store.get_file("corpus/image.jpg", destination)

    assert created.uri == "s3://artsearch-test/v1/corpus/image.jpg"
    assert repeated.created is False
    assert fetched.sha256 == file_identity(source)[0]
    assert destination.read_bytes() == source.read_bytes()


class MemoryS3FileSystem:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def exists(self, path: str) -> bool:
        return path in self.objects

    def info(self, path: str) -> dict[str, object]:
        return {"size": len(self.objects[path]), "ETag": '"test-etag"'}

    def put_file(self, source: str, destination: str) -> None:
        self.objects[destination] = Path(source).read_bytes()

    def get_file(self, source: str, destination: str) -> None:
        Path(destination).write_bytes(self.objects[source])

    def open(self, path: str, mode: str) -> BytesIO:
        assert mode == "rb"
        return BytesIO(self.objects[path])

    def rm(self, path: str) -> None:
        del self.objects[path]
