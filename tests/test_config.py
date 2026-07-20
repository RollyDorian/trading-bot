import pytest
from pydantic import ValidationError

from trading_bot.config import (
    IntegrationTestSettings,
    RuntimeMode,
    Settings,
    integration_test_database_url,
)


def test_default_mode_is_collect() -> None:
    settings = Settings(_env_file=None)
    assert settings.bot_mode is RuntimeMode.COLLECT
    assert settings.hibachi_symbol == "ETH/USDT-P"
    assert "trades" in settings.hibachi_topics
    assert "orderbook" in settings.hibachi_topics
    assert settings.reconnect_max_attempts == 5


def test_reconnect_max_delay_cannot_be_shorter_than_initial_delay() -> None:
    with pytest.raises(ValidationError, match="RECONNECT_MAX_DELAY"):
        Settings(
            _env_file=None,
            reconnect_initial_delay=10,
            reconnect_max_delay=1,
        )


@pytest.mark.parametrize("mode", ["paper", "live_minimal"])
def test_non_collect_modes_are_rejected(mode: str) -> None:
    with pytest.raises(ValidationError, match="COLLECT-only"):
        Settings(_env_file=None, bot_mode=mode)


def test_research_runtime_rejects_test_database() -> None:
    with pytest.raises(ValidationError, match="Research runtime"):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://user:password@localhost/cryptobot_test",
        )

    with pytest.raises(ValidationError, match="database_role"):
        Settings(_env_file=None, database_role="test")  # type: ignore[arg-type]


def test_test_configuration_requires_explicit_role_and_test_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_DATABASE_ROLE", raising=False)
    with pytest.raises(ValidationError, match="test_database_role"):
        IntegrationTestSettings(
            _env_file=None,
            test_database_url="postgresql+asyncpg://user:password@localhost/cryptobot_test",
        )
    with pytest.raises(ValidationError, match="explicit test database"):
        IntegrationTestSettings(
            _env_file=None,
            test_database_url="postgresql+asyncpg://user:password@localhost/cryptobot",
            test_database_role="test",
        )


def test_integration_database_cannot_target_research_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    research_url = "postgresql+asyncpg://user:password@localhost/cryptobot"
    monkeypatch.setenv("DATABASE_URL", research_url)
    monkeypatch.setenv("DATABASE_ROLE", "research")
    monkeypatch.setenv("TEST_DATABASE_URL", research_url)
    monkeypatch.setenv("TEST_DATABASE_ROLE", "test")

    with pytest.raises(ValidationError, match="explicit test database"):
        integration_test_database_url()
