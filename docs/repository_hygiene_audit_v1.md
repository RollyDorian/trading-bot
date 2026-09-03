# Repository hygiene audit v1

STATUS: `REPOSITORY_HYGIENE_AUDIT_READY`

DECISION: `STOP_FOR_LEAD_REVIEW`

**Audit only. No deletion, history rewriting, or semantic changes.**

## Summary

| Area | Files | Bytes | MiB | % of repo |
| --- | --- | --- | --- | --- |
| **Total tracked** | 409 | 30,309,380 | 28.91 | 100% |
| `docs/` | 86 | 27,757,259 | 26.47 | 91.6% |
| — 5 large research JSON | 5 | 27,045,546 | 25.79 | 89.2% |
| — other docs JSON (33) | 33 | 277,565 | 0.26 | 0.9% |
| — docs MD (48) | 48 | 434,148 | 0.41 | 1.4% |
| `src/` | 128 | 1,310,808 | 1.25 | 4.3% |
| `tests/` | 79 | 679,363 | 0.65 | 2.2% |
| `scripts/` | 31 | 321,699 | 0.31 | 1.1% |
| `extensions/` | 7 | 74,360 | 0.07 | 0.2% |
| `migrations/` | 6 | 26,112 | 0.02 | 0.1% |
| root files | 13 | 57,242 | 0.05 | 0.2% |
| `tests/fixtures/` | (incl above) | 18,698 | 0.02 | 0.1% |

**89.2% of tracked bytes are 5 research JSON files.**

Git pack: 426.70 KiB (delta-compressed). Loose objects: 4.10 MiB (672).
113 commits, 420 unique file paths ever committed.

## The 5 large research JSON files

| File | Bytes | MiB | Generator script | Data source |
| --- | --- | --- | --- | --- |
| `eth_tp_sl_first_touch_feasibility_clean_v1.json` | 9,358,051 | 8.93 | `scripts/eth_tp_sl_first_touch_feasibility.py` | `data/research/full_corpus/` (gitignored Parquet from B2 RAW) |
| `eth_tp_sl_first_touch_feasibility_v1.json` | 9,132,093 | 8.71 | same | same |
| `eth_first_passage_full_corpus_clean_v1.json` | 3,774,788 | 3.60 | `scripts/eth_first_passage_full_corpus.py` | same |
| `eth_first_passage_full_corpus_v1.json` | 3,509,369 | 3.35 | same | same |
| `eth_first_passage_opportunity_v1.json` | 1,271,245 | 1.21 | `scripts/eth_first_passage_opportunity.py` | same |

### Reproducibility

All 5 require `data/research/full_corpus/` Parquet derived from B2-archived verified
RAW generations via the offline research pipeline. The Parquet is gitignored. The B2
data exists and is verified, but regeneration requires: B2 download → restore
events → normalize → `market_state_1s` → run the script. This is a multi-step
manual pipeline, not a single `make` target.

**The `_clean` vs non-`_clean` pairs**: both exist in the same commit
(`b49823d`). The `_clean` versions were produced after the executable TOB path
fix (stale/fallback barrier contamination removed). The non-`_clean` originals
contain the pre-remediation results. The `_clean` variant is strictly larger
(+226 KiB and +265 KiB respectively) because remediation added fields.

**References to `_clean` variants**: only `docs/eth_executable_path_quality_remediation_v1.md`
and `scripts/eth_executable_path_quality_remediation.py` reference them by name.
The companion `.md` reports reference the JSON for machine-readable evidence.

## Exact duplicates

6 files in `docs/external_offload_proof/` are byte-identical copies of files in `docs/`:

| File | Bytes |
| --- | --- |
| `b2_throughput_report.json` | 337 |
| `compression_report.json` | 1,135 |
| `offload_b2_report.json` | 318 |
| `offload_local_report.json` | 614 |
| `split_report.json` | 713 |
| `status_sample.json` | 590 |
| **Total duplicated** | **3,707** |

These are small (3.7 KiB total). The `external_offload_proof/` subdirectory also
has 3 unique files (`final_status_v1.json`, `live_canary_status_v1.json`,
`segment_full_report.json`). The root copies appear to be earlier artifacts
before the proof was consolidated into the subdirectory.

## .gitignore coverage

The `.gitignore` is comprehensive (4,956 bytes). No tracked files match ignore
patterns. No `__pycache__`, `.pyc`, `.egg-info`, `node_modules`, or `.env` are
tracked. `data/`, `.pytest-tmp*`, `scripts/_vps_*` are all untracked as expected.

## Classification

### KEEP_SOURCE (hand-written or essential config)

| Path pattern | Files | Bytes | Rationale |
| --- | --- | --- | --- |
| `src/**/*.py` | 128 | 1.25 MiB | Application and research source |
| `tests/**/*.py` | ~55 | 0.64 MiB | Test source |
| `scripts/*.py`, `scripts/*.sh` | 31 | 0.31 MiB | Operational and research scripts |
| `extensions/**` | 7 | 0.07 MiB | MEXC UI capture extension |
| `migrations/**` | 6 | 0.02 MiB | Alembic migrations |
| Root config | 13 | 0.05 MiB | pyproject.toml, compose, Dockerfile, etc. |
| `AGENTS.md` | 1 | 0.03 MiB | Project instructions |

### KEEP_FIXTURE (test fixtures, small, referenced by tests)

| Path pattern | Files | Bytes | Referenced by |
| --- | --- | --- | --- |
| `tests/fixtures/hibachi/*.json` | 7 | ~2.3 KiB | `test_normalize.py`, `test_events.py` |
| `tests/fixtures/mexc_ui_capture/*.html` | 12 | ~15 KiB | `test_mexc_ui_capture.py`, `test_mexc_ui_wrapper_bbo.py`, `test_mexc_ui_locale_semantics.py` |
| `tests/fixtures/mexc_shadow/*.json` | 1 | 450 B | `test_mexc_shadow.py` |
| `tests/fixtures/external_market_data/*.json` | 2 | 393 B | `test_external_market_data.py` |
| `tests/fixtures/research/*.json` | 1 | 2.7 KiB | `test_archive_batch.py` |

### KEEP_EVIDENCE (non-reproducible research evidence)

| File | Bytes | MiB | Why non-reproducible |
| --- | --- | --- | --- |
| `eth_tp_sl_first_touch_feasibility_clean_v1.json` | 9.36M | 8.93 | Requires gitignored Parquet from B2 multi-step pipeline |
| `eth_first_passage_full_corpus_clean_v1.json` | 3.77M | 3.60 | Same |
| `eth_first_passage_opportunity_v1.json` | 1.27M | 1.21 | Same |
| All other `docs/*.json` (≤56 KiB each) | ~280K | 0.27 | Various research, canary, operational evidence |
| All `docs/*.md` | ~434K | 0.41 | Research milestone reports |

These JSON files are the machine-readable evidence of frozen research results.
The generator scripts exist and are committed, but the intermediate Parquet data
is gitignored and the regeneration pipeline is non-trivial. **If the JSON is
removed, the only recovery path is re-downloading from B2 and re-running the
full pipeline.** They are the durable record.

### DERIVED_CAN_REGENERATE (superseded by _clean, but kept for audit trail)

| File | Bytes | MiB | Superseded by |
| --- | --- | --- | --- |
| `eth_tp_sl_first_touch_feasibility_v1.json` | 9.13M | 8.71 | `_clean_v1.json` |
| `eth_first_passage_full_corpus_v1.json` | 3.51M | 3.35 | `_clean_v1.json` |
| **Subtotal** | **12.64M** | **12.06** | |

These are the pre-remediation research results. The `_clean` versions supersede
them. However: they document the contamination that was found and are referenced
in the remediation milestone narrative. They are technically regenerable from the
same B2 pipeline (without the TOB fix), but serve as negative evidence.

### MOVE_OUT_OF_GIT_CANDIDATE

| File(s) | Bytes | MiB | Rationale |
| --- | --- | --- | --- |
| Pre-clean non-`_clean` JSON pair | 12.64M | 12.06 | Superseded; negative evidence could be documented in MD only |
| 6 root `docs/` duplicate JSON | 3,707 | 0.004 | Byte-identical copies of `external_offload_proof/` files |

### DELETE_CANDIDATE

None identified. Even the smallest artifacts serve as evidence or are actively referenced.

### UNKNOWN_NEEDS_LEAD

| Item | Question |
| --- | --- |
| Pre-clean `_v1.json` pair (12.06 MiB) | Keep as negative evidence in Git, or archive summary + remove from tracking? |
| `docs/external_offload_proof/` duplicates (3.7 KiB) | Remove root copies or subdirectory copies? |
| `AGENTS.md` (26.9 KiB, growing) | Compact historical milestone prose into a summary table? |

## Largest Git blob history contributors

Top 5 blobs ever committed (including current):

| Blob | Bytes | MiB | File |
| --- | --- | --- | --- |
| `5d42d6bd` | 9,174,747 | 8.75 | `docs/eth_tp_sl_first_touch_feasibility_clean_v1.json` |
| `54e2ffa7` | 8,954,385 | 8.54 | `docs/eth_tp_sl_first_touch_feasibility_v1.json` |
| `1c650214` | 3,680,250 | 3.51 | `docs/eth_first_passage_full_corpus_clean_v1.json` |
| `bcc7e657` | 3,422,247 | 3.26 | `docs/eth_first_passage_full_corpus_v1.json` |
| `6f5a9c41` | 1,236,682 | 1.18 | `docs/eth_first_passage_opportunity_v1.json` |

Git pack compresses these heavily (total pack 427 KiB), so removing them from
the working tree alone would not reclaim pack space without history rewriting.

## Proposed retention policy (for lead review)

### Tier 1: In-Git (always tracked)

- Source code, tests, fixtures, migrations, scripts, extensions, configs
- Compact Markdown milestone reports (`docs/*.md`, ≤26 KiB each)
- Small machine-readable summaries (`docs/*.json` ≤60 KiB)
- `AGENTS.md` (possibly compacted)

### Tier 2: In-Git evidence (tracked, immutable)

- Large research JSON where raw source is gitignored/external and
  regeneration is non-trivial
- Only the **latest clean** version of each research result
- Pre-clean/superseded versions: summarize key deltas in MD, remove
  JSON from tracking (requires lead approval)

### Tier 3: Outside Git

- Bulky reproducible outputs (Parquet, NDJSON corpora) — already gitignored
- If a "compact summary" JSON is created for each large result, the full
  JSON could move to B2 or a release artifact

### Expected impact

| Action | Tracked bytes removed | % reduction | History rewrite needed |
| --- | --- | --- | --- |
| Remove pre-clean `_v1.json` pair | 12,641,462 | 41.7% | No (just `git rm`) |
| Remove 6 root duplicate JSON | 3,707 | 0.01% | No |
| Compact `AGENTS.md` history | ~15,000 | 0.05% | No |
| **Total without history rewrite** | **~12.66 MiB** | **~41.8%** | |
| Additionally: replace 3 large `_clean` JSON with compact summaries | 14,404,084 | 47.5% | No |
| **Total if all large JSON moved out** | **~27.05 MiB** | **89.2%** | |

Note: Git pack size would not shrink without `git filter-repo` or similar
history rewriting. Working-tree size and clone size would decrease for
shallow clones. Full history reclaim requires a separate approved rewrite.

## No action taken

This audit is read-only. No files were deleted, moved, rewritten, or gitignored.
