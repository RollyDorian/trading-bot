from enum import StrEnum
from typing import Literal

from pydantic import AnyHttpUrl, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeMode(StrEnum):
    COLLECT = "collect"
    PAPER = "paper"
    LIVE_MINIMAL = "live_minimal"


MarketTopic = Literal[
    "mark_price",
    "spot_price",
    "funding_rate_estimation",
    "trades",
    "orderbook",
    "ask_bid_price",
]


class Settings(BaseSettings):
    """Runtime settings with a hard COLLECT-only gate for the first milestone."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_mode: RuntimeMode = RuntimeMode.COLLECT
    hibachi_symbol: str = Field(default="ETH/USDT-P", min_length=1)
    hibachi_api_url: AnyHttpUrl = AnyHttpUrl("https://api.hibachi.xyz")
    hibachi_data_api_url: AnyHttpUrl = AnyHttpUrl("https://data-api.hibachi.xyz")
    hibachi_topics: tuple[MarketTopic, ...] = (
        "mark_price",
        "spot_price",
        "funding_rate_estimation",
        "trades",
        "orderbook",
        "ask_bid_price",
    )
    database_url: str = "postgresql+asyncpg://cryptobot:cryptobot@localhost:5432/cryptobot"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    reconnect_max_attempts: int = Field(default=5, ge=1, le=100)
    reconnect_initial_delay: float = Field(default=1.0, gt=0, le=60)
    reconnect_max_delay: float = Field(default=30.0, gt=0, le=300)

    @model_validator(mode="after")
    def enforce_collect_only(self) -> "Settings":
        if self.bot_mode is not RuntimeMode.COLLECT:
            raise ValueError(
                "This build is COLLECT-only; PAPER and LIVE_MINIMAL are disabled."
            )
        if self.reconnect_max_delay < self.reconnect_initial_delay:
            raise ValueError("RECONNECT_MAX_DELAY must be >= RECONNECT_INITIAL_DELAY.")
        return self
