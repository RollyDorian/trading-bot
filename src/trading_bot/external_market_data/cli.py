"""CLI entrypoint for the isolated external-ref collector."""

from __future__ import annotations

import asyncio
import logging
import sys

from trading_bot.external_market_data.runtime import (
    ExternalRefCollector,
    load_config_from_env,
)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config_from_env()
    if not config.enabled:
        print("EXTERNAL_REF_ENABLED=false; exiting without collecting", flush=True)
        raise SystemExit(0)
    collector = ExternalRefCollector(config)
    code = asyncio.run(collector.run())
    raise SystemExit(code)


if __name__ == "__main__":
    main()
    sys.stdout.flush()
