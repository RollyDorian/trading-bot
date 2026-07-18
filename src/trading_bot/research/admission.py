import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from trading_bot.research.dataset import validate_manifest
from trading_bot.research.quality import require_acceptable_quality
from trading_bot.research.replay import (
    BaselineConfig,
    CostConfig,
    configuration_hash,
    maximum_drawdown,
)

ADMISSION_SCHEMA_VERSION = 1
OFFLINE_REPLAY_REPORT = "offline_replay.json"
DISCLAIMER = (
    "Research criteria passed does not enable PAPER mode, prove profitability, or "
    "authorize trading. Manual review is required and BOT_MODE remains collect."
)

type DatasetSplit = Literal["training", "validation", "out_of_sample"]


class AdmissionInputError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AdmissionThresholds:
    """Conservative research placeholders; they are not expected-return promises."""

    minimum_quality_passing_datasets: int = 3
    minimum_oos_dataset_count: int = 2
    minimum_oos_trade_count: int = 30
    maximum_oos_drawdown: float = 100.0
    require_positive_oos_net_pnl: bool = True
    minimum_oos_utc_days: int = 2

    def __post_init__(self) -> None:
        counts = (
            self.minimum_quality_passing_datasets,
            self.minimum_oos_dataset_count,
            self.minimum_oos_trade_count,
            self.minimum_oos_utc_days,
        )
        if any(value < 1 for value in counts) or self.maximum_oos_drawdown < 0:
            raise ValueError("Admission thresholds must be positive and drawdown non-negative.")


DEFAULT_ADMISSION_THRESHOLDS = AdmissionThresholds()


def _load_json(path: Path, artifact: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"Required {artifact} is missing.") from error
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Required {artifact} is invalid.") from error
    if not isinstance(value, dict):
        raise ValueError(f"Required {artifact} is invalid.")
    return cast(dict[str, Any], value)


def _utc(value: Any, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Invalid {field} timestamp.") from error
    if parsed.tzinfo is None:
        raise ValueError(f"Invalid {field} timestamp.")
    return parsed.astimezone(UTC)


def _number(value: Any, field: str, *, non_negative: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Replay field {field} is invalid.")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Replay field {field} is invalid.") from error
    if not (-float("inf") < result < float("inf")) or (non_negative and result < 0):
        raise ValueError(f"Replay field {field} is invalid.")
    return result


def _validate_replay(
    dataset_dir: Path,
    dataset_id: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    report = _load_json(dataset_dir / OFFLINE_REPLAY_REPORT, "offline replay report")
    if report.get("result_type") != "offline_research_simulation":
        raise ValueError("Offline replay report type is incompatible.")
    if report.get("dataset_id") != dataset_id:
        raise ValueError("Offline replay dataset identity does not match the manifest.")
    if report.get("dataset_quality_status") != "pass" or report.get(
        "quality_warnings_allowed"
    ) is not False:
        raise ValueError("Offline replay was not produced from quality status pass.")
    replay_configuration_hash = report.get("configuration_hash")
    configuration = report.get("configuration")
    if not isinstance(replay_configuration_hash, str) or not replay_configuration_hash:
        raise ValueError("Offline replay configuration identity is missing.")
    if not isinstance(configuration, dict):
        raise ValueError("Offline replay configuration is missing.")
    try:
        signal = BaselineConfig(**cast(dict[str, Any], configuration["signal"]))
        costs = CostConfig(**cast(dict[str, Any], configuration["costs"]))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("Offline replay configuration is incompatible.") from error
    if configuration_hash(signal, costs) != replay_configuration_hash:
        raise ValueError("Offline replay configuration hash is invalid.")
    trades = report.get("trades")
    if not isinstance(trades, list):
        raise ValueError("Offline replay raw trades are missing.")
    normalized: list[dict[str, Any]] = []
    previous_exit: datetime | None = None
    for value in trades:
        if not isinstance(value, dict):
            raise ValueError("Offline replay raw trade is invalid.")
        trade = cast(dict[str, Any], value)
        entry = _utc(trade.get("entry_time"), "trade entry")
        exit_at = _utc(trade.get("exit_time"), "trade exit")
        if entry < start or exit_at > end or entry >= exit_at:
            raise ValueError("Offline replay trade is outside the dataset range.")
        if previous_exit is not None and entry < previous_exit:
            raise ValueError("Offline replay trades are not chronological.")
        previous_exit = exit_at
        gross_pnl = _number(trade.get("gross_pnl"), "gross_pnl")
        fees = _number(trade.get("fees"), "fees", non_negative=True)
        funding = _number(trade.get("funding"), "funding", non_negative=True)
        slippage_and_latency = _number(
            trade.get("slippage"), "slippage", non_negative=True
        )
        net_pnl = _number(trade.get("net_pnl"), "net_pnl")
        normalized_trade: dict[str, Any] = {
                "entry_time": entry,
                "exit_time": exit_at,
                "gross_pnl": gross_pnl,
                "fees": fees,
                "funding": funding,
                "slippage_and_latency": slippage_and_latency,
                "net_pnl": net_pnl,
        }
        expected_net = gross_pnl - fees - funding - slippage_and_latency
        if abs(net_pnl - expected_net) > 1e-8:
            raise ValueError("Offline replay trade net PnL is inconsistent with costs.")
        normalized.append(normalized_trade)
    expected = {
        "simulated_exits": len(normalized),
        "gross_pnl": sum(item["gross_pnl"] for item in normalized),
        "fees": sum(item["fees"] for item in normalized),
        "funding": sum(item["funding"] for item in normalized),
        "slippage_and_latency": sum(
            item["slippage_and_latency"] for item in normalized
        ),
        "net_pnl": sum(item["net_pnl"] for item in normalized),
    }
    if report.get("simulated_exits") != expected["simulated_exits"]:
        raise ValueError("Offline replay trade count does not match raw trades.")
    for field in ("gross_pnl", "fees", "funding", "slippage_and_latency", "net_pnl"):
        if abs(_number(report.get(field), field) - float(expected[field])) > 1e-8:
            raise ValueError(f"Offline replay aggregate {field} does not match raw trades.")
    return {"configuration_hash": replay_configuration_hash, "trades": normalized}


def _split(index: int, total: int, validation_count: int, oos_count: int) -> DatasetSplit:
    oos_start = total - oos_count
    validation_start = oos_start - validation_count
    if index >= oos_start:
        return "out_of_sample"
    if index >= validation_start:
        return "validation"
    return "training"


def _software_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _covered_days(ranges: list[tuple[datetime, datetime]]) -> int:
    days: set[date] = set()
    for start, end in ranges:
        current = start.date()
        final = (end - timedelta(microseconds=1)).date()
        while current <= final:
            days.add(current)
            current += timedelta(days=1)
    return len(days)


def evaluate_admission(
    dataset_dirs: list[Path],
    *,
    validation_count: int,
    oos_count: int,
    thresholds: AdmissionThresholds = DEFAULT_ADMISSION_THRESHOLDS,
) -> dict[str, Any]:
    if validation_count < 1 or oos_count < 1:
        raise AdmissionInputError("Validation and OOS counts must be positive.")
    if len(dataset_dirs) <= validation_count + oos_count:
        raise AdmissionInputError("At least one training dataset must remain after splitting.")
    names = [path.name for path in dataset_dirs]
    if len(set(names)) != len(names):
        raise AdmissionInputError("Duplicate dataset version names are not allowed.")

    datasets: list[dict[str, Any]] = []
    valid_ranges: list[tuple[int, datetime, datetime]] = []
    configuration_hashes: set[str] = set()
    for index, dataset_dir in enumerate(dataset_dirs):
        assignment = _split(index, len(dataset_dirs), validation_count, oos_count)
        item: dict[str, Any] = {
            "version": dataset_dir.name,
            "path": str(dataset_dir),
            "split": assignment,
            "start_utc": None,
            "end_utc": None,
            "quality_status": "invalid",
            "admissible": False,
            "rejection_reason": None,
        }
        try:
            manifest = validate_manifest(dataset_dir)
            dataset_id = str(manifest["dataset_id"])
            if dataset_id != dataset_dir.name:
                raise ValueError("Dataset identity does not match its directory name.")
            start = _utc(manifest.get("start_utc"), "dataset start")
            end = _utc(manifest.get("end_utc"), "dataset end")
            if start >= end:
                raise ValueError("Dataset UTC range is invalid.")
            row_counts = manifest.get("row_counts")
            if not isinstance(row_counts, dict) or int(row_counts.get("events", 0)) < 1:
                raise ValueError("Dataset is empty.")
            quality = require_acceptable_quality(dataset_dir, allow_warnings=False)
            if quality.get("status") != "pass":
                raise ValueError("Dataset quality report status is not pass.")
            replay = _validate_replay(dataset_dir, dataset_id, start, end)
            item.update(
                {
                    "version": dataset_id,
                    "start_utc": start.isoformat(),
                    "end_utc": end.isoformat(),
                    "quality_status": "pass",
                    "admissible": True,
                    "configuration_hash": replay["configuration_hash"],
                    "trades": replay["trades"],
                }
            )
            valid_ranges.append((index, start, end))
            configuration_hashes.add(str(replay["configuration_hash"]))
        except (KeyError, OSError, TypeError, ValueError) as error:
            item["rejection_reason"] = str(error)
        datasets.append(item)

    if len(valid_ranges) == len(dataset_dirs):
        for left, right in zip(valid_ranges, valid_ranges[1:], strict=False):
            if left[1] >= right[1]:
                raise AdmissionInputError("Dataset inputs are not chronological.")
            if left[2] > right[1]:
                raise AdmissionInputError("Dataset UTC ranges overlap.")

    compatible = len(configuration_hashes) <= 1
    if not compatible:
        for item in datasets:
            if item["admissible"]:
                item["admissible"] = False
                item["rejection_reason"] = "Replay configuration differs across datasets."

    oos_items = [item for item in datasets if item["split"] == "out_of_sample"]
    oos_trades = [
        trade
        for item in oos_items
        if item["admissible"]
        for trade in cast(list[dict[str, Any]], item.get("trades", []))
    ]
    oos_ranges = [
        (_utc(item["start_utc"], "dataset start"), _utc(item["end_utc"], "dataset end"))
        for item in oos_items
        if item["admissible"]
    ]
    aggregate = {
        "dataset_count": sum(item["admissible"] for item in oos_items),
        "trade_count": len(oos_trades),
        "gross_pnl": sum(float(trade["gross_pnl"]) for trade in oos_trades),
        "fees": sum(float(trade["fees"]) for trade in oos_trades),
        "funding": sum(float(trade["funding"]) for trade in oos_trades),
        "slippage_and_latency": sum(
            float(trade["slippage_and_latency"]) for trade in oos_trades
        ),
        "net_pnl": sum(float(trade["net_pnl"]) for trade in oos_trades),
        "win_rate": (
            sum(float(trade["net_pnl"]) > 0 for trade in oos_trades) / len(oos_trades)
            if oos_trades
            else 0.0
        ),
        "maximum_drawdown": maximum_drawdown(
            [float(trade["net_pnl"]) for trade in oos_trades]
        ),
        "utc_day_count": _covered_days(oos_ranges),
    }
    quality_count = sum(item["quality_status"] == "pass" for item in datasets)
    artifacts_valid = all(item["admissible"] for item in datasets)
    criteria = {
        "all_required_artifacts_valid": {
            "passed": artifacts_valid,
            "actual": artifacts_valid,
            "required": True,
        },
        "compatible_replay_configuration": {
            "passed": compatible,
            "actual": compatible,
            "required": True,
        },
        "minimum_quality_passing_datasets": {
            "passed": quality_count >= thresholds.minimum_quality_passing_datasets,
            "actual": quality_count,
            "required": thresholds.minimum_quality_passing_datasets,
        },
        "minimum_oos_dataset_count": {
            "passed": aggregate["dataset_count"] >= thresholds.minimum_oos_dataset_count,
            "actual": aggregate["dataset_count"],
            "required": thresholds.minimum_oos_dataset_count,
        },
        "minimum_oos_trade_count": {
            "passed": aggregate["trade_count"] >= thresholds.minimum_oos_trade_count,
            "actual": aggregate["trade_count"],
            "required": thresholds.minimum_oos_trade_count,
        },
        "maximum_oos_drawdown": {
            "passed": aggregate["maximum_drawdown"]
            <= thresholds.maximum_oos_drawdown,
            "actual": aggregate["maximum_drawdown"],
            "required": thresholds.maximum_oos_drawdown,
        },
        "positive_oos_net_pnl_after_costs": {
            "passed": (not thresholds.require_positive_oos_net_pnl)
            or aggregate["net_pnl"] > 0,
            "actual": aggregate["net_pnl"],
            "required": "> 0" if thresholds.require_positive_oos_net_pnl else "not required",
        },
        "minimum_oos_utc_days": {
            "passed": aggregate["utc_day_count"] >= thresholds.minimum_oos_utc_days,
            "actual": aggregate["utc_day_count"],
            "required": thresholds.minimum_oos_utc_days,
        },
    }
    failed = [name for name, criterion in criteria.items() if not criterion["passed"]]
    generated_at = max(
        (end for _, _, end in valid_ranges),
        default=datetime(1970, 1, 1, tzinfo=UTC),
    )
    for item in datasets:
        item.pop("trades", None)
    return {
        "schema_version": ADMISSION_SCHEMA_VERSION,
        "generated_at_utc": generated_at.isoformat(),
        "generation_timestamp_policy": "latest validated dataset end; Unix epoch if none validate",
        "software_revision": _software_revision(),
        "selected_dataset_versions": names,
        "split": {
            "training_count": len(dataset_dirs) - validation_count - oos_count,
            "validation_count": validation_count,
            "oos_count": oos_count,
        },
        "datasets": datasets,
        "oos_aggregate": aggregate,
        "acceptance_thresholds": asdict(thresholds),
        "criteria": criteria,
        "failed_criteria": failed,
        "admitted": not failed,
        "disclaimer": DISCLAIMER,
    }


def write_admission_report(report: dict[str, Any], path: Path, *, force: bool = False) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"Admission report already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
