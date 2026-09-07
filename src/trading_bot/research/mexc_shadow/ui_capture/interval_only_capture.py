"""MEXC UI interval-only capture remediation.

Mutations no longer enqueue raw market snapshots. Protocol v2.0.0 is unchanged.
Does not inspect mom/gap, PnL, ML, PAPER, or LIVE.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.stage_diagnostics import (
    DIAGNOSTIC_FORMAT_VERSION,
    EXTENSION_VERSION,
)

MILESTONE = "MEXC_UI_INTERVAL_ONLY_CAPTURE_REMEDIATION_V1"
MILESTONE_STATUS = "MEXC_UI_INTERVAL_ONLY_CAPTURE_REMEDIATION_READY"
MILESTONE_DECISION = "STOP_FOR_LEAD_REVIEW"
PROTOCOL_VERSION = "2.0.0"
CATALOG_VERSION = "v1.2"
EXTENSION_PREVIOUS = "1.3.4"


def milestone_report() -> dict[str, Any]:
    return {
        "milestone": MILESTONE,
        "status": MILESTONE_STATUS,
        "decision": MILESTONE_DECISION,
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "mom_gap_inspected": False,
        "protocol_version": PROTOCOL_VERSION,
        "protocol_changed": False,
        "protocol_v2_can_remain_unchanged": True,
        "catalog_version": CATALOG_VERSION,
        "extension_previous": EXTENSION_PREVIOUS,
        "extension_version": EXTENSION_VERSION,
        "diagnostic_format_version": DIAGNOSTIC_FORMAT_VERSION,
        "raw_observation_triggers": ["manual", "interval"],
        "mutation_raw_rows": False,
        "mutation_observer_role": "diagnostic_dirty_notification_only",
        "heartbeat_skips_on_clean_dirty_flag": False,
        "cached_field_copy": False,
        "forward_fill_missing_fields": False,
        "observation_timestamps": "extract_completion_not_timer_deadline",
        "persistence_decoupling": False,
        "indexeddb_batching": False,
        "hidden_tab_alternate_timer": False,
        "backdating_catchup": False,
        "hidden_tab_2hz": "out_of_scope",
        "intended_steady_state_hz": 2.0,
        "intended_plus_initial_manual_row": True,
        "long_capture_started": False,
        "next_operator_step": (
            "Lead review, then a short visible-tab gate against frozen protocol v2. "
            "Do not start the 8–12 h corpus in this milestone."
        ),
    }


def render_milestone_markdown(report: dict[str, Any]) -> str:
    return f"""# MEXC UI interval-only capture remediation v1

MILESTONE: `{report["milestone"]}`

STATUS: `{report["status"]}`

DECISION: `{report["decision"]}`

ML_STATUS: `{report["ml_status"]}`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

MOM/GAP: **not inspected**

Protocol v2.0.0: **unchanged** (frozen)

Catalog: `{report["catalog_version"]}` (unchanged)

Extension: `{report["extension_previous"]}` → `{report["extension_version"]}`

## Purpose

Remove mutation raw rows from the visible-path observation stream. The 1.3.4
visibility experiment showed final-visible content FIFO backpressure from
MutationObserver snapshots sharing emitChain with the 500 ms heartbeat.
This milestone implements interval-only raw observations. It does not restore
hidden-tab 2 Hz, does not batch IndexedDB, and does not decouple persistence.

## Required capture semantics

Raw market observations are:

1. one explicit initial/manual observation at session start;
2. one fresh full DOM observation on every configured 500 ms interval callback.

`MutationObserver` must not enqueue a raw market snapshot. It may only
increment bounded diagnostic counters and set `dirty_since_last_interval`.
The interval observation rereads every required DOM field regardless of that
flag. The dirty flag never skips a heartbeat, never copies cached market
fields into a new observation, and never forward-fills missing fields.

Observation timestamps stay extract-time: `received_at_local`,
`observed_at_local`, `monotonic_ms`. Locale provenance, catalog v1.2,
symbol/header/BBO parsing, sequence/storage fail-closed behavior, and stage
diagnostics remain. Visibility is still recorded.

Intended steady-state raw rate is approximately 2 Hz plus the single initial
row.

## What this does not do

- persistence decoupling
- IndexedDB batching
- alternate hidden-tab timer sources
- backdating or catch-up observations
- hidden-tab 2 Hz
- mom/gap retune, ML, PAPER, LIVE, protocol v2 changes, or a long capture

## Lead-review next step

{report["next_operator_step"]}
"""


def write_reports(*, out_json: Path, out_md: Path) -> dict[str, Any]:
    report = milestone_report()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    out_md.write_text(render_milestone_markdown(report), encoding="utf-8")
    return report
