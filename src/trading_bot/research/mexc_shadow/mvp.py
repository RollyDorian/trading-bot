"""Minimal local TAOUSDT shadow runner. Public observations only; never trades."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import signal
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, TextIO

from trading_bot.research.mexc_shadow.config import DEFAULT_COST_SCENARIOS, EngineConfig
from trading_bot.research.mexc_shadow.costs import summarize_costs
from trading_bot.research.mexc_shadow.features import FeatureEngine
from trading_bot.research.mexc_shadow.profiles import author_observed_v0
from trading_bot.research.mexc_shadow.shadow import ShadowBook
from trading_bot.research.mexc_shadow.signal import CandidateGate
from trading_bot.research.mexc_shadow.source import observation_from_mapping
from trading_bot.research.mexc_shadow.types import Candidate, Observation, ShadowTrade

MILESTONE = "MEXC_SHADOW_MVP_V0"
SYMBOL = "TAOUSDT"
LOCKED_CORPUS_SHA256 = "5c15b9714f804f8df5a327ae81fb2d7fb515ec052aeed0ff1af5df5a8680467c"
MAX_EVENT_GAP_MS = 2_000.0
MAX_LIVE_AGE_MS = 2_000.0
MAX_BODY_BYTES = 2_000_000
LATENCY_SAMPLE_LIMIT = 10_000
SLIPPAGE_BPS_PER_SIDE = (0.0, 1.0, 2.0)
ABANDONED_DATA_GAP = "ABANDONED_DATA_GAP"
_EXTENSION_ORIGIN = re.compile(r"chrome-extension://[a-p]{32}\Z")


@dataclass(frozen=True, slots=True)
class VariantSpec:
    variant_id: str
    gap_definition: str
    config: EngineConfig


def fixed_variants() -> tuple[VariantSpec, VariantSpec]:
    """The milestone's complete, closed exploratory variant set."""

    base = author_observed_v0()
    variants: list[VariantSpec] = []
    for variant_id, gap_definition in (
        ("exploratory_mid_mark_h1s", "mid_vs_mark"),
        ("exploratory_mid_index_h1s", "mid_vs_index"),
    ):
        signal_params = replace(
            base.signal,
            momentum_definition="mid_return_lookback",
            momentum_lookback=1,
            momentum_lookback_seconds=1.0,
            gap_definition=gap_definition,
        )
        variants.append(
            VariantSpec(
                variant_id=variant_id,
                gap_definition=gap_definition,
                config=replace(
                    base,
                    profile_id=variant_id,
                    signal=signal_params,
                    provenance_note=(
                        "Fixed exploratory MVP variant using existing profile thresholds; "
                        "not an identified author formula."
                    ),
                ),
            )
        )
    return variants[0], variants[1]


class _VariantRuntime:
    def __init__(self, spec: VariantSpec) -> None:
        self.spec = spec
        self.engine = FeatureEngine(spec.config.signal)
        self.gate = CandidateGate(spec.config)
        self.book = ShadowBook(spec.config.shadow)
        self.candidates: list[Candidate] = []
        self.counters: Counter[str] = Counter()

    def reset_features(self) -> None:
        self.engine = FeatureEngine(self.spec.config.signal)
        self.counters["feature_resets"] += 1

    def process(self, observation: Observation) -> list[dict[str, Any]]:
        emitted: list[dict[str, Any]] = []
        if self.spec.gap_definition == "mid_vs_mark" and observation.mark is None:
            self.counters["missing_reference"] += 1
            abandoned = self.abandon(observation.observed_at, "missing_reference")
            if abandoned is not None:
                emitted.append(abandoned)
            self.engine.update(observation)
            return emitted
        if self.spec.gap_definition == "mid_vs_index" and observation.index is None:
            self.counters["missing_reference"] += 1
            abandoned = self.abandon(observation.observed_at, "missing_reference")
            if abandoned is not None:
                emitted.append(abandoned)
            self.engine.update(observation)
            return emitted

        closed = self.book.on_observation(observation)
        if closed is not None:
            emitted.append(self._close_event(closed))

        features = self.engine.update(observation)
        candidate = self.gate.evaluate(
            features,
            position_open=self.book.position_open(observation.symbol),
            notional_multiplier=self.book.notional_multiplier_for(self.spec.config.shadow),
        )
        if candidate is None:
            self.counters["no_candidate"] += 1
            return emitted
        self.candidates.append(candidate)
        self.counters[f"candidate_{candidate.throttle}"] += 1
        if candidate.accepted_for_shadow:
            self.book.maybe_open(candidate, observation, self.spec.config.shadow)
            emitted.append(self._open_event(candidate, observation))
        return emitted

    def abandon(self, when: datetime | None, cause: str) -> dict[str, Any] | None:
        candidate = self.book.abandon(SYMBOL)
        if candidate is None:
            return None
        self.counters["abandoned_data_gap"] += 1
        self.counters[f"abandoned_cause_{cause}"] += 1
        return {
            "event": "CLOSE",
            "variant_id": self.spec.variant_id,
            "observed_at": when.isoformat() if when is not None else None,
            "symbol": candidate.symbol,
            "direction": candidate.direction,
            "entry_at": candidate.observed_at.isoformat(),
            "exit_reason": ABANDONED_DATA_GAP,
            "data_gap_cause": cause,
            "scored": False,
            "exit_price": None,
            "gross_bps": None,
            "net_bps": None,
        }

    def _open_event(self, candidate: Candidate, observation: Observation) -> dict[str, Any]:
        entry_price = observation.ask if candidate.direction == "long" else observation.bid
        event = {
            "event": "OPEN",
            "variant_id": self.spec.variant_id,
            "observed_at": candidate.observed_at.isoformat(),
            "symbol": candidate.symbol,
            "direction": candidate.direction,
            "entry_price": entry_price,
            "entry_bid": observation.bid,
            "entry_ask": observation.ask,
            "mom_bps": candidate.mom_bps,
            "gap_bps": candidate.gap_bps,
            "target_bps": candidate.target_bps,
        }
        return event

    def _close_event(self, trade: ShadowTrade) -> dict[str, Any]:
        event = {
            "event": "CLOSE",
            "variant_id": self.spec.variant_id,
            "observed_at": trade.exit_at.isoformat(),
            "symbol": trade.symbol,
            "direction": trade.direction,
            "entry_at": trade.entry_at.isoformat(),
            "exit_reason": trade.exit_reason,
            "entry_price": trade.entry_ask if trade.direction == "long" else trade.entry_bid,
            "exit_price": trade.exit_bid if trade.direction == "long" else trade.exit_ask,
            "gross_bps": trade.gross_bps,
            "net_bps": {
                scenario.name: trade.gross_bps - 2.0 * scenario.fee_bps_per_side
                for scenario in DEFAULT_COST_SCENARIOS
            },
        }
        return event


class ShadowMvpRunner:
    """Stateful causal runner shared by locked replay and localhost ingestion."""

    def __init__(self, *, event_stream: TextIO | None = None) -> None:
        self._variants = [_VariantRuntime(spec) for spec in fixed_variants()]
        self._event_stream = event_stream
        self._last_observed_at: datetime | None = None
        self._last_sequence: int | None = None
        self._capture_id: str | None = None
        self._source_latency_ms: deque[float] = deque(maxlen=LATENCY_SAMPLE_LIMIT)
        self._transport_latency_ms: deque[float] = deque(maxlen=LATENCY_SAMPLE_LIMIT)
        self.counters: Counter[str] = Counter()
        self.first_observed_at: datetime | None = None
        self.last_observed_at: datetime | None = None

    @property
    def variants(self) -> Sequence[_VariantRuntime]:
        return tuple(self._variants)

    def ingest_mapping(
        self,
        row: Mapping[str, Any],
        *,
        arrival_at: datetime | None = None,
        enforce_live_age: bool = False,
    ) -> list[dict[str, Any]]:
        self.counters["rows_seen"] += 1
        if str(row.get("schema") or "") != "mexc_ui_raw_snapshot":
            self.counters["non_snapshot_rows"] += 1
            return []
        try:
            observation = observation_from_mapping(row)
        except (KeyError, TypeError, ValueError):
            self.counters["data_invalid"] += 1
            return self._record_events(
                self._reset_and_abandon(
                    "invalid_snapshot",
                    _mapping_timestamp(row, arrival_at),
                )
            )
        return self.ingest_observation(
            observation,
            capture_id=str(row.get("capture_id") or ""),
            sequence=_optional_int(row.get("sequence")),
            arrival_at=arrival_at,
            enforce_live_age=enforce_live_age,
        )

    def ingest_observation(
        self,
        observation: Observation,
        *,
        capture_id: str = "test",
        sequence: int | None = None,
        arrival_at: datetime | None = None,
        enforce_live_age: bool = False,
    ) -> list[dict[str, Any]]:
        emitted: list[dict[str, Any]] = []
        if observation.symbol.replace("_", "").replace("/", "").replace("-", "") != SYMBOL:
            self.counters["wrong_symbol"] += 1
            return self._record_events(
                self._reset_and_abandon("wrong_symbol", observation.observed_at)
            )
        if observation.bid <= 0 or observation.ask <= observation.bid:
            self.counters["data_invalid"] += 1
            return self._record_events(
                self._reset_and_abandon("invalid_bbo", observation.observed_at)
            )

        source_latency = (observation.received_at - observation.observed_at).total_seconds() * 1000
        if math.isfinite(source_latency) and source_latency >= 0:
            self._source_latency_ms.append(source_latency)
        if arrival_at is not None:
            arrival = _as_utc(arrival_at)
            transport_latency = (arrival - observation.received_at).total_seconds() * 1000
            if math.isfinite(transport_latency) and transport_latency >= 0:
                self._transport_latency_ms.append(transport_latency)
            if enforce_live_age and (transport_latency < 0 or transport_latency > MAX_LIVE_AGE_MS):
                self.counters["stale_live"] += 1
                return self._record_events(
                    self._reset_and_abandon("stale_live", observation.observed_at)
                )

        observed = _as_utc(observation.observed_at)
        new_capture = self._capture_id is not None and capture_id != self._capture_id
        sequence_bad = (
            sequence is not None
            and self._last_sequence is not None
            and not new_capture
            and sequence <= self._last_sequence
        )
        if sequence_bad:
            self.counters["sequence_duplicate_or_reversal"] += 1
            return self._record_events(self._reset_and_abandon("sequence_invalid", observed))

        if new_capture:
            self.counters["capture_boundary"] += 1
            emitted.extend(self._reset_and_abandon("capture_boundary", observed))
        if self._last_observed_at is not None and not new_capture:
            gap_ms = (observed - self._last_observed_at).total_seconds() * 1000
            if gap_ms <= 0:
                self.counters["non_monotonic_time"] += 1
                return self._record_events(
                    self._reset_and_abandon("non_monotonic_time", observed)
                )
            if gap_ms > MAX_EVENT_GAP_MS:
                self.counters["event_gap"] += 1
                emitted.extend(self._reset_and_abandon("event_gap", observed))

        self._capture_id = capture_id
        self._last_sequence = sequence
        self._last_observed_at = observed
        self.first_observed_at = self.first_observed_at or observed
        self.last_observed_at = observed
        self.counters["observations_valid"] += 1

        for runtime in self._variants:
            emitted.extend(runtime.process(observation))
        return self._record_events(emitted)

    def _reset_features(self) -> None:
        for runtime in self._variants:
            runtime.reset_features()

    def _reset_and_abandon(self, cause: str, when: datetime | None) -> list[dict[str, Any]]:
        emitted: list[dict[str, Any]] = []
        for runtime in self._variants:
            event = runtime.abandon(when, cause)
            if event is not None:
                emitted.append(event)
            runtime.reset_features()
        return emitted

    def _record_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for event in events:
            self._write_event(event)
        return events

    def _write_event(self, event: Mapping[str, Any]) -> None:
        if self._event_stream is None:
            return
        self._event_stream.write(json.dumps(dict(event), sort_keys=True) + "\n")
        self._event_stream.flush()

    def summary(self) -> dict[str, Any]:
        duration_hours = None
        if self.first_observed_at is not None and self.last_observed_at is not None:
            duration_hours = (self.last_observed_at - self.first_observed_at).total_seconds() / 3600
        return {
            "milestone": MILESTONE,
            "status": "DEVELOPMENT_ONLY_NOT_UNSEEN_OOS",
            "symbol": SYMBOL,
            "duration_hours": duration_hours,
            "counters": dict(sorted(self.counters.items())),
            "data_invalid_counters": {
                name: self.counters[name]
                for name in (
                    "data_invalid",
                    "stale_live",
                    "event_gap",
                    "non_monotonic_time",
                    "sequence_duplicate_or_reversal",
                    "wrong_symbol",
                )
            },
            "latency_ms": {
                "source": _distribution(list(self._source_latency_ms)),
                "localhost_transport": _distribution(list(self._transport_latency_ms)),
                "sample_cap": LATENCY_SAMPLE_LIMIT,
            },
            "variants": [self._variant_summary(runtime) for runtime in self._variants],
            "notes": [
                "Exactly two fixed exploratory variants; neither is an identified author formula.",
                "Historical replay is development evidence, not unseen OOS.",
                "All entries/exits use executable ask/bid; no orders or private data are used.",
            ],
        }

    def _variant_summary(self, runtime: _VariantRuntime) -> dict[str, Any]:
        trades = runtime.book.trades
        gross = [trade.gross_bps for trade in trades]
        exit_reasons = Counter(trade.exit_reason for trade in trades)
        return {
            "variant_id": runtime.spec.variant_id,
            "formula": {
                "momentum": "mid_return_1s",
                "gap": runtime.spec.gap_definition,
                "orientation": "long mom>0 gap<0; short inverse",
            },
            "thresholds": asdict(runtime.spec.config.signal),
            "candidate_counts": dict(sorted(runtime.counters.items())),
            "trade_count": len(trades),
            "abandoned_count": runtime.counters["abandoned_data_gap"],
            "open_positions": runtime.book.open_count(),
            "exit_reasons": dict(sorted(exit_reasons.items())),
            "gross_bps": _distribution(gross),
            "cost_scenarios": summarize_costs(trades),
            "slippage_sensitivity": _slippage_matrix(trades),
            "maximum_drawdown_gross_bps": _maximum_drawdown(gross),
            "tail_losses_gross_bps": _tail_losses(gross),
        }


def replay_locked_corpus(
    path: Path,
    *,
    event_stream: TextIO | None = None,
) -> dict[str, Any]:
    actual_hash = _sha256(path)
    if actual_hash != LOCKED_CORPUS_SHA256:
        raise ValueError(
            f"locked corpus SHA-256 mismatch: expected {LOCKED_CORPUS_SHA256}, got {actual_hash}"
        )
    runner = ShadowMvpRunner(event_stream=event_stream)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid NDJSON at line {line_number}") from exc
            if not isinstance(row, dict):
                runner.counters["non_object_rows"] += 1
                continue
            runner.ingest_mapping(row)
    summary = runner.summary()
    summary["corpus"] = {"path": str(path), "sha256": actual_hash}
    return summary


class ShadowHttpServer(HTTPServer):
    runner: ShadowMvpRunner
    allowed_extension_origin: str | None


class _SnapshotHandler(BaseHTTPRequestHandler):
    server: ShadowHttpServer

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self.path != "/v1/snapshot":
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if not _origin_allowed(origin, self.server.allowed_extension_origin):
            self._reject_origin()
            return
        self.send_response(204)
        self._cors_headers(origin)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/snapshot":
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        if not _origin_allowed(origin, self.server.allowed_extension_origin):
            self._reject_origin()
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length < 2 or length > MAX_BODY_BYTES:
            self.send_error(413)
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("snapshot must be an object")
            events = self.server.runner.ingest_mapping(
                payload,
                arrival_at=datetime.now(UTC),
                enforce_live_age=True,
            )
        except (json.JSONDecodeError, ValueError):
            self.send_error(400)
            return
        body = json.dumps({"ok": True, "events": len(events)}).encode()
        self.send_response(200)
        self._cors_headers(origin)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self, origin: str | None) -> None:
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

    def _reject_origin(self) -> None:
        body = b'{"ok":false,"error":"origin_forbidden"}'
        self.send_response(403)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def build_server(
    runner: ShadowMvpRunner,
    port: int,
    *,
    extension_origin: str | None = None,
) -> ShadowHttpServer:
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    if extension_origin is not None and _EXTENSION_ORIGIN.fullmatch(extension_origin) is None:
        raise ValueError("extension_origin must be chrome-extension:// plus a 32-character id")
    server = ShadowHttpServer(("127.0.0.1", port), _SnapshotHandler)
    server.runner = runner
    server.allowed_extension_origin = extension_origin
    return server


def _origin_allowed(origin: str | None, extension_origin: str | None) -> bool:
    return origin is None or origin == extension_origin


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    if not ordered:
        return {
            "count": 0,
            "min": None,
            "p05": None,
            "p50": None,
            "p95": None,
            "max": None,
            "sum": 0.0,
            "mean": None,
        }
    total = sum(ordered)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "p05": _quantile(ordered, 0.05),
        "p50": _quantile(ordered, 0.50),
        "p95": _quantile(ordered, 0.95),
        "max": ordered[-1],
        "sum": total,
        "mean": total / len(ordered),
    }


def _quantile(ordered: Sequence[float], probability: float) -> float:
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _maximum_drawdown(gross: Sequence[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for value in gross:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _tail_losses(gross: Sequence[float]) -> dict[str, float | int | None]:
    losses = sorted(value for value in gross if value < 0)
    return {
        "loss_count": len(losses),
        "worst": losses[0] if losses else None,
        "p05": _quantile(losses, 0.05) if losses else None,
        "p10": _quantile(losses, 0.10) if losses else None,
    }


def _slippage_matrix(trades: Sequence[ShadowTrade]) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    for scenario in DEFAULT_COST_SCENARIOS:
        for slippage in SLIPPAGE_BPS_PER_SIDE:
            nets = [
                trade.gross_bps - 2.0 * (scenario.fee_bps_per_side + slippage)
                for trade in trades
            ]
            rows.append(
                {
                    "fee_bps_per_side": scenario.fee_bps_per_side,
                    "slippage_bps_per_side": slippage,
                    "n_trades": len(nets),
                    "sum_net_bps": sum(nets),
                    "mean_net_bps": sum(nets) / len(nets) if nets else 0.0,
                }
            )
    return rows


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping_timestamp(row: Mapping[str, Any], fallback: datetime | None) -> datetime | None:
    raw = row.get("observed_at_local") or row.get("received_at_local")
    if raw is not None:
        try:
            return _as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
        except ValueError:
            pass
    return _as_utc(fallback) if fallback is not None else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local public-data-only MEXC shadow MVP")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay", help="Replay the SHA-locked 10.6h corpus")
    replay.add_argument("--raw", type=Path, required=True)
    replay.add_argument("--summary", type=Path, required=True)
    replay.add_argument("--events", type=Path)
    serve = sub.add_parser("serve", help="Receive extension snapshots on loopback")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--extension-origin",
        required=True,
        help="Exact chrome-extension://<32-character-id> Origin allowed to POST",
    )
    serve.add_argument("--events", type=Path, required=True)
    serve.add_argument("--summary", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "replay":
        if args.events:
            args.events.parent.mkdir(parents=True, exist_ok=True)
        event_handle = args.events.open("w", encoding="utf-8") if args.events else None
        try:
            summary = replay_locked_corpus(args.raw, event_stream=event_handle)
        finally:
            if event_handle is not None:
                event_handle.close()
        _write_json(args.summary, summary)
        print(json.dumps(summary, sort_keys=True))
        return 0

    args.events.parent.mkdir(parents=True, exist_ok=True)
    with args.events.open("a", encoding="utf-8") as event_handle:
        runner = ShadowMvpRunner(event_stream=event_handle)
        server = build_server(runner, args.port, extension_origin=args.extension_origin)
        def stop(_signum: int, _frame: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        print(f"shadow receiver listening on http://127.0.0.1:{server.server_port}/v1/snapshot")
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            summary = runner.summary()
            _write_json(args.summary, summary)
            print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
