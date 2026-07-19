[CmdletBinding()]
param(
    [switch]$KeepDatabase
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$alembic = Join-Path $projectRoot ".venv\Scripts\alembic.exe"
$pytest = Join-Path $projectRoot ".venv\Scripts\pytest.exe"
$composeProject = "cryptobot-e2e"
$postgresPort = "55432"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing .venv. Create it and install the project with dev dependencies first."
}
if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required for the PostgreSQL end-to-end check."
}

Push-Location $projectRoot
try {
    $env:POSTGRES_PORT = $postgresPort
    $env:POSTGRES_DB = "cryptobot_test"
    docker compose --project-name $composeProject up -d --wait postgres
    if ($LASTEXITCODE -ne 0) { throw "PostgreSQL startup failed." }
    $env:TEST_DATABASE_URL = "postgresql+asyncpg://cryptobot:cryptobot@localhost:$postgresPort/cryptobot_test"
    $env:TEST_DATABASE_ROLE = "test"
    & $alembic -x database-role=test upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Alembic migration failed." }
    & $pytest tests/integration/test_collect_postgres.py
    if ($LASTEXITCODE -ne 0) { throw "COLLECT end-to-end check failed." }
}
finally {
    if (-not $KeepDatabase) {
        docker compose --project-name $composeProject down --volumes
    }
    Pop-Location
}
