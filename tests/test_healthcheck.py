from datetime import UTC, datetime, timedelta
from typing import NoReturn

import pytest

import trading_bot.healthcheck as healthcheck
from trading_bot.config import Settings


def test_receipt_freshness_requires_current_utc_timestamp() -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    assert healthcheck.receipt_is_fresh(
        now - timedelta(seconds=119),
        now=now,
        max_age_seconds=120,
    )
    assert not healthcheck.receipt_is_fresh(
        now - timedelta(seconds=121),
        now=now,
        max_age_seconds=120,
    )
    assert not healthcheck.receipt_is_fresh(None, now=now, max_age_seconds=120)
    assert not healthcheck.receipt_is_fresh(
        now + timedelta(seconds=1),
        now=now,
        max_age_seconds=120,
    )
    assert not healthcheck.receipt_is_fresh(
        datetime(2026, 7, 19, 11, 59),
        now=now,
        max_age_seconds=120,
    )


def _assert_sanitized_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: object,
) -> None:
    monkeypatch.setattr(healthcheck, "Settings", failure)
    with pytest.raises(SystemExit) as exit_info:
        healthcheck.main([])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert exit_info.value.code == healthcheck.FAILURE_EXIT_CODE
    assert captured.out == ""
    assert captured.err == f"{healthcheck.SAFE_FAILURE_MESSAGE}\n"
    for secret in (
        "postgresql",
        "sentinel_user",
        "sentinel_password",
        "sentinel.example",
        "configuration exploded",
        "Traceback",
        "RuntimeError",
    ):
        assert secret not in combined


def test_cli_sanitizes_settings_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = (
        "configuration exploded: "
        "postgresql+asyncpg://sentinel_user:sentinel_password@sentinel.example/research"
    )

    def fail_settings() -> NoReturn:
        raise RuntimeError(sentinel)

    _assert_sanitized_failure(monkeypatch, capsys, fail_settings)


def test_cli_sanitizes_unexpected_async_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = (
        "async exploded: "
        "postgresql+asyncpg://sentinel_user:sentinel_password@sentinel.example/research"
    )

    async def fail_healthcheck(settings: Settings, *, max_age_seconds: float) -> bool:
        del settings, max_age_seconds
        raise RuntimeError(sentinel)

    monkeypatch.setattr(healthcheck, "collector_is_healthy", fail_healthcheck)
    with pytest.raises(SystemExit) as exit_info:
        healthcheck.main([])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert exit_info.value.code == healthcheck.FAILURE_EXIT_CODE
    assert captured.out == ""
    assert captured.err == f"{healthcheck.SAFE_FAILURE_MESSAGE}\n"
    for secret in (
        "postgresql",
        "sentinel_user",
        "sentinel_password",
        "sentinel.example",
        "async exploded",
        "Traceback",
        "RuntimeError",
    ):
        assert secret not in combined


def test_cli_sanitizes_argument_parsing_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        healthcheck.main(["--max-age-seconds", "postgresql://secret@sentinel.example/db"])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert exit_info.value.code == healthcheck.FAILURE_EXIT_CODE
    assert captured.out == ""
    assert captured.err == f"{healthcheck.SAFE_FAILURE_MESSAGE}\n"
    for secret in ("postgresql", "secret", "sentinel.example", "Traceback"):
        assert secret not in combined


@pytest.mark.parametrize(
    ("healthy", "expected_code", "expected_stdout"),
    [
        (True, healthcheck.HEALTHY_EXIT_CODE, "healthy\n"),
        (False, healthcheck.UNHEALTHY_EXIT_CODE, ""),
    ],
)
def test_cli_preserves_healthy_and_stale_behavior(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    healthy: bool,
    expected_code: int,
    expected_stdout: str,
) -> None:
    async def health_result(settings: Settings, *, max_age_seconds: float) -> bool:
        del settings, max_age_seconds
        return healthy

    monkeypatch.setattr(healthcheck, "collector_is_healthy", health_result)
    with pytest.raises(SystemExit) as exit_info:
        healthcheck.main([])
    captured = capsys.readouterr()
    assert exit_info.value.code == expected_code
    assert captured.out == expected_stdout
    assert captured.err == ""
