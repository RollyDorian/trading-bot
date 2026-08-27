"""Backblaze B2 S3-compatible client for bounded research smoke only.

Credentials are passed explicitly to boto3; the default AWS credential chain is
not used. Remote smoke objects are never deleted automatically.

This module deliberately does not replace ``S3ArchiveStore`` (PyArrow /
``ARCHIVE_S3_*``). Smoke may keep a dedicated client; production archive must
later reuse or consolidate a shared S3-compatible abstraction rather than
maintaining two transport stacks.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from trading_bot.collector import sanitize_error_message

_REQUIRED_ENV = (
    "B2_S3_BUCKET",
    "B2_S3_ENDPOINT",
    "B2_S3_REGION",
    "B2_S3_ACCESS_KEY_ID",
    "B2_S3_SECRET_ACCESS_KEY",
)

CONNECT_TIMEOUT_DEFAULT = 10
READ_TIMEOUT_DEFAULT = 30
MAX_RETRIES_DEFAULT = 3

CONNECT_TIMEOUT_MIN = 1
CONNECT_TIMEOUT_MAX = 60
READ_TIMEOUT_MIN = 5
READ_TIMEOUT_MAX = 120
MAX_RETRIES_MIN = 0
MAX_RETRIES_MAX = 5

SMOKE_KEY_PREFIX = "smoke/"
DEFAULT_SMOKE_SIZE_BYTES = 2048
DEFAULT_SMOKE_MAX_SIZE_BYTES = 4096

ClientFactory = Callable[..., Any]


class B2ArchiveError(RuntimeError):
    """Raised for B2 archive failures with redacted messages."""


def _validate_object_key(object_key: str) -> str:
    path = PurePosixPath(object_key)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("object key is unsafe")
    return object_key


def _validate_smoke_object_key(object_key: str) -> str:
    key = _validate_object_key(object_key)
    if not key.startswith(SMOKE_KEY_PREFIX):
        raise ValueError("object key must start with smoke/")
    return key


def _parse_bounded_int(
    environ: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(name) from error
    if value < minimum or value > maximum:
        raise ValueError(name)
    return value


def _validate_endpoint(endpoint: str) -> str:
    if not endpoint:
        raise ValueError("B2_S3_ENDPOINT is invalid")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https":
        raise ValueError("B2_S3_ENDPOINT is invalid")
    if not parsed.hostname:
        raise ValueError("B2_S3_ENDPOINT is invalid")
    if parsed.username or parsed.password:
        raise ValueError("B2_S3_ENDPOINT is invalid")
    if parsed.path not in ("", "/"):
        raise ValueError("B2_S3_ENDPOINT is invalid")
    if parsed.query or parsed.fragment:
        raise ValueError("B2_S3_ENDPOINT is invalid")
    return endpoint.rstrip("/")


def _validate_bucket(bucket: str) -> str:
    if not bucket or "/" in bucket:
        raise ValueError("B2_S3_BUCKET is invalid")
    return bucket


def _validate_non_empty(value: str, name: str) -> str:
    if not value:
        raise ValueError(name)
    return value


@dataclass(frozen=True, slots=True)
class B2ArchiveConfig:
    bucket: str
    endpoint: str
    region: str
    access_key_id: str
    secret_access_key: str
    connect_timeout_seconds: int = CONNECT_TIMEOUT_DEFAULT
    read_timeout_seconds: int = READ_TIMEOUT_DEFAULT
    max_retries: int = MAX_RETRIES_DEFAULT

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> B2ArchiveConfig:
        source = os.environ if environ is None else environ
        missing = [name for name in _REQUIRED_ENV if not source.get(name, "").strip()]
        if missing:
            raise ValueError(", ".join(missing))

        bucket = _validate_bucket(source["B2_S3_BUCKET"].strip())
        endpoint = _validate_endpoint(source["B2_S3_ENDPOINT"].strip())
        region = _validate_non_empty(source["B2_S3_REGION"].strip(), "B2_S3_REGION")
        access_key_id = _validate_non_empty(
            source["B2_S3_ACCESS_KEY_ID"].strip(),
            "B2_S3_ACCESS_KEY_ID",
        )
        secret_access_key = _validate_non_empty(
            source["B2_S3_SECRET_ACCESS_KEY"].strip(),
            "B2_S3_SECRET_ACCESS_KEY",
        )
        connect_timeout_seconds = _parse_bounded_int(
            source,
            "B2_S3_CONNECT_TIMEOUT_SECONDS",
            default=CONNECT_TIMEOUT_DEFAULT,
            minimum=CONNECT_TIMEOUT_MIN,
            maximum=CONNECT_TIMEOUT_MAX,
        )
        read_timeout_seconds = _parse_bounded_int(
            source,
            "B2_S3_READ_TIMEOUT_SECONDS",
            default=READ_TIMEOUT_DEFAULT,
            minimum=READ_TIMEOUT_MIN,
            maximum=READ_TIMEOUT_MAX,
        )
        max_retries = _parse_bounded_int(
            source,
            "B2_S3_MAX_RETRIES",
            default=MAX_RETRIES_DEFAULT,
            minimum=MAX_RETRIES_MIN,
            maximum=MAX_RETRIES_MAX,
        )
        return cls(
            bucket=bucket,
            endpoint=endpoint,
            region=region,
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            max_retries=max_retries,
        )

    def redacted_summary(self) -> dict[str, object]:
        hostname = urlsplit(self.endpoint).hostname or ""
        return {
            "provider": "backblaze_b2",
            "bucket": self.bucket,
            "region": self.region,
            "endpoint_host": hostname,
            "credentials_set": True,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "read_timeout_seconds": self.read_timeout_seconds,
            "max_retries": self.max_retries,
        }

    def botocore_config(self) -> Config:
        return Config(
            connect_timeout=self.connect_timeout_seconds,
            read_timeout=self.read_timeout_seconds,
            retries={"max_attempts": self.max_retries, "mode": "standard"},
            signature_version="s3v4",
            # B2 expects path-style addressing for S3-compatible APIs.
            s3={"addressing_style": "path"},
            max_pool_connections=4,
        )


class B2ArchiveClient:
    """Explicit-credential B2 client; no delete API is exposed at this stage."""

    def __init__(
        self,
        config: B2ArchiveConfig,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._config = config
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
    def config(self) -> B2ArchiveConfig:
        return self._config

    def _wrap_client_error(self, action: str, error: ClientError) -> B2ArchiveError:
        message = sanitize_error_message(error)
        return B2ArchiveError(f"B2 {action} failed: {message}")

    def upload_file(self, local_path: Path, object_key: str) -> None:
        key = _validate_object_key(object_key)
        digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
        try:
            self._client.upload_file(
                str(local_path),
                self._config.bucket,
                key,
                ExtraArgs={"Metadata": {"sha256": digest}},
            )
        except ClientError as error:
            raise self._wrap_client_error("upload", error) from error

    def download_file(self, object_key: str, local_path: Path) -> None:
        key = _validate_object_key(object_key)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._client.download_file(self._config.bucket, key, str(local_path))
        except ClientError as error:
            raise self._wrap_client_error("download", error) from error

    def head_object(self, object_key: str) -> dict[str, object]:
        key = _validate_object_key(object_key)
        try:
            response = self._client.head_object(Bucket=self._config.bucket, Key=key)
        except ClientError as error:
            raise self._wrap_client_error("head_object", error) from error
        content_length = response.get("ContentLength")
        metadata = response.get("Metadata") or {}
        etag = response.get("ETag")
        return {
            "content_length": content_length,
            "metadata": dict(metadata),
            "etag": etag.strip('"') if isinstance(etag, str) else etag,
        }


def _new_smoke_object_key() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{SMOKE_KEY_PREFIX}{timestamp}-{secrets.token_hex(8)}.bin"


def run_roundtrip_smoke(
    client: B2ArchiveClient,
    *,
    work_dir: Path,
    size_bytes: int = DEFAULT_SMOKE_SIZE_BYTES,
    max_size_bytes: int = DEFAULT_SMOKE_MAX_SIZE_BYTES,
) -> dict[str, object]:
    """Upload synthetic bytes under smoke/, download, and compare SHA-256.

    The remote object is intentionally retained for operator review.
    """
    if size_bytes <= 0:
        raise ValueError("size_bytes must be positive")
    if size_bytes > max_size_bytes:
        raise ValueError("size_bytes exceeds smoke maximum")

    work_dir.mkdir(parents=True, exist_ok=True)
    payload = secrets.token_bytes(size_bytes)
    sha256_hex = hashlib.sha256(payload).hexdigest()
    upload_path = work_dir / f"smoke-upload-{secrets.token_hex(8)}.bin"
    download_path = work_dir / f"smoke-download-{secrets.token_hex(8)}.bin"
    upload_path.write_bytes(payload)

    object_key = _new_smoke_object_key()
    _validate_smoke_object_key(object_key)
    client.upload_file(upload_path, object_key)
    client.download_file(object_key, download_path)

    downloaded = download_path.read_bytes()
    verified = (
        len(downloaded) == size_bytes
        and hashlib.sha256(downloaded).hexdigest() == sha256_hex
    )

    # Optional remote metadata check; body SHA-256 remains authoritative.
    try:
        head = client.head_object(object_key)
        remote_length = head.get("content_length")
        metadata = head.get("metadata")
        if isinstance(remote_length, int) and remote_length != size_bytes:
            verified = False
        if isinstance(metadata, dict):
            remote_sha = metadata.get("sha256")
            if isinstance(remote_sha, str) and remote_sha != sha256_hex:
                verified = False
    except B2ArchiveError:
        pass

    return {
        "object_key": object_key,
        "size_bytes": size_bytes,
        "sha256_hex": sha256_hex,
        "verified": verified,
        "remote_retained": True,
    }
