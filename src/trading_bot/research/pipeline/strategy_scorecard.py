"""Comparable strategy-class scorecard for STRATEGY_SPACE_RETHINK."""

from __future__ import annotations

from typing import Any


def build_strategy_scorecard(
    *,
    opportunity: dict[str, Any],
    basis: dict[str, Any],
    funding: dict[str, Any],
    liquidity: dict[str, Any],
    volatility: dict[str, Any],
    external: dict[str, Any],
) -> list[dict[str, Any]]:
    """Rank by falsifiability and realistic path to positive economics."""

    # Extract coarse signals for decisions.
    non_ov = opportunity.get("non_overlapping_stride") or {}
    frac_60_10 = ((non_ov.get("60s") or {}).get("frac_ge_threshold") or {}).get("10")
    frac_300_10 = ((non_ov.get("300s") or {}).get("frac_ge_threshold") or {}).get("10")
    frac_1800_10 = ((non_ov.get("1800s") or {}).get("frac_ge_threshold") or {}).get(
        "10"
    )

    basis_exec = basis.get("executable_fade_proxy") or {}
    basis_300 = (basis_exec.get("300s") or {}).get("mean_signed_executable_mid_bps")
    basis_be = (basis.get("break_even") or {}).get("required_move_bps") or 11.0

    fund_8h = ((funding.get("carry_vs_costs") or {}).get("8h") or {})
    fund_covers = bool(fund_8h.get("covers_taker_rt_fees_alone"))
    fund_carry = fund_8h.get("expected_abs_carry_bps")

    # Best liquidity event by post-60s mean abs move among classes with n>=20.
    # p95 alone is not an economic edge estimate.
    best_liq_name = None
    best_liq_mean = None
    best_liq_p95 = None
    best_liq_n = 0
    for name, payload in (liquidity.get("events") or {}).items():
        abs60 = ((payload.get("forward") or {}).get("60s") or {}).get("abs") or {}
        n = int(abs60.get("n") or 0)
        mean = abs60.get("mean_bps")
        p95 = abs60.get("p95_bps")
        if n >= 20 and mean is not None and (
            best_liq_mean is None or float(mean) > float(best_liq_mean)
        ):
            best_liq_mean = float(mean)
            best_liq_p95 = float(p95) if p95 is not None else None
            best_liq_name = name
            best_liq_n = n
    liq_be = (liquidity.get("break_even_60s_taker") or {}).get(
        "required_move_bps"
    ) or 11.0

    stage1 = volatility.get("stage1_opportunity_prevalence_nonoverlap") or {}
    prev_60: dict[str, Any] = {}
    prev_300: dict[str, Any] = {}
    for key, val in stage1.items():
        thr = float(val.get("threshold_bps") or 0)
        if key.startswith("60s_ge_") and thr < 15:
            prev_60 = val
        if key.startswith("300s_ge_") and thr < 15:
            prev_300 = val
    vol_trans = volatility.get("low_to_high_transitions") or {}
    vol_p95 = ((vol_trans.get("post_300s_abs_moves") or {}).get("p95_bps"))
    vol_mean = ((vol_trans.get("post_300s_abs_moves") or {}).get("mean_bps"))

    # Decisions
    basis_decision = "REJECT_FOR_NOW"
    if (
        basis_300 is not None
        and abs(float(basis_300)) > 0.5 * float(basis_be)
        and int((basis_exec.get("300s") or {}).get("n") or 0) >= 50
    ):
        basis_decision = "WATCH"
    if (
        basis_300 is not None
        and float(basis_300) > float(basis_be)
        and int((basis_exec.get("300s") or {}).get("n") or 0) >= 100
    ):
        basis_decision = "PRIORITIZE"

    funding_decision = "REJECT_FOR_NOW"
    if fund_covers and fund_carry is not None and float(fund_carry) > 5.0:
        funding_decision = "WATCH"
    # Still not prioritize without hedgeable second leg.

    liq_decision = "REJECT_FOR_NOW"
    if best_liq_mean is not None and best_liq_n >= 30:
        if best_liq_mean > liq_be:
            liq_decision = "PRIORITIZE"
        elif best_liq_mean > 0.5 * liq_be or (
            best_liq_p95 is not None and best_liq_p95 > liq_be
        ):
            liq_decision = "WATCH"

    vol_decision = "REJECT_FOR_NOW"
    prevalence_60 = prev_60.get("prevalence")
    prevalence_300 = prev_300.get("prevalence")
    lifts = volatility.get("stage1_precursor_lift") or {}
    strong_lift = any(
        (item.get("lift") or 0) >= 1.5 and (item.get("n_high") or 0) >= 30
        for item in lifts.values()
    )
    # Opportunities exist at 5m often enough to WATCH a stage-1 framing, but
    # require precursor lift before PRIORITIZE.
    if prevalence_300 is not None and float(prevalence_300) >= 0.10:
        vol_decision = "WATCH"
    if (
        strong_lift
        and prevalence_60 is not None
        and float(prevalence_60) >= 0.02
        and (vol_mean is not None and float(vol_mean) > 0.5 * float(liq_be))
    ):
        vol_decision = "PRIORITIZE"

    # Larger-move classification is tied to opportunity target.
    opp_class_decision = vol_decision

    # External relative value: design-time judgment from opportunity ceiling
    # and Hibachi-only failure of short-horizon directional edge.
    external_decision = external.get("recommended_decision", "WATCH")

    rejected_short = {
        "STRATEGY_CLASS": "SHORT_HORIZON_DIRECTIONAL_MICROSTRUCTURE",
        "ECONOMIC_MECHANISM": (
            "Predict short-horizon ETH perp mid direction from Hibachi "
            "microstructure (flow/OFI/imbalance)."
        ),
        "EXISTING_DATA_SUPPORT": "FULL",
        "NEW_PUBLIC_DATA_REQUIRED": "no",
        "EXECUTION_OBSERVABILITY": "HIGH",
        "EXPECTED_TURNOVER": "very_high",
        "BREAK_EVEN_FRICTION": 11.05,
        "EXPLORATORY_GROSS_OPPORTUNITY": 2.3,
        "SAMPLE_SIZE": "adequate_exploratory_extreme_n~760",
        "MAIN_RISK": "gross << friction; maker fills adversely selected",
        "OVERFIT_RISK": "HIGH if thresholds retuned",
        "INFRA_COST": "LOW",
        "RESEARCH_VALUE": "NEGATIVE_RESULT_DURABLE",
        "DECISION": "REJECT_FOR_NOW",
        "evidence_note": "Prior milestone EXECUTION_AND_HORIZON_REASSESSMENT",
    }

    cards: list[dict[str, Any]] = [
        rejected_short,
        {
            "STRATEGY_CLASS": "BASIS_DISLOCATION_MEAN_REVERSION",
            "ECONOMIC_MECHANISM": (
                "Fade temporary Hibachi mark/spot vs executable mid dislocations."
            ),
            "EXISTING_DATA_SUPPORT": "PARTIAL",
            "NEW_PUBLIC_DATA_REQUIRED": "no",
            "EXECUTION_OBSERVABILITY": "MEDIUM",
            "EXPECTED_TURNOVER": "medium",
            "BREAK_EVEN_FRICTION": basis_be,
            "EXPLORATORY_GROSS_OPPORTUNITY": basis_300,
            "SAMPLE_SIZE": (basis_exec.get("300s") or {}).get("n"),
            "MAIN_RISK": "mark/spot may be non-executable reference mechanics",
            "OVERFIT_RISK": "MEDIUM",
            "INFRA_COST": "LOW",
            "RESEARCH_VALUE": "MEDIUM",
            "DECISION": basis_decision,
            "evidence_note": basis.get("limitation"),
        },
        {
            "STRATEGY_CLASS": "FUNDING_CARRY",
            "ECONOMIC_MECHANISM": (
                "Earn funding on one-sided or hedged perp exposure over hours."
            ),
            "EXISTING_DATA_SUPPORT": "PARTIAL",
            "NEW_PUBLIC_DATA_REQUIRED": "yes_for_hedged_carry",
            "EXECUTION_OBSERVABILITY": "LOW",
            "EXPECTED_TURNOVER": "low",
            "BREAK_EVEN_FRICTION": (funding.get("break_even_8h") or {}).get(
                "required_move_bps"
            ),
            "EXPLORATORY_GROSS_OPPORTUNITY": fund_carry,
            "SAMPLE_SIZE": (funding.get("funding_rate") or {}).get("n"),
            "MAIN_RISK": "unhedged directional risk; units/settlement uncertainty",
            "OVERFIT_RISK": "LOW",
            "INFRA_COST": "MEDIUM",
            "RESEARCH_VALUE": "LOW_WITHOUT_HEDGE",
            "DECISION": funding_decision,
            "evidence_note": funding.get("hedge_note"),
        },
        {
            "STRATEGY_CLASS": "RARE_LIQUIDITY_DISLOCATION_EVENTS",
            "ECONOMIC_MECHANISM": (
                "Trade sparse causal liquidity shocks (spread/depth/OFI/burst) "
                "for post-event executable moves or reversion."
            ),
            "EXISTING_DATA_SUPPORT": "FULL",
            "NEW_PUBLIC_DATA_REQUIRED": "no",
            "EXECUTION_OBSERVABILITY": "MEDIUM",
            "EXPECTED_TURNOVER": "low_sparse",
            "BREAK_EVEN_FRICTION": liq_be,
            "EXPLORATORY_GROSS_OPPORTUNITY": {
                "best_event": best_liq_name,
                "mean_abs_60s_bps": best_liq_mean,
                "p95_abs_60s_bps": best_liq_p95,
            },
            "SAMPLE_SIZE": best_liq_n,
            "MAIN_RISK": "event rarity; adverse selection; thin activity regime",
            "OVERFIT_RISK": "MEDIUM",
            "INFRA_COST": "LOW",
            "RESEARCH_VALUE": "MEDIUM",
            "DECISION": liq_decision,
            "evidence_note": f"best_event={best_liq_name}",
        },
        {
            "STRATEGY_CLASS": "VOLATILITY_REGIME_OR_OPPORTUNITY_TARGET",
            "ECONOMIC_MECHANISM": (
                "Stage-1: detect when |executable move| can exceed costs; "
                "optional Stage-2 direction only conditional on opportunity."
            ),
            "EXISTING_DATA_SUPPORT": "FULL",
            "NEW_PUBLIC_DATA_REQUIRED": "no",
            "EXECUTION_OBSERVABILITY": "MEDIUM",
            "EXPECTED_TURNOVER": "selective",
            "BREAK_EVEN_FRICTION": (volatility.get("break_even_60s") or {}).get(
                "required_move_bps"
            ),
            "EXPLORATORY_GROSS_OPPORTUNITY": {
                "stage1_60s_prevalence": prevalence_60,
                "stage1_300s_prevalence": prevalence_300,
                "vol_transition_post_300s_mean": vol_mean,
                "vol_transition_post_300s_p95": vol_p95,
                "nonoverlap_frac_ge_10bps": {
                    "60s": frac_60_10,
                    "300s": frac_300_10,
                    "1800s": frac_1800_10,
                },
            },
            "SAMPLE_SIZE": {
                "stage1_60s_n": prev_60.get("n"),
                "stage1_300s_n": prev_300.get("n"),
            },
            "MAIN_RISK": "opportunities may be unpredictable or too rare",
            "OVERFIT_RISK": "MEDIUM",
            "INFRA_COST": "LOW",
            "RESEARCH_VALUE": "HIGH_IF_PRECURSORS_EXIST",
            "DECISION": opp_class_decision,
            "evidence_note": volatility.get("note"),
        },
        {
            "STRATEGY_CLASS": "EXTERNAL_RELATIVE_VALUE_LEAD_LAG",
            "ECONOMIC_MECHANISM": (
                "External liquid ETH venue discovers price; Hibachi temporarily "
                "lags; trade convergence on Hibachi executable quotes."
            ),
            "EXISTING_DATA_SUPPORT": "NO",
            "NEW_PUBLIC_DATA_REQUIRED": "yes",
            "EXECUTION_OBSERVABILITY": "HIGH_IF_FEED_ADDED",
            "EXPECTED_TURNOVER": "medium",
            "BREAK_EVEN_FRICTION": external.get("indicative_break_even_bps"),
            "EXPLORATORY_GROSS_OPPORTUNITY": external.get(
                "expected_gross_opportunity_note"
            ),
            "SAMPLE_SIZE": "n/a_no_external_feed_yet",
            "MAIN_RISK": "Hibachi may not lag; latency/sync; storage; isolation",
            "OVERFIT_RISK": "MEDIUM",
            "INFRA_COST": "MEDIUM_HIGH",
            "RESEARCH_VALUE": "HIGH_GIVEN_HIBACHI_ONLY_CEILING",
            "DECISION": external_decision,
            "evidence_note": external.get("summary"),
        },
        {
            "STRATEGY_CLASS": "CROSS_VENUE_BASIS_OR_CARRY",
            "ECONOMIC_MECHANISM": (
                "Hibachi perp vs external spot/perp basis or funding differential "
                "(statistical RV; arb only if both legs executable)."
            ),
            "EXISTING_DATA_SUPPORT": "NO",
            "NEW_PUBLIC_DATA_REQUIRED": "yes",
            "EXECUTION_OBSERVABILITY": "LOW_UNTIL_TWO_LEG",
            "EXPECTED_TURNOVER": "low_to_medium",
            "BREAK_EVEN_FRICTION": external.get("two_leg_break_even_bps"),
            "EXPLORATORY_GROSS_OPPORTUNITY": "unknown_without_external",
            "SAMPLE_SIZE": "n/a",
            "MAIN_RISK": "calling arb without hedge; transfer/latency risk",
            "OVERFIT_RISK": "MEDIUM",
            "INFRA_COST": "HIGH",
            "RESEARCH_VALUE": "MEDIUM_AFTER_FEED",
            "DECISION": (
                "WATCH"
                if external_decision in {"PRIORITIZE", "WATCH"}
                else "REJECT_FOR_NOW"
            ),
            "evidence_note": "Depends on external public feed pilot; not arb by default.",
        },
    ]

    rank_order = {"PRIORITIZE": 0, "WATCH": 1, "REJECT_FOR_NOW": 2}
    cards.sort(
        key=lambda c: (
            rank_order.get(str(c["DECISION"]), 9),
            str(c["STRATEGY_CLASS"]),
        )
    )
    for idx, card in enumerate(cards, start=1):
        card["rank"] = idx
    return cards


def recommend_milestone_decision(scorecard: list[dict[str, Any]]) -> dict[str, Any]:
    """Map scorecard to milestone DECISION + recommended hypothesis."""

    def is_existing(card: dict[str, Any]) -> bool:
        return bool(
            card["NEW_PUBLIC_DATA_REQUIRED"] == "no"
            and card["STRATEGY_CLASS"] != "SHORT_HORIZON_DIRECTIONAL_MICROSTRUCTURE"
        )

    def is_external_family(card: dict[str, Any]) -> bool:
        return bool(
            card["STRATEGY_CLASS"]
            in {
                "EXTERNAL_RELATIVE_VALUE_LEAD_LAG",
                "CROSS_VENUE_BASIS_OR_CARRY",
            }
        )

    prioritize = [c for c in scorecard if c["DECISION"] == "PRIORITIZE"]
    watch = [c for c in scorecard if c["DECISION"] == "WATCH"]
    existing_p = [c for c in prioritize if is_existing(c)]
    external_p = [c for c in prioritize if is_external_family(c)]
    existing_w = [c for c in watch if is_existing(c)]
    external_w = [c for c in watch if is_external_family(c)]

    if existing_p:
        primary = existing_p[0]
        return {
            "DECISION": "PRIORITIZE_EXISTING_DATA_STRATEGY",
            "RECOMMENDED_HYPOTHESIS": primary["STRATEGY_CLASS"],
            "NEXT": (
                "pre-register one narrowly defined strategy hypothesis and validate "
                "it without ML on fresh chronological data"
            ),
        }
    # External design outranks weak existing WATCH when Hibachi-only ceiling is tight.
    if external_p or (
        any(
            c["STRATEGY_CLASS"] == "EXTERNAL_RELATIVE_VALUE_LEAD_LAG"
            and c["DECISION"] == "PRIORITIZE"
            for c in scorecard
        )
    ):
        return {
            "DECISION": "DESIGN_EXTERNAL_FEED_PILOT",
            "RECOMMENDED_HYPOTHESIS": "EXTERNAL_RELATIVE_VALUE_LEAD_LAG",
            "NEXT": (
                "review and approve a bounded isolated public "
                "external-market-data collector before implementation"
            ),
        }
    if existing_w:
        return {
            "DECISION": "COLLECT_MORE_BEFORE_DECIDING",
            "RECOMMENDED_HYPOTHESIS": existing_w[0]["STRATEGY_CLASS"],
            "NEXT": (
                "continue collection while preserving the current negative results; "
                "do not expand the strategy search space"
            ),
        }
    if external_w:
        return {
            "DECISION": "DESIGN_EXTERNAL_FEED_PILOT",
            "RECOMMENDED_HYPOTHESIS": "EXTERNAL_RELATIVE_VALUE_LEAD_LAG",
            "NEXT": (
                "review and approve a bounded isolated public "
                "external-market-data collector before implementation"
            ),
        }
    return {
        "DECISION": "NO_PROMISING_STRATEGY_CLASS",
        "RECOMMENDED_HYPOTHESIS": "NONE",
        "NEXT": (
            "reassess market choice and project objectives before further "
            "strategy implementation"
        ),
    }
