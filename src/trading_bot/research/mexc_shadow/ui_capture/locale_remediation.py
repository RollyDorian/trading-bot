"""Score locale-semantics remediation. Does not rewrite captures or retune profiles."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.ui_capture.long_observation import (
    scan_long_capture,
)
from trading_bot.research.mexc_shadow.ui_capture.normalize import (
    observation_from_snapshot,
    snapshot_from_mapping,
)
from trading_bot.research.mexc_shadow.ui_capture.quality import summarize_capture
from trading_bot.research.mexc_shadow.ui_capture.replay import replay_capture_smoke
from trading_bot.research.mexc_shadow.ui_capture.store import iter_all_mappings

# The 11.67h ru-RU export. Original wrapper decimals were not stored; do not /100.
HISTORICAL_CORPUS_SHA256 = (
    "7a41c34d4ae855850cd8a1a47e438e940c38e09d6f5a555c3f397e5650da9c2a"
)
HISTORICAL_CORPUS_NAME = "mexc_ui_capture_sessions_2026-09-03T04-12-21-619Z.ndjson"
SHORT_MIN_MS = 5 * 60 * 1000
SHORT_MAX_MS = 15 * 60 * 1000
# TAOUSDT visible prices are ~200. The comma-strip bug produced ~21800.
TAO_LAST_MIN = 50.0
TAO_LAST_MAX = 800.0


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def historical_corpus_record(path: Path | None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "role": "CAPTURE_INFRASTRUCTURE_EVIDENCE",
        "name": HISTORICAL_CORPUS_NAME,
        "sha256": HISTORICAL_CORPUS_SHA256,
        "rewrite": False,
        "rescale": False,
        "reason": (
            "mark/index were never captured and wrapper raw_text was String(parsed) "
            "after unconditional comma stripping. Heuristic /100 cannot restore the "
            "original DOM decimal representation."
        ),
        "present": False,
        "sha256_match": None,
    }
    if path is None or not path.is_file():
        return record
    digest = sha256_file(path)
    record["present"] = True
    record["path"] = str(path)
    record["bytes"] = path.stat().st_size
    record["sha256_observed"] = digest
    record["sha256_match"] = digest == HISTORICAL_CORPUS_SHA256
    return record


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def score_short_capture(path: Path) -> dict[str, Any]:
    """Score a 5–15 minute unpacked-extension export. Never retunes mom/gap."""

    quality = summarize_capture(path)
    scan = scan_long_capture(path)
    replay = replay_capture_smoke(path, "author_observed_v0", hypothesis_smoke=True)
    lasts: list[float] = []
    bids: list[float] = []
    asks: list[float] = []
    marks: list[float] = []
    indexes: list[float] = []
    locales: dict[str, int] = {}
    mark_selectors: dict[str, int] = {}
    index_selectors: dict[str, int] = {}
    raw_text_has_comma = 0
    wrapper_raw_is_stringified = 0
    n_strategy_ready = 0
    n_considered = 0
    for payload in iter_all_mappings(path):
        if payload.get("schema") != "mexc_ui_raw_snapshot":
            continue
        snap = snapshot_from_mapping(payload)
        n_considered += 1
        locale = str(snap.ui_locale or snap.parser_mode or "unknown")
        locales[locale] = locales.get(locale, 0) + 1
        rec = observation_from_snapshot(snap)
        obs = rec.observation
        if obs is None:
            continue
        if obs.last is not None:
            lasts.append(obs.last)
        bids.append(obs.bid)
        asks.append(obs.ask)
        if obs.mark is not None:
            marks.append(obs.mark)
        if obs.index is not None:
            indexes.append(obs.index)
        mark_rec = snap.fields.get("mark")
        index_rec = snap.fields.get("index")
        bid_rec = snap.fields.get("bid")
        if mark_rec and mark_rec.selector_id:
            mark_selectors[str(mark_rec.selector_id)] = (
                mark_selectors.get(str(mark_rec.selector_id), 0) + 1
            )
        if index_rec and index_rec.selector_id:
            index_selectors[str(index_rec.selector_id)] = (
                index_selectors.get(str(index_rec.selector_id), 0) + 1
            )
        if bid_rec and bid_rec.raw_text and "," in bid_rec.raw_text:
            raw_text_has_comma += 1
        if (
            bid_rec
            and bid_rec.raw_text is not None
            and bid_rec.value is not None
            and bid_rec.raw_text == str(bid_rec.value)
            and "," not in bid_rec.raw_text
        ):
            wrapper_raw_is_stringified += 1
        ready = all(
            (
                obs.symbol,
                obs.bid,
                obs.ask,
                obs.last,
                obs.mark,
                obs.index,
            )
        )
        if ready:
            n_strategy_ready += 1
    duration_ms = quality.duration_ms
    median_last = _median(lasts)
    scale_ok = (
        median_last is not None and TAO_LAST_MIN <= median_last <= TAO_LAST_MAX
    )
    duration_ok = (
        duration_ms is not None and SHORT_MIN_MS <= float(duration_ms) <= SHORT_MAX_MS
    )
    locale_ok = locales.get("ru-RU", 0) == n_considered and n_considered > 0
    crossed = quality.n_bid_ge_ask
    data_invalid = int(scan.get("n_data_invalid") or 0)
    ambiguity = 0
    for reason, count in (quality.invalid_reasons or {}).items():
        if "ambiguous" in str(reason):
            ambiguity += int(count)
    mark_struct = sum(
        count
        for key, count in mark_selectors.items()
        if key.startswith("header_struct:mark")
    )
    index_struct = sum(
        count
        for key, count in index_selectors.items()
        if key.startswith("header_struct:index")
    )
    swapped = False
    if marks and indexes and median_last is not None:
        # Titles swapped would still be near last; require distinct selector ids.
        swapped = mark_struct > 0 and index_struct > 0 and mark_selectors == index_selectors
    simultaneous = quality.n_simultaneous_bid_ask_last_mark_index
    simultaneous_rate = simultaneous / n_considered if n_considered else 0.0
    gates = {
        "duration_5_to_15_min": duration_ok,
        "absolute_price_scale": scale_ok,
        "bid_lt_ask": crossed == 0,
        "symbol_taousdt": False,
        "mark_index_present": n_strategy_ready > 0 and simultaneous_rate >= 0.8,
        "mark_index_not_swapped_selectors": mark_struct > 0 and index_struct > 0 and not swapped,
        "no_selector_ambiguity_burst": ambiguity == 0,
        "no_post_readiness_data_invalid": data_invalid == 0,
        "sequence_storage_ok": not quality.sequence_diagnostics,
        "export_replay_deterministic": quality.replay_determinism_sha256 is not None,
        "ru_RU_parser_mode": locale_ok,
        "wrapper_raw_text_not_stringified": wrapper_raw_is_stringified == 0,
    }
    # Symbol check from first valid observation in scan quality path.
    symbol_ok = False
    for payload in iter_all_mappings(path):
        if payload.get("schema") != "mexc_ui_raw_snapshot":
            continue
        rec = observation_from_snapshot(snapshot_from_mapping(payload))
        if rec.observation is not None:
            symbol_ok = rec.observation.symbol == "TAOUSDT"
            break
    gates["symbol_taousdt"] = symbol_ok
    passed = all(gates.values())
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "n_snapshots": n_considered,
        "duration_ms": duration_ms,
        "duration_hours": None if duration_ms is None else round(float(duration_ms) / 3_600_000, 4),
        "locales": locales,
        "median_last": median_last,
        "median_bid": _median(bids),
        "median_ask": _median(asks),
        "median_mark": _median(marks),
        "median_index": _median(indexes),
        "n_strategy_ready": n_strategy_ready,
        "strategy_ready_rate": n_strategy_ready / n_considered if n_considered else 0.0,
        "simultaneous_bid_ask_last_mark_index": simultaneous,
        "simultaneous_rate": simultaneous_rate,
        "n_data_invalid": data_invalid,
        "n_startup_warmup": scan.get("n_startup_warmup"),
        "n_ready_valid": scan.get("n_ready_valid"),
        "n_bid_ge_ask": crossed,
        "raw_text_has_comma": raw_text_has_comma,
        "wrapper_raw_is_stringified": wrapper_raw_is_stringified,
        "mark_selectors": mark_selectors,
        "index_selectors": index_selectors,
        "replay_canonical_sha256": quality.replay_determinism_sha256,
        "hypothesis_smoke": {
            "observations": replay.observations,
            "n_candidates": len(replay.candidates),
            "n_trades": len(replay.trades),
            "not_strategy_evidence": True,
        },
        "gates": gates,
        "passed": passed,
        "quality": {
            "n_raw": quality.n_raw,
            "n_valid_for_replay": quality.n_valid_for_replay,
            "n_invalid": quality.n_invalid,
            "invalid_reasons": quality.invalid_reasons,
            "interarrival_ms": quality.interarrival_ms,
            "timing_adequacy": quality.timing_adequacy,
            "n_sessions": quality.n_sessions,
            "n_chunks_total": quality.n_chunks_total,
        },
    }


def build_milestone_payload(
    *,
    short_raw: Path | None,
    historical_raw: Path | None,
) -> dict[str, Any]:
    historical = historical_corpus_record(historical_raw)
    short: dict[str, Any] | None = None
    if short_raw is not None and short_raw.is_file():
        short = score_short_capture(short_raw)
    short_passed = bool(short and short.get("passed"))
    if short is None:
        status = "MEXC_UI_LOCALE_DATA_SEMANTICS_REMEDIATION_IMPLEMENTATION_READY"
        decision = "GATE_PENDING_OPERATOR_CAPTURE"
    elif short_passed:
        status = "MEXC_UI_LOCALE_DATA_SEMANTICS_REMEDIATION_READY"
        decision = "STOP_FOR_LEAD_REVIEW"
    else:
        status = "MEXC_UI_LOCALE_DATA_SEMANTICS_REMEDIATION_SHORT_GATE_FAIL"
        decision = "STOP_FOR_LEAD_REVIEW"
    return {
        "milestone": "MEXC_UI_LOCALE_DATA_SEMANTICS_REMEDIATION_V1",
        "status": status,
        "decision": decision,
        "ml_status": "NOT_STARTED",
        "paper": False,
        "live": False,
        "strategy_tuning": False,
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "extension_version": "1.3.0",
        "catalog_version": "v1.1",
        "verified_header_aliases": {
            "en": {
                "mark": ["Fair Price", "Mark Price"],
                "index": ["Index Price"],
                "funding": ["Funding Rate / Countdown", "Funding Rate"],
                "order_book": ["Order Book"],
            },
            "ru-RU": {
                "mark": ["Справедливая цена"],
                "index": ["Индексная цена"],
                "funding": ["Ставка финансирования/Обратный отсчет"],
                "order_book": ["Книга ордеров"],
                "verified_on": "https://www.mexc.com/ru-RU/futures/TAO_USDT",
                "verified_at": "2026-09-03",
            },
        },
        "historical_corpus": historical,
        "short_validation": short,
        "selector_verification": {
            "page": "https://www.mexc.com/ru-RU/futures/TAO_USDT",
            "when": "2026-09-03",
            "login": False,
            "role": "SELECTOR_ALIAS_AND_STRUCTURE_CHECK",
            "not_the_operator_gate": True,
            "header_structure": {
                "root_class_contains": "contractDetail",
                "item_class_contains": "commonItem",
                "title_class_contains": "itemTitle",
                "value_class_contains": "itemContent",
                "last_class_contains": "lastPrice",
                "bbo": "asksWrapper/sell + bidsWrapper/buy",
            },
            "observed_decimal": "comma",
            "observed_titles": {
                "index": "Индексная цена",
                "mark": "Справедливая цена",
                "funding": "Ставка финансирования/Обратный отсчет",
                "order_book": "Книга ордеров",
            },
        },
        "notes": [
            "Frozen author_observed_v0 / conservative_v0 were not retuned.",
            "Historical 11.67h NDJSON must not be rewritten or rescaled by /100.",
            "Required strategy-ready fields are bid, ask, last, mark/fair, index, symbol.",
            "Funding is desirable but not blocking.",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    historical = payload["historical_corpus"]
    short = payload.get("short_validation")
    lines = [
        "# MEXC UI locale data semantics remediation v1",
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
        "STRATEGY_TUNING: **false** (frozen `author_observed_v0` / `conservative_v0`)",
        "",
        "Prior milestone: `docs/mexc_tao_long_observation_v1.md`",
        "",
        "## Purpose",
        "",
        "Make read-only capture semantically correct on logged-in",
        "`/ru-RU/futures/...` before another long strategy corpus.",
        "No mom/gap/exit/threshold tuning, ML, PAPER, or live execution.",
        "",
        "## What changed",
        "",
        "Extension **1.3.0**, selector catalog **v1.1**.",
        "",
        "1. Locale-aware `parseNumber` / `parsePrice` from pathname",
        "   (`ru-RU`, `en-US`, otherwise `unknown`). Unknown mode fails closed",
        "   on commas instead of stripping them.",
        "2. Wrapper bid/ask keep exact bounded DOM `raw_text` plus `raw_tokens`,",
        "   `parser_locale`, and selector. `String(parsed_value)` is gone.",
        "   Adjacent digit-only nested spans fail closed.",
        "3. Capture-time symbol from `/futures/TAO_USDT` and",
        "   `/xx-XX/futures/TAO_USDT` only.",
        "4. Fair / Index / Funding prefer `contractDetail` + `commonItem` +",
        "   `itemTitle` / `itemContent`. Locale labels were copied from the",
        "   live rendered header, not guessed.",
        "",
        "### Parser rules",
        "",
        "| Mode | Path | Decimal | Grouping | Fail closed |",
        "| --- | --- | --- | --- | --- |",
        "| `ru-RU` | `/ru-RU/futures/...` | comma | space/NBSP or 3-digit periods |"
        " `218.11` |",
        "| `en-US` | `/en-US/futures/...` | point | 3-digit commas | `218,11` |",
        "| `unknown` | `/futures/...` or other locales | point if unambiguous |"
        " spaces only | any comma; `1.234` |",
        "",
        "Examples: ru-RU `218,11` → 218.11; en-US `218.11` → 218.11;",
        "`1,234.56` en-US → 1234.56; unknown `218,11` → parse failure.",
        "Original raw text is preserved. Prices never come from `%` fields.",
        "",
        "### Header structure (verified 2026-09-03 on public ru-RU TAOUSDT)",
        "",
        "Logged-out market header only. This is **not** the 5–15 min operator gate.",
        "",
        "- Bounded root: CSS module class contains `contractDetail` and `commonItem`",
        "- Exclude `lastPriceWrapper` and `rateItem` (24h change percent)",
        "- Title `itemTitle` / value `itemContent`",
        "- Last: `lastPrice` excluding `lastPriceWrapper`",
        "- BBO: `asksWrapper`/`sell` and `bidsWrapper`/`buy` (same as 1.2.x)",
        "- Verified titles: Индексная цена, Справедливая цена,",
        "  Ставка финансирования/Обратный отсчет, Книга ордеров",
        "- Order-book and last prices on that page used a decimal comma in one",
        "  text node (`‎218,11`), which the old parser stored as `21811`",
        "",
        "Strategy-ready fields: bid, ask, last, mark/fair, index, symbol.",
        "Funding is captured when present and is not blocking.",
        "",
        "## Historical 11.67h corpus",
        "",
        f"- role: `{historical['role']}`",
        f"- name: `{historical['name']}`",
        f"- sha256: `{historical['sha256']}`",
        f"- rewrite: **{historical['rewrite']}**",
        f"- rescale /100: **{historical['rescale']}**",
        f"- present: {historical.get('present')}",
        f"- sha256_match: {historical.get('sha256_match')}",
        "",
        historical["reason"],
        "",
        "It remains infrastructure evidence. It is not a mom/gap corpus.",
        "",
        "## Short /ru-RU/ validation",
        "",
    ]
    if short is None:
        lines.extend(
            [
                "**Pending operator capture.** Reload unpacked extension 1.3.0 on",
                "logged-in `https://www.mexc.com/ru-RU/futures/TAO_USDT`, capture",
                "5–15 minutes, export NDJSON, and keep screenshots of bid, ask,",
                "last, Fair/Mark, and Index. Then re-run:",
                "",
                "```",
                "python -m trading_bot.research.mexc_shadow.ui_capture \\",
                "  locale-remediation --raw FILE \\",
                "  --out docs/mexc_ui_locale_data_semantics_remediation_v1.json \\",
                "  --md docs/mexc_ui_locale_data_semantics_remediation_v1.md",
                "```",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"- path: `{short['path']}`",
                f"- snapshots: {short['n_snapshots']}",
                f"- duration hours: {short['duration_hours']}",
                (
                    "- median last/bid/ask/mark/index: "
                    f"{short['median_last']} / {short['median_bid']} / "
                    f"{short['median_ask']} / {short['median_mark']} / "
                    f"{short['median_index']}"
                ),
                f"- strategy-ready rate: {short['strategy_ready_rate']}",
                (
                    "- simultaneous bid+ask+last+mark+index: "
                    f"{short['simultaneous_bid_ask_last_mark_index']} "
                    f"({short['simultaneous_rate']})"
                ),
                f"- DATA_INVALID: {short['n_data_invalid']}",
                f"- passed: **{short['passed']}**",
                "",
                "| Gate | Result |",
                "| --- | --- |",
            ]
        )
        for name, ok in short["gates"].items():
            lines.append(f"| `{name}` | {'PASS' if ok else 'FAIL'} |")
        lines.extend(["", "HYPOTHESIS_SMOKE is not strategy evidence.", ""])
    lines.extend(
        [
            "## Decision",
            "",
            f"**{payload['decision']}.** Do not start ML or PAPER. Do not retune frozen profiles.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def write_reports(
    *,
    out_json: Path,
    out_md: Path,
    short_raw: Path | None,
    historical_raw: Path | None,
) -> dict[str, Any]:
    payload = build_milestone_payload(short_raw=short_raw, historical_raw=historical_raw)
    out_json.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    out_md.write_text(render_markdown(payload), encoding="utf-8")
    return payload
