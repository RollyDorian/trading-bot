# Production B2 archive window export

This document describes the **safe production archive slice workflow**:
bounded PostgreSQL RAW window → immutable Parquet bundle → schema-5 quality
report → logical + physical checksums → upload via consolidated
``BotoS3ArchiveStore`` → download verification → restore validation → canonical
``COMPLETED`` marker.

Smoke-only transport remains in ``B2ArchiveClient`` (``archive-roundtrip-smoke``).
Production uploads use ``S3ArchiveStore.for_b2`` (boto3 ``BotoS3ArchiveStore``).

## Lifecycle

1. **Export locally** — ``archive-export-window`` without ``--confirm-upload``.
2. **Review** — inspect ``quality_report.json``, ``archive_metadata.json``,
   ``provenance.json``, ``logical_checksums.sha256``, and ``checksums.sha256``.
3. **Confirm upload** — re-run with ``--confirm-upload`` after operator approval.
4. **Remote verify** — upload path downloads each object and compares SHA-256.
5. **Restore validate** — full bundle is re-downloaded and ``validate_dataset`` runs.
6. **Canonical publish** — ``archives/<dataset_id>/COMPLETED`` is written only after
   all checks pass.
7. **Later restore check** — ``archive-verify-restore`` on another host (reads
   ``COMPLETED`` to locate the successful attempt).

**No retention or RAW deletion** is performed by these commands.

## Operator approval boundaries

| Step | Requires explicit flag / env |
| --- | --- |
| Local bundle only | default (no network) |
| Upload to B2 | ``--confirm-upload`` + ``B2_S3_*`` credentials |
| Upload with quality warnings | ``--allow-quality-warnings`` |
| Restore verification | ``archive-verify-restore`` (read-only download) |

Credentials stay outside Git in an operator-owned env file (mode ``0600``). CLI
output is redacted JSON only.

## Credentials

Use the same ``B2_S3_*`` variables documented in [b2_archive_smoke.md](b2_archive_smoke.md).

## CLI

### ``archive-export-window``

```powershell
hibachi-archive archive-export-window `
  --start 2026-07-18T00:00:00+00:00 `
  --end 2026-07-18T01:00:00+00:00 `
  --output-dir D:\archive-work
```

With upload (after local review):

```powershell
hibachi-archive archive-export-window `
  --start 2026-07-18T00:00:00+00:00 `
  --end 2026-07-18T01:00:00+00:00 `
  --output-dir D:\archive-work `
  --confirm-upload `
  --provider b2
```

### ``archive-verify-restore``

```powershell
hibachi-archive archive-verify-restore `
  --dataset-id eth-usdt-p_20260718T000000000000Z_20260718T010000000000Z_v2 `
  --output-dir D:\restore-work `
  --provider b2
```

## Bounds (defaults and hard caps)

| Limit | Default | Hard max |
| --- | --- | --- |
| Window duration | 1 hour | 6 hours |
| Rows | 50,000 | 200,000 |
| Bundle size | 64 MiB | 256 MiB |
| Min free disk | 3 GiB (operational floor) | cannot go below 3 GiB |

Override via ``--max-duration-seconds``, ``--max-rows``, ``--max-bytes``,
``--min-disk-bytes`` (cannot exceed hard caps; ``--min-disk-bytes`` cannot be
below the 3 GiB operational floor).

## Disk gates

Before build:

```text
free >= max(min_free_disk_bytes, 3 GiB)
free >= 3 GiB + 2 × max_bundle_bytes
```

The ``2 × max_bundle_bytes`` term reserves space for the local bundle write and
a verification download temp directory.

After build:

```text
free >= 3 GiB + actual_bundle_bytes
```

Build aborts with ``WindowExportError`` when either gate fails.

## Provenance and checksum model

| File | Role | In logical identity? |
| --- | --- | --- |
| ``events.parquet`` | Sorted RAW export | yes |
| ``candles_1s.parquet`` | Trade-derived candles | yes |
| ``README.md`` | Dataset notes | yes |
| ``quality_report.json`` | Schema **5** quality evidence | yes |
| ``archive_metadata.json`` | Window bounds, sources, topics, row counts | yes |
| ``logical_checksums.sha256`` | SHA-256 listing of logical artifacts | no (manifest of identity) |
| ``manifest.json`` | Research schema v2 metadata (includes real git SHA) | no (physical only) |
| ``provenance.json`` | Real ``git_commit``, tool version, export time | no (physical only) |
| ``checksums.sha256`` | SHA-256 listing of all uploaded immutable files | no (physical manifest) |

**Logical dataset identity** is ``logical_checksums.sha256`` (and its listed
digests). Rebuilding with a different git commit changes ``provenance.json`` and
``manifest.json`` but not logical identity when event content is unchanged.
The logical digest for ``quality_report.json`` excludes provenance-volatile
fields ``manifest_sha256`` and ``validated_at_utc``.

``remote_verification.json`` is written under
``{output_dir}/_verification/{dataset_id}/`` and is never part of either checksum
list.

## Remote layout and attempt markers

Upload artifacts publish under:

```text
archives/<dataset_id>/attempts/<attempt_id>/
```

where ``attempt_id`` is a UTC timestamp plus random hex. Within an attempt prefix
objects are never overwritten.

| Marker | Location | When |
| --- | --- | --- |
| ``INCOMPLETE`` | ``archives/<dataset_id>/attempts/<attempt_id>/INCOMPLETE`` | Partial upload or failed verification |
| ``COMPLETED`` | ``archives/<dataset_id>/COMPLETED`` | All upload + checksum + restore checks passed |

``archive-verify-restore`` reads ``COMPLETED`` to locate the attempt prefix and
refuses when only ``INCOMPLETE`` markers exist. **No delete APIs** are called;
orphan attempt objects remain until manual operator review.

## Partial upload / orphan objects

Upload is all-or-nothing on preflight (no overwrite within an attempt). If upload
or verification fails mid-way, already-published attempt objects are **not**
deleted. Status is recorded in the attempt ``INCOMPLETE`` marker and in
``{output_dir}/_verification/{dataset_id}/remote_verification.json``.

## Smoke vs production

| Component | Role |
| --- | --- |
| ``B2ArchiveClient`` | Bounded smoke round-trip under ``smoke/`` |
| ``BotoS3ArchiveStore`` | Production archive transport (no delete, no overwrite) |
| PyArrow ``S3ArchiveStore`` | Legacy ``ARCHIVE_S3_*`` export-day only |

## Tests

Default ``pytest`` uses mocks only. Opt-in network tests require ``B2_S3_INTEGRATION=1``.
