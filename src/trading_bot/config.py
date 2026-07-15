from enum import StrEnum
from typing import Literal

from pydantic import AnyHttpUrl, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeMode(StrEnum):
    COLLECT = "collect"
    PAPER = "paper"
    LIVE_MINIMAL = "live_minimal"


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
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @model_validator(mode="after")
    def enforce_collect_only(self) -> "Settings":
        if self.bot_mode is not RuntimeMode.COLLECT:
            raise ValueError(
                "This build is COLLECT-only; PAPER and LIVE_MINIMAL are disabled."
            )
        return self
