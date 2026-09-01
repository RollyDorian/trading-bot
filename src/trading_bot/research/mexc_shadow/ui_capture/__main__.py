"""CLI: local capture ingest / smoke replay / quality. No network, no orders."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.quality import quality_as_dict, summarize_capture
from trading_bot.research.mexc_shadow.ui_capture.replay import replay_capture_smoke
from trading_bot.research.mexc_shadow.ui_capture.store import append_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only MEXC UI capture tools. Does not place orders."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    extract = sub.add_parser("extract-html", help="Parse a synthetic HTML fixture")
    extract.add_argument("--html", type=Path, required=True)
    extract.add_argument("--out", type=Path, required=True)
    extract.add_argument("--received-at", required=True)
    extract.add_argument("--sequence", type=int, default=1)

    quality = sub.add_parser("quality", help="Summarize an NDJSON capture")
    quality.add_argument("--raw", type=Path, required=True)
    quality.add_argument("--out", type=Path, required=True)

    smoke = sub.add_parser("replay-smoke", help="Frozen-profile pipeline smoke")
    smoke.add_argument("--raw", type=Path, required=True)
    smoke.add_argument("--profile", default="author_observed_v0")
    smoke.add_argument("--out", type=Path, required=True)

    args = parser.parse_args(argv)
    if args.cmd == "extract-html":
        html = args.html.read_text(encoding="utf-8")
        snap = extract_html(
            html,
            received_at_local=args.received_at,
            sequence=args.sequence,
        )
        append_snapshot(args.out, snap)
        return 0
    if args.cmd == "quality":
        payload = quality_as_dict(summarize_capture(args.raw))
        args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return 0
    report = replay_capture_smoke(args.raw, args.profile)
    payload = {
        "profile_id": report.profile_id,
        "observations": report.observations,
        "n_candidates": len(report.candidates),
        "n_trades": len(report.trades),
        "n_open": report.n_open,
        "notes": list(report.notes),
        "pipeline_smoke_only": True,
    }
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
