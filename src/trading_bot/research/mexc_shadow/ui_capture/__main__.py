"""CLI: local capture ingest / smoke replay / quality. No network, no orders."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from trading_bot.research.mexc_shadow.ui_capture.extract import extract_html
from trading_bot.research.mexc_shadow.ui_capture.locale_remediation import (
    write_reports,
)
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

    locale_fix = sub.add_parser(
        "locale-remediation",
        help="Score locale/header/wrapper semantics. Does not retune mom/gap.",
    )
    locale_fix.add_argument(
        "--raw",
        type=Path,
        default=None,
        help="Optional 5-15 min ru-RU unpacked-extension NDJSON",
    )
    locale_fix.add_argument("--out", type=Path, required=True)
    locale_fix.add_argument("--md", type=Path, default=None)
    locale_fix.add_argument(
        "--historical",
        type=Path,
        default=Path("data/mexc_ui_capture")
        / "mexc_ui_capture_sessions_2026-09-03T04-12-21-619Z.ndjson",
        help="11.67h corpus classified as CAPTURE_INFRASTRUCTURE_EVIDENCE",
    )

    final_gate = sub.add_parser(
        "final-gate",
        help="Score 5-15 min 1.3.2 locale/header gate. Does not retune mom/gap.",
    )
    # Operator NDJSON only. Does not launch a long capture or hypothesis replay.
    final_gate.add_argument("--raw", type=Path, required=True)
    final_gate.add_argument("--out", type=Path, required=True)
    final_gate.add_argument("--md", type=Path, default=None)

    v2_gate = sub.add_parser(
        "v2-contract-gate",
        help="Score 5-15 min capture against protocol v2.0.0 data contract.",
    )
    # Operator NDJSON only. Does not execute mom/gap cells or start a long capture.
    v2_gate.add_argument("--raw", type=Path, required=True)
    v2_gate.add_argument("--out", type=Path, required=True)
    v2_gate.add_argument("--md", type=Path, default=None)

    forensics = sub.add_parser(
        "cadence-forensics",
        help="Forensics on interval cadence. Does not change capture or protocol.",
    )
    forensics.add_argument("--raw", type=Path, required=True)
    forensics.add_argument("--out", type=Path, required=True)
    forensics.add_argument("--md", type=Path, default=None)

    stage_diag = sub.add_parser(
        "stage-diagnostics",
        help="Stage-diagnostics contract report. Optional capture summary. No retune.",
    )
    stage_diag.add_argument("--out", type=Path, required=True)
    stage_diag.add_argument("--md", type=Path, default=None)
    stage_diag.add_argument(
        "--raw",
        type=Path,
        default=None,
        help="Optional NDJSON with stage_diagnostics; live capture is not required",
    )

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
    if args.cmd == "locale-remediation":
        md_path = args.md if args.md is not None else args.out.with_suffix(".md")
        write_reports(
            out_json=args.out,
            out_md=md_path,
            short_raw=args.raw,
            historical_raw=args.historical,
        )
        return 0
    if args.cmd == "final-gate":
        from trading_bot.research.mexc_shadow.ui_capture.final_gate import (
            write_reports as write_final_gate,
        )

        md_path = args.md if args.md is not None else args.out.with_suffix(".md")
        write_final_gate(raw=args.raw, out_json=args.out, out_md=md_path)
        return 0
    if args.cmd == "v2-contract-gate":
        from trading_bot.research.mexc_shadow.ui_capture.v2_contract_gate import (
            write_reports as write_v2_contract_gate,
        )

        md_path = args.md if args.md is not None else args.out.with_suffix(".md")
        write_v2_contract_gate(raw=args.raw, out_json=args.out, out_md=md_path)
        return 0
    if args.cmd == "cadence-forensics":
        from trading_bot.research.mexc_shadow.ui_capture.cadence_forensics import (
            write_reports as write_cadence_forensics,
        )

        md_path = args.md if args.md is not None else args.out.with_suffix(".md")
        write_cadence_forensics(raw=args.raw, out_json=args.out, out_md=md_path)
        return 0
    if args.cmd == "stage-diagnostics":
        from trading_bot.research.mexc_shadow.ui_capture import (
            stage_diagnostics as stage_diag_mod,
        )

        md_path = args.md if args.md is not None else args.out.with_suffix(".md")
        stage_report = stage_diag_mod.write_reports(out_json=args.out, out_md=md_path)
        if args.raw is not None:
            stage_report["capture_summary"] = (
                stage_diag_mod.summarize_capture_stage_diagnostics(args.raw)
            )
            args.out.write_text(json.dumps(stage_report, indent=2) + "\n", encoding="utf-8")
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
