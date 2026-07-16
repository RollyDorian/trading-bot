import sys

import pytest

from trading_bot.cli import _parse_args


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
