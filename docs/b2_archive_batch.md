# Production B2 batch archive workflow

This document describes the **bounded multi-window batch archive** workflow:
immutable plan → resumable operator-bounded runs → per-window export/upload or
``COMPLETED`` reuse verification → final batch reconciliation.

Batch archive is **not** retention. It never deletes RAW data, archive objects,
or PostgreSQL rows. A passing batch verification does **not** authorize DELETE.

See also [b2_archive_window.md](b2_archive_window.md) for single-window export,
checksum model, and remote layout.

## Lifecycle

1. **Plan** — ``archive-batch-plan`` reads PostgreSQL counts only (no uploads).
2. **Review** — inspect ``archive_batch_plan.json`` and ``plan.sha256``.
3. **Run (bounded)** — ``archive-batch-run --confirm-upload`` processes up to
   ``--max-windows`` pending windows per invocation.
4. **Resume** — re-run the same command; storage-complete and verified windows
   are not reprocessed.
5. **Reconcile** — when every window is storage-complete, ``batch_verification.json``
   is written under the batch progress directory.

Storage integrity and research admission eligibility are separate: a batch can
``pass`` storage reconciliation while ``admissible_coverage_continuous`` remains
``false`` when any window is quarantined.

## Operator approval

| Step | Requires explicit flag / env |
| --- | --- |
| Plan (DB counts only) | default (no remote I/O) |
| Remote upload run | ``--confirm-upload`` + ``B2_S3_*`` credentials |
| Upload with quality warnings | ``--allow-quality-warnings`` |
| Quarantine upload (rejected quality) | ``--confirm-quarantine-upload`` **and** ``--confirm-upload`` |
| New attempt after incomplete remote marker | ``--allow-new-attempt-after-incomplete`` |

``--allow-quality-warnings`` does **not** unlock ``rejected`` quality.
Quarantine registry append (``archives/quarantine/registry.jsonl``) is
**single-writer** read-modify-write; do not run concurrent quarantine uploads
against the same archive prefix.

Without ``--confirm-upload``, ``archive-batch-run`` refuses the remote upload path
(fail closed), matching single-window export behavior.

## Plan immutability

Plans are written to:

```text
{output_dir}/archive_batch_plan.json
{output_dir}/plan.sha256
```

A second plan write to the same directory is refused. On run, ``plan.sha256`` must
match the plan bytes; tampering aborts fail closed.

Plan fields include contiguous half-open windows, per-window ``dataset_id``,
expected event/trade counts, git commit provenance, and limits.

## Progress and artifacts

Given a plan at ``{batch_root}/archive_batch_plan.json``:

```text
{batch_root}/archive_batch_plan.json
{batch_root}/plan.sha256
{batch_root}/_batch/{plan_id}/progress.json
{batch_root}/_batch/{plan_id}/batch_verification.json   # only when all windows terminal
{batch_root}/windows/{dataset_id}/                      # local bundles
{batch_root}/_batch/{plan_id}/_verification/{dataset_id}/
```

``batch_root`` defaults to the directory containing the plan file. Override with
``archive-batch-run --output-dir`` when run artifacts must live elsewhere.

Progress updates are atomic (write temp + ``os.replace``) after each state change.

## Window states

| State | Meaning |
| --- | --- |
| ``pending`` | Not yet processed (includes crash-recovered ``running``) |
| ``running`` | Currently executing |
| ``completed_admissible`` | Storage ``COMPLETED`` and admission-eligible (quality ``pass``) |
| ``completed_quarantined`` | Storage ``COMPLETED`` but quarantined / not admission-eligible |
| ``skipped_verified`` | Remote ``COMPLETED`` exists and passed admissible verification |
| ``skipped_quarantined`` | Remote quarantined ``COMPLETED`` verified without re-upload |
| ``failed_storage`` | Upload, verify, or storage failure; blocks automatic retry |

Legacy progress files may contain ``completed`` or ``failed``; these map to
``completed_admissible`` and ``failed_storage`` on load.

Failed windows are never auto-retried. A subsequent run stops fail closed if any
``failed_storage`` window remains.

## Incomplete attempt policy

When remote ``INCOMPLETE`` markers exist under
``archives/{dataset_id}/attempts/*/INCOMPLETE``:

- **Default (fail closed):** window is marked ``failed``; batch stops.
- **With ``--allow-new-attempt-after-incomplete``:** a new attempt is created via
  the normal export/upload path. Old incomplete objects are left in place; no
  delete APIs are called.

## COMPLETED reuse

If ``archives/{dataset_id}/COMPLETED`` exists, the run downloads and verifies the
canonical attempt (checksums, schema-5 quality, ``archive_metadata.json`` event
counts vs plan). On match → ``skipped_verified`` or ``skipped_quarantined`` (no
re-upload). Rejected quality does **not** fail reuse when the archive is
quarantined and structurally verified. On mismatch → ``failed_storage``.

**COMPLETED reuse does not authorize retention or DELETE.**

## Reconciliation fields

``batch_verification.json`` reports storage and admissible coverage separately:

| Field | Meaning |
| --- | --- |
| ``storage_event_total_reconciled`` | All storage-complete windows match plan event totals |
| ``admissible_event_total`` | Events in admission-eligible windows only |
| ``quarantined_event_total`` | Events in quarantined windows |
| ``admissible_coverage_continuous`` | ``false`` when any window is quarantined or admissible totals diverge |
| ``failed_storage`` | Any window in ``failed_storage`` state |
| ``retention_authorized`` | Always ``false`` |

## Bounds

| Limit | Default | Hard max |
| --- | --- | --- |
| Windows per run | 3 | 24 |
| Plan span | 6 hours | 24 hours |
| Window size | 1 hour (``--window-seconds``) | must divide plan span exactly |
| Per-window rows | (plan ``limits.max_rows``) | 200,000 |
| Per-window bundle | (plan ``limits.max_bytes``) | 256 MiB |
| Upload bytes per run | 3 × 256 MiB | override ``--max-upload-bytes`` |
| Min free disk | 3 GiB floor | cannot go below 3 GiB |

## CLI examples

### Plan

```powershell
hibachi-archive archive-batch-plan `
  --start 2026-07-18T00:00:00+00:00 `
  --end 2026-07-18T03:00:00+00:00 `
  --window-seconds 3600 `
  --output-dir D:\archive-work\plans
```

### Run (bounded, resumable)

```powershell
hibachi-archive archive-batch-run `
  --plan D:\archive-work\plans\archive_batch_plan.json `
  --confirm-upload `
  --provider b2 `
  --max-windows 3 `
  --output-dir D:\archive-work
```

Quarantined batch run (rejected quality windows):

```powershell
hibachi-archive archive-batch-run `
  --plan D:\archive-work\plans\archive_batch_plan.json `
  --confirm-upload `
  --confirm-quarantine-upload `
  --provider b2
```

Re-run the same command until ``batch_verification.json`` reports ``pass`` or a
window is ``failed_storage``.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Plan success; run ``pass`` or intentional ``partial`` progress without failures |
| 1 | Run finished with ``failed`` status |
| 2 | Contract / validation error (bad plan, missing confirm flag, checksum mismatch) |

## Tests

Default ``pytest`` uses mocks and ``LocalArchiveStore`` only. No live B2 or
production PostgreSQL is required for batch unit tests.
