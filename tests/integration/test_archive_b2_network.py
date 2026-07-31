import os

import pytest

from trading_bot.archive.b2 import B2ArchiveClient, B2ArchiveConfig, run_roundtrip_smoke


def _integration_enabled() -> bool:
    if os.environ.get("B2_S3_INTEGRATION") != "1":
        return False
    required = (
        "B2_S3_BUCKET",
        "B2_S3_ENDPOINT",
        "B2_S3_REGION",
        "B2_S3_ACCESS_KEY_ID",
        "B2_S3_SECRET_ACCESS_KEY",
    )
    return all(os.environ.get(name, "").strip() for name in required)


@pytest.mark.skipif(
    not _integration_enabled(),
    reason="opt-in B2 network smoke requires B2_S3_INTEGRATION=1 and credentials",
)
def test_b2_roundtrip_smoke_network(tmp_path: object) -> None:
    from pathlib import Path

    work_dir = Path(str(tmp_path))
    config = B2ArchiveConfig.from_environ()
    client = B2ArchiveClient(config)
    result = run_roundtrip_smoke(client, work_dir=work_dir, size_bytes=512)
    assert result["verified"] is True
    assert result["remote_retained"] is True
