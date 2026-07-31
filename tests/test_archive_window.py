import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from trading_bot import archive_cli
from trading_bot.archive.b2 import B2ArchiveConfig
from trading_bot.archive.store import (
    ArchiveStoreError,
    BotoS3ArchiveStore,
    LocalArchiveStore,
    S3ArchiveStore,
)
from trading_bot.archive.window import (
    DEFAULT_MAX_BUNDLE_BYTES,
    DEFAULT_MAX_ROWS,
    HARD_MAX_ROWS,
    LOGICAL_CHECKSUM_ARTIFACTS,
    OPERATIONAL_DISK_FLOOR_BYTES,
    PHYSICAL_CHECKSUM_ARTIFACTS,
    UPLOAD_ARTIFACTS,
    VERIFICATION_DIRNAME,
    WindowExportError,
    WindowExportLimits,
    _read_logical_checksums,
    _read_physical_checksums,
    _write_logical_checksums,
    _write_physical_checksums,
    build_archive_bundle,
    upload_archive_bundle,
    verify_restore_archive,
)
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 18, tzinfo=UTC)
END = START + timedelta(minutes=30)
KNOWN_GIT_SHA = "abc123deadbeef00000000000000000000000001"


def _valid_environ() -> dict[str, str]:
    return {
        "B2_S3_BUCKET": "research-archive",
        "B2_S3_ENDPOINT": "https://s3.us-west-004.backblazeb2.com",
        "B2_S3_REGION": "us-west-004",
        "B2_S3_ACCESS_KEY_ID": "test-access-key-id",
        "B2_S3_SECRET_ACCESS_KEY": "test-secret-access-key-value",
    }


def _event(event_id: int, seconds: int, *, price: str = "100") -> MarketEvent:
    return MarketEvent(
        id=event_id,
        received_at=START + timedelta(seconds=seconds, milliseconds=event_id),
        exchange_at=START + timedelta(seconds=seconds),
        source="hibachi_ws",
        event_type="trades",
        symbol="ETH/USDT-P",
        sequence=event_id,
        connection_id="11111111-1111-1111-1111-111111111111",
        local_sequence=event_id,
        exchange_sequence=event_id,
        schema_version=2,
        latency_ms=1.0,
        payload={"topic": "trades", "price": price, "quantity": "1"},
    )


def _events(count: int = 3) -> list[MarketEvent]:
    return [_event(index + 1, index, price=str(100 + index)) for index in range(count)]


def _sufficient_free_bytes(*, limits: WindowExportLimits | None = None) -> int:
    limits = limits or WindowExportLimits()
    return OPERATIONAL_DISK_FLOOR_BYTES + 2 * limits.max_bundle_bytes + 1024**2


@pytest.fixture(autouse=True)
def _mock_disk_headroom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trading_bot.archive.window.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": _sufficient_free_bytes()})(),
    )


@pytest.fixture(autouse=True)
def _mock_git_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "trading_bot.research.dataset._git_commit",
        lambda: KNOWN_GIT_SHA,
    )


def _build_bundle(
    tmp_path: Path,
    events: list[MarketEvent] | None = None,
    *,
    limits: WindowExportLimits | None = None,
) -> Path:
    return build_archive_bundle(
        symbol="ETH/USDT-P",
        start=START,
        end=END,
        output_dir=tmp_path,
        events=events or _events(),
        limits=limits,
    )


class _FakeBotoClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.uploads: list[tuple[str, str, str]] = []
        self.puts: list[tuple[str, str, bytes]] = []
        self.deletes: list[tuple[str, str]] = []
        self.fail_on_key: str | None = None
        self.upload_count = 0

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "not found"}},
                "HeadObject",
            )
        payload = self.objects[Key]
        return {"ContentLength": len(payload)}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}},
                "GetObject",
            )

        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"Body": _Body(self.objects[Key])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        self.puts.append((Bucket, Key, Body))
        self.objects[Key] = Body

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self.upload_count += 1
        if self.fail_on_key == key:
            raise ClientError(
                {"Error": {"Code": "InternalError", "Message": "boom"}},
                "UploadFile",
            )
        self.uploads.append((filename, bucket, key))
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        Path(filename).write_bytes(self.objects[key])

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.deletes.append((Bucket, Key))


def _boto_store(fake: _FakeBotoClient) -> BotoS3ArchiveStore:
    config = B2ArchiveConfig.from_environ(_valid_environ())
    return BotoS3ArchiveStore(
        config,
        client_factory=lambda *_a, **_k: fake,
    )


def test_real_git_provenance_in_provenance_json(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    provenance = json.loads((bundle / "provenance.json").read_text(encoding="utf-8"))
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert provenance["git_commit"] == KNOWN_GIT_SHA
    assert manifest["software"]["git_commit"] == KNOWN_GIT_SHA


def test_provenance_change_does_not_change_logical_checksum(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _events()
    first = _build_bundle(tmp_path / "a", events)
    logical_first = (first / "logical_checksums.sha256").read_text(encoding="utf-8")

    monkeypatch.setattr(
        "trading_bot.research.dataset._git_commit",
        lambda: "ffffffffffffffffffffffffffffffffffffffff",
    )
    second = _build_bundle(tmp_path / "b", events)
    logical_second = (second / "logical_checksums.sha256").read_text(encoding="utf-8")

    assert logical_first == logical_second
    assert (
        json.loads((first / "provenance.json").read_text(encoding="utf-8"))["git_commit"]
        != json.loads((second / "provenance.json").read_text(encoding="utf-8"))["git_commit"]
    )


def test_event_content_changes_logical_checksum(tmp_path: Path) -> None:
    first = _build_bundle(tmp_path / "a", _events(3))
    second = _build_bundle(
        tmp_path / "b",
        [_event(1, 0, price="200"), _event(2, 1, price="201"), _event(3, 2, price="202")],
    )
    assert (first / "logical_checksums.sha256").read_text() != (
        second / "logical_checksums.sha256"
    ).read_text()


def test_logical_identity_stable_across_rebuilds(tmp_path: Path) -> None:
    events = _events()
    first = _build_bundle(tmp_path / "a", events)
    second = _build_bundle(tmp_path / "b", events)
    assert (first / "logical_checksums.sha256").read_text() == (
        second / "logical_checksums.sha256"
    ).read_text()
    assert hashlib.sha256((first / "events.parquet").read_bytes()).hexdigest() == (
        hashlib.sha256((second / "events.parquet").read_bytes()).hexdigest()
    )


def test_row_limit_aborts(tmp_path: Path) -> None:
    with pytest.raises(WindowExportError, match="event count exceeds"):
        _build_bundle(
            tmp_path,
            _events(3),
            limits=WindowExportLimits(max_rows=2),
        )


def test_window_duration_limit_aborts(tmp_path: Path) -> None:
    with pytest.raises(WindowExportError, match="window duration exceeds"):
        build_archive_bundle(
            symbol="ETH/USDT-P",
            start=START,
            end=START + timedelta(hours=2),
            output_dir=tmp_path,
            events=_events(),
            limits=WindowExportLimits(max_duration_seconds=3_600),
        )


def test_bundle_byte_limit_aborts(tmp_path: Path) -> None:
    with pytest.raises(WindowExportError, match="bundle size exceeds"):
        _build_bundle(
            tmp_path,
            _events(),
            limits=WindowExportLimits(max_bundle_bytes=1),
        )


def test_preflight_2x_footprint_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    limits = WindowExportLimits(max_bundle_bytes=DEFAULT_MAX_BUNDLE_BYTES)
    required = OPERATIONAL_DISK_FLOOR_BYTES + 2 * limits.max_bundle_bytes
    monkeypatch.setattr(
        "trading_bot.archive.window.shutil.disk_usage",
        lambda _path: type("Usage", (), {"free": required - 1})(),
    )
    with pytest.raises(WindowExportError, match="verification temp footprint"):
        _build_bundle(tmp_path, limits=limits)


def test_post_export_disk_headroom_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    limits = WindowExportLimits()
    call_count = {"n": 0}

    def fake_usage(_path: Path) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return type("Usage", (), {"free": _sufficient_free_bytes(limits=limits)})()
        return type("Usage", (), {"free": OPERATIONAL_DISK_FLOOR_BYTES})()

    monkeypatch.setattr("trading_bot.archive.window.shutil.disk_usage", fake_usage)
    with pytest.raises(WindowExportError, match="after bundle build"):
        _build_bundle(tmp_path, limits=limits)


def test_min_disk_floor_enforced_in_limits() -> None:
    with pytest.raises(ValueError, match="operational disk floor"):
        WindowExportLimits(min_free_disk_bytes=OPERATIONAL_DISK_FLOOR_BYTES - 1)


def test_quality_rejected_blocks_upload(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    quality_path = bundle / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["status"] = "rejected"
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    result = upload_archive_bundle(
        bundle,
        LocalArchiveStore(tmp_path / "store"),
        confirm_upload=True,
    )
    assert result["status"] == "failed"
    assert "rejected" in str(result["error"])


def test_quality_warning_requires_flag(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    quality_path = bundle / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["status"] = "warning"
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    _write_logical_checksums(bundle)
    _write_physical_checksums(bundle)
    blocked = upload_archive_bundle(
        bundle,
        LocalArchiveStore(tmp_path / "blocked-store"),
        confirm_upload=True,
        allow_quality_warnings=False,
    )
    assert blocked["status"] == "failed"
    allowed = upload_archive_bundle(
        bundle,
        LocalArchiveStore(tmp_path / "allowed-store"),
        confirm_upload=True,
        allow_quality_warnings=True,
        verification_root=tmp_path / VERIFICATION_DIRNAME,
    )
    assert allowed["status"] == "verified"


def test_schema_five_required_on_upload(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    quality_path = bundle / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["quality_report_version"] = 4
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    result = upload_archive_bundle(
        bundle,
        LocalArchiveStore(tmp_path / "store"),
        confirm_upload=True,
    )
    assert result["status"] == "failed"
    assert "schema 5" in str(result["error"])


def test_existing_remote_object_aborts_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trading_bot.archive.window._new_attempt_id",
        lambda: "fixed-attempt",
    )
    bundle = _build_bundle(tmp_path)
    fake = _FakeBotoClient()
    store = _boto_store(fake)
    dataset_id = bundle.name
    fake.objects[f"archives/{dataset_id}/attempts/fixed-attempt/events.parquet"] = b"existing"
    result = upload_archive_bundle(
        bundle,
        store,
        confirm_upload=True,
        verification_root=tmp_path / VERIFICATION_DIRNAME,
    )
    assert result["status"] == "failed"
    assert result["existing_keys"]
    assert fake.upload_count == 0
    assert fake.deletes == []


def test_partial_upload_writes_incomplete_not_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trading_bot.archive.window._new_attempt_id",
        lambda: "fixed-attempt",
    )
    bundle = _build_bundle(tmp_path)
    fake = _FakeBotoClient()
    store = _boto_store(fake)
    dataset_id = bundle.name
    fake.fail_on_key = f"archives/{dataset_id}/attempts/fixed-attempt/manifest.json"
    result = upload_archive_bundle(
        bundle,
        store,
        confirm_upload=True,
        verification_root=tmp_path / VERIFICATION_DIRNAME,
    )
    assert result["status"] == "failed"
    assert result["uploaded_keys"]
    assert result.get("orphan_risk") is True
    assert fake.deletes == []
    completed_key = f"archives/{dataset_id}/COMPLETED"
    incomplete_key = f"archives/{dataset_id}/attempts/fixed-attempt/INCOMPLETE"
    assert completed_key not in fake.objects
    assert incomplete_key in fake.objects
    verification_path = tmp_path / VERIFICATION_DIRNAME / dataset_id / "remote_verification.json"
    assert verification_path.is_file()
    assert not (bundle / "remote_verification.json").exists()


def test_completed_marker_only_after_all_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trading_bot.archive.window._new_attempt_id",
        lambda: "fixed-attempt",
    )
    bundle = _build_bundle(tmp_path)
    store = LocalArchiveStore(tmp_path / "remote")
    dataset_id = bundle.name
    upload = upload_archive_bundle(
        bundle,
        store,
        confirm_upload=True,
        verification_root=tmp_path / VERIFICATION_DIRNAME,
    )
    assert upload["status"] == "verified"
    completed_key = f"archives/{dataset_id}/COMPLETED"
    assert store.exists(completed_key)
    completed = json.loads(store.read_bytes(completed_key).decode("utf-8"))
    assert completed["status"] == "COMPLETED"
    assert completed["attempt_id"] == "fixed-attempt"
    assert completed["logical_checksums_sha256"]


def test_verification_report_not_in_checksum_artifacts(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    logical = set(_read_logical_checksums(bundle))
    physical = set(_read_physical_checksums(bundle))
    assert "remote_verification.json" not in logical
    assert "remote_verification.json" not in physical
    assert set(LOGICAL_CHECKSUM_ARTIFACTS) == logical
    assert set(PHYSICAL_CHECKSUM_ARTIFACTS) == physical
    assert "remote_verification.json" not in UPLOAD_ARTIFACTS


def test_checksum_mismatch_on_verify_restore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trading_bot.archive.window._new_attempt_id",
        lambda: "fixed-attempt",
    )
    bundle = _build_bundle(tmp_path)
    remote_root = tmp_path / "remote"
    store = LocalArchiveStore(remote_root)
    dataset_id = bundle.name
    upload = upload_archive_bundle(
        bundle,
        store,
        confirm_upload=True,
        verification_root=tmp_path / VERIFICATION_DIRNAME,
    )
    assert upload["status"] == "verified"
    tampered = (
        remote_root
        / "archives"
        / dataset_id
        / "attempts"
        / "fixed-attempt"
        / "events.parquet"
    )
    tampered.write_bytes(b"tampered")
    result = verify_restore_archive(store, dataset_id, tmp_path / "restore")
    assert result["status"] == "failed"
    assert "checksum" in str(result.get("error", "")).lower()


def test_verify_restore_refuses_without_completed_marker(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    store = LocalArchiveStore(tmp_path / "remote")
    dataset_id = bundle.name
    result = verify_restore_archive(store, dataset_id, tmp_path / "restore")
    assert result["status"] == "failed"
    assert "COMPLETED" in str(result.get("error", ""))


def test_verify_restore_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "trading_bot.archive.window._new_attempt_id",
        lambda: "fixed-attempt",
    )
    bundle = _build_bundle(tmp_path)
    store = LocalArchiveStore(tmp_path / "remote")
    dataset_id = bundle.name
    upload = upload_archive_bundle(
        bundle,
        store,
        confirm_upload=True,
        verification_root=tmp_path / VERIFICATION_DIRNAME,
    )
    assert upload["status"] == "verified"
    result = verify_restore_archive(store, dataset_id, tmp_path / "restore")
    assert result["status"] == "verified"
    assert result["quality_status"] == "pass"
    assert result["attempt_id"] == "fixed-attempt"


def test_boto_store_refuses_overwrite(tmp_path: Path) -> None:
    fake = _FakeBotoClient()
    store = _boto_store(fake)
    fake.objects["payload.bin"] = b"exists"
    with pytest.raises(ArchiveStoreError, match="refusing overwrite"):
        store.publish_bytes("payload.bin", b"new")


def test_s3_archive_store_for_b2_uses_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def factory(*_args: object, **kwargs: object) -> _FakeBotoClient:
        captured["kwargs"] = kwargs
        return _FakeBotoClient()

    monkeypatch.setattr("trading_bot.archive.store.boto3.client", factory)
    config = B2ArchiveConfig.from_environ(_valid_environ())
    store = S3ArchiveStore.for_b2(config)
    assert store.destination_label == "b2_s3"
    kwargs = captured["kwargs"]
    assert kwargs["aws_access_key_id"] == "test-access-key-id"
    assert kwargs["aws_secret_access_key"] == "test-secret-access-key-value"


def test_dry_run_upload_has_no_network_side_effects(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    result = upload_archive_bundle(
        bundle,
        LocalArchiveStore(tmp_path / "store"),
        confirm_upload=False,
    )
    assert result["status"] == "dry_run"
    assert result["confirm_upload"] is False


def test_archive_metadata_and_checksum_files_exist(tmp_path: Path) -> None:
    bundle = _build_bundle(tmp_path)
    metadata = json.loads((bundle / "archive_metadata.json").read_text(encoding="utf-8"))
    assert metadata["symbol"] == "ETH/USDT-P"
    assert metadata["topics"] == {"trades": 3}
    logical = _read_logical_checksums(bundle)
    physical = _read_physical_checksums(bundle)
    assert set(logical) == set(LOGICAL_CHECKSUM_ARTIFACTS)
    assert set(physical) == set(PHYSICAL_CHECKSUM_ARTIFACTS)
    assert (bundle / "provenance.json").is_file()


def test_hard_cap_enforced_in_limits() -> None:
    with pytest.raises(ValueError, match="hard cap"):
        WindowExportLimits(max_rows=HARD_MAX_ROWS + 1)


@pytest.mark.skip(reason="set B2_S3_INTEGRATION=1 to run network archive tests")
def test_optional_b2_integration_skipped_by_default() -> None:
    pass


def test_export_window_cli_local_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FakeEngine:
        async def dispose(self) -> None:
            return None

    async def fake_load(*_args: object, **_kwargs: object) -> list[MarketEvent]:
        return _events()

    monkeypatch.setattr(archive_cli, "create_engine", lambda _url: _FakeEngine())
    monkeypatch.setattr(archive_cli, "load_window_events", fake_load)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hibachi-archive",
            "archive-export-window",
            "--start",
            START.isoformat(),
            "--end",
            END.isoformat(),
            "--output-dir",
            str(tmp_path),
            "--max-rows",
            str(DEFAULT_MAX_ROWS),
            "--max-bytes",
            str(DEFAULT_MAX_BUNDLE_BYTES),
        ],
    )
    archive_cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "local_ready"
    assert payload["upload"]["status"] == "dry_run"
