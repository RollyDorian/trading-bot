"""Read-only MEXC UI market observation capture.

Observe already-rendered values, persist them locally, replay through mexc_shadow.
This subpackage must not click, submit, or call trading endpoints.

Replay helpers live in `.replay` so this package can be imported from the
observer without a circular import through the shadow engine.
"""

from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.store import append_snapshot

__all__ = [
    "append_snapshot",
    "extract_html",
    "observation_from_snapshot",
    "snapshot_from_mapping",
]
