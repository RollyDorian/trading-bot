import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from trading_bot import archive_cli
from trading_bot.archive.b2 import (
    DEFAULT_SMOKE_MAX_SIZE_BYTES,
    B2ArchiveClient,
    B2ArchiveConfig,
    B2ArchiveError,
    run_roundtrip_smoke,
)


def _valid_environ(**overrides: str) -> dict[str, str]:
    base = {
        "B2_S3_BUCKET": "research-smoke",
        "B2_S3_ENDPOINT": "https://s3.us-west-004.backblazeb2.com",
        "B2_S3_REGION": "us-west-004",
        "B2_S3_ACCESS_KEY_ID": "test-access-key-id",
        "B2_S3_SECRET_ACCESS_KEY": "test-secret-access-key-value",
    }
    base.update(overrides)
    return base


def test_config_missing_vars_lists_names_only() -> None:
    with pytest.raises(ValueError, match="B2_S3_BUCKET, B2_S3_ENDPOINT"):
        B2ArchiveConfig.from_environ({})


def test_config_rejects_invalid_endpoint_schemes() -> None:
    environ = _valid_environ(B2_S3_ENDPOINT="ftp://example.invalid")
    with pytest.raises(ValueError, match="B2_S3_ENDPOINT is invalid"):
        B2ArchiveConfig.from_environ(environ)

    environ = _valid_environ(
        B2_S3_ENDPOINT="https://user:password@example.invalid"
    )
    with pytest.raises(ValueError, match="B2_S3_ENDPOINT is invalid"):
        B2ArchiveConfig.from_environ(environ)

    environ = _valid_environ(
        B2_S3_ENDPOINT="https://example.invalid/path/with/secrets"
    )
    with pytest.raises(ValueError, match="B2_S3_ENDPOINT is invalid"):
        B2ArchiveConfig.from_environ(environ)


def test_config_redacted_summary_never_includes_secrets() -> None:
    config = B2ArchiveConfig.from_environ(_valid_environ())
    summary = config.redacted_summary()
    assert summary == {
        "provider": "backblaze_b2",
        "bucket": "research-smoke",
        "region": "us-west-004",
        "endpoint_host": "s3.us-west-004.backblazeb2.com",
        "credentials_set": True,
        "connect_timeout_seconds": 10,
        "read_timeout_seconds": 30,
        "max_retries": 3,
    }
    dumped = json.dumps(summary)
    assert "test-access-key-id" not in dumped
    assert "test-secret-access-key-value" not in dumped


def test_client_passes_explicit_credentials_and_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def factory(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("trading_bot.archive.b2.boto3.client", factory)
    config = B2ArchiveConfig.from_environ(_valid_environ())
    B2ArchiveClient(config)

    kwargs = captured["kwargs"]
    assert kwargs["aws_access_key_id"] == "test-access-key-id"
    assert kwargs["aws_secret_access_key"] == "test-secret-access-key-value"
    assert kwargs["endpoint_url"] == "https://s3.us-west-004.backblazeb2.com"
    assert kwargs["region_name"] == "us-west-004"
    botocore_config = kwargs["config"]
    assert botocore_config.connect_timeout == 10
    assert botocore_config.read_timeout == 30
    assert botocore_config.retries["max_attempts"] == 3
    assert botocore_config.signature_version == "s3v4"
    assert botocore_config.s3["addressing_style"] == "path"


class _FakeS3Client:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str, dict[str, str]]] = []
        self.downloads: list[tuple[str, str, str]] = []
        self.heads: list[tuple[str, str]] = []
        self.deletes: list[tuple[str, str]] = []
        self.upload_error: ClientError | None = None
        self.download_error: ClientError | None = None
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    def upload_file(
        self,
        filename: str,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> None:
        if self.upload_error is not None:
            raise self.upload_error
        metadata = {}
        if ExtraArgs and "Metadata" in ExtraArgs:
            metadata = dict(ExtraArgs["Metadata"])  # type: ignore[arg-type]
        self.uploads.append((filename, bucket, key, metadata))
        self.objects[key] = Path(filename).read_bytes()
        self.metadata[key] = metadata

    def download_file(self, bucket: str, key: str, filename: str, **_kwargs: object) -> None:
        if self.download_error is not None:
            raise self.download_error
        self.downloads.append((bucket, key, filename))
        Path(filename).write_bytes(self.objects[key])

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.heads.append((Bucket, Key))
        payload = self.objects[Key]
        return {
            "ContentLength": len(payload),
            "Metadata": self.metadata.get(Key, {}),
            "ETag": f'"{hashlib.sha256(payload).hexdigest()}"',
        }

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deletes.append((Bucket, Key))


def _client_with_fake(fake: _FakeS3Client) -> B2ArchiveClient:
    config = B2ArchiveConfig.from_environ(_valid_environ())
    return B2ArchiveClient(config, client_factory=lambda *_a, **_k: fake)


def _client_error(code: str, message: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "TestOperation",
    )


def test_upload_failure_is_sanitized(tmp_path: Path) -> None:
    fake = _FakeS3Client()
    fake.upload_error = _client_error(
        "AccessDenied",
        "secret=test-secret-access-key-value endpoint=https://user:pass@host",
    )
    client = _client_with_fake(fake)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"abc")
    with pytest.raises(B2ArchiveError) as error:
        client.upload_file(source, "smoke/test.bin")
    message = str(error.value)
    assert "test-secret-access-key-value" not in message
    assert "user:pass" not in message


def test_download_failure_is_sanitized() -> None:
    fake = _FakeS3Client()
    fake.download_error = _client_error(
        "NoSuchKey",
        "token=test-secret-access-key-value",
    )
    client = _client_with_fake(fake)
    with pytest.raises(B2ArchiveError) as error:
        client.download_file("smoke/test.bin", Path("missing.bin"))
    assert "test-secret-access-key-value" not in str(error.value)


def test_roundtrip_smoke_verifies_checksum(tmp_path: Path) -> None:
    client = _client_with_fake(_FakeS3Client())
    result = run_roundtrip_smoke(client, work_dir=tmp_path, size_bytes=128)
    assert result["verified"] is True
    assert result["remote_retained"] is True
    assert str(result["object_key"]).startswith("smoke/")
    assert result["size_bytes"] == 128
    assert isinstance(result["sha256_hex"], str)


def test_roundtrip_smoke_rejects_oversize(tmp_path: Path) -> None:
    client = _client_with_fake(_FakeS3Client())
    with pytest.raises(ValueError, match="exceeds smoke maximum"):
        run_roundtrip_smoke(
            client,
            work_dir=tmp_path,
            size_bytes=DEFAULT_SMOKE_MAX_SIZE_BYTES + 1,
        )


def test_roundtrip_smoke_keys_are_unique(tmp_path: Path) -> None:
    client = _client_with_fake(_FakeS3Client())
    first = run_roundtrip_smoke(client, work_dir=tmp_path, size_bytes=64)
    second = run_roundtrip_smoke(client, work_dir=tmp_path, size_bytes=64)
    assert first["object_key"] != second["object_key"]


def test_roundtrip_smoke_marks_verified_false_on_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = _FakeS3Client()
    client = _client_with_fake(fake)

    def corrupt_download(bucket: str, key: str, filename: str) -> None:
        fake.downloads.append((bucket, key, filename))
        Path(filename).write_bytes(b"tampered")

    fake.download_file = corrupt_download  # type: ignore[method-assign]
    result = run_roundtrip_smoke(client, work_dir=tmp_path, size_bytes=32)
    assert result["verified"] is False


def test_roundtrip_smoke_cli_exits_nonzero_when_unverified(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("B2_S3_BUCKET", "research-smoke")
    monkeypatch.setenv("B2_S3_ENDPOINT", "https://s3.us-west-004.backblazeb2.com")
    monkeypatch.setenv("B2_S3_REGION", "us-west-004")
    monkeypatch.setenv("B2_S3_ACCESS_KEY_ID", "cli-access-key")
    monkeypatch.setenv("B2_S3_SECRET_ACCESS_KEY", "cli-secret-value")

    def fake_smoke(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {
            "object_key": "smoke/unverified.bin",
            "size_bytes": 32,
            "sha256_hex": "0" * 64,
            "verified": False,
            "remote_retained": True,
        }

    monkeypatch.setattr(archive_cli, "run_roundtrip_smoke", fake_smoke)
    monkeypatch.setattr(
        "trading_bot.archive.b2.boto3.client",
        lambda *_a, **_k: object(),
    )
    monkeypatch.setattr(sys, "argv", ["hibachi-archive", "archive-roundtrip-smoke"])
    with pytest.raises(SystemExit) as error:
        archive_cli.main()
    assert error.value.code == 1
    assert '"verified":false' in capsys.readouterr().out.replace(" ", "")


def test_roundtrip_smoke_never_deletes_remote_object(tmp_path: Path) -> None:
    fake = _FakeS3Client()
    client = _client_with_fake(fake)
    run_roundtrip_smoke(client, work_dir=tmp_path, size_bytes=32)
    assert fake.deletes == []


def test_check_config_cli_has_no_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("B2_S3_BUCKET", "research-smoke")
    monkeypatch.setenv("B2_S3_ENDPOINT", "https://s3.us-west-004.backblazeb2.com")
    monkeypatch.setenv("B2_S3_REGION", "us-west-004")
    monkeypatch.setenv("B2_S3_ACCESS_KEY_ID", "cli-access-key")
    monkeypatch.setenv("B2_S3_SECRET_ACCESS_KEY", "cli-secret-value")

    def fail_network(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("network client must not be created")

    monkeypatch.setattr("trading_bot.archive.b2.boto3.client", fail_network)
    monkeypatch.setattr(sys, "argv", ["hibachi-archive", "archive-check-config"])
    archive_cli.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["provider"] == "backblaze_b2"
    assert "cli-secret-value" not in captured.out
    assert captured.err == ""


def test_roundtrip_smoke_cli_rejects_oversize_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("B2_S3_BUCKET", "research-smoke")
    monkeypatch.setenv("B2_S3_ENDPOINT", "https://s3.us-west-004.backblazeb2.com")
    monkeypatch.setenv("B2_S3_REGION", "us-west-004")
    monkeypatch.setenv("B2_S3_ACCESS_KEY_ID", "cli-access-key")
    monkeypatch.setenv("B2_S3_SECRET_ACCESS_KEY", "cli-secret-value")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hibachi-archive",
            "archive-roundtrip-smoke",
            "--size-bytes",
            str(DEFAULT_SMOKE_MAX_SIZE_BYTES + 1),
        ],
    )
    with pytest.raises(SystemExit) as error:
        archive_cli.main()
    assert error.value.code == 2
