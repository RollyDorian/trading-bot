"""Hard safety boundary for the MEXC shadow research engine.

This package must never grow order placement, private trading endpoints,
credential loading, or UI click-to-trade behavior.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Rejected at config parse time. Not an exhaustive OS secret scanner.
FORBIDDEN_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret_key",
        "access_key",
        "passphrase",
        "private_key",
        "trading_password",
        "session_token",
        "web_token",
        "csrf",
        "cookie",
        "authorization",
    }
)

# Import roots that would pull live trading or browser-driver stacks into this
# isolated research package. Names overlap the source-scan list on purpose.
FORBIDDEN_IMPORT_MODULES = frozenset(
    {
        "trading_bot.paper",
        "trading_bot.exchange",
        "playwright",
        "selenium",
    }
)

# Source-scan substrings. Tests fail if these appear under mexc_shadow/ except this file.
FORBIDDEN_SOURCE_MARKERS = (
    "place_order",
    "cancel_order",
    "modify_order",
    "create_order",
    "submit_order",
    "private/order",
    "api/v3/order",
    "spot/order",
    "click_trade",
    "click(trade",
    "playwright",
    "selenium",
    "webdriver",
    "anti_bot",
    "stealth",
    "human_delay",
    "fingerprint",
)


def assert_no_credential_keys(payload: Mapping[str, Any]) -> None:
    """Fail closed if a config mapping smuggles trading-credential field names."""

    lowered = {str(key).lower().replace("-", "_") for key in payload}
    hit = lowered & FORBIDDEN_CONFIG_KEYS
    if hit:
        raise ValueError(f"mexc_shadow config forbids credential fields: {sorted(hit)}")
    for value in payload.values():
        if isinstance(value, dict):
            assert_no_credential_keys(value)


def package_root() -> Path:
    return Path(__file__).resolve().parent


def package_source_violations() -> list[str]:
    """Return marker hits in package modules other than this safety module."""

    hits: list[str] = []
    for path in sorted(package_root().rglob("*.py")):
        if path.name == "safety.py":
            continue
        text = path.read_text(encoding="utf-8").lower()
        for marker in FORBIDDEN_SOURCE_MARKERS:
            if marker in text:
                hits.append(f"{path.relative_to(package_root())}:{marker}")
    return hits


def package_import_violations() -> list[str]:
    """Return imports that would bind this package to trading or browser drivers."""

    hits: list[str] = []
    for path in sorted(package_root().rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if _is_forbidden_import(name):
                    hits.append(f"{path.relative_to(package_root())}:{name}")
    return hits


def _is_forbidden_import(name: str) -> bool:
    for banned in FORBIDDEN_IMPORT_MODULES:
        if name == banned or name.startswith(f"{banned}."):
            return True
    return False


FORBIDDEN_JS_MARKERS = (
    ".click(",
    ".submit(",
    "dispatchEvent",
    "document.cookie",
    "XMLHttpRequest",
    "WebSocket(",
    "playwright",
    "selenium",
    "webdriver",
)


def extension_root() -> Path | None:
    """Repo extension dir when running from a checkout; None in a wheel-only install."""

    root = package_root().parents[4] / "extensions" / "mexc_ui_capture"
    return root if root.is_dir() else None


def extension_source_violations() -> list[str]:
    """Fail closed if the unpacked extension grows trading or remote I/O behavior."""

    root = extension_root()
    if root is None:
        return []
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in {".js", ".html"}:
            continue
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        for marker in FORBIDDEN_JS_MARKERS:
            if marker.lower() in lowered:
                hits.append(f"{path.name}:{marker}")
        if "fetch(" in lowered and "chrome.runtime.geturl" not in lowered:
            hits.append(f"{path.name}:fetch_without_runtime_geturl")
        if 'fetch("http' in lowered or "fetch('http" in lowered:
            hits.append(f"{path.name}:remote_fetch")
    return hits
