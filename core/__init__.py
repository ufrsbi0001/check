"""
core/ — Infrastructure package.

Houses foundational modules:
  - config.py          — env → dataclass config
  - config_center.py   — centralized tunables (Phase 1)
  - state.py           — runtime state (active_trades, cooldowns, etc.)
  - client.py          — Binance API wrapper
  - coins_config.py    — coin registry accessors

Root-level shims redirect `from core.config import X` etc. to the
canonical location under core/, so existing imports continue to
work during the migration. Shims will be removed in a later phase
once all internal imports are updated.
"""