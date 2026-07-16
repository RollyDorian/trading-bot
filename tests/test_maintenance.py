from datetime import UTC, datetime

import pytest

from trading_bot.storage.maintenance import ReplayFilter


def test_replay_filter_requires_valid_deterministic_window() -> None:
    start = datetime(2026, 7, 16, tzinfo=UTC)
    end = datetime(2026, 7, 17, tzinfo=UTC)

    replay_filter = ReplayFilter(
        symbol="ETH/USDT-P",
        start=start,
        end=end,
        event_types=("trades",),
        limit=100,
    )

    assert replay_filter.start == start
    assert replay_filter.end == end


@pytest.mark.parametrize(
    ("start", "end", "limit", "message"),
    [
        (None, None, 0, "limit"),
        (datetime(2026, 7, 17, tzinfo=UTC), datetime(2026, 7, 16, tzinfo=UTC), 1, "start"),
        (datetime(2026, 7, 16), None, 1, "timezone-aware"),
    ],
)
def test_replay_filter_rejects_unsafe_values(
    start: datetime | None,
    end: datetime | None,
    limit: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ReplayFilter(symbol="ETH/USDT-P", start=start, end=end, limit=limit)
