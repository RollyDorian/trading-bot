import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

import trading_bot.startup_diagnostic as diagnostic
from trading_bot.config import Settings


class FakeResult:
    def __init__(self, values: list[str]) -> None:
        self._values = values

    def scalars(self) -> "FakeResult":
        return self

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self._values)


class FakeConnection:
    def __init__(self, columns: dict[str, list[str]], failure: Exception | None = None) -> None:
        self.columns = columns
        self.failure = failure
        self.statements: list[str] = []

    async def execute(self, statement: Any, parameters: dict[str, str] | None = None) -> FakeResult:
        self.statements.append(str(statement))
        if self.failure is not None:
            raise self.failure
        if parameters is None:
            return FakeResult([])
        return FakeResult(self.columns[parameters["table_name"]])


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.disposed = False

    def connect(self):  # type: ignore[no-untyped-def]
        return _ConnectionContext(self.connection)

    async def dispose(self) -> None:
        self.disposed = True


class _ConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        return None


def _environment(**changes: str | None) -> dict[str, str | None]:
    environment = {
        "BOT_MODE": "collect",
        "DATABASE_ROLE": "research",
        "DATABASE_URL": "postgresql+asyncpg://redacted",
    }
    environment.update(changes)
    return environment


def _columns() -> dict[str, list[str]]:
    return {table: sorted(columns) for table, columns in diagnostic.REQUIRED_TABLE_COLUMNS.items()}


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://user:password@example.invalid/research",
        database_role="research",
    )


@pytest.mark.asyncio
async def test_diagnostic_validates_read_only_database_and_dependencies() -> None:
    connection = FakeConnection(_columns())
    engine = FakeEngine(connection)
    initialized = False

    def initialize(settings: Settings, repository: object) -> object:
        nonlocal initialized
        assert settings.bot_mode.value == "collect"
        assert repository is not None
        initialized = True
        return SimpleNamespace()

    result = await diagnostic.diagnose(
        environment=_environment(),
        settings_factory=_settings,
        engine_factory=lambda _: engine,  # type: ignore[arg-type]
        dependency_initializer=initialize,  # type: ignore[arg-type]
    )

    assert result.status == "ok"
    assert result.stage == "complete"
    assert result.database == "read_only"
    assert result.schema == "compatible"
    assert result.dependencies == "initialized"
    assert initialized
    assert engine.disposed
    statements = "\n".join(connection.statements).upper()
    assert "SET TRANSACTION READ ONLY" in statements
    assert "SELECT" in statements
    assert not any(
        keyword in statements for keyword in ("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")
    )


@pytest.mark.asyncio
async def test_missing_required_environment_fails_closed_without_constructing_settings() -> None:
    called = False

    def settings_factory() -> Settings:
        nonlocal called
        called = True
        return _settings()

    result = await diagnostic.diagnose(
        environment=_environment(DATABASE_URL=""),
        settings_factory=settings_factory,
    )

    assert result.status == "failed"
    assert result.stage == "config"
    assert result.error_class == "MissingRequiredEnvironment"
    assert result.database == "not_checked"
    assert not called


@pytest.mark.asyncio
async def test_schema_mismatch_fails_closed_and_disposes_engine() -> None:
    columns = _columns()
    columns["market_events"].remove("payload")
    engine = FakeEngine(FakeConnection(columns))

    result = await diagnostic.diagnose(
        environment=_environment(),
        settings_factory=_settings,
        engine_factory=lambda _: engine,  # type: ignore[arg-type]
    )

    assert result.status == "failed"
    assert result.stage == "database"
    assert result.error_class == "SchemaIncompatible"
    assert engine.disposed


@pytest.mark.asyncio
async def test_database_and_dependency_failures_report_stage_and_class_without_message() -> None:
    engine = FakeEngine(FakeConnection(_columns(), RuntimeError("secret-url password=hidden")))
    database_result = await diagnostic.diagnose(
        environment=_environment(),
        settings_factory=_settings,
        engine_factory=lambda _: engine,  # type: ignore[arg-type]
    )
    assert database_result.stage == "database"
    assert database_result.error_class == "RuntimeError"
    assert "secret" not in database_result.to_json()

    dependency_engine = FakeEngine(FakeConnection(_columns()))

    def fail_dependency(settings: Settings, repository: object) -> object:
        del settings, repository
        raise ValueError("token=hidden")

    dependency_result = await diagnostic.diagnose(
        environment=_environment(),
        settings_factory=_settings,
        engine_factory=lambda _: dependency_engine,  # type: ignore[arg-type]
        dependency_initializer=fail_dependency,  # type: ignore[arg-type]
    )
    assert dependency_result.stage == "dependencies"
    assert dependency_result.error_class == "ValueError"
    assert "hidden" not in dependency_result.to_json()


@pytest.mark.asyncio
async def test_database_timeout_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def timeout(_: object) -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(diagnostic, "DATABASE_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(diagnostic, "_validate_database", timeout)
    engine = FakeEngine(FakeConnection(_columns()))

    result = await diagnostic.diagnose(
        environment=_environment(),
        settings_factory=_settings,
        engine_factory=lambda _: engine,  # type: ignore[arg-type]
    )

    assert result.status == "failed"
    assert result.stage == "database"
    assert result.error_class == "TimeoutError"
    assert engine.disposed


def test_cli_is_bounded_and_never_prints_error_messages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def failed() -> diagnostic.DiagnosticResult:
        return diagnostic.DiagnosticResult(
            status="failed",
            stage="database",
            error_class="OperationalError",
            required_environment=diagnostic.REQUIRED_ENVIRONMENT_NAMES,
            database="read_only",
            schema="not_checked",
            dependencies="not_checked",
        )

    monkeypatch.setattr(diagnostic, "diagnose", failed)
    assert diagnostic.run() == diagnostic.FAILURE_EXIT_CODE
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "password" not in captured.out
    assert "Traceback" not in captured.out
    assert len(captured.out) < 300
