import json
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from trading_bot.research.dataset import sha256_file, validate_manifest

QUALITY_REPORT = "quality_report.json"
QUALITY_REPORT_VERSION = 3
PRICE_FIELDS = ("price", "tradePrice", "trade_price", "markPrice", "mark_price", "p")


def _price(payload_json: Any) -> tuple[bool, float | None]:
    try:
        payload = json.loads(str(payload_json))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False, None
    containers = [payload]
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        containers.append(payload["data"])
    for container in containers:
        if not isinstance(container, dict):
            continue
        for name in PRICE_FIELDS:
            if name not in container:
                continue
            try:
                value = float(container[name])
            except (TypeError, ValueError):
                return True, None
            return True, value if math.isfinite(value) and value > 0 else None
    return False, None


def _identity(row: dict[str, Any]) -> str:
    normalized = {
        key: value.isoformat() if isinstance(value, datetime) else value
        for key, value in row.items()
    }
    return json.dumps(normalized, sort_keys=True, default=str, separators=(",", ":"))


def validate_dataset(
    dataset_dir: Path,
    *,
    gap_warning_seconds: float = 60.0,
    price_discontinuity_percent: float = 20.0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate an immutable export; large price moves are review warnings only."""
    if gap_warning_seconds <= 0 or price_discontinuity_percent <= 0:
        raise ValueError("Quality thresholds must be positive.")
    manifest = validate_manifest(dataset_dir)
    manifest_path = dataset_dir / "manifest.json"
    parquet_paths = sorted(dataset_dir.glob("*.parquet"), key=lambda path: path.name)
    expected_parquet_names = {
        name for name in manifest["checksums"] if name.endswith(".parquet")
    }
    if {path.name for path in parquet_paths} != expected_parquet_names:
        raise ValueError("Dataset contains unexpected Parquet inputs.")
    rows = cast(list[dict[str, Any]], pq.read_table(dataset_dir / "events.parquet").to_pylist())
    timestamps = [row.get("exchange_at") or row.get("received_at") for row in rows]
    valid_times = [value for value in timestamps if isinstance(value, datetime)]
    manifest_start = datetime.fromisoformat(str(manifest["start_utc"])).astimezone(UTC)
    manifest_end = datetime.fromisoformat(str(manifest["end_utc"])).astimezone(UTC)
    range_violations = sum(
        timestamp < manifest_start or timestamp >= manifest_end for timestamp in valid_times
    )
    ordering_violations = sum(
        left > right for left, right in zip(valid_times, valid_times[1:], strict=False)
    )
    gaps = [
        (right - left).total_seconds()
        for left, right in zip(valid_times, valid_times[1:], strict=False)
        if right >= left
    ]
    largest_gap = max(gaps, default=0.0)
    duplicate_count = sum(count - 1 for count in Counter(_identity(row) for row in rows).values())

    invalid_prices = 0
    prices: list[float] = []
    for row in rows:
        available, price = _price(row.get("payload_json"))
        if (available and price is None) or (row.get("topic") == "trades" and not available):
            invalid_prices += 1
        elif price is not None:
            prices.append(price)
    discontinuities = sum(
        abs(right / left - 1) * 100 >= price_discontinuity_percent
        for left, right in zip(prices, prices[1:], strict=False)
    )

    sequences_by_stream: dict[tuple[Any, Any], list[int]] = {}
    for row in rows:
        sequence = row.get("sequence")
        if isinstance(sequence, int):
            stream = (row.get("source"), row.get("topic"))
            sequences_by_stream.setdefault(stream, []).append(sequence)
    sequence_anomalies: int | None = None
    if sequences_by_stream:
        sequence_anomalies = sum(
            right != left + 1
            for values in sequences_by_stream.values()
            for left, right in zip(values, values[1:], strict=False)
        )

    findings: list[str] = []
    status = "pass"
    if not rows:
        status = "rejected"
        findings.append("Dataset contains no market events.")
    if ordering_violations:
        status = "rejected"
        findings.append(f"Found {ordering_violations} timestamp ordering violation(s).")
    if range_violations:
        status = "rejected"
        findings.append(f"Found {range_violations} timestamp(s) outside the manifest range.")
    warning_reasons = (
        (duplicate_count, f"Found {duplicate_count} duplicate event(s)."),
        (invalid_prices, f"Found {invalid_prices} invalid price value(s)."),
        (sequence_anomalies or 0, f"Found {sequence_anomalies} sequence anomaly/anomalies."),
        (
            int(largest_gap > gap_warning_seconds),
            f"Largest timestamp gap {largest_gap:.3f}s exceeds {gap_warning_seconds:.3f}s.",
        ),
        (
            discontinuities,
            f"Found {discontinuities} price discontinuity/discontinuities at or above "
            f"{price_discontinuity_percent:.3f}%; these may be real market moves and "
            "require review.",
        ),
    )
    for present, finding in warning_reasons:
        if present:
            findings.append(finding)
            if status == "pass":
                status = "warning"
    if not findings:
        findings.append("No configured data-quality anomalies found.")

    coverage_start = min(valid_times).isoformat() if valid_times else None
    coverage_end = max(valid_times).isoformat() if valid_times else None
    report = {
        "quality_report_version": QUALITY_REPORT_VERSION,
        "dataset_version": manifest["dataset_id"],
        "manifest_sha256": sha256_file(manifest_path),
        "parquet_inputs": {
            path.name: {"sha256": sha256_file(path), "size_bytes": path.stat().st_size}
            for path in parquet_paths
        },
        "validated_at_utc": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "row_count": len(rows),
        "coverage": {"start_utc": coverage_start, "end_utc": coverage_end},
        "duplicate_event_count": duplicate_count,
        "invalid_or_missing_price_count": invalid_prices,
        "timestamp_ordering_violations": ordering_violations,
        "timestamp_manifest_range_violations": range_violations,
        "sequence_anomalies": sequence_anomalies,
        "largest_timestamp_gap_seconds": largest_gap,
        "gap_warning_threshold_seconds": gap_warning_seconds,
        "price_discontinuity_count": discontinuities,
        "price_discontinuity_threshold_percent": price_discontinuity_percent,
        "price_discontinuity_interpretation": "Review warning; not proof of bad market data.",
        "status": status,
        "findings": findings,
    }
    (dataset_dir / QUALITY_REPORT).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return report


def require_acceptable_quality(dataset_dir: Path, *, allow_warnings: bool) -> dict[str, Any]:
    path = dataset_dir / QUALITY_REPORT
    if not path.is_file():
        raise ValueError(
            "Dataset quality report is missing; run validate-dataset before evaluation."
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            "Dataset quality report is unreadable; run validate-dataset again."
        ) from error
    if not isinstance(value, dict):
        raise ValueError("Dataset quality report is invalid; run validate-dataset again.")
    report = cast(dict[str, Any], value)
    if report.get("quality_report_version") != QUALITY_REPORT_VERSION:
        raise ValueError(
            "Dataset quality report version is unsupported; run validate-dataset again."
        )
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file() or report.get("manifest_sha256") != sha256_file(
        manifest_path
    ):
        raise ValueError(
            "Dataset manifest changed after validation; run validate-dataset again."
        )
    recorded = report.get("parquet_inputs")
    if not isinstance(recorded, dict):
        raise ValueError(
            "Dataset quality report lacks input integrity data; run validate-dataset again."
        )
    current_paths = {
        item.name: item for item in dataset_dir.glob("*.parquet") if item.is_file()
    }
    if set(recorded) != set(current_paths):
        raise ValueError(
            "Dataset Parquet inputs changed after validation; run validate-dataset again."
        )
    for name, expected in recorded.items():
        if not isinstance(expected, dict):
            raise ValueError(
                "Dataset quality report has invalid input integrity data; "
                "run validate-dataset again."
            )
        current = current_paths[name]
        if (
            expected.get("size_bytes") != current.stat().st_size
            or expected.get("sha256") != sha256_file(current)
        ):
            raise ValueError(
                f"Dataset Parquet input changed after validation: {name}; "
                "run validate-dataset again."
            )
    status = report.get("status")
    if status == "rejected":
        raise ValueError("Dataset quality status is rejected; evaluation refused.")
    if status == "warning" and not allow_warnings:
        raise ValueError("Dataset quality has warnings; pass --allow-warnings to evaluate.")
    if status not in {"pass", "warning"}:
        raise ValueError("Dataset quality report has an invalid status.")
    return report
