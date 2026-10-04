# tests/ — Regression tests for the bot's hardening fixes

## What this covers

Pure-function regression tests for the 8 hardening fixes plus review
Points 4 and 5. These tests do **NOT** hit Binance, place orders, or
touch the network. They only call in-process functions and check
outputs.

Groups:
- **C4** — `_emg_cid` determinism (emergency close idempotency)
- **C5 / C6** — `_risk_size_with_floor_guard` abort logic
- **state.py** — `DailyTracker.is_limit_reached` fail-closed
- **Point 4** — `adopted_fallback_sl_pct` config key + module consistency
- **Utility** — `adjust_qty` / `adjust_price` flooring
- **C2** — fast-reconcile window length

## Setup

```cmd
pip install pytest