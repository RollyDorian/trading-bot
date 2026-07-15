import argparse
import asyncio
import json
import logging

import structlog

from trading_bot.collector import build_supervisor
from trading_bot.config import Settings
from trading_bot.exchange import HibachiPublicExchange
from trading_bot.service import CollectionBootstrap
from trading_bot.storage.database import create_engine, create_session_factory
from trading_bot.storage.repository import EventRepository


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hibachi COLLECT-only research service")
    parser.add_argument(
        "--stream",
        action="store_true",
        help="persist the configured public WebSocket topics until stopped",
    )
    return parser.parse_args()


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


def main() -> None:
    args = _parse_args()
    settings = Settings()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[settings.log_level]
        )
    )
    log = structlog.get_logger()
    log.info("starting", mode=settings.bot_mode.value, symbol=settings.hibachi_symbol)

    exchange = HibachiPublicExchange(
        api_url=str(settings.hibachi_api_url),
        data_api_url=str(settings.hibachi_data_api_url),
    )
    metadata = CollectionBootstrap(settings, exchange).validate_contract()
    print(json.dumps(metadata, default=str, sort_keys=True))
    if args.stream:
        asyncio.run(_stream(settings))
