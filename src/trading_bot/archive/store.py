import os
import shutil
from pathlib import Path, PurePosixPath
from typing import Protocol

import pyarrow.fs as pafs  # type: ignore[import-untyped]


class ArchiveStore(Protocol):
    @property
    def destination_label(self) -> str: ...

    def exists(self, key: str) -> bool: ...

    def read_bytes(self, key: str) -> bytes: ...

    def publish_bytes(self, key: str, value: bytes) -> None: ...

    def publish_file(self, key: str, source: Path) -> None: ...

    def download_file(self, key: str, destination: Path) -> None: ...


def _safe_key(key: str) -> PurePosixPath:
    path = PurePosixPath(key)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("archive object key is unsafe")
    return path


class LocalArchiveStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def destination_label(self) -> str:
        return "filesystem"

    def _path(self, key: str) -> Path:
        return self._root.joinpath(*_safe_key(key).parts)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def publish_bytes(self, key: str, value: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
        try:
            temporary.write_bytes(value)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def publish_file(self, key: str, source: Path) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def download_file(self, key: str, destination: Path) -> None:
        shutil.copyfile(self._path(key), destination)


class S3ArchiveStore:
    """S3-compatible store; credentials are constructor inputs and are never logged."""

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
