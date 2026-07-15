from dataclasses import asdict
from typing import Any

from trading_bot.config import Settings
from trading_bot.exchange import PublicExchange


class CollectionBootstrap:
    """Validates public exchange metadata before collection can start."""

    def __init__(self, settings: Settings, exchange: PublicExchange) -> None:
        self._settings = settings
        self._exchange = exchange

    def validate_contract(self) -> dict[str, Any]:
        contract = self._exchange.get_contract(self._settings.hibachi_symbol)
        if contract.status.upper() not in {"ACTIVE", "LIVE", "NORMAL", "OPEN"}:
            raise RuntimeError(
                f"Contract {contract.symbol} is not active (status={contract.status!r})"
            )
        return asdict(contract)
