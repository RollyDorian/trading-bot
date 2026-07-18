import sys

import pytest

from trading_bot.cli import _parse_args, _parse_evaluate_command, _parse_export_command


@pytest.mark.parametrize(
    "arguments",
    [
        ["hibachi-bot", "--event-type", "trades"],
        ["hibachi-bot", "--retention-before", "2026-06-01T00:00:00Z"],
        ["hibachi-bot", "--confirm-retention"],
    ],
)
def test_maintenance_options_require_explicit_action(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit):
        _parse_args()


def test_confirmed_retention_arguments_are_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hibachi-bot",
            "--retention-before",
            "2026-06-01T00:00:00Z",
            "--confirm-retention",
        ],
    )

    args = _parse_args()

    assert args.confirm_retention is True
    assert args.retention_before.isoformat() == "2026-06-01T00:00:00+00:00"


def test_dataset_export_requires_explicit_bounded_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["hibachi-bot", "--export-dataset"])
    with pytest.raises(SystemExit):
        _parse_args()


def test_dataset_export_range_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hibachi-bot",
            "--export-dataset",
            "--start",
            "2026-07-18T00:00:00Z",
            "--end",
            "2026-07-18T01:00:00Z",
        ],
    )
    args = _parse_args()
    assert args.export_dataset is True


def test_versioned_export_subcommand_arguments() -> None:
    args = _parse_export_command(
        [
            "--out",
            "datasets",
            "--version",
            "v1_20260718",
            "--start",
            "2026-07-18T00:00:00Z",
            "--end",
            "2026-07-19T00:00:00Z",
        ]
    )
    assert args.version == "v1_20260718"
    assert args.out.name == "datasets"


def test_evaluate_subcommand_arguments() -> None:
    args = _parse_evaluate_command(
        ["datasets/v1_20260718", "--window", "30", "--threshold-bps", "8"]
    )
    assert args.window == 30
    assert args.threshold_bps == 8
