from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
import uuid

from artsearch.production.config import ObjectStoreConfig


class ObjectStoreError(RuntimeError):
    """Raised when an object cannot be verified or transferred durably."""


class ObjectCollisionError(ObjectStoreError):
    """Raised when an immutable object key already contains different bytes."""


@dataclass(frozen=True)
class ObjectRef:
    key: str
    uri: str
    size: int
    sha256: str
    etag: str | None = None
    created: bool = True

    def to_dict(self) -> dict[str, str | int | bool | None]:
        return asdict(self)


class ObjectStore(Protocol):
    def put_file(
        self,
        source: str | Path,
        key: str,
        *,
        expected_sha256: str | None = None,
    ) -> ObjectRef: ...

    def get_file(self, key: str, destination: str | Path) -> ObjectRef: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...


class LocalObjectStore:
    """Filesystem implementation with the same immutable-key contract as S3."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put_file(
        self,
        source: str | Path,
        key: str,
        *,
        expected_sha256: str | None = None,
    ) -> ObjectRef:
        source_path = Path(source)
        digest, size = file_identity(source_path)
        _check_expected_hash(digest, expected_sha256)
        safe_key = normalize_object_key(key)
        destination = self.root.joinpath(*PurePosixPath(safe_key).parts)
        if destination.exists():
            existing_digest, existing_size = file_identity(destination)
            if existing_digest != digest or existing_size != size:
                raise ObjectCollisionError(
                    f"object key already contains different bytes: {safe_key}"
                )
            return ObjectRef(
                key=safe_key,
                uri=destination.resolve().as_uri(),
                size=size,
                sha256=digest,
                created=False,
            )

        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        created = True
        try:
            with source_path.open("rb") as source_handle, temporary.open("xb") as target_handle:
                while chunk := source_handle.read(1024 * 1024):
                    target_handle.write(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                existing_digest, existing_size = file_identity(destination)
                if existing_digest != digest or existing_size != size:
                    raise ObjectCollisionError(
                        f"object key already contains different bytes: {safe_key}"
                    )
                created = False
            temporary.unlink()
            _fsync_directory(destination.parent)
        except ObjectCollisionError:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ObjectStoreError(f"could not store local object {safe_key}: {exc}") from exc
        return ObjectRef(
            key=safe_key,
            uri=destination.resolve().as_uri(),
            size=size,
            sha256=digest,
            created=created,
        )

    def get_file(self, key: str, destination: str | Path) -> ObjectRef:
        safe_key = normalize_object_key(key)
        source = self.root.joinpath(*PurePosixPath(safe_key).parts)
        if not source.is_file():
            raise ObjectStoreError(f"object does not exist: {safe_key}")
        digest, size = file_identity(source)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
                while chunk := source_handle.read(1024 * 1024):
                    target_handle.write(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise ObjectStoreError(f"could not fetch local object {safe_key}: {exc}") from exc
        return ObjectRef(
            key=safe_key,
            uri=source.resolve().as_uri(),
            size=size,
            sha256=digest,
            created=False,
        )

    def exists(self, key: str) -> bool:
        safe_key = normalize_object_key(key)
        return self.root.joinpath(*PurePosixPath(safe_key).parts).is_file()

    def delete(self, key: str) -> bool:
        safe_key = normalize_object_key(key)
        path = self.root.joinpath(*PurePosixPath(safe_key).parts)
        if not path.exists():
            return False
        path.unlink()
        _fsync_directory(path.parent)
        return True


class S3ObjectStore:
    """S3-compatible immutable object storage backed by s3fs.

    Credentials are deliberately omitted from configuration. ``s3fs`` resolves
    the standard AWS environment/profile/role credential chain at runtime.
    """

    def __init__(
        self,
        config: ObjectStoreConfig,
        *,
        filesystem: Any | None = None,
    ) -> None:
        if config.provider != "s3":
            raise ValueError("S3ObjectStore requires provider='s3'")
        self.config = config
        self._filesystem = filesystem

    def put_file(
        self,
        source: str | Path,
        key: str,
        *,
        expected_sha256: str | None = None,
    ) -> ObjectRef:
        source_path = Path(source)
        digest, size = file_identity(source_path)
        _check_expected_hash(digest, expected_sha256)
        safe_key = normalize_object_key(key)
        remote_path = self._remote_path(safe_key)
        filesystem = self._fs()

        if filesystem.exists(remote_path):
            info = filesystem.info(remote_path)
            remote_size = _remote_size(info)
            if remote_size != size:
                raise ObjectCollisionError(f"object key already has another size: {safe_key}")
            remote_digest, verified_size = _remote_identity(filesystem, remote_path)
            if remote_digest != digest or verified_size != size:
                raise ObjectCollisionError(
                    f"object key already contains different bytes: {safe_key}"
                )
            return self._ref(safe_key, size, digest, info=info, created=False)

        try:
            filesystem.put_file(str(source_path), remote_path)
            info = filesystem.info(remote_path)
        except Exception as exc:
            raise ObjectStoreError(f"could not upload s3://{remote_path}: {exc}") from exc
        if _remote_size(info) != size:
            try:
                filesystem.rm(remote_path)
            except Exception:
                pass
            raise ObjectStoreError(f"uploaded object size verification failed: {safe_key}")
        remote_digest, verified_size = _remote_identity(filesystem, remote_path)
        if remote_digest != digest or verified_size != size:
            try:
                filesystem.rm(remote_path)
            except Exception:
                pass
            raise ObjectStoreError(f"uploaded object hash verification failed: {safe_key}")
        return self._ref(safe_key, size, digest, info=info, created=True)

    def get_file(self, key: str, destination: str | Path) -> ObjectRef:
        safe_key = normalize_object_key(key)
        remote_path = self._remote_path(safe_key)
        filesystem = self._fs()
        if not filesystem.exists(remote_path):
            raise ObjectStoreError(f"object does not exist: {safe_key}")
        info = filesystem.info(remote_path)
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            filesystem.get_file(remote_path, str(temporary))
            digest, size = file_identity(temporary)
            if size != _remote_size(info):
                raise ObjectStoreError(f"downloaded object size verification failed: {safe_key}")
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if isinstance(exc, ObjectStoreError):
                raise
            raise ObjectStoreError(f"could not download s3://{remote_path}: {exc}") from exc
        return self._ref(safe_key, size, digest, info=info, created=False)

    def exists(self, key: str) -> bool:
        safe_key = normalize_object_key(key)
        remote_path = self._remote_path(safe_key)
        try:
            return bool(self._fs().exists(remote_path))
        except Exception as exc:
            raise ObjectStoreError(f"could not inspect s3://{remote_path}: {exc}") from exc

    def delete(self, key: str) -> bool:
        safe_key = normalize_object_key(key)
        remote_path = self._remote_path(safe_key)
        filesystem = self._fs()
        if not filesystem.exists(remote_path):
            return False
        try:
            filesystem.rm(remote_path)
        except Exception as exc:
            raise ObjectStoreError(f"could not delete s3://{remote_path}: {exc}") from exc
        return True

    def _fs(self) -> Any:
        if self._filesystem is None:
            try:
                import s3fs
            except ImportError as exc:
                raise ObjectStoreError(
                    "S3 support requires the data extra: pip install -e '.[data]'"
                ) from exc
            client_kwargs: dict[str, str] = {}
            if self.config.endpoint_url:
                client_kwargs["endpoint_url"] = self.config.endpoint_url
            if self.config.region:
                client_kwargs["region_name"] = self.config.region
            self._filesystem = s3fs.S3FileSystem(
                anon=False,
                client_kwargs=client_kwargs,
                config_kwargs={"signature_version": "s3v4"},
            )
        return self._filesystem

    def _remote_path(self, key: str) -> str:
        return f"{self.config.bucket}/{self.config.prefix}/{key}"

    def _ref(
        self,
        key: str,
        size: int,
        digest: str,
        *,
        info: dict[str, Any],
        created: bool,
    ) -> ObjectRef:
        etag = info.get("ETag") or info.get("etag")
        return ObjectRef(
            key=key,
            uri=f"s3://{self._remote_path(key)}",
            size=size,
            sha256=digest,
            etag=str(etag).strip('"') if etag else None,
            created=created,
        )


def build_object_store(config: ObjectStoreConfig) -> ObjectStore:
    if config.provider == "local":
        return LocalObjectStore(config.local_root / config.prefix)
    return S3ObjectStore(config)


def content_addressed_key(namespace: str, sha256: str, *, suffix: str = "") -> str:
    if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
        raise ValueError("sha256 must be a lowercase hexadecimal digest")
    safe_namespace = normalize_object_key(namespace)
    safe_suffix = suffix.strip()
    if safe_suffix and ("/" in safe_suffix or "\\" in safe_suffix):
        raise ValueError("object suffix must not contain a path separator")
    return f"{safe_namespace}/sha256/{sha256[:2]}/{sha256[2:4]}/{sha256}{safe_suffix}"


def normalize_object_key(key: str) -> str:
    if "\\" in key:
        raise ValueError("object keys must use POSIX separators")
    path = PurePosixPath(key.strip("/"))
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("object key must be a non-empty relative path")
    return str(path)


def object_ref_json(ref: ObjectRef) -> str:
    return json.dumps(ref.to_dict(), sort_keys=True)


def file_identity(path: str | Path) -> tuple[str, int]:
    path = Path(path)
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ObjectStoreError(f"could not read object source {path}: {exc}") from exc
    return digest.hexdigest(), size


def _check_expected_hash(actual: str, expected: str | None) -> None:
    if expected is not None and actual != expected:
        raise ObjectStoreError(f"source SHA-256 mismatch: expected {expected}, got {actual}")


def _remote_size(info: dict[str, Any]) -> int:
    value = info.get("size", info.get("Size"))
    if value is None:
        raise ObjectStoreError("S3 object metadata did not include a size")
    return int(value)


def _remote_identity(filesystem: Any, path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with filesystem.open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except Exception as exc:
        raise ObjectStoreError(f"could not verify remote object {path}: {exc}") from exc
    return digest.hexdigest(), size


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
