import argparse
import asyncio
import json
import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

import structlog

from trading_bot.collector import build_supervisor
from trading_bot.config import Settings
from trading_bot.exchange import HibachiPublicExchange
from trading_bot.paper import PaperEngine
from trading_bot.service import CollectionBootstrap
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.maintenance import DataMaintenance, ReplayFilter
from trading_bot.storage.repository import EventRepository


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hibachi COLLECT-only research service")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--stream",
        action="store_true",
        help="persist the configured public WebSocket topics until stopped",
    )
    action.add_argument("--quality-date", type=date.fromisoformat, metavar="YYYY-MM-DD")
    action.add_argument("--replay", action="store_true", help="print deterministic JSONL replay")
    action.add_argument("--paper", action="store_true", help="run account-free PAPER simulation")
    action.add_argument(
        "--paper-backtest", action="store_true", help="backtest stored public market events"
    )
    action.add_argument(
        "--retention-before",
        type=_parse_datetime,
        metavar="TIMESTAMP",
        help="delete events older than an explicit timezone-aware timestamp",
    )
    parser.add_argument("--start", type=_parse_datetime, help="replay start timestamp")
    parser.add_argument("--end", type=_parse_datetime, help="replay end timestamp")
    parser.add_argument("--event-type", action="append", default=[])
    parser.add_argument("--limit", type=int, default=10_000)
    parser.add_argument("--confirm-retention", action="store_true")
    parser.add_argument("--duration-seconds", type=float, default=28_800.0)
    args = parser.parse_args()
    replay_options_used = (
        args.start is not None
        or args.end is not None
        or bool(args.event_type)
        or args.limit != 10_000
    )
    if replay_options_used and not (args.replay or args.paper_backtest):
        parser.error("--start, --end, --event-type, and --limit require replay or backtest")
    if args.retention_before is not None and not args.confirm_retention:
        parser.error("--retention-before requires --confirm-retention")
    if args.confirm_retention and args.retention_before is None:
        parser.error("--confirm-retention requires --retention-before")
    if args.duration_seconds <= 0:
        parser.error("--duration-seconds must be positive")
    return args


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed


async def _stream(settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    repository = EventRepository(create_session_factory(engine))
    supervisor = build_supervisor(
        symbol=settings.hibachi_symbol,
        topics=settings.hibachi_topics,
        data_api_url=str(settings.hibachi_data_api_url),
        repository=repository,
        max_attempts=settings.reconnect_max_attempts,
        initial_delay=settings.reconnect_initial_delay,
        max_delay=settings.reconnect_max_delay,
    )
    try:
        await supervisor.run()
    finally:
        await engine.dispose()


class _PaperSink:
    def __init__(self, repository: EventRepository, paper: PaperEngine) -> None:
        self._repository = repository
        self._paper = paper

    async def append_market_event(self, event: Any) -> None:
        await self._repository.append_market_event(event)
        self._paper.on_event(event.payload, event.received_at)

    async def append_system_event(self, **event: Any) -> None:
        await self._repository.append_system_event(**event)


async def _paper_stream(settings: Settings, duration_seconds: float) -> dict[str, Any]:
    engine = create_engine(settings.database_url)
    repository = EventRepository(create_session_factory(engine))
    paper = PaperEngine()
    sink = _PaperSink(repository, paper)
    supervisor = build_supervisor(
        symbol=settings.hibachi_symbol,
        topics=settings.hibachi_topics,
        data_api_url=str(settings.hibachi_data_api_url),
        repository=sink,  # type: ignore[arg-type]
        max_attempts=settings.reconnect_max_attempts,
        initial_delay=settings.reconnect_initial_delay,
        max_delay=settings.reconnect_max_delay,
    )
    try:
        try:
            async with asyncio.timeout(duration_seconds):
                await supervisor.run()
        except TimeoutError:
            pass
        paper.close()
        return paper.report()
    finally:
        await engine.dispose()


async def _paper_backtest(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    engine = create_engine(settings.database_url)
    maintenance = DataMaintenance(create_session_factory(engine))
    paper = PaperEngine()
    try:
        events = await maintenance.replay(
            ReplayFilter(
                symbol=settings.hibachi_symbol,
                start=args.start,
                end=args.end,
                limit=args.limit,
            )
        )
        for event in events:
            paper.on_event(event.payload, event.received_at)
        paper.close(events[-1].received_at if events else None)
        report = paper.report()
        report["events_replayed"] = len(events)
        return report
    finally:
        await engine.dispose()


def _event_json(event: Any) -> str:
    return json.dumps(
        {
            "id": event.id,
            "received_at": event.received_at.isoformat(),
            "exchange_at": event.exchange_at.isoformat() if event.exchange_at else None,
            "source": event.source,
            "event_type": event.event_type,
            "symbol": event.symbol,
            "sequence": event.sequence,
            "latency_ms": event.latency_ms,
            "payload": event.payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


async def _maintenance(args: argparse.Namespace, settings: Settings) -> None:
    engine = create_engine(settings.database_url)
    maintenance = DataMaintenance(create_session_factory(engine))
    try:
        if args.quality_date is not None:
            metrics = await maintenance.daily_quality(
                args.quality_date,
                symbol=settings.hibachi_symbol,
            )
            print(json.dumps([asdict(metric) for metric in metrics], default=str, sort_keys=True))
        elif args.replay:
            events = await maintenance.replay(
                ReplayFilter(
                    symbol=settings.hibachi_symbol,
                    start=args.start,
                    end=args.end,
                    event_types=tuple(args.event_type),
                    limit=args.limit,
                )
            )
            for event in events:
                print(_event_json(event))
        elif args.retention_before is not None:
            result = await maintenance.prune_before(
                args.retention_before,
                confirmed=args.confirm_retention,
            )
            print(json.dumps(asdict(result), sort_keys=True))
    finally:
        await engine.dispose()


def main() -> None:
    args = _parse_args()
    settings = Settings()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        )
    )
    if args.paper_backtest:
        print(json.dumps(asyncio.run(_paper_backtest(args, settings)), default=str, sort_keys=True))
        return
    if args.quality_date is not None or args.replay or args.retention_before is not None:
        asyncio.run(_maintenance(args, settings))
        return

    log = structlog.get_logger()
    log.info("starting", mode=settings.bot_mode.value, symbol=settings.hibachi_symbol)

    exchange = HibachiPublicExchange(
        api_url=str(settings.hibachi_api_url),
        data_api_url=str(settings.hibachi_data_api_url),
    )
    metadata = CollectionBootstrap(settings, exchange).validate_contract()
    print(json.dumps(metadata, default=str, sort_keys=True))
    if args.paper:
        print(
            json.dumps(
                asyncio.run(_paper_stream(settings, args.duration_seconds)),
                default=str,
                sort_keys=True,
            )
        )
    elif args.stream:
        asyncio.run(_stream(settings))
