import asyncio
import base64
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading_bot.archive.exporter import ArchiveRequest
from trading_bot.storage.models import MarketEvent

MAX_PC_ARCHIVE_BATCH_SIZE = 5000
_SAFE_ALIAS = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SAFE_REMOTE_PATH = re.compile(r"/[A-Za-z0-9_./-]{1,512}\Z")
_SAFE_SYMBOL = re.compile(r"[A-Za-z0-9_./:-]{1,32}\Z")


def _utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("archive timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _sql(request: ArchiveRequest, last_id: int) -> str:
    if not _SAFE_SYMBOL.fullmatch(request.symbol):
        raise ValueError("archive symbol is invalid")
    start = _utc(request.start)
    end = _utc(request.end)
    symbol = request.symbol.replace("'", "''")
    return f"""
COPY (
  SELECT jsonb_build_object(
    'id', id,
    'received_at', received_at,
    'exchange_at', exchange_at,
    'source', source,
    'event_type', event_type,
    'symbol', symbol,
    'sequence', sequence,
    'connection_id', connection_id,
    'local_sequence', local_sequence,
    'exchange_sequence', exchange_sequence,
    'schema_version', schema_version,
    'latency_ms', latency_ms,
    'payload', payload
  )::text
  FROM market_events
  WHERE id > {last_id}
    AND received_at >= '{start}'::timestamptz
    AND received_at < '{end}'::timestamptz
    AND symbol = '{symbol}'
  ORDER BY id
  LIMIT {request.batch_size}
) TO STDOUT
""".strip()


def _remote_script(project_dir: str, env_file: str, sql: str) -> str:
    encoded_sql = base64.b64encode(sql.encode("utf-8")).decode("ascii")
    return f"""set -eu
cd {project_dir}
for service in postgres collector; do
  container=$(docker compose --env-file {env_file} \
    -f compose.yaml -f compose.production.yaml ps -q "$service")
  [ -n "$container" ]
  [ "$(docker inspect --format '{{{{.State.Health.Status}}}}' "$container")" = healthy ]
done
free_kib=$(df -Pk . | awk 'NR == 2 {{print $4}}')
[ "$free_kib" -gt 4194304 ]
sql=$(printf '%s' '{encoded_sql}' | base64 -d)
docker compose --env-file {env_file} -f compose.yaml -f compose.production.yaml \
  exec -T postgres sh -eu -c '
    export PGOPTIONS="-c default_transaction_read_only=on -c statement_timeout=5000"
    exec psql -X -qAt -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "$1"
  ' sh "$sql"
"""


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("remote archive timestamp is invalid")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("remote archive timestamp is naive")
    return result.astimezone(UTC)


def _event(value: dict[str, Any]) -> MarketEvent:
    received_at = _timestamp(value.get("received_at"))
    payload = value.get("payload")
    if received_at is None or not isinstance(payload, dict):
        raise ValueError("remote archive event is malformed")
    return MarketEvent(
        id=int(value["id"]),
        received_at=received_at,
        exchange_at=_timestamp(value.get("exchange_at")),
        source=str(value["source"]),
        event_type=str(value["event_type"]),
        symbol=str(value["symbol"]),
        sequence=int(value["sequence"]) if value.get("sequence") is not None else None,
        connection_id=(
            str(value["connection_id"]) if value.get("connection_id") is not None else None
        ),
        local_sequence=(
            int(value["local_sequence"]) if value.get("local_sequence") is not None else None
        ),
        exchange_sequence=(
            int(value["exchange_sequence"])
            if value.get("exchange_sequence") is not None
            else None
        ),
        schema_version=int(value.get("schema_version") or 1),
        latency_ms=(
            float(value["latency_ms"]) if value.get("latency_ms") is not None else None
        ),
        payload=payload,
    )


class SshArchiveBatchReader:
    def __init__(
        self,
        *,
        ssh_alias: str,
        remote_project_dir: str,
        remote_env_file: str,
        ssh_config: Path | None = None,
        ssh_executable: str = "ssh",
        timeout_seconds: int = 30,
    ) -> None:
        if not _SAFE_ALIAS.fullmatch(ssh_alias):
            raise ValueError("SSH alias is invalid")
        if not _SAFE_REMOTE_PATH.fullmatch(remote_project_dir):
            raise ValueError("remote project path is invalid")
        if not _SAFE_REMOTE_PATH.fullmatch(remote_env_file):
            raise ValueError("remote environment path is invalid")
        if not 5 <= timeout_seconds <= 120:
            raise ValueError("SSH timeout is invalid")
        self._alias = ssh_alias
        self._project_dir = remote_project_dir
        self._env_file = remote_env_file
        self._config = ssh_config
        self._executable = ssh_executable
        self._timeout = timeout_seconds

    async def __call__(
        self,
        request: ArchiveRequest,
        last_id: int,
    ) -> list[MarketEvent]:
        if request.batch_size > MAX_PC_ARCHIVE_BATCH_SIZE:
            raise ValueError(
                f"PC archive batch size exceeds {MAX_PC_ARCHIVE_BATCH_SIZE}"
            )
        return await asyncio.to_thread(self._read, request, last_id)

    def _read(self, request: ArchiveRequest, last_id: int) -> list[MarketEvent]:
        script = _remote_script(
            self._project_dir,
            self._env_file,
            _sql(request, last_id),
        )
        encoded = base64.b64encode(script.encode("utf-8")).decode("ascii")
        command = [self._executable]
        if self._config is not None:
            command.extend(("-F", str(self._config)))
        command.extend(
            (
                "-o",
                "BatchMode=yes",
                self._alias,
                f"printf '%s' '{encoded}' | base64 -d | sh",
            )
        )
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self._timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError("remote archive source unavailable") from error
        if result.returncode != 0:
            raise RuntimeError("remote archive source unavailable")
        lines = result.stdout.splitlines()
        if len(lines) > request.batch_size:
            raise RuntimeError("remote archive source exceeded batch limit")
        try:
            events = [_event(json.loads(line)) for line in lines if line]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("remote archive source returned invalid data") from error
        previous_id = last_id
        for event in events:
            if event.id is None or event.id <= previous_id:
                raise RuntimeError("remote archive source order is invalid")
            previous_id = event.id
        return events
