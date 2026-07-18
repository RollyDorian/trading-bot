import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol, cast

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.collector import sanitize_error_data, sanitize_error_text
from trading_bot.config import Settings
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import SystemEvent


class DashboardStore(Protocol):
    async def system_events(self, limit: int) -> list[dict[str, Any]]: ...

    async def system_event(self, event_id: int) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...


def _system_event(event: SystemEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "occurred_at": event.occurred_at.isoformat(),
        "severity": event.severity,
        "event_type": event.event_type,
        "component": event.component,
        "message": sanitize_error_text(event.message),
        "details": sanitize_error_data(event.details),
    }


def _safe_system_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(event)
    sanitized["message"] = sanitize_error_text(str(event.get("message", "")))
    sanitized["details"] = sanitize_error_data(event.get("details", {}))
    return sanitized


class DatabaseDashboardStore:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory

    async def system_events(self, limit: int) -> list[dict[str, Any]]:
        statement = select(SystemEvent).order_by(SystemEvent.occurred_at.desc()).limit(limit)
        async with self._session_factory() as session:
            return [_system_event(event) for event in (await session.scalars(statement)).all()]

    async def system_event(self, event_id: int) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            event = await session.get(SystemEvent, event_id)
        return _system_event(event) if event is not None else None

    async def close(self) -> None:
        await self._engine.dispose()


def _safe_dataset_dir(root: Path, version: str) -> Path:
    candidate = (root / version).resolve()
    if candidate.parent != root.resolve():
        raise HTTPException(status_code=404, detail="Dataset not found")
    return candidate


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def list_datasets(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
        manifest = _read_json(directory / "manifest.json") if directory.is_dir() else None
        if manifest is None:
            continue
        quality = _read_json(directory / "quality_report.json")
        status = str(quality.get("status")) if quality else "not_validated"
        findings = quality.get("findings") if quality else None
        reason = (
            str(findings[0])
            if isinstance(findings, list) and findings
            else "Not validated" if quality is None else "No findings"
        )
        result.append(
            {
                "manifest": manifest,
                "quality": quality,
                "quality_status": status,
                "quality_reason": reason,
            }
        )
    return result


def create_app(
    *,
    database_url: str | None = None,
    datasets_dir: Path | None = None,
    store: DashboardStore | None = None,
) -> FastAPI:
    root = datasets_dir or Path(os.getenv("DATASETS_DIR", "datasets"))
    dashboard_store = store
    if dashboard_store is None:
        engine = create_engine(database_url or Settings().database_url)
        dashboard_store = DatabaseDashboardStore(engine, create_session_factory(engine))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await dashboard_store.close()

    app = FastAPI(title="Hibachi COLLECT Research Dashboard", lifespan=lifespan)
    index_path = Path(__file__).parent / "static" / "index.html"

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/api/system/recent")
    async def recent_system_events(
        limit: int = Query(default=20, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        return [
            _safe_system_event_payload(event)
            for event in await dashboard_store.system_events(limit)
        ]

    @app.get("/api/system/{event_id}")
    async def system_event_detail(event_id: int) -> dict[str, Any]:
        event = await dashboard_store.system_event(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="System event not found")
        return _safe_system_event_payload(event)

    @app.get("/api/datasets")
    async def datasets() -> list[dict[str, Any]]:
        return list_datasets(root)

    @app.get("/api/datasets/{version}/quality")
    async def dataset_quality(version: str) -> dict[str, Any]:
        report = _read_json(_safe_dataset_dir(root, version) / "quality_report.json")
        if report is None:
            raise HTTPException(status_code=404, detail="Quality report not found")
        return report

    return app


app = create_app()
