"""Long TAOUSDT corpus admissibility against frozen protocol v2.0.0.

Scores the first corrected identification capture after extension 1.3.5.
Does not import mom/gap formulas, does not execute any of the 21 cells, and
does not compute PnL. The whole session is scored; bad periods are not cropped.
The 500 ms causal grid is materialized for the 95% coverage bar and hashed
only if every frozen input gate passes.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.catalog import (
    SCHEMA_NAME,
    SCHEMA_VERSION,
)
from trading_bot.research.mexc_shadow.ui_capture.durable import (
    DEFAULT_CHUNK_SIZE,
    is_session_record,
)
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.quality import _ok_price
from trading_bot.research.mexc_shadow.ui_capture.stage_diagnostics import (
    expected_interval_opportunities,
    finite_number,
)
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings
from trading_bot.research.mexc_shadow.ui_capture.v2_contract_gate import (
    FROZEN_INTERARRIVAL_LE_2000_MIN,
    FROZEN_P95_MAX_MS,
    FROZEN_SIMULTANEOUS_MIN,
    PROTOCOL_V1_VERSION,
    PROTOCOL_V2_AMENDMENT_COMMIT,
    PROTOCOL_V2_MERGE_COMMIT,
    PROTOCOL_VERSION,
    REQUIRED_CATALOG,
    REQUIRED_INTERVAL_MS,
    STRATEGY_FIELDS,
    TAO_LAST_MAX,
    TAO_LAST_MIN,
    V133_SNAPSHOT_KEYS,
    _locale_route,
    _percentile,
    _scale_audit_ok,
    sha256_file,
)

MILESTONE = "MEXC_TAO_CORRECTED_LONG_CORPUS_ADMISSIBILITY_V1"
STATUS_ADMISSIBLE = "MEXC_TAO_CORRECTED_LONG_CORPUS_ADMISSIBLE"
STATUS_INADEQUATE = "DATA_INADEQUATE"
MILESTONE_DECISION = "STOP_FOR_LEAD_REVIEW"
EXTENSION_EXPECTED = "1.3.5"
REQUIRED_USABLE_HOURS = 8.0
GRID_MS = 500
ASOF_MAX_MS = 1000
GAP_RESET_MS = 2000
# Merge timestamp of protocol v2 onto main (PR #48, 2026-09-07 17:40:46 +0400).
PROTOCOL_V2_COMMIT_UTC = datetime(2026, 9, 7, 13, 40, 46, tzinfo=UTC)
LOCKED_FILENAME = (
    "mexc_ui_capture_e41b48eb-852c-4a36-88b6-9fc9a10ced32_2026-09-21T06-04-21-527Z.ndjson"
)
LOCKED_CORPUS_SHA256 = (
    "5c15b9714f804f8df5a327ae81fb2d7fb515ec052aeed0ff1af5df5a8680467c"
)
LOCKED_BYTE_COUNT = 394807859
DEFAULT_CAPTURE = Path("data") / "mexc_ui_capture" / LOCKED_FILENAME
DEFAULT_MANIFEST = Path("docs") / "mexc_tao_corrected_long_corpus_execution_manifest_v1.json"
DEFAULT_GRID_PATH = (
    Path("data") / "mexc_ui_capture" / "mexc_tao_corrected_long_corpus_grid_v1.ndjson"
)

# Historical 11.67 h file and every short-gate SHA. Forbidden as identification data.
EXCLUDED_CORPUS_SHA256 = frozenset(
    {
        "7a41c34d4ae855850cd8a1a47e438e940c38e09d6f5a555c3f397e5650da9c2a",
        "d1d4c08f785efa489760cf33d853223277f00c4837ac9773249c5345f74e3f45",
        "1f83d307cc42324f803b26c01f6a6afde5eb1dc65b938909cf50eaeb09b56f2b",
        "c3699bb2261b153dda093e36e801e9f352ffb02891345c7aea1b1ff1ba6c81c2",
        "5665207fd95c46611fecf8a2082ba39ceb5809ada5f7dd26aefdb4807f9613e9",
    }
)
EXCLUDED_NAME_PARTS = (
    "mexc_ui_capture_sessions_2026-09-03",
    "e03fc35f-4280-486c-8144-86ffb1aa6515",
    "3c6e0474",
    "56f504d4",
)


@dataclass(frozen=True, slots=True)
class CompactRow:
    """Causal-grid input. Not a mom/gap feature row."""

    received_ms: float
    received_at: str
    sequence: int
    session_id: str
    trigger: str
    as_of_eligible: bool
    five_ok: bool
    bid: float | None
    ask: float | None
    last: float | None
    mark: float | None
    index: float | None


def _report_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _stamp_ms(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    return parsed.timestamp() * 1000.0


def _align_ceil(ms: float, step: int = GRID_MS) -> int:
    """Next UTC grid tick at or after receipt time."""

    x = int(math.ceil(ms - 1e-9))
    rem = x % step
    return x if rem == 0 else x + (step - rem)


def _align_floor(ms: float, step: int = GRID_MS) -> int:
    x = int(math.floor(ms + 1e-9))
    return (x // step) * step


def _nested_count(block: Any, name: str) -> int | None:
    if not isinstance(block, dict):
        return None
    value = block.get(name)
    if value is None:
        return None
    return int(value)


def _visible_to_hidden_count(lifecycle: list[dict[str, Any]]) -> int:
    n = 0
    for event in lifecycle:
        if event.get("kind") != "visibilitychange":
            continue
        if event.get("from_state") == "visible" and event.get("to_state") == "hidden":
            n += 1
    return n


def _five_tuple(row: CompactRow) -> tuple[float | None, ...]:
    return (row.bid, row.ask, row.last, row.mark, row.index)


def split_usable_segments(
    rows: list[CompactRow], *, gap_reset_ms: float = GAP_RESET_MS
) -> list[list[CompactRow]]:
    """Split on session change or raw gap > 2,000 ms. Do not drop quality failures."""

    segments: list[list[CompactRow]] = []
    current: list[CompactRow] = []
    previous: CompactRow | None = None
    for row in rows:
        if previous is None:
            current = [row]
        elif (
            row.session_id != previous.session_id
            or (row.received_ms - previous.received_ms) > gap_reset_ms
        ):
            segments.append(current)
            current = [row]
        else:
            current.append(row)
        previous = row
    if current:
        segments.append(current)
    return segments


def usable_hours_from_segments(segments: list[list[CompactRow]]) -> float:
    """Wall time remaining after session/time-gap exclusions, in hours."""

    total_ms = 0.0
    for segment in segments:
        if len(segment) >= 2:
            total_ms += segment[-1].received_ms - segment[0].received_ms
    return total_ms / 3_600_000.0


def materialize_causal_grid(
    rows: list[CompactRow], *, as_of_max_ms: float = ASOF_MAX_MS
) -> list[dict[str, Any]]:
    """Frozen 500 ms UTC grid with causal as-of ≤ 1,000 ms. One snapshot per tick."""

    grid: list[dict[str, Any]] = []
    for segment in split_usable_segments(rows):
        if not segment:
            continue
        start_ms = segment[0].received_ms
        end_ms = segment[-1].received_ms
        tick = _align_ceil(start_ms)
        end_tick = _align_floor(end_ms)
        index = 0
        last_valid = -1
        session_id = segment[0].session_id
        while tick <= end_tick:
            while index < len(segment) and segment[index].received_ms <= tick:
                if segment[index].as_of_eligible:
                    last_valid = index
                index += 1
            if last_valid >= 0 and (tick - segment[last_valid].received_ms) <= as_of_max_ms:
                src = segment[last_valid]
                grid.append(
                    {
                        "t_ms": tick,
                        "session_id": src.session_id,
                        "seq": src.sequence,
                        "received_at": src.received_at,
                        "lag_ms": round(tick - src.received_ms, 6),
                        "bid": src.bid,
                        "ask": src.ask,
                        "last": src.last,
                        "mark": src.mark,
                        "index": src.index,
                        "five_ok": src.five_ok,
                    }
                )
            else:
                grid.append(
                    {
                        "t_ms": tick,
                        "session_id": session_id,
                        "seq": None,
                        "received_at": None,
                        "lag_ms": None,
                        "bid": None,
                        "ask": None,
                        "last": None,
                        "mark": None,
                        "index": None,
                        "five_ok": False,
                    }
                )
            tick += GRID_MS
    return grid


def hash_grid_rows(grid: list[dict[str, Any]]) -> str:
    """SHA-256 of canonical NDJSON. Stable across processes using the same rows."""

    digest = hashlib.sha256()
    for row in grid:
        line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def write_grid_ndjson(grid: list[dict[str, Any]], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in grid:
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            encoded = (line + "\n").encode("utf-8")
            digest.update(encoded)
            handle.write(line + "\n")
    return digest.hexdigest()


def _excluded_path(path: Path) -> bool:
    text = path.as_posix().replace("\\", "/")
    if "tests/fixtures" in text:
        return True
    name = path.name
    return any(part in name for part in EXCLUDED_NAME_PARTS)


def score_long_corpus_admissibility(path: Path) -> dict[str, Any]:
    """Score one capture against frozen protocol-v2 long-corpus input gates."""

    digest = sha256_file(path)
    byte_count = path.stat().st_size
    rows: list[CompactRow] = []
    n_considered = 0
    n_schema_mismatch = 0
    n_missing_v133 = 0
    n_wrong_interval = 0
    n_locale_route_fail = 0
    n_scale_fail = 0
    scale_fail_reason: str | None = None
    n_bad_stamp = 0
    n_unknown_on_ready = 0
    n_strategy_ready = 0
    n_startup_warmup = 0
    n_data_invalid = 0
    n_ambiguous = 0
    n_bid_ge_ask = 0
    n_interval = 0
    n_unchanged_interval = 0
    n_missing_diag = 0
    n_wrong_extension = 0
    n_stale = 0
    n_mismatch = 0
    scale_outliers = 0
    previous_five: tuple[float | None, ...] | None = None
    first_trigger: str | None = None
    later_triggers: Counter[str] = Counter()
    triggers: Counter[str] = Counter()
    vis_states: Counter[str] = Counter()
    vis_extract: Counter[str] = Counter()
    locales: Counter[str] = Counter()
    locale_sources: Counter[str] = Counter()
    document_langs: Counter[str] = Counter()
    locale_routes: Counter[str] = Counter()
    paths: Counter[str] = Counter()
    catalogs: Counter[str] = Counter()
    schema_versions: Counter[int] = Counter()
    intervals: Counter[int] = Counter()
    mark_selectors: Counter[str] = Counter()
    index_selectors: Counter[str] = Counter()
    producer_epochs: set[str] = set()
    session_generations: set[int] = set()
    worker_boots: set[str] = set()
    session_extensions: list[str] = []
    session_intervals: list[int] = []
    session_chunk_mismatch = 0
    storage_errors: list[str] = []
    scan_sessions: list[dict[str, Any]] = []
    seq_diag: list[dict[str, Any]] = []
    seq_prev: dict[str, int] = {}
    seq_seen: dict[str, set[int]] = {}
    session_ready: dict[str, bool] = {}
    lifecycle: list[dict[str, Any]] = []
    counters: dict[str, Any] = {}
    high_water: dict[str, Any] = {}
    first_symbol: str | None = None
    first_received: str | None = None
    last_received: str | None = None
    lasts: list[float] = []
    replay = hashlib.sha256()
    replay_first = True
    capture_ids: set[str] = set()
    session_ids: set[str] = set()

    for payload in iter_all_mappings(path):
        if is_session_record(payload):
            session_id = str(payload.get("session_id") or "")
            if session_id:
                session_ids.add(session_id)
            ext = payload.get("extension_version")
            if ext is not None:
                session_extensions.append(str(ext))
            epoch = payload.get("producer_epoch")
            if epoch:
                producer_epochs.add(str(epoch))
            generation = payload.get("session_generation")
            if generation is not None:
                session_generations.add(int(generation))
            worker = payload.get("worker_boot_id")
            if worker:
                worker_boots.add(str(worker))
            interval = payload.get("interval_ms")
            if interval is not None:
                session_intervals.append(int(interval))
            record_type = payload.get("record_type")
            compact_session = {
                "record_type": record_type,
                "session_id": payload.get("session_id"),
                "status": payload.get("status"),
                "storage_error": payload.get("storage_error"),
                "n_snapshots": payload.get("n_snapshots"),
                "n_chunks": payload.get("n_chunks"),
                "first_sequence": payload.get("first_sequence"),
                "last_sequence": payload.get("last_sequence"),
                "started_at": payload.get("started_at"),
                "ended_at": payload.get("ended_at"),
                "producer_epoch": payload.get("producer_epoch"),
                "session_generation": payload.get("session_generation"),
                "worker_boot_id": payload.get("worker_boot_id"),
                "extension_version": payload.get("extension_version"),
            }
            if record_type == "session_end":
                if payload.get("storage_error"):
                    storage_errors.append(str(payload["storage_error"]))
                if str(payload.get("status") or "") == "failed":
                    storage_errors.append(f"session {session_id} status=failed")
                n_session_snaps = payload.get("n_snapshots")
                n_chunks = payload.get("n_chunks")
                chunk_size = int(payload.get("chunk_size") or DEFAULT_CHUNK_SIZE)
                if n_session_snaps is not None and n_chunks is not None:
                    expected_chunks = (
                        math.ceil(int(n_session_snaps) / chunk_size)
                        if int(n_session_snaps)
                        else 0
                    )
                    if int(n_chunks) != expected_chunks:
                        session_chunk_mismatch += 1
                    first_seq = payload.get("first_sequence")
                    last_seq = payload.get("last_sequence")
                    if first_seq is not None and int(first_seq) != 1:
                        session_chunk_mismatch += 1
                    if last_seq is not None and int(last_seq) != int(n_session_snaps):
                        session_chunk_mismatch += 1
                summary = payload.get("stage_diagnostic_summary") or {}
                if isinstance(summary, dict):
                    counters = dict(summary.get("counters") or {})
                    high_water = dict(summary.get("high_water") or {})
                    lifecycle = list(summary.get("lifecycle") or [])
                    for epoch in summary.get("producer_epochs") or []:
                        producer_epochs.add(str(epoch))
                    for worker in summary.get("worker_boot_ids") or []:
                        worker_boots.add(str(worker))
            scan_sessions.append(compact_session)
            continue
        if payload.get("schema") != SCHEMA_NAME:
            continue
        n_considered += 1
        if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
            n_schema_mismatch += 1
        if any(key not in payload for key in V133_SNAPSHOT_KEYS):
            n_missing_v133 += 1
        snap = snapshot_from_mapping(payload)
        session_id = str(snap.capture_id or "_unknown")
        capture_ids.add(session_id)
        seq = int(snap.sequence)
        seen = seq_seen.setdefault(session_id, set())
        if seq in seen:
            seq_diag.append(
                {
                    "kind": "duplicate_sequence",
                    "sequence": seq,
                    "session_id": session_id,
                }
            )
        seen.add(seq)
        previous_seq = seq_prev.get(session_id)
        if previous_seq is not None and seq != previous_seq + 1:
            seq_diag.append(
                {
                    "kind": "gap",
                    "expected": previous_seq + 1,
                    "got": seq,
                    "session_id": session_id,
                }
            )
        seq_prev[session_id] = seq
        trigger = str(snap.trigger)
        triggers[trigger] += 1
        if first_trigger is None:
            first_trigger = trigger
        else:
            later_triggers[trigger] += 1
        locale = str(snap.parser_locale or snap.ui_locale or snap.parser_mode or "unknown")
        locales[locale] += 1
        locale_sources[str(snap.locale_source or "unknown")] += 1
        document_langs[str(snap.document_lang or "")] += 1
        paths[str(snap.page_path or "")] += 1
        catalogs[str(snap.selector_catalog_version or "")] += 1
        schema_versions[int(snap.schema_version)] += 1
        if snap.sample_interval_ms is None:
            n_wrong_interval += 1
        else:
            intervals[int(snap.sample_interval_ms)] += 1
            if int(snap.sample_interval_ms) != REQUIRED_INTERVAL_MS:
                n_wrong_interval += 1
        route = _locale_route(snap)
        if route is None:
            n_locale_route_fail += 1
            locale_routes["inadmissible"] += 1
        else:
            locale_routes[route] += 1
        scale_ok_row, scale_reason = _scale_audit_ok(snap)
        if not scale_ok_row:
            n_scale_fail += 1
            if scale_fail_reason is None:
                scale_fail_reason = scale_reason
        header = snap.header_diagnostics or {}
        if header.get("ambiguity_reason"):
            n_ambiguous += 1
        for reason in snap.invalid_reasons:
            if "ambiguous" in str(reason):
                n_ambiguous += 1
        for name in STRATEGY_FIELDS:
            field = snap.fields.get(name)
            if field is not None and field.parse_status == "ambiguous":
                n_ambiguous += 1
        diag = snap.stage_diagnostics
        if not isinstance(diag, dict):
            n_missing_diag += 1
        else:
            ext = str(diag.get("extension_version") or "")
            if ext != EXTENSION_EXPECTED:
                n_wrong_extension += 1
            vis_states[str(diag.get("visibility_state") or "unknown")] += 1
            vis_extract[str(diag.get("visibility_state_at_extract") or "unknown")] += 1
            if diag.get("stale_generation"):
                n_stale += 1
            if diag.get("session_id_mismatch"):
                n_mismatch += 1
            epoch = diag.get("producer_epoch")
            if epoch:
                producer_epochs.add(str(epoch))
            worker = diag.get("worker_boot_id")
            if worker:
                worker_boots.add(str(worker))
        rec = observation_from_snapshot(snap)
        obs = rec.observation
        bid = _ok_price(snap.fields.get("bid"))
        ask = _ok_price(snap.fields.get("ask"))
        last = _ok_price(snap.fields.get("last"))
        mark = _ok_price(snap.fields.get("mark"))
        index = _ok_price(snap.fields.get("index"))
        if bid is not None and ask is not None and bid >= ask:
            n_bid_ge_ask += 1
        five_ok = (
            bid is not None
            and ask is not None
            and last is not None
            and mark is not None
            and index is not None
            and bid < ask
        )
        as_of_eligible = obs is not None
        ready = session_ready.get(session_id, False)
        if obs is None:
            if ready:
                n_data_invalid += 1
            else:
                n_startup_warmup += 1
        else:
            session_ready[session_id] = True
            if first_symbol is None:
                first_symbol = obs.symbol
            if obs.last is not None:
                lasts.append(obs.last)
                if not (TAO_LAST_MIN <= obs.last <= TAO_LAST_MAX):
                    scale_outliers += 1
            if five_ok:
                n_strategy_ready += 1
                if (
                    str(snap.parser_locale or "unknown") == "unknown"
                    or str(snap.locale_source or "unknown") == "unknown"
                ):
                    n_unknown_on_ready += 1
            line = json.dumps(
                {
                    "seq": snap.sequence,
                    "symbol": obs.symbol,
                    "bid": obs.bid,
                    "ask": obs.ask,
                    "last": obs.last,
                    "mark": obs.mark,
                    "index": obs.index,
                    "received_at": obs.received_at.isoformat(),
                },
                sort_keys=True,
            )
            if not replay_first:
                replay.update(b"\n")
            replay_first = False
            replay.update(line.encode("utf-8"))
        mark_rec = snap.fields.get("mark")
        index_rec = snap.fields.get("index")
        if mark_rec and mark_rec.selector_id:
            mark_selectors[str(mark_rec.selector_id)] += 1
        if index_rec and index_rec.selector_id:
            index_selectors[str(index_rec.selector_id)] += 1
        received_at = str(snap.received_at_local)
        stamp_ms = _stamp_ms(received_at)
        if stamp_ms is None:
            n_bad_stamp += 1
            continue
        if first_received is None:
            first_received = received_at
        last_received = received_at
        compact = CompactRow(
            received_ms=stamp_ms,
            received_at=received_at,
            sequence=seq,
            session_id=session_id,
            trigger=trigger,
            as_of_eligible=as_of_eligible,
            five_ok=five_ok,
            bid=bid,
            ask=ask,
            last=last,
            mark=mark,
            index=index,
        )
        if trigger == "interval":
            n_interval += 1
            five = _five_tuple(compact)
            if previous_five is not None and five == previous_five:
                n_unchanged_interval += 1
            previous_five = five
        else:
            previous_five = _five_tuple(compact)
        rows.append(compact)

    arrivals = [row.received_ms for row in rows]
    deltas = [arrivals[i] - arrivals[i - 1] for i in range(1, len(arrivals))]
    ordered = sorted(deltas)
    le_2000 = sum(1 for delta in deltas if delta <= 2000.0)
    n_gt_2000 = len(deltas) - le_2000
    n_gt_1000 = sum(1 for delta in deltas if delta > 1000.0)
    frac_le_2000 = le_2000 / len(deltas) if deltas else 0.0
    inter = {
        "n": len(ordered),
        "p50_ms": _percentile(ordered, 0.50),
        "p90_ms": _percentile(ordered, 0.90),
        "p95_ms": _percentile(ordered, 0.95),
        "p99_ms": _percentile(ordered, 0.99),
        "min_ms": ordered[0] if ordered else None,
        "max_ms": ordered[-1] if ordered else None,
    }
    p95 = inter.get("p95_ms")
    p95_ok = p95 is not None and float(p95) <= FROZEN_P95_MAX_MS
    frac_ok = bool(deltas) and frac_le_2000 >= FROZEN_INTERARRIVAL_LE_2000_MIN
    segments = split_usable_segments(rows)
    hours = usable_hours_from_segments(segments)
    wall_ms = (
        None if len(arrivals) < 2 else arrivals[-1] - arrivals[0]
    )
    grid = materialize_causal_grid(rows)
    n_grid = len(grid)
    n_grid_five = sum(1 for row in grid if row.get("five_ok"))
    grid_rate = n_grid_five / n_grid if n_grid else 0.0
    n_manual = int(triggers.get("manual") or 0)
    n_mutation = int(triggers.get("mutation") or 0)
    later_only_interval = (
        n_considered > 1
        and first_trigger == "manual"
        and set(later_triggers) <= {"interval"}
        and int(later_triggers.get("interval") or 0) == n_considered - 1
    )
    vis_ok = (
        n_considered > 0
        and n_missing_diag == 0
        and vis_states == {"visible": n_considered}
        and vis_extract == {"visible": n_considered}
    )
    session_ext_ok = bool(session_extensions) and all(
        value == EXTENSION_EXPECTED for value in session_extensions
    )
    extension_ok = (
        n_considered > 0
        and n_missing_diag == 0
        and n_wrong_extension == 0
        and session_ext_ok
    )
    catalog_ok = catalogs.get(REQUIRED_CATALOG, 0) == n_considered and n_considered > 0
    schema_ok = (
        n_considered > 0
        and n_schema_mismatch == 0
        and schema_versions.get(SCHEMA_VERSION, 0) == n_considered
    )
    interval_ok = (
        n_considered > 0
        and n_wrong_interval == 0
        and intervals.get(REQUIRED_INTERVAL_MS, 0) == n_considered
        and all(value == REQUIRED_INTERVAL_MS for value in session_intervals)
    )
    seq_ok = (
        not seq_diag
        and n_bad_stamp == 0
        and session_chunk_mismatch == 0
        and all(
            seq_prev.get(session_id) == len(seen) and set(range(1, len(seen) + 1)) == seen
            for session_id, seen in seq_seen.items()
        )
        and bool(seq_seen)
    )
    mark_struct = sum(
        count for key, count in mark_selectors.items() if key.startswith("header_struct:mark")
    )
    index_struct = sum(
        count for key, count in index_selectors.items() if key.startswith("header_struct:index")
    )
    swapped = any("index" in key for key in mark_selectors) or any(
        key.startswith("header_struct:mark") for key in index_selectors
    )
    first_dt = None
    if first_received is not None:
        first_dt = datetime.fromisoformat(first_received.replace("Z", "+00:00"))
    designer_ok = first_dt is not None and first_dt > PROTOCOL_V2_COMMIT_UTC
    new_capture_ok = (
        digest not in EXCLUDED_CORPUS_SHA256
        and not _excluded_path(path)
        and n_considered > 0
    )
    enqueued = counters.get("callbacks_enqueued") or {}
    persisted = counters.get("snapshots_persisted") or {}
    enqueued_mut = _nested_count(enqueued, "mutation")
    persisted_mut = _nested_count(persisted, "mutation")
    mutation_not_enqueued = n_mutation == 0 and (enqueued_mut in {None, 0}) and (
        persisted_mut in {None, 0}
    )
    expected = counters.get("expected_interval_opportunities")
    timer_mono = None
    stop_mono = None
    for event in lifecycle:
        kind = event.get("kind")
        mono = finite_number(event.get("mono"))
        if kind == "timer_registered" and mono is not None:
            timer_mono = mono
        if kind == "session_stop" and mono is not None:
            stop_mono = mono
    if expected is None:
        expected = expected_interval_opportunities(
            timer_mono, stop_mono, REQUIRED_INTERVAL_MS
        )
    timer_cb = int(counters.get("timer_callbacks") or 0)
    persisted_interval = _nested_count(persisted, "interval")
    if persisted_interval is None:
        persisted_interval = n_interval
    heartbeat_ok = n_interval > 0 and n_unchanged_interval > 0
    # Frozen heartbeat audit is persist-of-timer, not reconstructed elapsed slots.
    # expected_interval_opportunities is (stop-register)//500 and can differ by a
    # couple of fence ticks; interarrival bars already judge missing ticks.
    heartbeat_persisted_ok = True
    if counters:
        heartbeat_persisted_ok = timer_cb > 0 and persisted_interval == timer_cb
    lifecycle_kinds = Counter(str(event.get("kind")) for event in lifecycle)
    unexplained = (
        n_stale > 0
        or n_mismatch > 0
        or int(counters.get("stale_generation_detected") or 0) > 0
        or int(counters.get("session_id_mismatch_detected") or 0) > 0
        or len(producer_epochs) > 1
        or len(worker_boots) > 1
        or len(session_generations) > 1
        or int(lifecycle_kinds.get("worker_boot") or 0) > 1
        or len(session_ids) > 1
    )
    median_last = None
    if lasts:
        ordered_last = sorted(lasts)
        median_last = ordered_last[len(ordered_last) // 2]
    replay_sha = None if replay_first else replay.hexdigest()
    visible_hidden = _visible_to_hidden_count(lifecycle)
    lifecycle_present = bool(lifecycle) or bool(counters)
    gates = {
        "designer_did_not_inspect_before_v2_commit": designer_ok,
        "new_corrected_long_capture_not_excluded": new_capture_ok,
        "schema_mexc_ui_raw_snapshot_v1": schema_ok,
        "catalog_v1_2": catalog_ok,
        "configured_interval_500_ms": interval_ok,
        "extension_1_3_5": extension_ok,
        "heartbeat_unchanged_interval_commits": heartbeat_ok,
        "heartbeat_persisted_equals_timer_callbacks": heartbeat_persisted_ok,
        "sequence_chunk_continuity": seq_ok,
        "no_storage_errors": not storage_errors,
        "usable_hours_ge_8": hours >= REQUIRED_USABLE_HOURS,
        "locale_v2_provenance": n_considered > 0 and n_locale_route_fail == 0,
        "no_unknown_locale_on_strategy_ready": n_unknown_on_ready == 0,
        "symbol_taousdt": first_symbol == "TAOUSDT",
        "exactly_one_manual_start_raw_row": n_manual == 1 and first_trigger == "manual",
        "remaining_raw_rows_interval": later_only_interval
        and int(triggers.get("interval") or 0) == n_considered - 1,
        "zero_mutation_raw_rows": n_mutation == 0 and mutation_not_enqueued,
        "visibility_lifecycle_evidence": lifecycle_present and n_missing_diag == 0,
        "operator_intended_continuously_visible": vis_ok and visible_hidden == 0,
        "no_unexplained_producer_session_worker_transitions": (
            n_considered > 0 and not unexplained
        ),
        "grid_simultaneous_coverage_ge_95pct": n_grid > 0
        and grid_rate >= FROZEN_SIMULTANEOUS_MIN,
        "frozen_interarrival_p95": p95_ok,
        "frozen_interarrival_le_2000": frac_ok,
        "raw_text_tokens_match_locale_scale": n_scale_fail == 0 and n_considered > 0,
        "absolute_price_scale": (
            median_last is not None
            and TAO_LAST_MIN <= median_last <= TAO_LAST_MAX
            and scale_outliers == 0
        ),
        "bid_lt_ask": n_bid_ge_ask == 0 and n_considered > 0,
        "mark_index_not_swapped": mark_struct > 0 and index_struct > 0 and not swapped,
        "no_sustained_selector_ambiguity": n_ambiguous == 0,
        "no_post_readiness_data_invalid": n_data_invalid == 0,
        "export_replay_deterministic": replay_sha is not None,
    }
    if path.name == LOCKED_FILENAME:
        gates["corpus_sha256_matches_locked_manifest"] = digest == LOCKED_CORPUS_SHA256
        gates["corpus_byte_count_matches_locked_manifest"] = byte_count == LOCKED_BYTE_COUNT
    failure_classes = [name for name, ok in gates.items() if not ok]
    passed = not failure_classes
    grid_sha = hash_grid_rows(grid) if passed else None
    return {
        "protocol_version_scored": PROTOCOL_VERSION,
        "protocol_version_not_scored": PROTOCOL_V1_VERSION,
        "path": _report_path(path),
        "filename": path.name,
        "sha256": digest,
        "byte_count": byte_count,
        "n_snapshots": n_considered,
        "n_grid_rows": n_grid,
        "n_grid_five_ok": n_grid_five,
        "grid_simultaneous_rate": grid_rate,
        "grid_simultaneous_pct": round(grid_rate * 100.0, 4),
        "grid_sha256": grid_sha,
        "grid_materialized": passed,
        "usable_hours": hours,
        "n_usable_segments": len(segments),
        "wall_duration_ms": wall_ms,
        "wall_duration_hours": None if wall_ms is None else wall_ms / 3_600_000.0,
        "page_paths": dict(paths),
        "locales": dict(locales),
        "locale_sources": dict(locale_sources),
        "document_langs": dict(document_langs),
        "locale_routes": dict(locale_routes),
        "n_locale_route_fail": n_locale_route_fail,
        "catalog_versions": dict(catalogs),
        "schema_versions": {str(key): val for key, val in sorted(schema_versions.items())},
        "sample_interval_ms": {str(key): val for key, val in sorted(intervals.items())},
        "session_interval_ms": session_intervals,
        "median_last": median_last,
        "n_strategy_ready": n_strategy_ready,
        "n_startup_warmup": n_startup_warmup,
        "n_data_invalid": n_data_invalid,
        "n_bid_ge_ask": n_bid_ge_ask,
        "n_scale_audit_fail": n_scale_fail,
        "scale_audit_first_reason": scale_fail_reason,
        "n_selector_ambiguity": n_ambiguous,
        "mark_selectors": dict(mark_selectors),
        "index_selectors": dict(index_selectors),
        "replay_canonical_sha256": replay_sha,
        "storage_errors": storage_errors,
        "sessions": scan_sessions,
        "sequence_diagnostics": seq_diag,
        "trigger_counts": dict(triggers),
        "first_trigger": first_trigger,
        "later_trigger_counts": dict(later_triggers),
        "n_interval_triggers": n_interval,
        "n_unchanged_interval_commits": n_unchanged_interval,
        "n_missing_stage_diagnostics": n_missing_diag,
        "n_wrong_extension": n_wrong_extension,
        "session_extension_versions": session_extensions,
        "visibility_states": dict(vis_states),
        "visibility_states_at_extract": dict(vis_extract),
        "visible_to_hidden_transitions": visible_hidden,
        "n_stale_generation": n_stale,
        "n_session_id_mismatch": n_mismatch,
        "producer_epochs": sorted(producer_epochs),
        "session_generations": sorted(session_generations),
        "worker_boot_ids": sorted(worker_boots),
        "lifecycle_kinds": dict(lifecycle_kinds),
        "expected_interval_opportunities": int(expected or 0),
        "timer_callbacks": timer_cb,
        "mutation_callbacks": int(counters.get("mutation_callbacks") or 0),
        "callbacks_enqueued": dict(enqueued) if isinstance(enqueued, dict) else {},
        "snapshots_persisted": dict(persisted) if isinstance(persisted, dict) else {},
        "content_queue_depth_high_water": high_water.get("content_queue_depth"),
        "capture_ids": sorted(capture_ids),
        "session_ids": sorted(session_ids),
        "utc_start": first_received,
        "utc_end": last_received,
        "first_symbol": first_symbol,
        "interarrival_ms": inter,
        "interarrival_frac_le_2000_ms": frac_le_2000,
        "interarrival_n_gt_2000_ms": n_gt_2000,
        "interarrival_n_gt_1000_ms": n_gt_1000,
        "hours_requirement": {
            "protocol_v2_item": 6,
            "required_usable_hours": REQUIRED_USABLE_HOURS,
            "applied": True,
            "usable_hours": hours,
        },
        "gates": gates,
        "failure_classes": failure_classes,
        "passed": passed,
        "whole_session_scored": True,
        "bad_periods_cropped": False,
        "mom_gap_inspected": False,
        "n_cells_executed": 0,
        "_grid": grid,
    }


def build_milestone_payload(
    raw: Path, *, grid_out: Path | None = None
) -> dict[str, Any]:
    scored = score_long_corpus_admissibility(raw)
    grid = scored.pop("_grid")
    status = STATUS_ADMISSIBLE if scored["passed"] else STATUS_INADEQUATE
    grid_written = None
    if scored["passed"] and grid_out is not None:
        written_sha = write_grid_ndjson(grid, grid_out)
        if written_sha != scored["grid_sha256"]:
            raise RuntimeError("grid SHA-256 changed while writing NDJSON")
        grid_written = _report_path(grid_out)
    return {
        "milestone": MILESTONE,
        "status": status,
        "decision": MILESTONE_DECISION,
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "mom_gap_inspected": False,
        "n_cells_executed": 0,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "extension_expected": EXTENSION_EXPECTED,
        "catalog_required": REQUIRED_CATALOG,
        "protocol": {
            "version_scored": PROTOCOL_VERSION,
            "version_not_scored": PROTOCOL_V1_VERSION,
            "amendment_doc": (
                "docs/mexc_mom_gap_hypothesis_protocol_v2_data_contract_amendment.md"
            ),
            "v1_design_doc": "docs/mexc_mom_gap_hypothesis_protocol_design_v1.md",
            "v1_blob": "4e8d05940b2bc759acff881779159a7bcba1604e",
            "v2_merge_commit": PROTOCOL_V2_MERGE_COMMIT,
            "v2_amendment_commit": PROTOCOL_V2_AMENDMENT_COMMIT,
        },
        "frozen_protocol_v2": {
            "simultaneous_min": FROZEN_SIMULTANEOUS_MIN,
            "interarrival_le_2000_min": FROZEN_INTERARRIVAL_LE_2000_MIN,
            "p95_max_ms": FROZEN_P95_MAX_MS,
            "usable_hours_min": REQUIRED_USABLE_HOURS,
            "grid_ms": GRID_MS,
            "as_of_max_ms": ASOF_MAX_MS,
            "gap_reset_ms": GAP_RESET_MS,
            "thresholds_relaxed": False,
        },
        "operator_condition": "intended continuously-visible Chrome session",
        "grid_written": grid_written,
        "long_validation": scored,
        "notes": [
            "Scored against protocol v2.0.0 data admissibility, not v1.0.0.",
            "Whole session scored; no hidden-phase crop and no bad-period crop.",
            "Did not inspect mom/gap formulas or execute any of the 21 cells.",
            "Did not retune frozen profiles, thresholds, exits, or lookbacks.",
            "Grid SHA-256 is recorded only when every input gate passes.",
        ],
    }


def _gate_cell(ok: bool | None) -> str:
    if ok is True:
        return "PASS"
    if ok is False:
        return "FAIL"
    return "N/A"


def render_markdown(payload: dict[str, Any]) -> str:
    scored = payload["long_validation"]
    inter = scored.get("interarrival_ms") or {}
    hours = scored.get("hours_requirement") or {}
    lines = [
        "# MEXC TAO corrected long-corpus admissibility v1",
        "",
        f"STATUS: `{payload['status']}`",
        "",
        f"DECISION: `{payload['decision']}`",
        "",
        "ML_STATUS: `NOT_STARTED`",
        "",
        "PAPER: **false**",
        "",
        "LIVE: **false**",
        "",
        "STRATEGY_TUNING: **false**",
        "",
        "MOM/GAP: **not inspected** (0 of 21 cells executed)",
        "",
        "## Purpose",
        "",
        "Score the first corrected long TAOUSDT corpus after the accepted",
        "extension-1.3.5 visible-path gate against frozen protocol **v2.0.0**.",
        "Bad periods are not cropped. The 21 identification cells are not run.",
        "",
        "## Locked inputs",
        "",
        f"- protocol v2 merge commit: `{payload['protocol']['v2_merge_commit']}`",
        f"- protocol v2 amendment commit: `{payload['protocol']['v2_amendment_commit']}`",
        f"- corpus path: `{scored.get('path')}`",
        f"- corpus sha256: `{scored.get('sha256')}`",
        f"- byte count: {scored.get('byte_count')}",
        f"- extension: `{payload.get('extension_expected')}`",
        f"- catalog: `{payload.get('catalog_required')}`",
        f"- operator condition: {payload.get('operator_condition')}",
        "",
        "## Capture",
        "",
        f"- snapshots: {scored.get('n_snapshots')}",
        f"- utc_start: `{scored.get('utc_start')}`",
        f"- utc_end: `{scored.get('utc_end')}`",
        f"- usable hours: {scored.get('usable_hours')}",
        f"- wall hours: {scored.get('wall_duration_hours')}",
        f"- usable segments: {scored.get('n_usable_segments')}",
        f"- page_paths: `{scored.get('page_paths')}`",
        f"- parser_locale: `{scored.get('locales')}`",
        f"- locale_source: `{scored.get('locale_sources')}`",
        f"- document_lang: `{scored.get('document_langs')}`",
        f"- locale routes: `{scored.get('locale_routes')}`",
        f"- trigger_counts: `{scored.get('trigger_counts')}`",
        f"- first_trigger: `{scored.get('first_trigger')}`",
        (
            "- heartbeat unchanged interval commits: "
            f"{scored.get('n_unchanged_interval_commits')}"
        ),
        (
            "- expected/timer/persisted interval: "
            f"{scored.get('expected_interval_opportunities')} / "
            f"{scored.get('timer_callbacks')} / "
            f"{(scored.get('snapshots_persisted') or {}).get('interval')}"
        ),
        f"- visibility_states: `{scored.get('visibility_states')}`",
        (
            "- visible→hidden transitions: "
            f"{scored.get('visible_to_hidden_transitions')}"
        ),
        f"- producer_epochs: `{scored.get('producer_epochs')}`",
        f"- worker_boot_ids: `{scored.get('worker_boot_ids')}`",
        (
            f"- grid rows / five-ok / rate: {scored.get('n_grid_rows')} / "
            f"{scored.get('n_grid_five_ok')} / {scored.get('grid_simultaneous_pct')}%"
        ),
        f"- grid sha256: `{scored.get('grid_sha256')}`",
        f"- grid written: `{payload.get('grid_written')}`",
        f"- DATA_INVALID after ready: {scored.get('n_data_invalid')}",
        f"- selector ambiguity count: {scored.get('n_selector_ambiguity')}",
        f"- median last: {scored.get('median_last')}",
        (
            "- raw interarrival p50/p90/p95/p99/max: "
            f"{inter.get('p50_ms')} / {inter.get('p90_ms')} / "
            f"{inter.get('p95_ms')} / {inter.get('p99_ms')} / {inter.get('max_ms')}"
        ),
        (
            "- frac≤2000ms / n>2000ms / n>1000ms: "
            f"{scored.get('interarrival_frac_le_2000_ms')} / "
            f"{scored.get('interarrival_n_gt_2000_ms')} / "
            f"{scored.get('interarrival_n_gt_1000_ms')}"
        ),
        f"- replay_canonical_sha256: `{scored.get('replay_canonical_sha256')}`",
        f"- passed: **{scored['passed']}**",
        "",
        "| Gate | Result |",
        "| --- | --- |",
    ]
    for name, ok in scored["gates"].items():
        lines.append(f"| `{name}` | {_gate_cell(ok)} |")
    lines.extend(["", "### Failure classes", ""])
    if scored["failure_classes"]:
        for name in scored["failure_classes"]:
            lines.append(f"- `{name}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Findings", ""])
    if scored["passed"]:
        lines.append(
            "Every frozen protocol-v2 long-corpus input gate passed. The 500 ms "
            "causal grid was materialized once and hashed. The 21 cells were "
            "not evaluated."
        )
        n_gt = scored.get("interarrival_n_gt_2000_ms")
        if n_gt:
            lines.extend(
                [
                    "",
                    (
                        f"One or more raw gaps exceeded 2,000 ms (n={n_gt}, "
                        f"max={inter.get('max_ms')} ms). Those holes were excluded "
                        "from usable hours by the frozen 2,000 ms reset; they were "
                        "not cropped to manufacture other bars. The 99% ≤2,000 ms "
                        "interarrival bar still passed on the whole session."
                    ),
                ]
            )
    else:
        lines.append(
            "STATUS is **DATA_INADEQUATE**. Failure classes are listed above "
            "and were not relaxed. Bad periods were not cropped. The 21 cells "
            "were not run and the identification grid SHA was not recorded."
        )
    lines.extend(
        [
            "",
            "## Hours and grid",
            "",
            f"- required usable hours: {hours.get('required_usable_hours')}",
            f"- usable hours after gap exclusions: {hours.get('usable_hours')}",
            f"- 500 ms grid rows: {scored.get('n_grid_rows')}",
            f"- as-of bound: {payload['frozen_protocol_v2']['as_of_max_ms']} ms",
            f"- gap reset: {payload['frozen_protocol_v2']['gap_reset_ms']} ms",
            "",
            "## Decision",
            "",
            "**STOP_FOR_LEAD_REVIEW.** Do not execute the 21 cells. Do not retune "
            "mom/gap. No PnL, ML, PAPER, or LIVE.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def update_execution_manifest(payload: dict[str, Any], manifest_path: Path) -> None:
    """Bind scoring results onto the already-committed execution manifest."""

    locked = json.loads(manifest_path.read_text(encoding="utf-8"))
    scored = payload["long_validation"]
    locked_sha = str((locked.get("corpus") or {}).get("sha256") or "")
    if scored.get("sha256") != locked_sha:
        raise RuntimeError("refusing to update manifest: corpus SHA-256 mismatch")
    locked["manifest_status"] = "ADMISSIBILITY_SCORED"
    locked["quality_status"] = payload["status"]
    locked["decision"] = payload["decision"]
    locked["mom_gap_inspected"] = False
    locked["n_cells_executed"] = 0
    locked["numeric_scale_audit"] = (
        "PASS" if scored["gates"].get("raw_text_tokens_match_locale_scale") else "FAIL"
    )
    locked["grid"] = {
        **dict(locked.get("grid") or {}),
        "materialized": bool(scored.get("grid_materialized")),
        "sha256": scored.get("grid_sha256"),
        "n_rows": scored.get("n_grid_rows"),
        "n_five_ok": scored.get("n_grid_five_ok"),
        "simultaneous_pct": scored.get("grid_simultaneous_pct"),
        "written": payload.get("grid_written"),
        "reason": (
            "hashed_after_all_input_gates_passed"
            if scored["passed"]
            else "not_recorded_because_input_gate_failed"
        ),
    }
    locked["admissibility"] = {
        "passed": scored["passed"],
        "usable_hours": scored.get("usable_hours"),
        "failure_classes": scored.get("failure_classes"),
        "replay_canonical_sha256": scored.get("replay_canonical_sha256"),
        "whole_session_scored": True,
        "bad_periods_cropped": False,
    }
    note = "Admissibility scored after the locked manifest commit. Still 0 of 21 cells."
    notes = list(locked.get("notes") or [])
    if note not in notes:
        notes.append(note)
    locked["notes"] = notes
    manifest_path.write_text(
        json.dumps(locked, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def write_reports(
    *,
    raw: Path,
    out_json: Path,
    out_md: Path,
    grid_out: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    payload = build_milestone_payload(raw, grid_out=grid_out)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    out_md.write_text(render_markdown(payload), encoding="utf-8")
    if manifest_path is not None:
        update_execution_manifest(payload, manifest_path)
    return payload
