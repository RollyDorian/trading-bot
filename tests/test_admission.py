import json
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.cli import main
from trading_bot.research.admission import (
    AdmissionInputError,
    AdmissionThresholds,
    evaluate_admission,
    write_admission_report,
)
from trading_bot.research.dataset import write_dataset
from trading_bot.research.quality import validate_dataset
from trading_bot.research.replay import BaselineConfig, CostConfig, configuration_hash
from trading_bot.storage.models import MarketEvent

START = datetime(2026, 7, 1, tzinfo=UTC)
PASSING = AdmissionThresholds(
    minimum_quality_passing_datasets=4,
    minimum_oos_dataset_count=2,
    minimum_oos_trade_count=2,
    maximum_oos_drawdown=20,
    minimum_oos_utc_days=2,
)


def _dataset(root: Path, day: float, *, net_pnl: float = 10.0) -> Path:
    start = START + timedelta(days=day)
    events = [
        MarketEvent(
            id=index + 1,
            received_at=start + timedelta(seconds=index),
            exchange_at=start + timedelta(seconds=index),
            source="fixture",
            event_type="trades",
            symbol="ETH/USDT-P",
            sequence=index + 1,
            latency_ms=0.0,
            payload={"price": 100 + index, "quantity": 1},
        )
        for index in range(2)
    ]
    directory = write_dataset(
        events=events,
        symbol="ETH/USDT-P",
        start=start,
        end=start + timedelta(days=1),
        output_root=root,
    )
    validate_dataset(directory, now=start + timedelta(days=1))
    trade = {
        "direction": 1,
        "entry_time": start.isoformat(),
        "exit_time": (start + timedelta(hours=1)).isoformat(),
        "entry_price": 100,
        "exit_price": 101,
        "gross_pnl": net_pnl + 3,
        "fees": 1,
        "funding": 1,
        "slippage": 1,
        "net_pnl": net_pnl,
    }
    signal = BaselineConfig()
    costs = CostConfig()
    report = {
        "result_type": "offline_research_simulation",
        "dataset_id": directory.name,
        "configuration_hash": configuration_hash(signal, costs),
        "configuration": {"signal": asdict(signal), "costs": asdict(costs)},
        "simulated_exits": 1,
        "gross_pnl": net_pnl + 3,
        "fees": 1,
        "funding": 1,
        "slippage_and_latency": 1,
        "net_pnl": net_pnl,
        "trades": [trade],
        "dataset_quality_status": "pass",
        "quality_warnings_allowed": False,
    }
    (directory / "offline_replay.json").write_text(json.dumps(report), encoding="utf-8")
    return directory


def _datasets(tmp_path: Path, pnls: tuple[float, ...] = (10, 10, 10, 10)) -> list[Path]:
    return [_dataset(tmp_path, day, net_pnl=pnl) for day, pnl in enumerate(pnls)]


def test_happy_path_uses_chronological_oos_only(tmp_path: Path) -> None:
    report = evaluate_admission(
        _datasets(tmp_path), validation_count=1, oos_count=2, thresholds=PASSING
    )
    assert report["admitted"] is True
    assert [item["split"] for item in report["datasets"]] == [
        "training",
        "validation",
        "out_of_sample",
        "out_of_sample",
    ]
    assert report["oos_aggregate"]["net_pnl"] == 20
    assert all(item["admissible"] for item in report["datasets"])


def test_failed_quality_rejects_admission(tmp_path: Path) -> None:
    datasets = _datasets(tmp_path)
    quality_path = datasets[-1] / "quality_report.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["status"] = "rejected"
    quality_path.write_text(json.dumps(quality), encoding="utf-8")
    report = evaluate_admission(
        datasets, validation_count=1, oos_count=2, thresholds=PASSING
    )
    assert report["admitted"] is False
    assert report["datasets"][-1]["admissible"] is False


@pytest.mark.parametrize("filename", ["manifest.json", "offline_replay.json"])
def test_missing_required_artifact_fails_closed(tmp_path: Path, filename: str) -> None:
    datasets = _datasets(tmp_path)
    (datasets[-1] / filename).unlink()
    report = evaluate_admission(
        datasets, validation_count=1, oos_count=2, thresholds=PASSING
    )
    assert report["admitted"] is False
    assert "all_required_artifacts_valid" in report["failed_criteria"]


@pytest.mark.parametrize("filename", ["manifest.json", "offline_replay.json"])
def test_invalid_required_artifact_fails_closed(tmp_path: Path, filename: str) -> None:
    datasets = _datasets(tmp_path)
    (datasets[-1] / filename).write_text("{", encoding="utf-8")
    report = evaluate_admission(
        datasets, validation_count=1, oos_count=2, thresholds=PASSING
    )
    assert report["admitted"] is False
    assert report["datasets"][-1]["rejection_reason"] is not None


def test_split_requires_training_dataset(tmp_path: Path) -> None:
    with pytest.raises(AdmissionInputError, match="training"):
        evaluate_admission(_datasets(tmp_path)[:2], validation_count=1, oos_count=1)


def test_duplicate_non_chronological_and_overlap_are_rejected(tmp_path: Path) -> None:
    datasets = _datasets(tmp_path)
    with pytest.raises(AdmissionInputError, match="Duplicate"):
        evaluate_admission(
            [datasets[0], datasets[0], datasets[2]], validation_count=1, oos_count=1
        )
    with pytest.raises(AdmissionInputError, match="chronological"):
        evaluate_admission(
            [datasets[1], datasets[0], datasets[2]], validation_count=1, oos_count=1
        )
    overlap_root = tmp_path / "overlap"
    overlap = [
        _dataset(overlap_root, 0),
        _dataset(overlap_root, 0.5),
        _dataset(overlap_root, 2),
    ]
    with pytest.raises(AdmissionInputError, match="overlap"):
        evaluate_admission(overlap, validation_count=1, oos_count=1)


def test_negative_oos_net_pnl_rejects(tmp_path: Path) -> None:
    report = evaluate_admission(
        _datasets(tmp_path, (10, 10, 10, -20)),
        validation_count=1,
        oos_count=2,
        thresholds=PASSING,
    )
    assert report["admitted"] is False
    assert "positive_oos_net_pnl_after_costs" in report["failed_criteria"]


def test_excessive_aggregate_oos_drawdown_rejects(tmp_path: Path) -> None:
    thresholds = AdmissionThresholds(
        minimum_quality_passing_datasets=4,
        minimum_oos_dataset_count=2,
        minimum_oos_trade_count=2,
        maximum_oos_drawdown=5,
        require_positive_oos_net_pnl=False,
        minimum_oos_utc_days=2,
    )
    report = evaluate_admission(
        _datasets(tmp_path, (10, 10, 20, -10)),
        validation_count=1,
        oos_count=2,
        thresholds=thresholds,
    )
    assert report["admitted"] is False
    assert report["oos_aggregate"]["maximum_drawdown"] == 10


def test_cli_exits_nonzero_when_not_admitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasets = _datasets(tmp_path)
    output = tmp_path / "admission.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hibachi-bot",
            "admit-paper",
            "--datasets",
            *(str(path) for path in datasets),
            "--validation-count",
            "1",
            "--oos-count",
            "2",
            "--report",
            str(output),
        ],
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 1
    assert output.is_file()


def test_admission_report_requires_force_to_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "admission.json"
    write_admission_report({"admitted": False}, path)
    with pytest.raises(FileExistsError):
        write_admission_report({"admitted": True}, path)
    write_admission_report({"admitted": True}, path, force=True)
    assert json.loads(path.read_text(encoding="utf-8"))["admitted"] is True
