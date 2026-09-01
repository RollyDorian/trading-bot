"""Safety-boundary tests: no orders, credentials, or trading-stack imports."""

from __future__ import annotations

from trading_bot.research.mexc_shadow.safety import (
    extension_source_violations,
    package_import_violations,
    package_source_violations,
)


def test_capture_and_extension_safety_boundaries() -> None:
    assert package_source_violations() == []
    assert package_import_violations() == []
    assert extension_source_violations() == []
