# Hibachi ETH perpetual research bot

Safety-first Python service for researching short-horizon strategies on the
Hibachi `ETH/USDT-P` perpetual contract.

## Current milestone

The repository is intentionally **COLLECT-only**. The executable can read public
exchange metadata and validate the configured contract, but it contains no order
placement, cancellation, account, or withdrawal commands.

## Requirements

- Python 3.13+
- Network access to Hibachi public APIs

The Python requirement follows the current official `hibachi-xyz` SDK rather
than the older `3.12+` assumption in the original specification.

## Local setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
hibachi-bot
```

The default configuration uses only public endpoints and requires no API keys.
Secrets must never be committed, logged, or sent to Telegram.

## Safety invariant

`BOT_MODE` must be `collect`. Any other value fails during configuration loading.
Trading modes will be introduced only after data validation, persistent storage,
paper execution, risk controls, and explicit acceptance criteria are implemented.

