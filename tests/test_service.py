from decimal import Decimal

import pytest

from trading_bot.config import Settings
from trading_bot.exchange import ContractMetadata
from trading_bot.service import CollectionBootstrap


class StubExchange:
    def __init__(self, status: str = "ACTIVE") -> None:
        self.status = status

    def get_contract(self, symbol: str) -> ContractMetadata:
        return ContractMetadata(
            symbol=symbol,
            tick_size=Decimal("0.01"),
            step_size=Decimal("0.0001"),
            min_order_size=Decimal("0.0001"),
            min_notional=Decimal("1"),
            status=self.status,
        )


@pytest.mark.parametrize("status", ["ACTIVE", "LIVE", "NORMAL", "OPEN"])
def test_active_contract_is_accepted(status: str) -> None:
    settings = Settings(_env_file=None)
    result = CollectionBootstrap(settings, StubExchange(status)).validate_contract()
    assert result["symbol"] == "ETH/USDT-P"
    assert result["status"] == status


def test_inactive_contract_fails_closed() -> None:
    settings = Settings(_env_file=None)
    with pytest.raises(RuntimeError, match="not active"):
        CollectionBootstrap(settings, StubExchange("SUSPENDED")).validate_contract()
