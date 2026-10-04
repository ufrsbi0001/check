"""
orders — Facade package.

REV 23.1 (2026-10-03) — LIVE BACKWARD-COMPAT FOR DEAD CONFIG SNAPSHOTS:
  ✅ orders/utils.py REV 23.1 removed six module-level config snapshots
     (LEVERAGE, RISK_PERCENT, MAX_OPEN_POSITIONS, MAX_TOTAL_MARGIN_PCT,
     PARTIAL_CLOSE_USDT, MAX_HOLD_MINUTES). Every live consumer
     (orders/entry.py REV 1.8.0, orders/manage.py REV 1.9.0) already
     reads them LIVE via config_center.
  ✅ This facade no longer imports them statically. Instead a
     module-level __getattr__ (PEP 562) returns them LAZILY via
     config_center.GLOBAL — so:
        • `from orders import LEVERAGE` still works (backward compat)
        • The value is LIVE (runtime update_runtime() reflects here)
        • `from orders.utils import LEVERAGE` raises AttributeError
          (as intended — use config_center directly)
     Verified no in-repo consumer depends on the old static path.
  ✅ __all__ retains the six names so `from orders import *` still
     resolves them (Python calls __getattr__ for each name in
     __all__ if not found on the module).

REV 23.0 (2026-10-02) — UNIFIED CONFIG CLEANUP.
REV 1.5.0 (2026-09-28) — SPLIT FROM orders.py.
"""
from .utils import (
    _get_risk_unit, _pick_real_sl,
    _derive_sl_level, _vol_class, _r_thresholds_per_class,
    get_trade_status,
    DRY_RUN,   # only surviving static constant in utils.py
)

from .exit import (
    emergency_close_retry, robust_cancel_all, handle_trade_close,
    get_total_pnl,
)

from .repair import (
    reconcile_active_trade, cancel_specific_sl, cancel_all_sl_stops,
    cancel_orphan_bot_orders, sync_existing_positions, clean_orders,
)

from .entry import place_order_fixed

from .manage import (
    manage_single_trade, trade_manager_loop,
    _detect_tp1_fill_and_move_be,
)


# ═════════════════════════════════════════════════════════════
#  REV 23.1 — LIVE BACKWARD-COMPAT SHIM (PEP 562)
#  Six config names that used to be static re-exports from
#  orders.utils are now served lazily from config_center.GLOBAL.
#  This gives:
#    • Backward compat: `from orders import LEVERAGE` still works.
#    • Liveness: the returned value tracks runtime updates.
#    • Clean utils.py: no stale snapshots anywhere.
# ═════════════════════════════════════════════════════════════
_CC_KEY_FOR_ORDER_ATTR: dict[str, str] = {
    "LEVERAGE":             "leverage",
    "RISK_PERCENT":         "risk_percent",
    "MAX_OPEN_POSITIONS":   "max_open_positions",
    "MAX_TOTAL_MARGIN_PCT": "max_total_margin_pct",
    "PARTIAL_CLOSE_USDT":   "partial_close_usdt",
    "MAX_HOLD_MINUTES":     "hold_minutes",
}


def __getattr__(name: str):
    """
    PEP 562 module-level __getattr__.

    Resolves the six legacy config names from config_center.GLOBAL
    on EVERY access — so runtime update_runtime() changes propagate.
    Any other unknown name raises AttributeError (normal Python
    behaviour preserved).
    """
    cc_key = _CC_KEY_FOR_ORDER_ATTR.get(name)
    if cc_key is not None:
        from core import config_center as _cc
        return _cc.get(cc_key)
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


__all__ = [
    # Entry
    "place_order_fixed",
    # Manage
    "manage_single_trade", "trade_manager_loop",
    # Repair
    "reconcile_active_trade", "cancel_specific_sl", "cancel_all_sl_stops",
    "cancel_orphan_bot_orders", "sync_existing_positions", "clean_orders",
    # Exit
    "emergency_close_retry", "robust_cancel_all", "handle_trade_close",
    "get_total_pnl",
    # Utils
    "get_trade_status",
    "_get_risk_unit", "_pick_real_sl",
    "_derive_sl_level", "_vol_class", "_r_thresholds_per_class",
    "DRY_RUN",
    # Legacy config names — resolved lazily via __getattr__ (LIVE reads)
    "LEVERAGE", "RISK_PERCENT", "MAX_OPEN_POSITIONS", "MAX_TOTAL_MARGIN_PCT",
    "PARTIAL_CLOSE_USDT", "MAX_HOLD_MINUTES",
]