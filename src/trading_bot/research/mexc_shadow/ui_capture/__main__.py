"""CLI: local capture ingest / smoke replay / quality. No network, no orders."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.long_report import build_milestone_report
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
    smoke.add_argument(
        "--hypothesis-smoke",
        action="store_true",
        help="Label the replay HYPOTHESIS_SMOKE (not performance evidence)",
    )

    long_report = sub.add_parser(
        "long-report",
        help="Phase A gates plus descriptive long-capture stats. No retune.",
    )
    long_report.add_argument("--raw", type=Path, default=None)
    long_report.add_argument("--phase-b-raw", type=Path, default=None)
    long_report.add_argument("--out", type=Path, required=True)
    long_report.add_argument("--screenshot-agreement", default="NOT_VERIFIED")
    long_report.add_argument("--restart-attested", action="store_true")

    long_obs = sub.add_parser(
        "long-observation",
        help="8-12h TAOUSDT quality + descriptive stats. No retune.",
    )
    long_obs.add_argument("--raw", type=Path, required=True)
    long_obs.add_argument("--out", type=Path, required=True)
    long_obs.add_argument("--md", type=Path, default=None)

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
    if args.cmd == "long-report":
        payload = build_milestone_report(
            args.raw,
            phase_b_path=args.phase_b_raw,
            screenshot_agreement=args.screenshot_agreement,
            restart_attested=args.restart_attested,
        )
        args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        return 0
    if args.cmd == "long-observation":
        from trading_bot.research.mexc_shadow.ui_capture.long_observation import (
            write_long_observation_reports,
        )

        md_path = args.md if args.md is not None else args.out.with_suffix(".md")
        write_long_observation_reports(args.raw, out_json=args.out, out_md=md_path)
        return 0
    report = replay_capture_smoke(
        args.raw,
        args.profile,
        hypothesis_smoke=args.hypothesis_smoke,
    )
    payload = {
        "profile_id": report.profile_id,
        "observations": report.observations,
        "n_candidates": len(report.candidates),
        "n_trades": len(report.trades),
        "n_open": report.n_open,
        "notes": list(report.notes),
        "pipeline_smoke_only": True,
        "hypothesis_smoke": bool(args.hypothesis_smoke),
    }
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
