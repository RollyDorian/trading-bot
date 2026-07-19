import os
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


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


def _database_identity(database_url: str) -> tuple[str, int, str]:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("Database must use PostgreSQL.")
    if url.host is None or url.database is None:
        raise ValueError("Database URL must identify a PostgreSQL host and database.")
    return (url.host.casefold(), url.port or 5432, url.database.casefold())


def _is_test_database_name(database_name: str) -> bool:
    return "test" in database_name.replace("-", "_").split("_")


def validate_research_database_url(database_url: str) -> str:
    """Reject non-PostgreSQL and explicitly test-only runtime targets."""

    *_, database_name = _database_identity(database_url)
    if _is_test_database_name(database_name):
        raise ValueError("Research runtime cannot target an explicit test database.")
    return database_url


class IntegrationTestSettings(BaseSettings):
    """Explicit, fail-closed PostgreSQL target for integration tests only."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    test_database_url: str
    test_database_role: Literal["test"]

    @model_validator(mode="after")
    def validate_test_target(self) -> "IntegrationTestSettings":
        *_, database_name = _database_identity(self.test_database_url)
        if not _is_test_database_name(database_name):
            raise ValueError("TEST_DATABASE_URL must identify an explicit test database.")
        return self


def integration_test_database_url() -> str:
    """Return an isolated test URL only when role and target guards all pass."""

    test_database_url = os.getenv("TEST_DATABASE_URL")
    test_database_role = os.getenv("TEST_DATABASE_ROLE")
    if test_database_url is None or test_database_role is None:
        raise ValueError("TEST_DATABASE_URL and TEST_DATABASE_ROLE are required.")
    if test_database_role != "test":
        raise ValueError("TEST_DATABASE_ROLE must be 'test'.")
    test_settings = IntegrationTestSettings(
        test_database_url=test_database_url,
        test_database_role="test",
    )
    research_settings = Settings()
    if _database_identity(test_settings.test_database_url) == _database_identity(
        research_settings.database_url
    ):
        raise ValueError("TEST_DATABASE_URL must not target the research database.")
    return test_settings.test_database_url


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
    database_role: Literal["research"] = "research"
    dashboard_token: str | None = None
    admission_report_path: Path = Path("paper-admission-report.json")
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
        validate_research_database_url(self.database_url)
        return self
