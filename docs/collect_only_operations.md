# COLLECT-only database isolation and operations

## Database roles

Collector, exporter, dashboard, and normal Alembic commands use `DATABASE_URL` with
`DATABASE_ROLE=research`. The runtime rejects PostgreSQL database names containing a
standalone `test` segment. Integration tests never read `DATABASE_URL` as their write
target; they require both `TEST_DATABASE_URL` and `TEST_DATABASE_ROLE=test`, require an
explicit test database name, and reject the research target.

The 2024 fixture timestamp entered the earlier audit because PostgreSQL integration tests
and research processes all read the same `DATABASE_URL`. An earlier integration fixture
used the fixed millisecond value `1720000000000`; integration tests committed fixture rows
to the shared append-only `market_events` table and intentionally did not truncate it. A
later bounded exporter queried the same table and included the fixture whose `received_at`
fell inside the export window while its `exchange_at` was from 2024. No collector timestamp
normalization defect caused that row. The current research-pipeline fixture uses current
timestamps, but before this change it still wrote through the same unsafe shared URL.

## Research environment

Keep the real values outside Git. These are placeholders:

```powershell
$env:BOT_MODE = "collect"
$env:DATABASE_ROLE = "research"
$env:DATABASE_URL = "postgresql+asyncpg://<research-user>:<password>@<host>:5432/<research-db>"
```

Do not set `TEST_DATABASE_URL` in the collector or dashboard environment. Never print or
commit the populated variables.

Apply migrations only after verifying the shell is configured for the research role:

```powershell
if ($env:BOT_MODE -ne "collect" -or $env:DATABASE_ROLE -ne "research") { throw "Unsafe role" }
.\.venv\Scripts\python.exe -m alembic upgrade head
```

## Collect a fresh bounded interval

Choose and record UTC bounds before collection. Start the explicit public stream:

```powershell
$startUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
.\.venv\Scripts\hibachi-bot.exe --stream
```

Stop with `Ctrl+C`. The collector disconnects the WebSocket client and executor. Then
record an exclusive end bound:

```powershell
$endUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
```

In a second shell with the same research configuration, verify freshness without showing
payloads or credentials:

```powershell
.\.venv\Scripts\hibachi-bot.exe --replay --start $startUtc --limit 5
```

The returned `received_at` values must be current UTC values and ordered. The dashboard
`/api/status` endpoint also reports `event_count`, latest receipt timestamp, age, and
freshness state. A stale or fault state must be investigated before export.

Export the bounded immutable dataset:

```powershell
.\.venv\Scripts\hibachi-bot.exe --export-dataset `
  --start $startUtc `
  --end $endUtc
```

Use the printed dataset directory for quality and cost-aware replay:

```powershell
$dataset = "<printed-dataset-directory>"
.\.venv\Scripts\hibachi-bot.exe validate-dataset --dataset $dataset
.\.venv\Scripts\hibachi-bot.exe --offline-replay $dataset `
  --report "$dataset/offline_replay.json"
```

After at least four non-overlapping chronological datasets exist, run admission with at
least one training, one validation, and two OOS datasets:

```powershell
$datasets = @("<train>", "<validation>", "<oos-1>", "<oos-2>")
.\.venv\Scripts\hibachi-bot.exe admit-paper `
  --datasets $datasets `
  --validation-count 1 `
  --oos-count 2 `
  --report paper-admission-report.json
```

`FAIL`, including zero replay trades, is an expected research result. Do not suppress
schema 4 warnings or change admission thresholds in response. `PASS` would still require
human review and would not enable PAPER.

## Integration-test database

Use a different PostgreSQL database and explicit role:

```powershell
$env:TEST_DATABASE_ROLE = "test"
$env:TEST_DATABASE_URL = "postgresql+asyncpg://<test-user>:<password>@<host>:5432/<project-test-db>"
.\.venv\Scripts\python.exe -m alembic -x database-role=test upgrade head
.\.venv\Scripts\python.exe -m pytest tests/integration
```

The checked-in `scripts/e2e_collect.ps1` creates an isolated Compose project, port,
database name, and volume and removes them by default. Test fixtures remain append-only
inside that disposable test database and cannot target the configured research database.
