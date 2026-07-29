from pathlib import Path

import pytest

from trading_bot.normalization.resources import (
    GIB,
    MIB,
    ResourceLimits,
    evaluate_resources,
)


class Probe:
    def __init__(self, *, disk: int, rss: int) -> None:
        self.disk = disk
        self.rss = rss

    def disk_free_bytes(self, path: Path) -> int:
        assert path == Path("capacity")
        return self.disk

    def rss_bytes(self) -> int:
        return self.rss


@pytest.mark.parametrize(
    ("disk", "rss", "state", "reason"),
    [
        (5 * GIB, 100 * MIB, "run", "ready"),
        (4 * GIB, 100 * MIB, "pause", "disk_pause"),
        (3 * GIB - 1, 100 * MIB, "stop", "disk_hard_stop"),
        (5 * GIB, 128 * MIB, "pause", "rss_pause"),
        (5 * GIB, 160 * MIB, "stop", "rss_hard_stop"),
    ],
)
def test_resource_boundaries_fail_closed(
    disk: int,
    rss: int,
    state: str,
    reason: str,
) -> None:
    result = evaluate_resources(
        probe=Probe(disk=disk, rss=rss),
        path=Path("capacity"),
        limits=ResourceLimits(),
        batch_size=100,
    )
    assert (result.state, result.reason) == (state, reason)


def test_resource_probe_failure_is_not_treated_as_ready() -> None:
    class FailedProbe(Probe):
        def rss_bytes(self) -> int:
            raise OSError("sensitive diagnostic")

    with pytest.raises(RuntimeError, match="resource state is unavailable") as caught:
        evaluate_resources(
            probe=FailedProbe(disk=5 * GIB, rss=0),
            path=Path("capacity"),
            limits=ResourceLimits(),
            batch_size=100,
        )
    assert "sensitive" not in str(caught.value)
