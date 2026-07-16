import pytest
from pydantic import ValidationError

from trading_bot.config import RuntimeMode, Settings


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
