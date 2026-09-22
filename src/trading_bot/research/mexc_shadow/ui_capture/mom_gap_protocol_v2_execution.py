"""Frozen MEXC mom/gap protocol-v2 executor.

Offline and read-only: consumes the SHA-locked 500 ms grid, never places orders,
and never evaluates PnL, target reach, costs, exits, profiles, or ML.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROTOCOL_MERGE_SHA = "0b4f761bb4c7fa8a6e4520901bfc32f8f2bf59d1"
PROTOCOL_AMENDMENT_COMMIT_SHA = "cb679745b1fda6ecde509d8f2656232a89dd192b"
PROTOCOL_AMENDMENT_BLOB_SHA = "e19c26ef6b6f94123a767f165f7e5d5c8cb0dfd8"
LOCKED_CORPUS_SHA256 = "5c15b9714f804f8df5a327ae81fb2d7fb515ec052aeed0ff1af5df5a8680467c"
LOCKED_GRID_SHA256 = "3bf630648ee2abfa1839d720c5e5ffe271266e2d6c6c7453870cc0b92a698d89"
PROTOCOL_DOC = "docs/mexc_mom_gap_hypothesis_protocol_v2_data_contract_amendment.md"
EXECUTOR_ID = "MEXC_MOM_GAP_PROTOCOL_V2_EXECUTOR"
EXECUTOR_VERSION = "1.0.0"
GRID_MS = 500
REARM_MS = 2_000
NON_OVERLAP_MS = 10_000
WILSON_Z_99 = 2.5758293035489004
LOOKBACKS_S = (1, 2, 5)
HORIZONS_S = (1, 2, 5, 10)
PRIMARY_HORIZONS_S = (2, 5)
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)


@dataclass(frozen=True)
class Family:
    id: str
    mom_kind: str
    gap_left: str
    gap_right: str
    follower: str
    reference: str
    momentum_source: str


FAMILIES = (
    Family("F01_MID_RET_MID_MARK", "return", "q", "m", "q", "m", "q"),
    Family("F02_MID_RET_MID_INDEX", "return", "q", "i", "q", "i", "q"),
    Family("F03_MARK_RET_MID_MARK", "return", "q", "m", "q", "m", "m"),
    Family("F04_INDEX_RET_MID_INDEX", "return", "q", "i", "q", "i", "i"),
    Family("F05_LAST_RET_MID_LAST", "return", "q", "l", "q", "l", "l"),
    Family("F06_LAST_RET_LAST_MARK", "return", "l", "m", "l", "m", "l"),
    Family("F07_MID_SMA_MID_MARK", "sma", "q", "m", "q", "m", "q"),
)


@dataclass(frozen=True)
class GridRow:
    ordinal: int
    segment: int
    t_ms: int
    session_id: str
    bid: float | None
    ask: float | None
    last: float | None
    mark: float | None
    index: float | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or not self.bid < self.ask:
            return None
        return (self.bid + self.ask) / 2.0

    def price(self, name: str) -> float | None:
        return {
            "q": self.mid,
            "l": self.last,
            "m": self.mark,
            "i": self.index,
        }[name]


@dataclass(frozen=True)
class FeatureRow:
    row: GridRow
    mom: float | None
    gap: float | None


@dataclass(frozen=True)
class Episode:
    row: GridRow
    mom: float
    gap: float
    side: str


class LockMismatchError(RuntimeError):
    """Raised before feature calculation if a frozen input lock differs."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def verify_locks(
    *, repo: Path, corpus: Path, grid: Path, manifest: Path, code_commit_sha: str
) -> dict[str, Any]:
    """Verify every frozen lock before any grid row is parsed."""

    corpus_sha = sha256_file(corpus)
    grid_sha = sha256_file(grid)
    if corpus_sha != LOCKED_CORPUS_SHA256:
        raise LockMismatchError(f"corpus SHA mismatch: {corpus_sha}")
    if grid_sha != LOCKED_GRID_SHA256:
        raise LockMismatchError(f"grid SHA mismatch: {grid_sha}")

    if _git(repo, "cat-file", "-t", PROTOCOL_MERGE_SHA) != "commit":
        raise LockMismatchError("protocol merge SHA is not a commit")
    if _git(repo, "cat-file", "-t", PROTOCOL_AMENDMENT_COMMIT_SHA) != "commit":
        raise LockMismatchError("protocol amendment SHA is not a commit")
    amendment_blob = _git(repo, "rev-parse", f"{PROTOCOL_AMENDMENT_COMMIT_SHA}:{PROTOCOL_DOC}")
    head_blob = _git(repo, "rev-parse", f"HEAD:{PROTOCOL_DOC}")
    if amendment_blob != PROTOCOL_AMENDMENT_BLOB_SHA or head_blob != amendment_blob:
        raise LockMismatchError(
            f"protocol amendment blob mismatch: commit={amendment_blob}, HEAD={head_blob}"
        )
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", PROTOCOL_MERGE_SHA, "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode != 0:
        raise LockMismatchError("protocol merge SHA is not an ancestor of HEAD")
    head = _git(repo, "rev-parse", "HEAD")
    if head != code_commit_sha:
        raise LockMismatchError(f"executor code commit mismatch: HEAD={head}")

    locked = json.loads(manifest.read_text(encoding="utf-8"))
    if (locked.get("protocol") or {}).get("commit_sha") != PROTOCOL_MERGE_SHA:
        raise LockMismatchError("manifest protocol merge SHA mismatch")
    if (locked.get("protocol") or {}).get("amendment_commit_sha") != (
        PROTOCOL_AMENDMENT_COMMIT_SHA
    ):
        raise LockMismatchError("manifest protocol amendment SHA mismatch")
    if (locked.get("corpus") or {}).get("sha256") != corpus_sha:
        raise LockMismatchError("manifest corpus SHA mismatch")
    if (locked.get("grid") or {}).get("sha256") != grid_sha:
        raise LockMismatchError("manifest grid SHA mismatch")
    if not bool((locked.get("admissibility") or {}).get("passed")):
        raise LockMismatchError("committed input manifest is not admissible")

    return {
        "all_locks_verified_before_feature_calculation": True,
        "protocol_merge_commit": PROTOCOL_MERGE_SHA,
        "protocol_amendment_commit": PROTOCOL_AMENDMENT_COMMIT_SHA,
        "protocol_amendment_blob": amendment_blob,
        "protocol_head_blob": head_blob,
        "protocol_merge_is_head_ancestor": True,
        "corpus_sha256": corpus_sha,
        "grid_sha256": grid_sha,
        "executor_code_commit": code_commit_sha,
        "manifest_admissible": True,
        "manifest": locked,
    }


def _positive_number(value: Any) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    return None


def load_grid(path: Path) -> list[GridRow]:
    rows: list[GridRow] = []
    previous_t: int | None = None
    previous_session: str | None = None
    segment = -1
    with path.open("r", encoding="utf-8") as handle:
        for ordinal, line in enumerate(handle):
            raw = json.loads(line)
            t_ms = int(raw["t_ms"])
            session_id = str(raw["session_id"])
            if previous_t is not None and t_ms <= previous_t:
                raise ValueError("grid times must be strictly increasing")
            if previous_t is None or session_id != previous_session or t_ms - previous_t != GRID_MS:
                segment += 1
            rows.append(
                GridRow(
                    ordinal=ordinal,
                    segment=segment,
                    t_ms=t_ms,
                    session_id=session_id,
                    bid=_positive_number(raw.get("bid")),
                    ask=_positive_number(raw.get("ask")),
                    last=_positive_number(raw.get("last")),
                    mark=_positive_number(raw.get("mark")),
                    index=_positive_number(raw.get("index")),
                )
            )
            previous_t = t_ms
            previous_session = session_id
    if not rows:
        raise ValueError("locked grid is empty")
    return rows


def bps(current: float, base: float) -> float:
    return 10_000.0 * (current / base - 1.0)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    """Repository-standard nearest-rank index floor(n*q), capped at n-1."""

    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 12)


def _quantiles(values: Sequence[float]) -> dict[str, float | None]:
    return {f"p{int(q * 100):02d}": _rounded(_percentile(values, q)) for q in QUANTILES}


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def wilson_99(successes: int, total: int) -> dict[str, int | float | None]:
    if total == 0:
        return {
            "successes": successes,
            "total": total,
            "estimate": None,
            "lower": None,
            "upper": None,
        }
    estimate = successes / total
    z2 = WILSON_Z_99**2
    denominator = 1.0 + z2 / total
    center = (estimate + z2 / (2.0 * total)) / denominator
    half = (
        WILSON_Z_99
        * math.sqrt(estimate * (1.0 - estimate) / total + z2 / (4.0 * total**2))
        / denominator
    )
    return {
        "successes": successes,
        "total": total,
        "estimate": _rounded(estimate),
        "lower": _rounded(max(0.0, center - half)),
        "upper": _rounded(min(1.0, center + half)),
    }


def _feature_rows(rows: list[GridRow], family: Family, lookback_s: int) -> list[FeatureRow]:
    by_key = {(row.segment, row.t_ms): row for row in rows}
    lookback_ms = lookback_s * 1000
    window_size = lookback_ms // GRID_MS + 1
    features: list[FeatureRow] = []
    for row in rows:
        current_source = row.price(family.momentum_source)
        mom: float | None = None
        if current_source is not None:
            if family.mom_kind == "return":
                earlier = by_key.get((row.segment, row.t_ms - lookback_ms))
                earlier_source = None if earlier is None else earlier.price(family.momentum_source)
                if earlier_source is not None:
                    mom = bps(current_source, earlier_source)
            else:
                start = row.ordinal - window_size + 1
                if start >= 0:
                    window = rows[start : row.ordinal + 1]
                    prices = [sample.mid for sample in window]
                    valid_prices = [price for price in prices if price is not None]
                    if (
                        len(window) == window_size
                        and window[0].segment == row.segment
                        and len(valid_prices) == len(prices)
                    ):
                        mean_mid = sum(valid_prices) / window_size
                        mom = bps(current_source, mean_mid)
        left = row.price(family.gap_left)
        right = row.price(family.gap_right)
        gap = None if left is None or right is None else bps(left, right)
        features.append(FeatureRow(row=row, mom=mom, gap=gap))
    return features


def quadrant_counts(features: Iterable[FeatureRow]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for feature in features:
        if feature.mom is None or feature.gap is None:
            counts["unavailable"] += 1
        elif feature.mom > 0 and feature.gap > 0:
            counts["mom_pos_gap_pos"] += 1
        elif feature.mom > 0 and feature.gap < 0:
            counts["mom_pos_gap_neg"] += 1
        elif feature.mom < 0 and feature.gap > 0:
            counts["mom_neg_gap_pos"] += 1
        elif feature.mom < 0 and feature.gap < 0:
            counts["mom_neg_gap_neg"] += 1
        else:
            counts["zero_mom_or_gap"] += 1
    keys = (
        "mom_pos_gap_pos",
        "mom_pos_gap_neg",
        "mom_neg_gap_pos",
        "mom_neg_gap_neg",
        "zero_mom_or_gap",
        "unavailable",
    )
    return {key: counts[key] for key in keys}


def _eligible(feature: FeatureRow) -> tuple[bool, str | None]:
    if feature.mom is None or feature.gap is None:
        return False, None
    if abs(feature.mom) < 3.0 or abs(feature.gap) < 1.5:
        return False, None
    if feature.mom > 0 and feature.gap < 0:
        return True, "long"
    if feature.mom < 0 and feature.gap > 0:
        return True, "short"
    return False, None


def construct_episodes(features: Sequence[FeatureRow]) -> list[Episode]:
    episodes: list[Episode] = []
    armed = True
    ineligible_since: int | None = None
    previous_segment: int | None = None
    for feature in features:
        if feature.row.segment != previous_segment:
            armed = True
            ineligible_since = None
            previous_segment = feature.row.segment
        is_eligible, side = _eligible(feature)
        if is_eligible:
            if (
                not armed
                and ineligible_since is not None
                and feature.row.t_ms - ineligible_since > REARM_MS
            ):
                armed = True
            if armed:
                assert feature.mom is not None and feature.gap is not None and side is not None
                episodes.append(Episode(feature.row, feature.mom, feature.gap, side))
                armed = False
            ineligible_since = None
            continue
        if not armed:
            if ineligible_since is None:
                ineligible_since = feature.row.t_ms
            elif feature.row.t_ms - ineligible_since > REARM_MS:
                armed = True
    return episodes


def non_overlapping(episodes: Sequence[Episode]) -> list[Episode]:
    kept: list[Episode] = []
    last_by_segment: dict[int, int] = {}
    for episode in episodes:
        previous = last_by_segment.get(episode.row.segment)
        if previous is None or episode.row.t_ms - previous >= NON_OVERLAP_MS:
            kept.append(episode)
            last_by_segment[episode.row.segment] = episode.row.t_ms
    return kept


def _band_summary(episodes: Sequence[Episode]) -> dict[str, Any]:
    abs_mom = [abs(episode.mom) for episode in episodes]
    abs_gap = [abs(episode.gap) for episode in episodes]
    mom_in = sum(3.0 <= value <= 5.5 for value in abs_mom)
    gap_in = sum(1.5 <= value <= 5.2 for value in abs_gap)
    joint = sum(
        3.0 <= abs(episode.mom) <= 5.5 and 1.5 <= abs(episode.gap) <= 5.2 for episode in episodes
    )
    return {
        "n": len(episodes),
        "abs_mom_bps": {
            "quantiles": _quantiles(abs_mom),
            "band_share_3_0_to_5_5": _rounded(_ratio(mom_in, len(episodes))),
            "share_above_5_5": _rounded(
                _ratio(sum(value > 5.5 for value in abs_mom), len(episodes))
            ),
        },
        "abs_gap_bps": {
            "quantiles": _quantiles(abs_gap),
            "band_share_1_5_to_5_2": _rounded(_ratio(gap_in, len(episodes))),
            "share_above_5_2": _rounded(
                _ratio(sum(value > 5.2 for value in abs_gap), len(episodes))
            ),
        },
        "joint_band_share": _rounded(_ratio(joint, len(episodes))),
        "target_proxy_bps": {
            "definition": "2*abs(gap)",
            "quantiles": _quantiles([2.0 * value for value in abs_gap]),
            "performance_interpretation": False,
        },
    }


def _band_pass(overall: dict[str, Any], long: dict[str, Any], short: dict[str, Any]) -> bool:
    mom_median = overall["abs_mom_bps"]["quantiles"]["p50"]
    gap_median = overall["abs_gap_bps"]["quantiles"]["p50"]
    joint = overall["joint_band_share"]
    long_joint = long["joint_band_share"]
    short_joint = short["joint_band_share"]
    return bool(
        mom_median is not None
        and 3.0 <= mom_median <= 5.5
        and gap_median is not None
        and 1.5 <= gap_median <= 5.2
        and joint is not None
        and joint >= 0.70
        and long_joint is not None
        and long_joint >= 0.60
        and short_joint is not None
        and short_joint >= 0.60
    )


def _hour_key(t_ms: int) -> str:
    stamp = datetime.fromtimestamp(t_ms / 1000.0, tz=UTC)
    return stamp.replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")


def _frequency(episodes: Sequence[Episode], rows: Sequence[GridRow]) -> dict[str, Any]:
    usable_hours = len(rows) * GRID_MS / 3_600_000.0
    long_n = sum(episode.side == "long" for episode in episodes)
    short_n = len(episodes) - long_n
    exposure = Counter(_hour_key(row.t_ms) for row in rows)
    event_counts: dict[str, Counter[str]] = {}
    for episode in episodes:
        bucket = event_counts.setdefault(_hour_key(episode.row.t_ms), Counter())
        bucket["total"] += 1
        bucket[episode.side] += 1
    per_hour: list[dict[str, Any]] = []
    for hour in sorted(exposure):
        hours = exposure[hour] * GRID_MS / 3_600_000.0
        counts = event_counts.get(hour, Counter())
        per_hour.append(
            {
                "utc_hour": hour,
                "exposure_hours": _rounded(hours),
                "total": counts["total"],
                "long": counts["long"],
                "short": counts["short"],
                "rate_per_hour": _rounded(counts["total"] / hours),
                "long_rate_per_hour": _rounded(counts["long"] / hours),
                "short_rate_per_hour": _rounded(counts["short"] / hours),
            }
        )
    return {
        "usable_hours": _rounded(usable_hours),
        "candidate_rate_per_hour": _rounded(len(episodes) / usable_hours),
        "long_rate_per_hour": _rounded(long_n / usable_hours),
        "short_rate_per_hour": _rounded(short_n / usable_hours),
        "per_utc_hour": per_hour,
        "frequency_status": "FREQUENCY_REFERENCE_UNAVAILABLE",
        "author_rate": None,
        "scoring_effect": "NON_SCORING",
    }


def _response_value(row: GridRow, name: str) -> float | None:
    return row.price(name)


def horizon_diagnostics(
    episodes: Sequence[Episode],
    rows_by_key: dict[tuple[int, int], GridRow],
    family: Family,
    horizon_s: int,
) -> dict[str, Any]:
    follower_moves: list[float] = []
    reference_moves: list[float] = []
    closures: list[float] = []
    momentum_moves: list[float] = []
    executable_mid_moves: list[float] = []
    follower_shares: list[float] = []
    for episode in episodes:
        future = rows_by_key.get((episode.row.segment, episode.row.t_ms + horizon_s * 1000))
        if future is None:
            continue
        a0 = _response_value(episode.row, family.follower)
        a1 = _response_value(future, family.follower)
        b0 = _response_value(episode.row, family.reference)
        b1 = _response_value(future, family.reference)
        x0 = _response_value(episode.row, family.momentum_source)
        x1 = _response_value(future, family.momentum_source)
        q0 = episode.row.mid
        q1 = future.mid
        if None in (a0, a1, b0, b1, x0, x1, q0, q1):
            continue
        assert a0 is not None and a1 is not None
        assert b0 is not None and b1 is not None
        assert x0 is not None and x1 is not None
        assert q0 is not None and q1 is not None
        direction = 1.0 if episode.side == "long" else -1.0
        follower = direction * bps(a1, a0)
        reference = direction * bps(b1, b0)
        closure = follower - reference
        momentum = direction * bps(x1, x0)
        executable_mid = direction * bps(q1, q0)
        follower_moves.append(follower)
        reference_moves.append(reference)
        closures.append(closure)
        momentum_moves.append(momentum)
        executable_mid_moves.append(executable_mid)
        if closure > 0:
            denominator = max(follower, 0.0) + max(-reference, 0.0)
            follower_shares.append(1.0 if denominator == 0 else max(follower, 0.0) / denominator)

    n = len(follower_moves)
    return {
        "horizon_s": horizon_s,
        "n_non_overlapping_representatives": len(episodes),
        "n_available": n,
        "n_missing_future": len(episodes) - n,
        "median_follower_bps": _rounded(_percentile(follower_moves, 0.50)),
        "median_reference_bps": _rounded(_percentile(reference_moves, 0.50)),
        "median_gap_closure_bps": _rounded(_percentile(closures, 0.50)),
        "median_momentum_source_bps": _rounded(_percentile(momentum_moves, 0.50)),
        "closing_events": len(follower_shares),
        "median_follower_share_on_closure": _rounded(_percentile(follower_shares, 0.50)),
        "wilson_99": {
            "follower_positive": wilson_99(sum(value > 0 for value in follower_moves), n),
            "gap_closure_positive": wilson_99(sum(value > 0 for value in closures), n),
            "momentum_continuation_positive": wilson_99(
                sum(value > 0 for value in momentum_moves), n
            ),
            "momentum_reversal_negative": wilson_99(sum(value < 0 for value in momentum_moves), n),
            "executable_mid_positive": wilson_99(
                sum(value > 0 for value in executable_mid_moves), n
            ),
        },
    }


def _lower(diag: dict[str, Any], key: str) -> float | None:
    value = diag["wilson_99"][key]["lower"]
    return None if value is None else float(value)


def classify_mechanism(family: Family, horizons: dict[int, dict[str, Any]]) -> str:
    primary = [horizons[horizon] for horizon in PRIMARY_HORIZONS_S]
    if family.id == "F06_LAST_RET_LAST_MARK" and any(
        (_lower(diag, "executable_mid_positive") or 0.0) <= 0.50 for diag in primary
    ):
        return "NON_EXECUTABLE_PRINT_ONLY"

    catch_up = all(
        (_lower(diag, "follower_positive") or 0.0) > 0.50
        and (_lower(diag, "gap_closure_positive") or 0.0) > 0.50
        and diag["median_follower_share_on_closure"] is not None
        and float(diag["median_follower_share_on_closure"]) >= 2.0 / 3.0
        and (_lower(diag, "momentum_continuation_positive") or 0.0) > 0.50
        for diag in primary
    )
    if catch_up:
        return "MOMENTUM_LAG_CATCHUP"

    momentum_reversion = all(
        (_lower(diag, "momentum_reversal_negative") or 0.0) > 0.50 for diag in primary
    )
    retreat_closure = all(
        (_lower(diag, "gap_closure_positive") or 0.0) > 0.50
        and diag["median_follower_share_on_closure"] is not None
        and float(diag["median_follower_share_on_closure"]) <= 1.0 / 3.0
        and (_lower(diag, "follower_positive") or 0.0) <= 0.50
        for diag in primary
    )
    if momentum_reversion or retreat_closure:
        return "SIMPLE_MEAN_REVERSION"
    return "MECHANISM_UNRESOLVED"


def _mechanism_bundle(
    episodes: Sequence[Episode], rows_by_key: dict[tuple[int, int], GridRow], family: Family
) -> tuple[dict[str, dict[str, Any]], str]:
    selected = non_overlapping(episodes)
    diagnostics = {
        str(horizon): horizon_diagnostics(selected, rows_by_key, family, horizon)
        for horizon in HORIZONS_S
    }
    by_int = {int(horizon): value for horizon, value in diagnostics.items()}
    return diagnostics, classify_mechanism(family, by_int)


def _temporal_stability(
    episodes: Sequence[Episode],
    rows: Sequence[GridRow],
    rows_by_key: dict[tuple[int, int], GridRow],
    family: Family,
) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    full_hours = len(rows) * GRID_MS / 3_600_000.0
    full_rate = len(episodes) / full_hours
    decisive: set[str] = set()
    for index in range(6):
        start = index * len(rows) // 6
        end = (index + 1) * len(rows) // 6
        block_episodes = [episode for episode in episodes if start <= episode.row.ordinal < end]
        hours = (end - start) * GRID_MS / 3_600_000.0
        bands = _band_summary(block_episodes)
        evaluable = len(block_episodes) >= 10
        horizons, mechanism = _mechanism_bundle(block_episodes, rows_by_key, family)
        if evaluable and mechanism != "MECHANISM_UNRESOLVED":
            decisive.add(mechanism)
        mom_median = bands["abs_mom_bps"]["quantiles"]["p50"]
        gap_median = bands["abs_gap_bps"]["quantiles"]["p50"]
        rate = len(block_episodes) / hours if hours else 0.0
        blocks.append(
            {
                "block": index + 1,
                "row_start_ordinal": start,
                "row_end_ordinal_exclusive": end,
                "usable_hours": _rounded(hours),
                "total_episodes": len(block_episodes),
                "long_episodes": sum(ep.side == "long" for ep in block_episodes),
                "short_episodes": sum(ep.side == "short" for ep in block_episodes),
                "evaluable": evaluable,
                "candidate_rate_per_hour": _rounded(rate),
                "rate_ratio_to_full": _rounded(rate / full_rate) if full_rate else None,
                "episode_share": _rounded(_ratio(len(block_episodes), len(episodes))),
                "mom_median_in_author_band": bool(
                    mom_median is not None and 3.0 <= mom_median <= 5.5
                ),
                "gap_median_in_author_band": bool(
                    gap_median is not None and 1.5 <= gap_median <= 5.2
                ),
                "joint_band_share": bands["joint_band_share"],
                "mechanism_classification": mechanism,
                "primary_horizons": {key: horizons[key] for key in ("2", "5")},
            }
        )
    evaluable_blocks = [block for block in blocks if block["evaluable"]]
    median_passes = sum(
        block["mom_median_in_author_band"] and block["gap_median_in_author_band"]
        for block in evaluable_blocks
    )
    joint_passes = sum(
        block["joint_band_share"] is not None and block["joint_band_share"] >= 0.60
        for block in evaluable_blocks
    )
    all_rates = all(
        block["rate_ratio_to_full"] is not None and 0.25 <= block["rate_ratio_to_full"] <= 4.0
        for block in evaluable_blocks
    )
    concentration = all(
        block["episode_share"] is not None and block["episode_share"] <= 0.40
        for block in evaluable_blocks
    )
    contradiction = len(decisive) > 1
    passed = bool(
        len(evaluable_blocks) >= 4
        and median_passes >= 4
        and joint_passes >= 4
        and all_rates
        and concentration
        and not contradiction
    )
    return {
        "status": "TEMPORALLY_STABLE" if passed else "TEMPORAL_STABILITY_FAILED",
        "passed": passed,
        "evaluable_blocks": len(evaluable_blocks),
        "blocks_with_both_medians_in_author_bands": median_passes,
        "blocks_with_joint_band_share_ge_0_60": joint_passes,
        "all_evaluable_block_rate_ratios_in_0_25_to_4_0": all_rates,
        "no_block_above_0_40_episode_share": concentration,
        "mechanism_contradiction": contradiction,
        "decisive_block_mechanisms": sorted(decisive),
        "blocks": blocks,
    }


def _cell_status(
    *,
    episodes: Sequence[Episode],
    band_passed: bool,
    stability_passed: bool,
    mechanism: str,
) -> tuple[str, list[str]]:
    long_n = sum(episode.side == "long" for episode in episodes)
    short_n = len(episodes) - long_n
    if len(episodes) < 60 or long_n < 20 or short_n < 20:
        reasons = []
        if len(episodes) < 60:
            reasons.append("TOTAL_EPISODES_LT_60")
        if long_n < 20:
            reasons.append("LONG_EPISODES_LT_20")
        if short_n < 20:
            reasons.append("SHORT_EPISODES_LT_20")
        return "INSUFFICIENT_EVENTS", reasons
    reasons = []
    if not band_passed:
        reasons.append("BAND_SIMILARITY_FAILED")
    if not stability_passed:
        reasons.append("TEMPORAL_STABILITY_FAILED")
    if mechanism in {"SIMPLE_MEAN_REVERSION", "NON_EXECUTABLE_PRINT_ONLY"}:
        reasons.append(mechanism)
    if reasons:
        return "IDENTITY_REJECTED", reasons
    if mechanism == "MECHANISM_UNRESOLVED":
        return "MECHANISM_UNRESOLVED", ["PRIMARY_HORIZON_MECHANISM_GATES_NOT_MET"]
    return "PLAUSIBLE_B", []


def evaluate_cell(rows: list[GridRow], family: Family, lookback_s: int) -> dict[str, Any]:
    features = _feature_rows(rows, family, lookback_s)
    quadrants = quadrant_counts(features)
    episodes = construct_episodes(features)
    long_episodes = [episode for episode in episodes if episode.side == "long"]
    short_episodes = [episode for episode in episodes if episode.side == "short"]
    bands = {
        "overall": _band_summary(episodes),
        "long": _band_summary(long_episodes),
        "short": _band_summary(short_episodes),
    }
    band_passed = _band_pass(bands["overall"], bands["long"], bands["short"])
    rows_by_key = {(row.segment, row.t_ms): row for row in rows}
    horizons, mechanism = _mechanism_bundle(episodes, rows_by_key, family)
    stability = _temporal_stability(episodes, rows, rows_by_key, family)
    status, reasons = _cell_status(
        episodes=episodes,
        band_passed=band_passed,
        stability_passed=bool(stability["passed"]),
        mechanism=mechanism,
    )
    return {
        "cell_id": f"{family.id}__H{lookback_s}S",
        "family_id": family.id,
        "lookback_s": lookback_s,
        "formula_orientation": {
            "mom_kind": family.mom_kind,
            "momentum_source": family.momentum_source,
            "gap": f"bps({family.gap_left},{family.gap_right})",
            "follower": family.follower,
            "reference": family.reference,
        },
        "feature_rows_available": len(rows) - quadrants["unavailable"],
        "quadrant_counts_before_eligibility": quadrants,
        "author_compatible_sign_rule": "long:mom>0,gap<0;short:mom<0,gap>0",
        "magnitude_rule_bps": {"abs_mom_min": 3.0, "abs_gap_min": 1.5},
        "episode_rule": {
            "representative": "first_eligible_row",
            "rearm": "continuously_ineligible_strictly_greater_than_2000ms",
        },
        "total_episodes": len(episodes),
        "long_episodes": len(long_episodes),
        "short_episodes": len(short_episodes),
        "sufficiency": {
            "required_total": 60,
            "required_each_side": 20,
            "passed": len(episodes) >= 60
            and len(long_episodes) >= 20
            and len(short_episodes) >= 20,
        },
        "band_summaries": bands,
        "band_similarity": "BAND_SIMILARITY_PASS" if band_passed else "BAND_SIMILARITY_FAIL",
        "direct_author_match_status": "DIRECT_MATCH_NOT_SCORABLE",
        "direct_author_match_reason": "NO_ADEQUATE_TIMESTAMPED_AUTHOR_ROWS_IN_FROZEN_MANIFEST",
        "target_unit_status": "ALGEBRAIC_DIAGNOSTIC_ONLY_AUTHOR_TARGET_MATCH_NOT_SCORABLE",
        "frequency": _frequency(episodes, rows),
        "temporal_stability": stability,
        "future_response_selection": {
            "rule": "earliest_representative_then_exclude_later_representatives_within_10s",
            "n_input_episodes": len(episodes),
            "n_non_overlapping": len(non_overlapping(episodes)),
        },
        "future_horizons": horizons,
        "primary_horizons_s": list(PRIMARY_HORIZONS_S),
        "mechanism_classification": mechanism,
        "failure_reasons": reasons,
        "exact_rejection_failure_reason": reasons[0] if reasons else "NONE",
        "final_cell_status": status,
    }


def protocol_conclusion(cell_results: Sequence[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    statuses = Counter(str(cell["final_cell_status"]) for cell in cell_results)
    plausible = [
        cell for cell in cell_results if cell["final_cell_status"] in {"PLAUSIBLE_A", "PLAUSIBLE_B"}
    ]
    evaluable = [
        cell
        for cell in cell_results
        if cell["final_cell_status"] not in {"DATA_INADEQUATE", "INSUFFICIENT_EVENTS"}
    ]
    rules = [
        ("DATA_INADEQUATE", statuses["DATA_INADEQUATE"] == len(cell_results)),
        ("IDENTIFICATION_INSUFFICIENT", len(evaluable) == 0),
        ("UNIQUE_IDENTITY_SUPPORTED", statuses["PLAUSIBLE_A"] == 1),
        (
            "PROVISIONAL_UNIQUE_FREQUENCY_UNAVAILABLE",
            statuses["PLAUSIBLE_B"] == 1 and statuses["PLAUSIBLE_A"] == 0,
        ),
        ("UNIQUE_IDENTITY_SUPPORTED_DIRECT_ERROR_DOMINANCE", False),
        (
            "FORMULA_SUPPORTED_LOOKBACK_UNRESOLVED",
            len(plausible) > 1 and len({cell["family_id"] for cell in plausible}) == 1,
        ),
        (
            "PLAUSIBLE_SET_NOT_UNIQUE",
            len({cell["family_id"] for cell in plausible}) > 1,
        ),
        (
            "MEXC_ONLY_FAMILY_REJECTED",
            bool(evaluable)
            and all(cell["final_cell_status"] == "IDENTITY_REJECTED" for cell in evaluable),
        ),
        ("IDENTIFICATION_UNRESOLVED", True),
    ]
    selected: str | None = None
    trace: list[dict[str, Any]] = []
    for order, (rule, matched) in enumerate(rules, start=1):
        evaluated = selected is None
        trace.append(
            {
                "order": order,
                "rule": rule,
                "evaluated": evaluated,
                "matched": bool(matched) if evaluated else None,
            }
        )
        if evaluated and matched:
            selected = (
                "UNIQUE_IDENTITY_SUPPORTED"
                if rule == "UNIQUE_IDENTITY_SUPPORTED_DIRECT_ERROR_DOMINANCE"
                else rule
            )
    assert selected is not None
    return selected, trace


def build_result(
    *,
    repo: Path,
    corpus: Path,
    grid: Path,
    manifest: Path,
    code_commit_sha: str,
) -> dict[str, Any]:
    locks = verify_locks(
        repo=repo,
        corpus=corpus,
        grid=grid,
        manifest=manifest,
        code_commit_sha=code_commit_sha,
    )
    rows = load_grid(grid)
    locked = locks.pop("manifest")
    raw_manifest_hours = (locked.get("admissibility") or {}).get("usable_hours")
    if not isinstance(raw_manifest_hours, int | float) or isinstance(raw_manifest_hours, bool):
        raise LockMismatchError("manifest usable_hours is missing or non-numeric")
    manifest_hours = float(raw_manifest_hours)
    grid_hours = len(rows) * GRID_MS / 3_600_000.0
    if not math.isclose(manifest_hours, grid_hours, rel_tol=0.0, abs_tol=1e-9):
        raise LockMismatchError(
            f"usable hours differ: manifest={manifest_hours}, grid={grid_hours}"
        )

    cells = [
        evaluate_cell(rows, family, lookback_s) for family in FAMILIES for lookback_s in LOOKBACKS_S
    ]
    conclusion, trace = protocol_conclusion(cells)
    all_evaluable_rejected = bool(
        [cell for cell in cells if cell["sufficiency"]["passed"]]
    ) and all(
        cell["final_cell_status"] == "IDENTITY_REJECTED"
        for cell in cells
        if cell["sufficiency"]["passed"]
    )
    capture = locked.get("capture") or {}
    return {
        "milestone": "MEXC_MOM_GAP_PROTOCOL_V2_EXECUTION_V1",
        "status": "MEXC_MOM_GAP_PROTOCOL_V2_EXECUTION_READY",
        "decision": "STOP_FOR_LEAD_REVIEW",
        "protocol_id": "MEXC_MOM_GAP_HYPOTHESIS_PROTOCOL_V2_DATA_CONTRACT_AMENDMENT",
        "protocol_version": "2.0.0",
        "executor_name_and_version": f"{EXECUTOR_ID}/{EXECUTOR_VERSION}",
        "executor_code_commit": code_commit_sha,
        "locks": locks,
        "input_manifest": {
            "path": manifest.as_posix(),
            "quality_status": locked.get("quality_status"),
            "capture_ids": capture.get("capture_ids"),
            "session_ids": capture.get("session_ids"),
            "utc_start": capture.get("utc_start"),
            "utc_end": capture.get("utc_end"),
            "usable_hours": _rounded(grid_hours),
            "grid_rows": len(rows),
            "grid_segments": len({row.segment for row in rows}),
        },
        "fixed_methods": {
            "cell_order": [
                f"{family.id}__H{horizon}S" for family in FAMILIES for horizon in LOOKBACKS_S
            ],
            "percentile_method": "sorted_index_min(n-1,floor(n*q))",
            "wilson_confidence": 0.99,
            "wilson_z": WILSON_Z_99,
            "no_pnl_cost_target_reach_sharpe_win_rate_pvalue_score_ml": True,
        },
        "frequency_reference_status": "FREQUENCY_REFERENCE_UNAVAILABLE",
        "direct_match_status": "DIRECT_MATCH_NOT_SCORABLE",
        "cell_results": cells,
        "cell_status_counts": dict(
            sorted(Counter(cell["final_cell_status"] for cell in cells).items())
        ),
        "protocol_decision_trace": trace,
        "protocol_conclusion": conclusion,
        "external_reference_gate": {
            "state": "MEXC_ONLY_INCONCLUSIVE",
            "independent_admissible_captures_completed": 1,
            "independent_admissible_captures_required": 2,
            "all_evaluable_cells_rejected_this_capture": all_evaluable_rejected,
            "reason": "TWO_INDEPENDENT_ADMISSIBLE_CAPTURE_REPLICATION_RULE_NOT_MET",
            "external_reference_probably_required": False,
        },
        "profile_changed": False,
        "strategy_tuning": False,
        "ml_started": False,
        "paper": False,
        "live": False,
    }


def render_markdown(result: dict[str, Any]) -> str:
    manifest = result["input_manifest"]
    locks = result["locks"]
    lines = [
        "# MEXC mom/gap protocol v2 execution v1",
        "",
        f"STATUS: `{result['status']}`",
        "",
        f"DECISION: `{result['decision']}`",
        "",
        f"PROTOCOL_CONCLUSION: `{result['protocol_conclusion']}`",
        "",
        f"EXTERNAL_REFERENCE_GATE: `{result['external_reference_gate']['state']}`",
        "",
        "ML_STATUS: `NOT_STARTED`",
        "",
        "PAPER: **false**",
        "",
        "LIVE: **false**",
        "",
        "## Locks and corpus",
        "",
        f"- protocol merge SHA: `{locks['protocol_merge_commit']}`",
        f"- amendment commit: `{locks['protocol_amendment_commit']}`",
        f"- amendment blob: `{locks['protocol_amendment_blob']}`",
        f"- corpus SHA-256: `{locks['corpus_sha256']}`",
        f"- frozen grid SHA-256: `{locks['grid_sha256']}`",
        f"- executor code commit: `{result['executor_code_commit']}`",
        f"- usable hours: {manifest['usable_hours']}",
        f"- grid rows / segments: {manifest['grid_rows']} / {manifest['grid_segments']}",
        f"- UTC: `{manifest['utc_start']}` — `{manifest['utc_end']}`",
        f"- quality: `{manifest['quality_status']}`",
        "- all locks verified before feature calculation: **true**",
        "",
        "## Frozen interpretation",
        "",
        "All 21 cells were executed in registered family/lookback order. Formula orientation, "
        "thresholds, episode rearm, six blocks, response horizons, Wilson gates, and conclusion "
        "ordering are unchanged. Percentiles use the repository's deterministic sorted index "
        "`min(n-1, floor(n*q))`. No PnL, target reach, exits, costs, Sharpe, win rate, p-values, "
        "weighted scores, profiles, or ML were read or calculated.",
        "",
        "The frozen evidence manifest has no adequate timestamped individual author rows. "
        "Therefore direct numeric matching is `DIRECT_MATCH_NOT_SCORABLE` and frequency is "
        "`FREQUENCY_REFERENCE_UNAVAILABLE` for every cell.",
        "",
        "## All 21 cell results",
        "",
        "| Cell | Episodes L/S | Band | Stability | Mechanism | Status | Exact reason |",
        "| --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for cell in result["cell_results"]:
        lines.append(
            f"| `{cell['cell_id']}` | {cell['total_episodes']} "
            f"{cell['long_episodes']}/{cell['short_episodes']} | "
            f"`{cell['band_similarity']}` | `{cell['temporal_stability']['status']}` | "
            f"`{cell['mechanism_classification']}` | `{cell['final_cell_status']}` | "
            f"`{cell['exact_rejection_failure_reason']}` |"
        )

    for cell in result["cell_results"]:
        bands = cell["band_summaries"]["overall"]
        frequency = cell["frequency"]
        lines.extend(
            [
                "",
                f"### {cell['cell_id']}",
                "",
                f"- episodes total/long/short: {cell['total_episodes']} / "
                f"{cell['long_episodes']} / {cell['short_episodes']}",
                "- quadrants: `"
                f"{json.dumps(cell['quadrant_counts_before_eligibility'], sort_keys=True)}`",
                f"- abs(mom) p10/p25/p50/p75/p90: "
                f"`{json.dumps(bands['abs_mom_bps']['quantiles'], sort_keys=True)}`",
                f"- abs(gap) p10/p25/p50/p75/p90: "
                f"`{json.dumps(bands['abs_gap_bps']['quantiles'], sort_keys=True)}`",
                f"- mom/gap/joint band shares: "
                f"{bands['abs_mom_bps']['band_share_3_0_to_5_5']} / "
                f"{bands['abs_gap_bps']['band_share_1_5_to_5_2']} / "
                f"{bands['joint_band_share']}",
                f"- rates total/long/short per hour: {frequency['candidate_rate_per_hour']} / "
                f"{frequency['long_rate_per_hour']} / {frequency['short_rate_per_hour']}",
                f"- direct/frequency: `{cell['direct_author_match_status']}` / "
                f"`{frequency['frequency_status']}`",
                f"- non-overlapping representatives: "
                f"{cell['future_response_selection']['n_non_overlapping']}",
                f"- final: `{cell['final_cell_status']}`; reasons: "
                f"`{json.dumps(cell['failure_reasons'])}`",
                "",
                "| Horizon | n | F>0 99% Wilson | C>0 99% Wilson | M>0 99% Wilson | "
                "M<0 99% Wilson | median follower share |",
                "| ---: | ---: | --- | --- | --- | --- | ---: |",
            ]
        )
        for horizon in ("1", "2", "5", "10"):
            diag = cell["future_horizons"][horizon]
            wilson = diag["wilson_99"]
            lines.append(
                f"| {horizon}s | {diag['n_available']} | "
                f"{wilson['follower_positive']['estimate']} "
                f"[{wilson['follower_positive']['lower']}, "
                f"{wilson['follower_positive']['upper']}] | "
                f"{wilson['gap_closure_positive']['estimate']} "
                f"[{wilson['gap_closure_positive']['lower']}, "
                f"{wilson['gap_closure_positive']['upper']}] | "
                f"{wilson['momentum_continuation_positive']['estimate']} "
                f"[{wilson['momentum_continuation_positive']['lower']}, "
                f"{wilson['momentum_continuation_positive']['upper']}] | "
                f"{wilson['momentum_reversal_negative']['estimate']} "
                f"[{wilson['momentum_reversal_negative']['lower']}, "
                f"{wilson['momentum_reversal_negative']['upper']}] | "
                f"{diag['median_follower_share_on_closure']} |"
            )
        lines.extend(
            [
                "",
                "| Block | Episodes | Evaluable | Mom+gap medians in band | Joint share | "
                "Rate ratio | Mechanism |",
                "| ---: | ---: | --- | --- | ---: | ---: | --- |",
            ]
        )
        for block in cell["temporal_stability"]["blocks"]:
            medians = block["mom_median_in_author_band"] and block["gap_median_in_author_band"]
            lines.append(
                f"| {block['block']} | {block['total_episodes']} | {block['evaluable']} | "
                f"{medians} | {block['joint_band_share']} | {block['rate_ratio_to_full']} | "
                f"`{block['mechanism_classification']}` |"
            )

    lines.extend(
        [
            "",
            "## Exact protocol decision trace",
            "",
            "| Order | Rule | Evaluated | Matched |",
            "| ---: | --- | --- | --- |",
        ]
    )
    for step in result["protocol_decision_trace"]:
        lines.append(
            f"| {step['order']} | `{step['rule']}` | {step['evaluated']} | {step['matched']} |"
        )
    external = result["external_reference_gate"]
    lines.extend(
        [
            "",
            "## Final conclusion",
            "",
            f"Protocol conclusion: `{result['protocol_conclusion']}`.",
            "",
            f"External-reference state: `{external['state']}`. This is only one independent "
            f"admissible capture ({external['independent_admissible_captures_completed']} of "
            f"{external['independent_admissible_captures_required']}); protocol section 10 does "
            "not permit `EXTERNAL_REFERENCE_PROBABLY_REQUIRED` from this execution alone.",
            "",
            "`STOP_FOR_LEAD_REVIEW`",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(result: dict[str, Any], *, out_json: Path, out_md: Path) -> None:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(result, indent=2, sort_keys=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    out_md.write_text(render_markdown(result), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute frozen MEXC mom/gap protocol v2")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--grid", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--code-commit", required=True)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path, required=True)
    args = parser.parse_args(argv)
    result = build_result(
        repo=args.repo,
        corpus=args.corpus,
        grid=args.grid,
        manifest=args.manifest,
        code_commit_sha=args.code_commit,
    )
    write_reports(result, out_json=args.out_json, out_md=args.out_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
