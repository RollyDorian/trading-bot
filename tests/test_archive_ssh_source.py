import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_bot import archive_cli
from trading_bot.archive.exporter import ArchiveRequest
from trading_bot.archive.ssh_source import SshArchiveBatchReader, _remote_script, _sql


def request(tmp_path: Path, *, batch_size: int = 1000) -> ArchiveRequest:
    start = datetime(2026, 7, 21, tzinfo=UTC)
    return ArchiveRequest(
        start=start,
        end=start + timedelta(days=1),
        symbol="ETH/USDT-P",
        work_dir=tmp_path / "work",
        capacity_path=tmp_path,
        batch_size=batch_size,
    )


def row(raw_id: int) -> str:
    return json.dumps(
        {
            "id": raw_id,
            "received_at": "2026-07-21T00:00:01+00:00",
            "exchange_at": None,
            "source": "hibachi",
            "event_type": "mark_price",
            "symbol": "ETH/USDT-P",
            "sequence": None,
            "connection_id": None,
            "local_sequence": None,
            "exchange_sequence": None,
            "schema_version": 1,
            "latency_ms": None,
            "payload": {"price": "1"},
        }
    )


def test_ssh_source_uses_bounded_read_only_copy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: list[str] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, f"{row(1)}\n{row(2)}\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    reader = SshArchiveBatchReader(
        ssh_alias="collector-host",
        remote_project_dir="/srv/collector",
        remote_env_file="/srv/secrets/runtime.env",
        ssh_config=tmp_path / "config",
    )
    events = asyncio.run(reader(request(tmp_path), 0))
    assert [event.id for event in events] == [1, 2]
    assert "BatchMode=yes" in captured
    sql = _sql(request(tmp_path), 0).upper()
    assert sql.startswith("COPY (")
    assert "SELECT " in sql
    assert " LIMIT 1000" in sql
    assert not any(
        keyword in sql
        for keyword in ("INSERT ", "UPDATE ", "DELETE ", "VACUUM ", "ANALYZE ", "ALTER ")
    )
    remote = _remote_script("/srv/collector", "/srv/secrets/runtime.env", sql)
    assert "default_transaction_read_only=on" in remote
    assert "statement_timeout=5000" in remote
    assert "4194304" in remote
    assert ".State.Health.Status" in remote
    assert "compose up" not in remote
    assert "compose run" not in remote


@pytest.mark.parametrize(
    "stdout,returncode",
    [
        ("not-json\n", 0),
        (f"{row(2)}\n{row(1)}\n", 0),
        ("credential-bearing sentinel", 1),
    ],
)
def test_ssh_source_fails_closed_and_redacts_remote_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stdout: str,
    returncode: int,
) -> None:
    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout,
            "postgresql://user:password@private.invalid/research",
        )

    monkeypatch.setattr(subprocess, "run", run)
    reader = SshArchiveBatchReader(
        ssh_alias="collector-host",
        remote_project_dir="/srv/collector",
        remote_env_file="/srv/secrets/runtime.env",
    )
    with pytest.raises(RuntimeError) as error:
        asyncio.run(reader(request(tmp_path), 0))
    message = str(error.value)
    assert "postgresql://" not in message
    assert "password" not in message
    assert "private.invalid" not in message


def test_ssh_source_rejects_unsafe_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SshArchiveBatchReader(
            ssh_alias="bad alias",
            remote_project_dir="/srv/collector",
            remote_env_file="/srv/secrets/runtime.env",
        )
    reader = SshArchiveBatchReader(
        ssh_alias="collector-host",
        remote_project_dir="/srv/collector",
        remote_env_file="/srv/secrets/runtime.env",
    )
    with pytest.raises(ValueError, match="exceeds"):
        asyncio.run(reader(request(tmp_path, batch_size=5001), 0))


def test_pc_cli_redacts_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    async def fail(_: object) -> None:
        raise RuntimeError(
            "postgresql://private-user:private-password@private-host/research"
        )

    monkeypatch.setattr(archive_cli, "_pc_export", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hibachi-archive",
            "pc-export-day",
            "--start",
            "2026-07-21T00:00:00+00:00",
            "--end",
            "2026-07-22T00:00:00+00:00",
            "--symbol",
            "ETH/USDT-P",
            "--root",
            str(tmp_path / "root"),
            "--work-dir",
            str(tmp_path / "work"),
            "--capacity-path",
            str(tmp_path),
            "--ssh-alias",
            "collector-host",
            "--remote-project-dir",
            "/srv/collector",
            "--remote-env-file",
            "/srv/secrets/runtime.env",
        ],
    )
    with pytest.raises(SystemExit) as error:
        archive_cli.main()
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "PC archive failed\n"
