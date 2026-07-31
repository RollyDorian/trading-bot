# Backblaze B2 archive smoke (research only)

This repository includes a bounded Backblaze B2 S3-compatible client for
**research smoke only**. It is intentionally separate from the existing
PyArrow-based `S3ArchiveStore` (`ARCHIVE_S3_*`) used by
`hibachi-archive export-day`.

For this smoke milestone a dedicated boto3 client is acceptable: it needs
explicit credentials (no discovery), path-style addressing, and bounded
timeouts/retries that the PyArrow adapter does not expose. **Production RAW
archive must not grow a second long-lived S3 stack.** A later milestone should
reuse or consolidate a shared S3-compatible abstraction (one credential and
transport policy) and keep smoke/production callers thin on top of it.

Production RAW archive export and retention remain **deferred** and are not
wired to this client.

## Secrets

- Keep B2 credentials outside Git in an operator-owned env file with mode `0600`.
- Never commit keys, bucket policy exports, or smoke output containing secrets.
- CLI commands print redacted JSON only.

Required process environment variables:

| Variable | Purpose |
| --- | --- |
| `B2_S3_BUCKET` | Target bucket name (no `/`) |
| `B2_S3_ENDPOINT` | HTTPS S3-compatible endpoint URL (host only, no path credentials) |
| `B2_S3_REGION` | Provider region |
| `B2_S3_ACCESS_KEY_ID` | Application key ID |
| `B2_S3_SECRET_ACCESS_KEY` | Application key secret |

Optional tunables:

| Variable | Default | Bounds |
| --- | --- | --- |
| `B2_S3_CONNECT_TIMEOUT_SECONDS` | `10` | `1`–`60` |
| `B2_S3_READ_TIMEOUT_SECONDS` | `30` | `5`–`120` |
| `B2_S3_MAX_RETRIES` | `3` | `0`–`5` |

Example placeholders (not real values):

```bash
export B2_S3_BUCKET="your-bucket-name"
export B2_S3_ENDPOINT="https://s3.<region>.backblazeb2.com"
export B2_S3_REGION="<region>"
export B2_S3_ACCESS_KEY_ID="<application-key-id>"
export B2_S3_SECRET_ACCESS_KEY="<application-key-secret>"
```

## Operator workflow

Load the operator env file in the shell **before** invoking CLI commands. The
library never opens a fixed env file path.

Linux/macOS:

```bash
set -a
source /path/to/operator-b2.env
set +a
hibachi-archive archive-check-config
hibachi-archive archive-roundtrip-smoke
```

PowerShell:

```powershell
Get-Content -Path 'C:\path\to\operator-b2.env' | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $name, $value = $_.Split('=', 2)
  Set-Item -Path "Env:$($name.Trim())" -Value $value.Trim()
}
hibachi-archive archive-check-config
hibachi-archive archive-roundtrip-smoke --size-bytes 2048
```

`archive-check-config` validates configuration and prints a redacted JSON summary
with **no network** access.

`archive-roundtrip-smoke` creates synthetic random bytes locally (default 2048,
hard max 4096), uploads under `smoke/<timestamp>-<id>.bin`, downloads, and
compares SHA-256. It does **not** delete the remote object.

## Remote retention

Smoke objects remain in the bucket until an operator deletes them manually after
review. There is no automatic cleanup in this milestone.

## Integration tests

Opt-in network coverage is skipped by default. To run locally against real B2:

```bash
export B2_S3_INTEGRATION=1
# plus the required B2_S3_* variables
pytest tests/integration/test_archive_b2_network.py
```

Default `pytest` runs use mocks only.
