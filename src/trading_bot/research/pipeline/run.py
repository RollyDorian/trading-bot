"""End-to-end offline research pipeline runner (local workspace only)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.research.pipeline import RESEARCH_PIPELINE_NAME, RESEARCH_PIPELINE_VERSION
from trading_bot.research.pipeline.baselines import (
    MarketStateBaselineConfig,
    replay_market_state_baseline,
)
from trading_bot.research.pipeline.features import write_features_v1, write_labels_v1
from trading_bot.research.pipeline.market_state import build_market_state_1s
from trading_bot.research.pipeline.normalize_offline import (
    NormalizeStats,
    normalize_events_parquet,
)
from trading_bot.research.replay import CostConfig


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_research_pipeline_v1(
    *,
    events_parquet: Path,
    workspace: Path,
    source_dataset_id: str,
    source_evidence: dict[str, Any] | None = None,
    symbol: str = "ETH/USDT-P",
) -> dict[str, Any]:
    """Build normalized → market_state_1s → features/labels → baselines.

    Never writes to production PostgreSQL or mutates B2.
    """

    workspace.mkdir(parents=True, exist_ok=True)
    normalized_dir = workspace / "normalized_events"
    market_state_path = workspace / "market_state_1s" / "market_state_1s.parquet"
    features_path = workspace / "features" / "features_v1.parquet"
    labels_path = workspace / "labels" / "labels_v1.parquet"
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    norm_stats: NormalizeStats = normalize_events_parquet(events_parquet, normalized_dir)
    ms_stats = build_market_state_1s(normalized_dir, market_state_path, symbol=symbol)
    feat_stats = write_features_v1(market_state_path, features_path)
    label_stats = write_labels_v1(market_state_path, labels_path)

    costs = CostConfig()
    baselines = []
    for name in ("momentum", "mean_reversion", "imbalance"):
        report = replay_market_state_baseline(
            market_state_path,
            signal=MarketStateBaselineConfig(name=name),
            costs=costs,
        )
        summary = {k: v for k, v in report.items() if k != "trade_details"}
        baselines.append(summary)
        (reports_dir / f"baseline_{name}.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )

    artifacts = {
        "events_parquet": str(events_parquet),
        "normalized_dir": str(normalized_dir),
        "market_state_1s": str(market_state_path),
        "features_v1": str(features_path),
        "labels_v1": str(labels_path),
    }
    hashes = {
        key: _sha256_file(Path(path))
        for key, path in artifacts.items()
        if key != "normalized_dir" and Path(path).is_file()
    }
    for parquet in sorted(normalized_dir.glob("*.parquet")):
        hashes[f"normalized/{parquet.name}"] = _sha256_file(parquet)

    config = {
        "research_pipeline": RESEARCH_PIPELINE_NAME,
        "research_pipeline_version": RESEARCH_PIPELINE_VERSION,
        "symbol": symbol,
        "costs": asdict(costs),
        "source_dataset_id": source_dataset_id,
        "source_evidence": source_evidence or {},
    }
    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()

    manifest = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "config": config,
        "config_hash": config_hash,
        "normalization": asdict(norm_stats),
        "market_state_1s": ms_stats,
        "features_v1": feat_stats,
        "labels_v1": label_stats,
        "baselines": baselines,
        "artifacts": artifacts,
        "artifact_sha256": hashes,
        "splits": {
            "design": "chronological train/validation/OOS; no row shuffle",
            "recommendation": (
                "use prior_continuous for train/val exploration; "
                "hold g_7471913_7871913 (or later) for final OOS"
            ),
            "purge_embargo": "required around label horizons (5–60s) for walk-forward",
        },
    }
    manifest_path = workspace / "dataset_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    manifest["manifest_path"] = str(manifest_path)
    manifest["manifest_sha256"] = _sha256_file(manifest_path)
    return manifest
