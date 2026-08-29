"""Read-only verified-B2 inventory and chronological discovery/OOS freeze.

Does not mutate B2 or scan production PostgreSQL as a historical source.
Corpus roles are assigned from archive timestamps before any price-path stats.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.archive.store import ArchiveStore
from trading_bot.archive.window import ARCHIVE_KEY_PREFIX, COMPLETED_MARKER_NAME
from trading_bot.research.pipeline.first_passage_opportunity import (
    MIN_OOS_RESERVED_UTC_DAYS,
    OOS_RESERVED_UTC_DAYS,
    _dt,
)
from trading_bot.research.pipeline.market_state import build_market_state_1s
from trading_bot.research.pipeline.normalize_offline import normalize_events_parquet

# Frozen v1 OOS dates. Expansion must not analyze first-passage on these days
# and must not silently reassign later verified days into the same OOS bucket.
V1_UNTOUCHED_OOS_UTC_DATES: tuple[str, ...] = (
    "2026-08-07",
    "2026-08-09",
    "2026-08-10",
)
# Predeclared full-day bar (not fitted from v1 hit rates). A UTC date is full
# when non-quarantined COMPLETED windows cover at least this many hours of it.
FULL_UTC_DAY_MIN_ELIGIBLE_HOURS = 23.0
NEW_HOLDOUT_MIN_FULL_UTC_DAYS = 2

_DATASET_ID_RE = re.compile(
    r"^(?P<symbol>[a-z0-9-]+)_"
    r"(?P<start>\d{8}T\d{12}Z)_"
    r"(?P<end>\d{8}T\d{12}Z)_"
    r"v(?P<ver>\d+)$"
)


def operator_b2_env_candidates() -> tuple[Path, ...]:
    """Standard operator credential files. Values are never logged."""

    extra = os.environ.get("HIBACHI_B2_ENV", "").strip()
    paths = [
        Path(".env"),
        Path.home() / ".env",
        Path.home() / ".config" / "trading-bot" / "b2.env",
    ]
    if extra:
        paths.insert(0, Path(extra))
    return tuple(paths)


def load_operator_b2_environ() -> dict[str, Any]:
    """Load B2 env from operator files without overriding a live shell.

    Returns metadata only (which path names existed), never secret values.
    """

    loaded: list[str] = []
    for path in operator_b2_env_candidates():
        if load_optional_dotenv(path):
            loaded.append(path.name)
    return {
        "loaded_filenames": loaded,
        "b2_bucket_present": bool(os.environ.get("B2_S3_BUCKET", "").strip()),
        "mutations": False,
    }


def load_optional_dotenv(path: Path) -> bool:
    """Load KEY=VALUE lines into os.environ without overriding existing values.

    Never logs values. Returns True when the file existed.
    """

    if not path.is_file():
        return False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return True


def parse_dataset_id_window(dataset_id: str) -> tuple[datetime | None, datetime | None]:
    """Parse compact dataset_id timestamps (same layout as generate_dataset_id)."""

    match = _DATASET_ID_RE.match(dataset_id)
    if match is None:
        return None, None
    fmt = "%Y%m%dT%H%M%S%fZ"
    start = datetime.strptime(match.group("start"), fmt).replace(tzinfo=UTC)
    end = datetime.strptime(match.group("end"), fmt).replace(tzinfo=UTC)
    return start, end


def list_verified_eth_completed(store: ArchiveStore) -> list[dict[str, Any]]:
    """Index COMPLETED ETH RAW windows from the existing integrity marker contract.

    Only objects whose canonical COMPLETED marker has status COMPLETED are kept.
    Quarantine registry keys are ignored. No uploads, deletes, or overwrites.
    """

    keys = store.list_keys(ARCHIVE_KEY_PREFIX)
    suffix = f"/{COMPLETED_MARKER_NAME}"
    rows: list[dict[str, Any]] = []
    for key in keys:
        if not key.endswith(suffix):
            continue
        parts = key.split("/")
        if len(parts) < 3 or parts[0] != ARCHIVE_KEY_PREFIX:
            continue
        if "quarantine" in parts:
            continue
        dataset_id = parts[1]
        if not dataset_id.startswith("eth-usdt-p_"):
            continue
        payload = json.loads(store.read_bytes(key).decode("utf-8"))
        if payload.get("status") != COMPLETED_MARKER_NAME:
            continue
        start, end = parse_dataset_id_window(dataset_id)
        rows.append(
            {
                "dataset_id": dataset_id,
                "completed_key": key,
                "attempt_id": payload.get("attempt_id"),
                "start_utc": start.isoformat() if start is not None else None,
                "end_utc": end.isoformat() if end is not None else None,
                "quarantined": bool(payload.get("quarantined")),
                "research_quality_status": payload.get("research_quality_status"),
                "admission_eligible": payload.get("admission_eligible"),
                "logical_checksums_sha256": payload.get("logical_checksums_sha256"),
                # Per-file content hashes from the COMPLETED marker (not credentials).
                "logical_artifacts": payload.get("logical_artifacts"),
            }
        )
    rows.sort(key=lambda row: str(row.get("start_utc") or row["dataset_id"]))
    return rows


def _utc_date(value: str | datetime) -> str:
    return _dt(value).date().isoformat()


def freeze_discovery_oos(
    windows: list[dict[str, Any]],
    *,
    oos_utc_days: int = OOS_RESERVED_UTC_DAYS,
    min_oos_utc_days: int = MIN_OOS_RESERVED_UTC_DAYS,
) -> dict[str, Any]:
    """Assign discovery vs untouched OOS from timestamps only (no price stats).

    Prefers the last ``oos_utc_days`` UTC dates of verified coverage. Never
    silently shrinks OOS to enlarge discovery: thin discovery is reported to
    the lead instead.
    """

    if oos_utc_days < min_oos_utc_days:
        raise ValueError("oos_utc_days is below the predeclared minimum")
    eligible = [
        dict(row)
        for row in windows
        if row.get("start_utc") and not row.get("quarantined")
    ]
    excluded_quarantined = [
        row["dataset_id"] for row in windows if row.get("quarantined")
    ]
    dates = sorted({_utc_date(str(row["start_utc"])) for row in eligible})
    reserved_days = min(oos_utc_days, len(dates))
    oos_dates = dates[-reserved_days:] if dates else []
    oos_set = set(oos_dates)
    discovery = [row for row in eligible if _utc_date(str(row["start_utc"])) not in oos_set]
    oos = [row for row in eligible if _utc_date(str(row["start_utc"])) in oos_set]
    lead_alerts: list[str] = []
    if len(dates) < oos_utc_days:
        lead_alerts.append(
            "OOS_DATES_FEWER_THAN_PREFERRED_"
            f"{oos_utc_days}_RESERVED_{len(oos_dates)}"
        )
    discovery_hours = _covered_hours(discovery)
    oos_hours = _covered_hours(oos)
    if discovery_hours < 12.0:
        lead_alerts.append("DISCOVERY_THIN_OOS_PRESERVED")
    if not discovery:
        lead_alerts.append("DISCOVERY_EMPTY_AFTER_OOS_RESERVE")
    return {
        "oos_utc_days_requested": oos_utc_days,
        "oos_utc_dates": oos_dates,
        "discovery_utc_dates": sorted(
            {_utc_date(str(row["start_utc"])) for row in discovery}
        ),
        "discovery_windows": discovery,
        "oos_windows": oos,
        "discovery_window_count": len(discovery),
        "oos_window_count": len(oos),
        "discovery_covered_hours_est": discovery_hours,
        "oos_covered_hours_est": oos_hours,
        "excluded_quarantined_dataset_ids": excluded_quarantined,
        "lead_alerts": lead_alerts,
        "price_movement_inspected": False,
        "note": (
            "Last verified UTC dates are untouched OOS. Split used archive "
            "timestamps only; no price-path statistics entered this decision."
        ),
    }


def freeze_series_oos(
    times: list[datetime],
    *,
    oos_utc_days: int = OOS_RESERVED_UTC_DAYS,
    min_oos_utc_days: int = MIN_OOS_RESERVED_UTC_DAYS,
) -> dict[str, Any]:
    """Same freeze using decision_time dates when B2 window metadata is absent."""

    if oos_utc_days < min_oos_utc_days:
        raise ValueError("oos_utc_days is below the predeclared minimum")
    dates = sorted({_dt(ts).date().isoformat() for ts in times})
    reserved_days = min(oos_utc_days, len(dates))
    oos_dates = dates[-reserved_days:] if dates else []
    oos_set = set(oos_dates)
    oos_start: datetime | None = None
    if oos_dates:
        oos_start = datetime.fromisoformat(f"{oos_dates[0]}T00:00:00+00:00")
    discovery_times = [ts for ts in times if _dt(ts).date().isoformat() not in oos_set]
    oos_times = [ts for ts in times if _dt(ts).date().isoformat() in oos_set]
    lead_alerts: list[str] = []
    if len(dates) < oos_utc_days:
        lead_alerts.append(
            "OOS_DATES_FEWER_THAN_PREFERRED_"
            f"{oos_utc_days}_RESERVED_{len(oos_dates)}"
        )
    discovery_hours = len(discovery_times) / 3600.0
    if discovery_hours < 12.0:
        lead_alerts.append("DISCOVERY_THIN_OOS_PRESERVED")
    if not discovery_times:
        lead_alerts.append("DISCOVERY_EMPTY_AFTER_OOS_RESERVE")
    return {
        "oos_utc_days_requested": oos_utc_days,
        "oos_utc_dates": oos_dates,
        "discovery_utc_dates": sorted(
            {_dt(ts).date().isoformat() for ts in discovery_times}
        ),
        "oos_start_utc": oos_start.isoformat() if oos_start is not None else None,
        "discovery_row_count": len(discovery_times),
        "oos_row_count": len(oos_times),
        "discovery_usable_hours": discovery_hours,
        "oos_usable_hours": len(oos_times) / 3600.0,
        "lead_alerts": lead_alerts,
        "price_movement_inspected": False,
    }


def _covered_hours(windows: list[dict[str, Any]]) -> float:
    total = 0.0
    for row in windows:
        start = row.get("start_utc")
        end = row.get("end_utc")
        if not start or not end:
            continue
        total += max(0.0, (_dt(end) - _dt(start)).total_seconds() / 3600.0)
    return total


def utc_dates_between(start: datetime, end: datetime) -> list[date]:
    start_d = _dt(start).date()
    end_d = _dt(end).date()
    days: list[date] = []
    cursor = start_d
    while cursor <= end_d:
        days.append(cursor)
        cursor = cursor + timedelta(days=1)
    return days


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def events_parquet_object_key(dataset_id: str, attempt_id: str) -> str:
    """Canonical RAW events object under a completed attempt (GET-only)."""

    return f"{ARCHIVE_KEY_PREFIX}/{dataset_id}/attempts/{attempt_id}/events.parquet"


def overlap_hours_on_utc_date(
    start_utc: str | datetime,
    end_utc: str | datetime,
    day: date,
) -> float:
    """Hours of [start, end) that fall on the UTC calendar day."""

    start = _dt(start_utc)
    end = _dt(end_utc)
    day0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
    day1 = day0 + timedelta(days=1)
    lo = max(start, day0)
    hi = min(end, day1)
    return max(0.0, (hi - lo).total_seconds() / 3600.0)


def inventory_utc_day_coverage(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-UTC-day archive coverage. Timestamp metadata only; no price stats."""

    hours: dict[str, float] = defaultdict(float)
    eligible_hours: dict[str, float] = defaultdict(float)
    quarantined_hours: dict[str, float] = defaultdict(float)
    window_count: dict[str, int] = defaultdict(int)
    quarantined_count: dict[str, int] = defaultdict(int)
    quality_pass_count: dict[str, int] = defaultdict(int)
    quality_rejected_count: dict[str, int] = defaultdict(int)
    quality_other_count: dict[str, int] = defaultdict(int)
    dates: set[str] = set()

    for row in windows:
        start = row.get("start_utc")
        end = row.get("end_utc")
        if not start or not end:
            continue
        quarantined = bool(row.get("quarantined"))
        quality = row.get("research_quality_status")
        for day in utc_dates_between(_dt(str(start)), _dt(str(end)) - timedelta(microseconds=1)):
            key = day.isoformat()
            covered = overlap_hours_on_utc_date(str(start), str(end), day)
            if covered <= 0.0:
                continue
            dates.add(key)
            hours[key] += covered
            window_count[key] += 1
            if quarantined:
                quarantined_hours[key] += covered
                quarantined_count[key] += 1
            else:
                eligible_hours[key] += covered
            if quality == "pass":
                quality_pass_count[key] += 1
            elif quality == "rejected":
                quality_rejected_count[key] += 1
            else:
                quality_other_count[key] += 1

    ordered = sorted(dates)
    rows = []
    for key in ordered:
        elig = eligible_hours[key]
        rows.append(
            {
                "utc_date": key,
                "window_count": window_count[key],
                "quarantined_window_count": quarantined_count[key],
                "quality_pass_window_count": quality_pass_count[key],
                "quality_rejected_window_count": quality_rejected_count[key],
                "quality_other_window_count": quality_other_count[key],
                "covered_hours": hours[key],
                "eligible_hours": elig,
                "quarantined_hours": quarantined_hours[key],
                "full_utc_day": elig >= FULL_UTC_DAY_MIN_ELIGIBLE_HOURS,
            }
        )
    return {
        "full_utc_day_min_eligible_hours": FULL_UTC_DAY_MIN_ELIGIBLE_HOURS,
        "utc_dates": ordered,
        "days": rows,
        "price_movement_inspected": False,
    }


def freeze_full_corpus_expansion(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Assign expanded discovery vs frozen v1 OOS vs optional new holdout.

    Split uses archive timestamps only. Later verified days after the v1 OOS
    dates are not auto-OOS. A new final holdout is reserved only when at least
    ``NEW_HOLDOUT_MIN_FULL_UTC_DAYS`` later UTC dates meet the predeclared
    full-day coverage bar; those days are not materialized for first-passage.
    """

    v1_oos = set(V1_UNTOUCHED_OOS_UTC_DATES)
    v1_oos_last = max(V1_UNTOUCHED_OOS_UTC_DATES)
    coverage = inventory_utc_day_coverage(windows)
    eligible = [
        dict(row)
        for row in windows
        if row.get("start_utc") and not row.get("quarantined")
    ]
    excluded_quarantined = [
        row["dataset_id"] for row in windows if row.get("quarantined")
    ]
    eligible_hours_by_date = {
        item["utc_date"]: float(item["eligible_hours"]) for item in coverage["days"]
    }
    later_dates = [item["utc_date"] for item in coverage["days"] if item["utc_date"] > v1_oos_last]
    full_later = [
        day
        for day in later_dates
        if eligible_hours_by_date.get(day, 0.0) >= FULL_UTC_DAY_MIN_ELIGIBLE_HOURS
    ]
    lead_alerts: list[str] = []
    holdout_dates: list[str] = []
    holdout_rule = (
        "Last "
        f"{NEW_HOLDOUT_MIN_FULL_UTC_DAYS} full later UTC days "
        f"(eligible hours >= {FULL_UTC_DAY_MIN_ELIGIBLE_HOURS}) plus any later "
        "dates on/after that anchor. Not applied when fewer than "
        f"{NEW_HOLDOUT_MIN_FULL_UTC_DAYS} full later days exist."
    )
    if len(full_later) >= NEW_HOLDOUT_MIN_FULL_UTC_DAYS:
        anchor = full_later[-NEW_HOLDOUT_MIN_FULL_UTC_DAYS]
        holdout_dates = [day for day in later_dates if day >= anchor]
    else:
        lead_alerts.append(
            "NEW_FINAL_HOLDOUT_UNAVAILABLE_NO_TWO_FULL_LATER_UTC_DAYS"
        )

    # Thin-holdout alternative is recorded for the lead and is not applied.
    if len(later_dates) >= NEW_HOLDOUT_MIN_FULL_UTC_DAYS:
        thin_later = later_dates[-NEW_HOLDOUT_MIN_FULL_UTC_DAYS:]
    else:
        thin_later = list(later_dates)
    holdout_set = set(holdout_dates)

    discovery: list[dict[str, Any]] = []
    untouched_oos: list[dict[str, Any]] = []
    new_holdout: list[dict[str, Any]] = []
    for row in eligible:
        start_day = _utc_date(str(row["start_utc"]))
        if start_day in v1_oos:
            untouched_oos.append(row)
        elif start_day in holdout_set:
            new_holdout.append(row)
        else:
            discovery.append(row)

    discovery_dates = sorted({_utc_date(str(row["start_utc"])) for row in discovery})
    if not discovery:
        lead_alerts.append("DISCOVERY_EMPTY_AFTER_OOS_AND_HOLDOUT")
    if _covered_hours(discovery) < 12.0:
        lead_alerts.append("DISCOVERY_THIN")

    return {
        "v1_untouched_oos_utc_dates": list(V1_UNTOUCHED_OOS_UTC_DATES),
        "new_holdout_utc_dates": holdout_dates,
        "new_holdout_full_later_utc_dates": full_later,
        "new_holdout_applied": bool(holdout_dates),
        "new_holdout_rule": holdout_rule,
        "thin_holdout_alternative_not_applied": {
            "utc_dates": thin_later,
            "note": (
                "Last later UTC dates even when they are not full days. "
                "Not used: extra verified periods after v1 OOS are not "
                "auto-OOS. Lead may reserve a thin holdout separately."
            ),
        },
        "discovery_utc_dates": discovery_dates,
        "discovery_windows": discovery,
        "untouched_oos_windows": untouched_oos,
        "new_holdout_windows": new_holdout,
        "discovery_window_count": len(discovery),
        "untouched_oos_window_count": len(untouched_oos),
        "new_holdout_window_count": len(new_holdout),
        "discovery_covered_hours_est": _covered_hours(discovery),
        "untouched_oos_covered_hours_est": _covered_hours(untouched_oos),
        "new_holdout_covered_hours_est": _covered_hours(new_holdout),
        "excluded_quarantined_dataset_ids": excluded_quarantined,
        "inventory_utc_day_coverage": coverage,
        "lead_alerts": lead_alerts,
        "price_movement_inspected": False,
        "note": (
            "v1 OOS dates stay untouched. Later verified COMPLETED windows are "
            "expanded discovery unless a predeclared full-day holdout exists. "
            "No price-path statistics entered this decision. Do not use OOS "
            "or holdout to choose horizon/TP."
        ),
    }


def load_completed_logical_artifacts(
    store: ArchiveStore, dataset_id: str
) -> dict[str, Any]:
    """Read the COMPLETED marker only (GET). No mutations."""

    key = f"{ARCHIVE_KEY_PREFIX}/{dataset_id}/{COMPLETED_MARKER_NAME}"
    payload = cast(
        dict[str, Any], json.loads(store.read_bytes(key).decode("utf-8"))
    )
    if payload.get("status") != COMPLETED_MARKER_NAME:
        raise ValueError("canonical marker is not COMPLETED")
    return payload


def download_completed_events_parquet(
    store: ArchiveStore,
    dataset_id: str,
    destination: Path,
    *,
    attempt_id: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """GET events.parquet for a COMPLETED attempt and verify logical sha256.

    Never PUT/DELETE/COPY on the store. Failed checksums delete the local
    partial file so a corrupt cache cannot be reused.
    """

    marker = load_completed_logical_artifacts(store, dataset_id)
    if marker.get("quarantined"):
        raise ValueError("refusing to restore a quarantined COMPLETED window")
    resolved_attempt = str(attempt_id or marker.get("attempt_id") or "")
    if not resolved_attempt:
        raise ValueError("COMPLETED marker missing attempt_id")
    artifacts = marker.get("logical_artifacts") or {}
    expected = str(expected_sha256 or artifacts.get("events.parquet") or "")
    if not expected:
        raise ValueError("COMPLETED marker missing logical_artifacts events.parquet")
    object_key = events_parquet_object_key(dataset_id, resolved_attempt)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and _sha256_file(destination) == expected:
        return {
            "status": "cache_hit",
            "dataset_id": dataset_id,
            "sha256": expected,
            "path": str(destination),
            "b2_mutations": False,
        }
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        partial.unlink()
    store.download_file(object_key, partial)
    actual = _sha256_file(partial)
    if actual != expected:
        partial.unlink()
        raise ValueError("events.parquet logical sha256 mismatch")
    partial.replace(destination)
    return {
        "status": "downloaded",
        "dataset_id": dataset_id,
        "sha256": actual,
        "path": str(destination),
        "b2_mutations": False,
    }


def concat_parquet_files(paths: list[Path], destination: Path) -> None:
    """Stream-concatenate Parquet files with the first file's schema."""

    if not paths:
        raise ValueError("concat_parquet_files requires at least one path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer: pq.ParquetWriter | None = None
    try:
        for path in paths:
            parquet_file = pq.ParquetFile(path)
            for batch in parquet_file.iter_batches(batch_size=5_000):
                if writer is None:
                    writer = pq.ParquetWriter(destination, batch.schema)
                writer.write_batch(batch)
    finally:
        if writer is not None:
            writer.close()


def materialize_discovery_market_state(
    store: ArchiveStore,
    windows: list[dict[str, Any]],
    *,
    restore_root: Path,
    runs_root: Path,
    force_rebuild: bool = False,
    keep_normalized: bool = False,
) -> dict[str, Any]:
    """Restore discovery RAW via GET and build market_state_1s per UTC day.

    Uses the existing normalize_events_parquet → build_market_state_1s path.
    Does not run features, labels, baselines, or any B2 mutation.
    """

    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in windows:
        by_day[_utc_date(str(row["start_utc"]))].append(row)
    day_outputs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for day in sorted(by_day):
        day_windows = sorted(by_day[day], key=lambda item: str(item.get("start_utc")))
        print(
            json.dumps(
                {
                    "phase": "materialize_day",
                    "utc_date": day,
                    "windows": len(day_windows),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        event_paths: list[Path] = []
        skipped = False
        for row in day_windows:
            dataset_id = str(row["dataset_id"])
            dest = restore_root / dataset_id / "events.parquet"
            try:
                download_completed_events_parquet(
                    store,
                    dataset_id,
                    dest,
                    attempt_id=str(row.get("attempt_id") or "") or None,
                )
            except Exception as error:
                errors.append(
                    {
                        "dataset_id": dataset_id,
                        "error_type": type(error).__name__,
                    }
                )
                print(
                    json.dumps(
                        {
                            "phase": "materialize_window_error",
                            "utc_date": day,
                            "dataset_id": dataset_id,
                            "error_type": type(error).__name__,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                skipped = True
                break
            event_paths.append(dest)
        if skipped or not event_paths:
            continue
        day_dir = runs_root / f"utc_{day}"
        market_state_path = day_dir / "market_state_1s" / "market_state_1s.parquet"
        if market_state_path.is_file() and not force_rebuild:
            day_outputs.append(
                {
                    "utc_date": day,
                    "status": "cache_hit",
                    "market_state_1s": str(market_state_path),
                    "window_count": len(event_paths),
                }
            )
            continue
        concat_path = day_dir / "events_concat.parquet"
        normalized_dir = day_dir / "normalized_events"
        try:
            concat_parquet_files(event_paths, concat_path)
            normalize_events_parquet(concat_path, normalized_dir)
            build_market_state_1s(normalized_dir, market_state_path)
        except Exception as error:
            errors.append(
                {
                    "utc_date": day,
                    "error_type": type(error).__name__,
                    "error": str(error)[:400],
                }
            )
            print(
                json.dumps(
                    {
                        "phase": "materialize_day_error",
                        "utc_date": day,
                        "error_type": type(error).__name__,
                        "error": str(error)[:400],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            continue
        # Drop bulky intermediates; keep per-window events.parquet for checksum cache.
        if concat_path.exists():
            concat_path.unlink()
        if normalized_dir.is_dir() and not keep_normalized:
            for child in normalized_dir.glob("*.parquet"):
                child.unlink()
        day_outputs.append(
            {
                "utc_date": day,
                "status": "built",
                "market_state_1s": str(market_state_path),
                "window_count": len(event_paths),
            }
        )
    return {
        "day_outputs": day_outputs,
        "errors": errors,
        "market_state_paths": [
            Path(item["market_state_1s"])
            for item in day_outputs
            if item.get("market_state_1s")
        ],
        "b2_mutations": False,
    }


def materialize_from_local_restored(
    *,
    restore_root: Path,
    runs_root: Path,
    discovery_dates: Sequence[str],
    force_rebuild: bool = True,
    keep_normalized: bool = False,
) -> dict[str, Any]:
    """Build market_state_1s from already-restored events.parquet (no B2)."""

    by_day: dict[str, list[Path]] = defaultdict(list)
    if restore_root.is_dir():
        for child in sorted(restore_root.iterdir()):
            events = child / "events.parquet"
            if not events.is_file():
                continue
            start, _end = parse_dataset_id_window(child.name)
            if start is None:
                continue
            day = start.date().isoformat()
            if day in discovery_dates:
                by_day[day].append(events)
    day_outputs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for day in discovery_dates:
        event_paths = by_day.get(day) or []
        if not event_paths:
            errors.append({"utc_date": day, "error_type": "MissingRestoredEvents"})
            continue
        print(
            json.dumps(
                {
                    "phase": "materialize_local_day",
                    "utc_date": day,
                    "windows": len(event_paths),
                    "force_rebuild": force_rebuild,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        day_dir = runs_root / f"utc_{day}"
        market_state_path = day_dir / "market_state_1s" / "market_state_1s.parquet"
        if market_state_path.is_file() and not force_rebuild:
            day_outputs.append(
                {
                    "utc_date": day,
                    "status": "cache_hit",
                    "market_state_1s": str(market_state_path),
                    "window_count": len(event_paths),
                }
            )
            continue
        concat_path = day_dir / "events_concat.parquet"
        normalized_dir = day_dir / "normalized_events"
        try:
            concat_parquet_files(event_paths, concat_path)
            normalize_events_parquet(concat_path, normalized_dir)
            build_market_state_1s(normalized_dir, market_state_path)
        except Exception as error:
            errors.append(
                {
                    "utc_date": day,
                    "error_type": type(error).__name__,
                    "error": str(error)[:400],
                }
            )
            print(
                json.dumps(
                    {
                        "phase": "materialize_local_day_error",
                        "utc_date": day,
                        "error_type": type(error).__name__,
                        "error": str(error)[:400],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            continue
        if concat_path.exists():
            concat_path.unlink()
        if normalized_dir.is_dir() and not keep_normalized:
            for parquet_child in normalized_dir.glob("*.parquet"):
                parquet_child.unlink()
        day_outputs.append(
            {
                "utc_date": day,
                "status": "built",
                "market_state_1s": str(market_state_path),
                "window_count": len(event_paths),
            }
        )
    return {
        "day_outputs": day_outputs,
        "errors": errors,
        "market_state_paths": [
            Path(item["market_state_1s"])
            for item in day_outputs
            if item.get("market_state_1s")
        ],
        "b2_mutations": False,
    }
