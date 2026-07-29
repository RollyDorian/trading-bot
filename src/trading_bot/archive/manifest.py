import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ArchiveObject:
    dataset: str
    key: str
    row_count: int
    size_bytes: int
    sha256: str
    min_raw_event_id: int
    max_raw_event_id: int
    raw_id_sha256: str
    parquet_schema_sha256: str


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    dataset_group: str
    interval_start_utc: str
    interval_end_utc: str
    symbol: str
    min_raw_event_id: int
    max_raw_event_id: int
    raw_row_count: int
    raw_id_sha256: str
    pipeline_version: int
    schema_version: int
    created_at_utc: str
    destination: str
    verification_status: Literal["verified"]
    objects: tuple[ArchiveObject, ...]

    def to_bytes(self) -> bytes:
        return (
            json.dumps(asdict(self), separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, value: bytes) -> "ArchiveManifest":
        raw: dict[str, Any] = json.loads(value)
        objects = tuple(ArchiveObject(**item) for item in raw.pop("objects"))
        return cls(objects=objects, **raw)


@dataclass(frozen=True, slots=True)
class ArchiveCheckpoint:
    interval_start_utc: str
    interval_end_utc: str
    symbol: str
    last_raw_event_id: int
    row_count: int
    objects: tuple[ArchiveObject, ...]

    def to_bytes(self) -> bytes:
        return (
            json.dumps(asdict(self), separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, value: bytes) -> "ArchiveCheckpoint":
        raw: dict[str, Any] = json.loads(value)
        objects = tuple(ArchiveObject(**item) for item in raw.pop("objects"))
        return cls(objects=objects, **raw)


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("archive timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def raw_id_digest(ids: list[int]) -> str:
    digest = hashlib.sha256()
    for raw_id in ids:
        digest.update(raw_id.to_bytes(8, byteorder="big", signed=True))
    return digest.hexdigest()
