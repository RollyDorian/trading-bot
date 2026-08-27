"""Offline research pipeline v1: verified RAW → market_state_1s → baselines.

Isolated from the production collector and PostgreSQL hot buffer. Persistent
analytical outputs are Parquet under an operator research workspace.
"""

from __future__ import annotations

RESEARCH_PIPELINE_VERSION = 1
RESEARCH_PIPELINE_NAME = "offline_market_state_v1"

# Causal as-of staleness ceilings (seconds), chosen from production cadence:
# ~59k events/h across topics ⇒ multi-Hz quotes/books; mark/spot slower;
# funding estimate updates infrequently.
MAX_STALE_BOOK_SECONDS = 5.0
MAX_STALE_QUOTE_SECONDS = 5.0
MAX_STALE_MARK_SECONDS = 30.0
MAX_STALE_SPOT_SECONDS = 30.0
MAX_STALE_FUNDING_SECONDS = 3_600.0

LABEL_HORIZONS_SECONDS: tuple[int, ...] = (5, 15, 30, 60)
