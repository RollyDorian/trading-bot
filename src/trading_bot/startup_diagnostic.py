"""Bounded, read-only collector startup-prerequisite diagnostic."""

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Never

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from trading_bot.collector import MarketCollector, build_collector
from trading_bot.config import Settings
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.repository import EventRepository

REQUIRED_ENVIRONMENT_NAMES = ("BOT_MODE", "DATABASE_ROLE", "DATABASE_URL")
REQUIRED_TABLE_COLUMNS: Mapping[str, frozenset[str]] = {
    "market_events": frozenset(
        {"id", "received_at", "source", "event_type", "symbol", "payload"}
    ),
    "system_events": frozenset(
        {"id", "occurred_at", "severity", "event_type", "component", "message", "details"}
    ),
}
SUCCESS_EXIT_CODE = 0
FAILURE_EXIT_CODE = 2
DATABASE_TIMEOUT_SECONDS = 5.0


class MissingRequiredEnvironment(ValueError):
    """A deployment-required environment variable is absent or blank."""


class SchemaIncompatible(RuntimeError):
    """The database does not expose the collector's required append-only schema."""


@dataclass(frozen=True, slots=True)
class DiagnosticResult:
    status: str
    stage: str
    error_class: str | None
    required_environment: tuple[str, ...]
    database: str
    schema: str
    dependencies: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


DependencyInitializer = Callable[[Settings, EventRepository], MarketCollector]


def _require_environment(environment: Mapping[str, str | None]) -> None:
    missing = [
        name for name in REQUIRED_ENVIRONMENT_NAMES if not (environment.get(name) or "").strip()
    ]
    if missing:
        raise MissingRequiredEnvironment


async def _validate_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        for table, required_columns in REQUIRED_TABLE_COLUMNS.items():
            columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = current_schema() AND table_name = :table_name"
                        ),
                        {"table_name": table},
                    )
                ).scalars()
            )
            if not required_columns <= columns:
                raise SchemaIncompatible


def _initialize_dependencies(settings: Settings, repository: EventRepository) -> MarketCollector:
    """Construct dependencies only; never connect, subscribe, or start collection."""

    return build_collector(
        symbol=settings.hibachi_symbol,
        topics=settings.hibachi_topics,
        data_api_url=str(settings.hibachi_data_api_url),
        repository=repository,
    )


async def diagnose(
    *,
    environment: Mapping[str, str | None] | None = None,
    settings_factory: Callable[[], Settings] = Settings,
    engine_factory: Callable[[str], AsyncEngine] = create_engine,
    dependency_initializer: DependencyInitializer = _initialize_dependencies,
) -> DiagnosticResult:
    """Validate startup prerequisites without starting a stream or writing data."""

    stage = "config"
    engine: AsyncEngine | None = None
    try:
        _require_environment(os.environ if environment is None else environment)
        settings = settings_factory()
        stage = "database"
        engine = engine_factory(settings.database_url)
        await asyncio.wait_for(_validate_database(engine), timeout=DATABASE_TIMEOUT_SECONDS)
        stage = "dependencies"
        repository = EventRepository(create_session_factory(engine))
        dependency_initializer(settings, repository)
    except BaseException as error:
        return DiagnosticResult(
            status="failed",
            stage=stage,
            error_class=type(error).__name__,
            required_environment=REQUIRED_ENVIRONMENT_NAMES,
            database="read_only" if stage != "config" else "not_checked",
            schema="compatible" if stage == "dependencies" else "not_checked",
            dependencies="not_checked",
        )
    finally:
        if engine is not None:
            await engine.dispose()
    return DiagnosticResult(
        status="ok",
        stage="complete",
        error_class=None,
        required_environment=REQUIRED_ENVIRONMENT_NAMES,
        database="read_only",
        schema="compatible",
        dependencies="initialized",
    )


def run() -> int:
    result = asyncio.run(diagnose())
    print(result.to_json())
    return SUCCESS_EXIT_CODE if result.status == "ok" else FAILURE_EXIT_CODE


def main() -> Never:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
