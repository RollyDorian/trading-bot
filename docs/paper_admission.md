# Paper admission research policy

Paper admission is a deterministic research gate. It is not proof of profitability,
does not predict future returns, and does not authorize execution. `BOT_MODE=collect`
remains the only supported runtime mode.

## Artifact and data policy

A dataset is eligible only when its manifest and checksums validate, it is non-empty,
and its current quality report has status `pass`. Admission also requires a compatible
cost-aware `offline_replay.json` whose dataset and configuration identities match.
Missing, unreadable, invalid, stale, or incompatible required artifacts fail closed.
The fee-free momentum evaluation is never an admission input.

Dataset arguments are ordered chronologically. The command assigns the leading datasets
to training, the next trailing partition to validation, and the final trailing partition
to out-of-sample (OOS). Ranges must be strictly chronological and non-overlapping.
Parameter selection may use training and validation data only; final OOS data must not be
used to select parameters or acceptance thresholds. Only OOS trades are aggregated for
the performance decision.

## Default acceptance policy

The typed defaults live only in `AdmissionThresholds` in
`src/trading_bot/research/admission.py`. They are conservative research placeholders,
not promises of profit:

- At least 3 quality-passing datasets overall.
- At least 2 OOS datasets.
- At least 30 aggregate OOS simulated trades.
- Aggregate OOS maximum drawdown no greater than 100 report-currency units.
- Positive aggregate OOS net PnL after modeled fees, funding, slippage, and latency.
- OOS coverage across at least 2 distinct UTC days.
- Every required artifact must validate and all replay configuration hashes must match.

Aggregate win rate is computed from aggregate wins and trades. Aggregate maximum
drawdown is computed from the chronological sequence of raw OOS trade net results; it is
never averaged from per-dataset drawdowns. If raw trades are unavailable or inconsistent,
admission fails closed.

## Human decision boundary

No result automatically transitions the service to PAPER. Even when every criterion
passes, the report means only: “Research criteria passed — manual review required; PAPER
remains disabled.” A human must explicitly review data provenance, splits, assumptions,
cost models, stability, operational controls, and the report before making any future
paper-mode decision. Such a decision requires separate implementation and acceptance
work; this gate never changes configuration or runtime mode.
