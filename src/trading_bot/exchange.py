from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ContractMetadata:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_order_size: Decimal
    min_notional: Decimal
    status: str


class PublicExchange(Protocol):
    def get_contract(self, symbol: str) -> ContractMetadata: ...


class HibachiPublicExchange:
    """Read-only adapter around the official Hibachi SDK."""

    def __init__(self, api_url: str, data_api_url: str) -> None:
        from hibachi_xyz import HibachiApiClient  # type: ignore[import-untyped]

        self._client = HibachiApiClient(api_url=api_url, data_api_url=data_api_url)

    def get_contract(self, symbol: str) -> ContractMetadata:
        inventory = self._client.get_inventory()
        markets: list[Any] = inventory.markets
        for market in markets:
            contract = market.contract
            if contract.symbol == symbol:
                return ContractMetadata(
                    symbol=contract.symbol,
                    tick_size=Decimal(contract.tickSize),
                    step_size=Decimal(contract.stepSize),
                    min_order_size=Decimal(contract.minOrderSize),
                    min_notional=Decimal(contract.minNotional),
                    status=contract.status,
                )
        raise LookupError(f"Contract {symbol!r} is absent from Hibachi inventory")
