from trading_bot.normalization.pilot import capacity_estimate


def test_capacity_estimate_requires_a_meaningful_sample() -> None:
    result = capacity_estimate(
        normalized_bytes=1000,
        source_rows=999,
        source_span_seconds=600,
        production_free_bytes=10_000,
        production_hard_floor_bytes=1000,
    )
    assert result.uncertainty == "insufficient_sample"
    assert result.daily_bytes is None


def test_capacity_estimate_is_explicitly_linear_and_respects_floor() -> None:
    result = capacity_estimate(
        normalized_bytes=1_000_000,
        source_rows=10_000,
        source_span_seconds=1000,
        production_free_bytes=10_000_000_000,
        production_hard_floor_bytes=3_000_000_000,
    )
    assert result.uncertainty == "linear_extrapolation"
    assert result.daily_bytes == 86_400_000
    assert result.seven_day_bytes == 604_800_000
    assert result.headroom_days == 81.02
