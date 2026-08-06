"""Archive object storage.

Production Backblaze B2 uploads use ``BotoS3ArchiveStore`` via
``S3ArchiveStore.for_b2`` (explicit boto3 credentials, no overwrite, no delete).
The legacy PyArrow ``S3ArchiveStore`` remains for ``ARCHIVE_S3_*`` export-day.
``B2ArchiveClient`` in ``b2.py`` is smoke-only and must not become a second
production uploader stack.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

import boto3  # type: ignore[import-untyped]
import pyarrow.fs as pafs  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from trading_bot.archive.b2 import B2ArchiveConfig
from trading_bot.collector import sanitize_error_message

ClientFactory = Callable[..., Any]


class ArchiveStoreError(RuntimeError):
    """Raised for archive store failures with redacted messages."""


class ArchiveStore(Protocol):
    @property
    def destination_label(self) -> str: ...

    def exists(self, key: str) -> bool: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def read_bytes(self, key: str) -> bytes: ...

    def publish_bytes(self, key: str, value: bytes) -> None: ...

    def publish_file(self, key: str, source: Path) -> None: ...

    def download_file(self, key: str, destination: Path) -> None: ...

    def append_bytes(self, key: str, value: bytes) -> None: ...


def _safe_key(key: str) -> PurePosixPath:
    path = PurePosixPath(key)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("archive object key is unsafe")
    return path


class LocalArchiveStore:
    def __init__(
        self,
        root: Path,
        *,
        destination_label: str = "filesystem",
        protected: bool = False,
    ) -> None:
        self._root = root.resolve()
        self._destination_label = destination_label
        self._protected = protected
        self._root.mkdir(parents=True, exist_ok=True)
        if self._protected:
            self._root.chmod(0o700)

    @property
    def destination_label(self) -> str:
        return self._destination_label

    def _path(self, key: str) -> Path:
        return self._root.joinpath(*_safe_key(key).parts)

    def _prepare_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._protected:
            current = path.parent
            while current != self._root.parent:
                current.chmod(0o700)
                if current == self._root:
                    break
                current = current.parent

    def _protect_file(self, path: Path) -> None:
        if self._protected:
            path.chmod(0o600)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_keys(self, prefix: str) -> list[str]:
        safe_prefix = _safe_key(prefix)
        base = self._root.joinpath(*safe_prefix.parts)
        if not base.exists():
            return []
        keys: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(self._root).as_posix()
            keys.append(relative)
        return sorted(keys)

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def publish_bytes(self, key: str, value: bytes) -> None:
        path = self._path(key)
        self._prepare_parent(path)
        temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
        try:
            temporary.write_bytes(value)
            self._protect_file(temporary)
            os.replace(temporary, path)
            self._protect_file(path)
        finally:
            temporary.unlink(missing_ok=True)

    def publish_file(self, key: str, source: Path) -> None:
        path = self._path(key)
        self._prepare_parent(path)
        temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
        try:
            shutil.copyfile(source, temporary)
            self._protect_file(temporary)
            os.replace(temporary, path)
            self._protect_file(path)
        finally:
            temporary.unlink(missing_ok=True)

    def download_file(self, key: str, destination: Path) -> None:
        shutil.copyfile(self._path(key), destination)

    def append_bytes(self, key: str, value: bytes) -> None:
        """Append-only update for bounded evidence registries (e.g. quarantine JSONL)."""
        path = self._path(key)
        self._prepare_parent(path)
        temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
        try:
            existing = path.read_bytes() if path.is_file() else b""
            temporary.write_bytes(existing + value)
            self._protect_file(temporary)
            os.replace(temporary, path)
            self._protect_file(path)
        finally:
            temporary.unlink(missing_ok=True)


class PcArchiveStore(LocalArchiveStore):
    """Owner-protected archive located off the collector host."""

    def __init__(self, root: Path) -> None:
        super().__init__(
            root,
            destination_label="pc_filesystem",
            protected=True,
        )


class BotoS3ArchiveStore:
    """Production S3/B2 transport via boto3; never deletes or overwrites objects."""

    def __init__(
        self,
        config: B2ArchiveConfig,
        *,
        prefix: str = "",
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config = config
        self._prefix = str(_safe_key(prefix)).strip("/") if prefix else ""
        factory = client_factory or boto3.client
        self._client = factory(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=config.botocore_config(),
        )

    @property
    def destination_label(self) -> str:
        return "b2_s3"

    def _object_key(self, key: str) -> str:
        suffix = str(_safe_key(key))
        if self._prefix:
            return f"{self._prefix}/{suffix}"
        return suffix

    def _wrap_client_error(self, action: str, error: ClientError) -> ArchiveStoreError:
        message = sanitize_error_message(error)
        return ArchiveStoreError(f"S3 {action} failed: {message}")

    def exists(self, key: str) -> bool:
        object_key = self._object_key(key)
        try:
            self._client.head_object(Bucket=self._config.bucket, Key=object_key)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise self._wrap_client_error("head_object", error) from error
        return True

    def list_keys(self, prefix: str) -> list[str]:
        object_prefix = self._object_key(prefix)
        if object_prefix and not object_prefix.endswith("/"):
            object_prefix = f"{object_prefix}/"
        keys: list[str] = []
        continuation: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self._config.bucket,
                "Prefix": object_prefix,
            }
            if continuation:
                request["ContinuationToken"] = continuation
            try:
                response = self._client.list_objects_v2(**request)
            except ClientError as error:
                raise self._wrap_client_error("list_objects_v2", error) from error
            for item in response.get("Contents", []):
                key = str(item.get("Key", ""))
                if not key:
                    continue
                if self._prefix and key.startswith(f"{self._prefix}/"):
                    keys.append(key[len(self._prefix) + 1 :])
                elif not self._prefix:
                    keys.append(key)
            if not response.get("IsTruncated"):
                break
            continuation = response.get("NextContinuationToken")
            if not continuation:
                break
        return sorted(keys)

    def read_bytes(self, key: str) -> bytes:
        object_key = self._object_key(key)
        try:
            response = self._client.get_object(
                Bucket=self._config.bucket,
                Key=object_key,
            )
        except ClientError as error:
            raise self._wrap_client_error("get_object", error) from error
        body = response.get("Body")
        if body is None:
            raise ArchiveStoreError("S3 get_object returned no body")
        return bytes(body.read())

    def publish_bytes(self, key: str, value: bytes) -> None:
        if self.exists(key):
            raise ArchiveStoreError(f"refusing overwrite for existing key: {key}")
        object_key = self._object_key(key)
        try:
            self._client.put_object(
                Bucket=self._config.bucket,
                Key=object_key,
                Body=value,
            )
        except ClientError as error:
            raise self._wrap_client_error("put_object", error) from error

    def publish_file(self, key: str, source: Path) -> None:
        if self.exists(key):
            raise ArchiveStoreError(f"refusing overwrite for existing key: {key}")
        object_key = self._object_key(key)
        try:
            self._client.upload_file(
                str(source),
                self._config.bucket,
                object_key,
            )
        except ClientError as error:
            raise self._wrap_client_error("upload_file", error) from error

    def download_file(self, key: str, destination: Path) -> None:
        object_key = self._object_key(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(
                self._config.bucket,
                object_key,
                str(destination),
            )
        except ClientError as error:
            raise self._wrap_client_error("download_file", error) from error

    def append_bytes(self, key: str, value: bytes) -> None:
        """Append-only update for bounded evidence registries (e.g. quarantine JSONL)."""
        object_key = self._object_key(key)
        existing = b""
        try:
            response = self._client.get_object(
                Bucket=self._config.bucket,
                Key=object_key,
            )
            body = response.get("Body")
            if body is not None:
                existing = bytes(body.read())
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise self._wrap_client_error("get_object", error) from error
        try:
            self._client.put_object(
                Bucket=self._config.bucket,
                Key=object_key,
                Body=existing + value,
            )
        except ClientError as error:
            raise self._wrap_client_error("put_object", error) from error


class S3ArchiveStore:
    """Legacy PyArrow S3-compatible store for ``ARCHIVE_S3_*`` export-day."""

    def __init__(
        self,
        *,
        bucket: str,
        prefix: str = "",
        endpoint_override: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        filesystem: pafs.FileSystem | None = None,
    ) -> None:
        if not bucket or "/" in bucket:
            raise ValueError("archive bucket is invalid")
        self._bucket = bucket
        self._prefix = str(_safe_key(prefix)).strip("/") if prefix else ""
        self._filesystem = filesystem or pafs.S3FileSystem(
            endpoint_override=endpoint_override,
            access_key=access_key,
            secret_key=secret_key,
        )

    @classmethod
    def for_b2(cls, config: B2ArchiveConfig, *, prefix: str = "") -> BotoS3ArchiveStore:
        """Production B2 adapter; prefer this over ``B2ArchiveClient`` for uploads."""
        return BotoS3ArchiveStore(config, prefix=prefix)

    @property
    def destination_label(self) -> str:
        return "s3"

    def _path(self, key: str) -> str:
        suffix = str(_safe_key(key))
        middle = f"{self._prefix}/" if self._prefix else ""
        return f"{self._bucket}/{middle}{suffix}"

    def exists(self, key: str) -> bool:
        return bool(
            self._filesystem.get_file_info(self._path(key)).type == pafs.FileType.File
        )

    def list_keys(self, prefix: str) -> list[str]:
        safe_prefix = str(_safe_key(prefix))
        base_path = self._path(safe_prefix)
        if not base_path.endswith("/"):
            base_path = f"{base_path}/"
        selector = pafs.FileSelector(base_path, recursive=True)
        keys: list[str] = []
        bucket_prefix = f"{self._bucket}/"
        middle = f"{self._prefix}/" if self._prefix else ""
        for info in self._filesystem.get_file_info(selector):
            if info.type != pafs.FileType.File:
                continue
            full_path = info.path
            if full_path.startswith(bucket_prefix):
                full_path = full_path[len(bucket_prefix) :]
            if middle and full_path.startswith(middle):
                full_path = full_path[len(middle) :]
            keys.append(full_path)
        return sorted(keys)

    def read_bytes(self, key: str) -> bytes:
        with self._filesystem.open_input_file(self._path(key)) as stream:
            return bytes(stream.read())

    def publish_bytes(self, key: str, value: bytes) -> None:
        path = self._path(key)
        temporary = f"{path}.partial"
        with self._filesystem.open_output_stream(temporary) as stream:
            stream.write(value)
        self._filesystem.copy_file(temporary, path)
        self._filesystem.delete_file(temporary)

    def publish_file(self, key: str, source: Path) -> None:
        path = self._path(key)
        temporary = f"{path}.partial"
        with (
            source.open("rb") as input_stream,
            self._filesystem.open_output_stream(temporary) as output_stream,
        ):
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        self._filesystem.copy_file(temporary, path)
        self._filesystem.delete_file(temporary)

    def download_file(self, key: str, destination: Path) -> None:
        with (
            self._filesystem.open_input_file(self._path(key)) as input_stream,
            destination.open("wb") as output_stream,
        ):
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)

    def append_bytes(self, key: str, value: bytes) -> None:
        existing = self.read_bytes(key) if self.exists(key) else b""
        self.publish_bytes(key, existing + value)
