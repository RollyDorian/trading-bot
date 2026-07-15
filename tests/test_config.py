import pytest
from pydantic import ValidationError

from trading_bot.config import RuntimeMode, Settings


def test_default_mode_is_collect() -> None:
    settings = Settings(_env_file=None)
    assert settings.bot_mode is RuntimeMode.COLLECT
    assert settings.hibachi_symbol == "ETH/USDT-P"
    assert "trades" in settings.hibachi_topics
    assert "orderbook" in settings.hibachi_topics


@pytest.mark.parametrize("mode", ["paper", "live_minimal"])
def test_non_collect_modes_are_rejected(mode: str) -> None:
    with pytest.raises(ValidationError, match="COLLECT-only"):
        Settings(_env_file=None, bot_mode=mode)
