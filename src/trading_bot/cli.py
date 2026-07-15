import json
import logging

import structlog

from trading_bot.config import Settings
from trading_bot.exchange import HibachiPublicExchange
from trading_bot.service import CollectionBootstrap


def main() -> None:
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
