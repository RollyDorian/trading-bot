import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trading_bot.collector import sanitize_error_data, sanitize_error_text
from trading_bot.config import Settings
from trading_bot.research.evaluator import evaluate_momentum
from trading_bot.research.exporter import VersionedDatasetExporter
from trading_bot.research.signals.momentum import MomentumConfig
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.models import MarketEvent, SystemEvent

STALE_AFTER_SECONDS = 30


class DatasetExportRequest(BaseModel):
    start: datetime
    end: datetime
    version: str | None = None


class DatasetExportResponse(BaseModel):
    version: str
    path: str


class DatasetEvaluateRequest(BaseModel):
    window: int = Field(default=20, ge=1)
    threshold_bps: float = Field(default=5.0, ge=0)


class DatasetEvaluationResponse(BaseModel):
    model_config = {"extra": "allow"}


class DashboardAuthResponse(BaseModel):
    required: bool


class AdmissionVisibilityResponse(BaseModel):
    available: bool
    report: dict[str, Any] | None = None


def collector_status(
    *,
    event_count: int,
    last_event: datetime | None,
    coverage_start: datetime | None,
    events_last_minute: int,
    average_latency_ms_1m: float | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    age_seconds = (
        max(0.0, (current - last_event).total_seconds()) if last_event is not None else None
    )
    state = (
        "healthy"
        if age_seconds is not None and age_seconds <= STALE_AFTER_SECONDS
        else "stale"
    )
    return {
        "state": state,
        "healthy": state == "healthy",
        "event_count": event_count,
        "last_event_timestamp": last_event.isoformat() if last_event else None,
        "last_event_age_seconds": age_seconds,
        "events_per_minute": events_last_minute,
        "coverage_start_timestamp": coverage_start.isoformat() if coverage_start else None,
        "coverage_end_timestamp": last_event.isoformat() if last_event else None,
        "average_latency_ms_1m": average_latency_ms_1m,
    }


class DashboardStore(Protocol):
    async def status(self) -> dict[str, Any]: ...

    async def recent(self, limit: int) -> list[dict[str, Any]]: ...

    async def system_events(self, limit: int) -> list[dict[str, Any]]: ...

    async def system_event(self, event_id: int) -> dict[str, Any] | None: ...

    async def close(self) -> None: ...


class DatabaseDashboardStore:
    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory

    async def status(self) -> dict[str, Any]:
        minute_ago = datetime.now(UTC) - timedelta(minutes=1)
        try:
            async with self._session_factory() as session:
                count, coverage_start, last_event, events_last_minute, latency = (
                    await session.execute(
                        select(
                            func.count(MarketEvent.id),
                            func.min(MarketEvent.received_at),
                            func.max(MarketEvent.received_at),
                            func.count(MarketEvent.id).filter(
                                MarketEvent.received_at >= minute_ago
                            ),
                            func.avg(MarketEvent.latency_ms).filter(
                                MarketEvent.received_at >= minute_ago
                            ),
                        )
                    )
                ).one()
        except SQLAlchemyError:
            return {
                "state": "fault",
                "event_count": 0,
                "last_event_timestamp": None,
                "last_event_age_seconds": None,
                "events_per_minute": None,
                "coverage_start_timestamp": None,
                "coverage_end_timestamp": None,
                "average_latency_ms_1m": None,
                "healthy": False,
                "error": "database_unavailable",
            }
        return collector_status(
            event_count=count,
            last_event=last_event,
            coverage_start=coverage_start,
            events_last_minute=events_last_minute,
            average_latency_ms_1m=float(latency) if latency is not None else None,
        )

    async def recent(self, limit: int) -> list[dict[str, Any]]:
        statement = select(MarketEvent).order_by(MarketEvent.received_at.desc()).limit(limit)
        async with self._session_factory() as session:
            events = list((await session.scalars(statement)).all())
        events.reverse()
        return [
            {
                "id": event.id,
                "received_at": event.received_at.isoformat(),
                "exchange_at": event.exchange_at.isoformat() if event.exchange_at else None,
                "topic": event.event_type,
                "symbol": event.symbol,
                "sequence": event.sequence,
                "latency_ms": event.latency_ms,
                "payload": event.payload,
            }
            for event in events
        ]

    async def system_events(self, limit: int) -> list[dict[str, Any]]:
        statement = select(SystemEvent).order_by(SystemEvent.occurred_at.desc()).limit(limit)
        async with self._session_factory() as session:
            events = list((await session.scalars(statement)).all())
        return [
            {
                "id": event.id,
                "occurred_at": event.occurred_at.isoformat(),
                "severity": event.severity,
                "event_type": event.event_type,
                "component": event.component,
                "message": sanitize_error_text(event.message),
                "details": sanitize_error_data(event.details),
            }
            for event in events
        ]

    async def system_event(self, event_id: int) -> dict[str, Any] | None:
        async with self._session_factory() as session:
            event = await session.get(SystemEvent, event_id)
        if event is None:
            return None
        return {
            "id": event.id,
            "occurred_at": event.occurred_at.isoformat(),
            "severity": event.severity,
            "event_type": event.event_type,
            "component": event.component,
            "message": sanitize_error_text(event.message),
            "details": sanitize_error_data(event.details),
        }

    async def close(self) -> None:
        await self._engine.dispose()


def _safe_dataset_dir(root: Path, version: str) -> Path:
    candidate = (root / version).resolve()
    root = root.resolve()
    if candidate.parent != root:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return candidate


def _list_datasets(root: Path) -> list[dict[str, Any]]:
    if not root.is_dir():
        return []
    datasets: list[dict[str, Any]] = []
    for directory in sorted(root.iterdir(), key=lambda path: path.name, reverse=True):
        manifest_path = directory / "manifest.json"
        if not directory.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        evaluation_path = directory / "eval_momentum.json"
        evaluation = None
        if evaluation_path.is_file():
            try:
                evaluation = json.loads(evaluation_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                evaluation = None
        quality_path = directory / "quality_report.json"
        quality = None
        if quality_path.is_file():
            try:
                quality = json.loads(quality_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                quality = None
        findings = quality.get("findings") if isinstance(quality, dict) else None
        quality_status = (
            str(quality.get("status")) if isinstance(quality, dict) else "not_validated"
        )
        quality_reason = (
            str(findings[0])
            if isinstance(findings, list) and findings
            else "Not validated" if quality is None else "No findings"
        )
        datasets.append(
            {
                "manifest": manifest,
                "evaluation": evaluation,
                "quality": quality,
                "quality_status": quality_status,
                "quality_reason": quality_reason,
            }
        )
    return datasets


def create_app(
    *,
    database_url: str | None = None,
    datasets_dir: Path | None = None,
    store: DashboardStore | None = None,
    settings: Settings | None = None,
    admission_report_path: Path | None = None,
) -> FastAPI:
    dashboard_settings = settings or Settings()
    root = datasets_dir or Path(os.getenv("DATASETS_DIR", "datasets"))
    admission_path = admission_report_path or dashboard_settings.admission_report_path
    dashboard_store = store
    if dashboard_store is None:
        engine = create_engine(database_url or dashboard_settings.database_url)
        dashboard_store = DatabaseDashboardStore(engine, create_session_factory(engine))

    export_engine = create_engine(database_url or dashboard_settings.database_url)
    exporter = VersionedDatasetExporter(create_session_factory(export_engine))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await dashboard_store.close()
        await export_engine.dispose()

    app = FastAPI(title="Hibachi COLLECT Research Dashboard", lifespan=lifespan)
    static_index = Path(__file__).parent / "static" / "index.html"

    async def require_dashboard_token(
        authorization: str | None = Header(default=None),
    ) -> None:
        token = dashboard_settings.dashboard_token
        if token is not None and authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_index)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return await dashboard_store.status()

    @app.get("/api/datasets")
    async def datasets() -> list[dict[str, Any]]:
        return _list_datasets(root)

    @app.get("/api/dashboard/auth", response_model=DashboardAuthResponse)
    async def dashboard_auth() -> DashboardAuthResponse:
        return DashboardAuthResponse(required=dashboard_settings.dashboard_token is not None)

    @app.get("/api/admission", response_model=AdmissionVisibilityResponse)
    async def admission() -> AdmissionVisibilityResponse:
        if not admission_path.is_file():
            return AdmissionVisibilityResponse(available=False)
        try:
            value = json.loads(admission_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return AdmissionVisibilityResponse(available=False)
        if not isinstance(value, dict):
            return AdmissionVisibilityResponse(available=False)
        sanitized = sanitize_error_data(value)
        if not isinstance(sanitized, dict):
            return AdmissionVisibilityResponse(available=False)
        return AdmissionVisibilityResponse(available=True, report=sanitized)

    @app.post(
        "/api/datasets/export",
        response_model=DatasetExportResponse,
        dependencies=[Depends(require_dashboard_token)],
    )
    async def export_dataset(request: DatasetExportRequest) -> DatasetExportResponse:
        if request.start.utcoffset() != timedelta(0) or request.end.utcoffset() != timedelta(0):
            raise HTTPException(status_code=400, detail="Timestamps must be UTC")
        if request.start >= request.end:
            raise HTTPException(status_code=400, detail="Export start must be earlier than end")
        try:
            path = await exporter.export(
                output_root=root,
                symbol=dashboard_settings.hibachi_symbol,
                version=request.version,
                start=request.start,
                end=request.end,
            )
        except FileExistsError as error:
            raise HTTPException(status_code=400, detail="Dataset version already exists") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail="Invalid export request") from error
        except (OSError, SQLAlchemyError) as error:
            raise HTTPException(status_code=400, detail="Dataset export failed") from error
        return DatasetExportResponse(version=path.name, path=str(path))

    @app.post(
        "/api/datasets/{version}/evaluate",
        response_model=DatasetEvaluationResponse,
        dependencies=[Depends(require_dashboard_token)],
    )
    async def evaluate_dataset(
        version: str, request: DatasetEvaluateRequest
    ) -> dict[str, Any]:
        directory = _safe_dataset_dir(root, version)
        if not directory.is_dir():
            raise HTTPException(status_code=404, detail="Dataset not found")
        try:
            return evaluate_momentum(
                directory,
                MomentumConfig(
                    window=request.window,
                    threshold_bps=request.threshold_bps,
                ),
            )
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail="Dataset evaluation failed") from error

    @app.get("/api/datasets/{version}/eval")
    async def dataset_evaluation(version: str) -> dict[str, Any]:
        path = _safe_dataset_dir(root, version) / "eval_momentum.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Evaluation not found")
        try:
            return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=500, detail="Invalid evaluation file") from error

    @app.get("/api/datasets/{version}/quality")
    async def dataset_quality(version: str) -> dict[str, Any]:
        path = _safe_dataset_dir(root, version) / "quality_report.json"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Quality report not found")
        try:
            return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=500, detail="Invalid quality report") from error

    @app.get("/api/market/recent")
    async def recent(limit: int = Query(default=200, ge=1, le=1_000)) -> list[dict[str, Any]]:
        return await dashboard_store.recent(limit)

    @app.get("/api/system/recent")
    async def system_events(
        limit: int = Query(default=20, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        return [
            {
                **event,
                "message": sanitize_error_text(str(event.get("message", ""))),
                "details": sanitize_error_data(event.get("details", {})),
            }
            for event in await dashboard_store.system_events(limit)
        ]

    @app.get("/api/system/{event_id}")
    async def system_event_detail(event_id: int) -> dict[str, Any]:
        event = await dashboard_store.system_event(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="System event not found")
        return {
            **event,
            "message": sanitize_error_text(str(event.get("message", ""))),
            "details": sanitize_error_data(event.get("details", {})),
        }

    return app


app = create_app()
