"""Append-only local NDJSON capture. No remote storage."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from trading_bot.research.mexc_shadow.safety import assert_no_credential_keys
from trading_bot.research.mexc_shadow.ui_capture.schema import UiRawSnapshot


def append_snapshot(path: Path, snapshot: UiRawSnapshot | Mapping[str, Any]) -> None:
    """Append one snapshot. Existing bytes are never rewritten."""

    payload = snapshot.as_dict() if isinstance(snapshot, UiRawSnapshot) else dict(snapshot)
    assert_no_credential_keys(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def iter_raw_mappings(path: Path) -> Iterator[dict[str, Any]]:
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_no} capture line must be an object")
        assert_no_credential_keys(payload)
        yield payload
