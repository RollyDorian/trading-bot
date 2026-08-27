"""Isolated Binance USD-M external reference market-data collector (COLLECT-only).

Completely separate failure domain from the Hibachi collector.
"""

from __future__ import annotations

from trading_bot.external_market_data.contract import (
    CONTRACT_NAME,
    CONTRACT_VERIFIED_AT_UTC,
)

__all__ = ["CONTRACT_NAME", "CONTRACT_VERIFIED_AT_UTC"]
