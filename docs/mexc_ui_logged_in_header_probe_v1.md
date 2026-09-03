# MEXC logged-in market-header probe v1

STATUS: `MEXC_UI_LOGGED_IN_HEADER_PROBE_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

EXTENSION: `1.3.1`

PAPER: **false**

LIVE: **false**

STRATEGY_TUNING: **false**

## Purpose

Collect enough bounded structural evidence from the rendered logged-in MEXC
market header to identify the exact Fair/Index/Funding title/value layout.
The final extractor is intentionally unchanged because no logged-in structural
fixture yet proves its classes.

## Probe contract

Only visible elements already matching the configured `contractDetail` +
`commonItem` market-header item are inspected. At most 12 items are retained.
Each item contains capped class strings/tokens, at most 8 direct-child summaries,
at most 24 relevant descendant class tokens, at most 16 visible text tokens,
and at most 8 allowlisted attribute records (`title`, `aria-label`,
`aria-labelledby`, `data-title`, `data-tooltip`, `data-original-title`, `role`).
It also records whether the current `itemTitle` and `itemContent` tokens matched.

No HTML, `outerHTML`, full-page DOM, or arbitrary attributes are recorded.
Subtrees whose class, allowlisted attributes, or direct text indicate account,
balance, wallet, position, order form/history, margin, assets, equity,
credentials, UID, or email are pruned and represented only as redacted children.
The serialized schema repeats the same caps and allowlist.

The content script emits the probe on the first accepted snapshot of a capture
session and again only when its value-independent structural signature changes.
Ordinary numeric market updates do not duplicate it. A structural change can
force a diagnostic snapshot even when the market-value key is unchanged.

## Extraction boundary

Within an already-matched market-header item, generic label fallback is disabled
for mark/index/funding. Only the currently registered `itemTitle`/`itemContent`
structure may decode those fields. Title/value mismatch and unknown-title cases
therefore remain fail-closed. No aliases were added, and no value is read from
account, position, order, or order-form UI.

## Verification fixtures

- current public ru-RU header structure;
- known title with title-class mismatch;
- known title with value-class mismatch;
- unknown title with current title/value classes;
- 14-item truncation, string/array bounds, and private-subtree redaction;
- once-per-session emission, numeric-change suppression, structural-change
  re-emission, and new-session re-emission.

## Lead-review next step

After review, reload extension 1.3.1 and run one short logged-in
`/ru-RU/futures/TAO_USDT` capture. Inspect only `market_header_probe` records.
If they prove exact title/value structure, implement that extractor in a separate
reviewed task; otherwise retain fail-closed mark/index behavior and revise the
bounded probe deliberately. Do not start a long corpus from this milestone.
