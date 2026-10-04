"""
orders/utils.py — Shared constants and low-level helpers.

REV 23.1 (2026-10-03) — DEAD CONSTANT CLEANUP + SL SELECTION CLARITY:
  ✅ Removed dead module-level config snapshots:
       LEVERAGE, RISK_PERCENT, MAX_OPEN_POSITIONS,
       MAX_TOTAL_MARGIN_PCT, PARTIAL_CLOSE_USDT, MAX_HOLD_MINUTES.
     All were imported ONCE at module load — runtime update_runtime()
     changes did not propagate. Every consumer (orders/entry.py
     REV 1.8.0, orders/manage.py REV 1.9.0) already migrated to live
     CC.get() reads. Verified dead via grep before removal.
  ✅ Also removed now-unused `from core import config_center as CC`
     module alias (only served the removed snapshots).
  ✅ DRY_RUN retained — still consumed by orders/entry.py.
  ✅ _pick_real_sl: algo-order fallback loop now skips empty-type
     records (matches the primary regular-order loop's strictness).
     Previously an empty type would slip through and could pick a
     TP trigger price as an SL candidate.
  ✅ _derive_sl_level: "no risk_unit" warning downgraded to debug —
     the % fallback path is a designed graceful degradation, not an
     error condition, and it fires frequently on legacy trades.

REV 23.0 (2026-10-02) — UNIFIED CONFIG CLEANUP.
REV 1.5.0 (2026-09-28) — SPLIT FROM orders.py.
"""
from __future__ import annotations

import threading
import time

from core.config import CONFIG

from core.client import (
    get_client, refresh_timestamp, _run_with_timeout,
    _get_open_algo_orders, logger,
)
from core.state import PKT, get_active_trade
from core.coins_config import get_coin_vol_class

# ── REV 23.0 — Unified config source ──
from core.config_center import (
    get_config as _get_central_config,
    VOL_CLASS_R_THRESHOLDS as _VOL_CLASS_R_THRESHOLDS,
)


# ═════════════════════════════════════════════════════════════
#  Constants
# ═════════════════════════════════════════════════════════════
# REV 23.1 — Only DRY_RUN remains here. All trading-parameter
# snapshots were removed; consumers read LIVE via CC.get().
DRY_RUN = CONFIG.dry_run


# ═════════════════════════════════════════════════════════════
#  Shared mutable state (module-local)
# ═════════════════════════════════════════════════════════════
_JUST_CLOSED: dict[str, float] = {}
_JUST_CLOSED_LOCK = threading.Lock()

BOT_CONDITIONAL_TYPES = {
    'STOP_MARKET', 'TAKE_PROFIT_MARKET', 'STOP',
    'TAKE_PROFIT', 'TRAILING_STOP_MARKET',
}

_RECONCILE_SKIP_FRESH_SEC = 90.0


# ═════════════════════════════════════════════════════════════
#  Client accessor
# ═════════════════════════════════════════════════════════════
def _cl():
    return get_client()


# ═════════════════════════════════════════════════════════════
#  Risk-unit helper
# ═════════════════════════════════════════════════════════════
def _get_risk_unit(active: dict | None) -> float | None:
    if not active:
        return None
    try:
        entry = float(active.get('entry', 0) or 0)
        if entry <= 0:
            return None
        init_sl = float(active.get('initial_sl', 0) or 0)
        if init_sl > 0:
            r = abs(entry - init_sl)
            return r if r > 0 else None
        if int(active.get('sl_level', 0) or 0) == 0:
            cur_sl = float(active.get('sl', 0) or 0)
            if cur_sl > 0:
                r = abs(entry - cur_sl)
                return r if r > 0 else None
    except (TypeError, ValueError):
        return None
    return None


# ═════════════════════════════════════════════════════════════
#  Per-vol-class thresholds
#  REV 23.0 — Now served by config_center.VOL_CLASS_R_THRESHOLDS.
# ═════════════════════════════════════════════════════════════
def _vol_class(symbol: str) -> str:
    try:
        return get_coin_vol_class(symbol)
    except Exception as e:
        logger.debug(f"_vol_class({symbol}) failed: {e}, defaulting to MED")
        return "MED"


def _r_thresholds_per_class(symbol: str, cfg: dict) -> dict:
    """
    REV 23.0 — Reads from config_center.VOL_CLASS_R_THRESHOLDS.
    Falls back to MED if vol_class not found.
    """
    vcls = _vol_class(symbol)
    base = _VOL_CLASS_R_THRESHOLDS.get(vcls, _VOL_CLASS_R_THRESHOLDS["MED"])
    return {
        "be_r":          base["be_r"],
        "lock1_r":       base["lock1_r"],
        "lock2_r":       base["lock2_r"],
        "be_stop_r":     base["be_stop_r"],
        "lock1_stop_r":  base["lock1_stop_r"],
        "lock2_stop_r":  base["lock2_stop_r"],
        "partial_stop_r": float(cfg.get("partial_stop_r", 0.30)),
        "max_hold_bars": base["max_hold_bars"],
    }


# ═════════════════════════════════════════════════════════════
#  SL discovery — most advanced SL on the book
# ═════════════════════════════════════════════════════════════
def _pick_real_sl(pair, entry, is_long):
    """
    Return the most-advanced SL on the book.

    "Most advanced" = highest SL for a LONG, lowest SL for a SHORT
    (i.e. closest to or past entry — reflects the current SL level).

    REV 23.1 — algo-order fallback now skips empty-type records,
    matching the primary regular-order loop. Otherwise an algo order
    with no type field could contribute its trigger price (possibly
    a TP) as an SL candidate.
    """
    candidates = []
    client = _cl()
    if client is None:
        return None, []

    try:
        orders = _run_with_timeout(
            lambda: client.futures_get_open_orders(symbol=pair),
            4, f"pick_sl:{pair}"
        )
        for o in orders or []:
            otype = (o.get('type') or '').upper()
            if otype not in ('STOP_MARKET', 'STOP'):
                continue
            try:
                v = float(o.get('stopPrice') or o.get('triggerPrice') or 0)
            except (TypeError, ValueError):
                continue
            if v > 0 and v > entry * 0.5 and v < entry * 1.5:
                candidates.append(v)
    except Exception as e:
        logger.debug(f"[_pick_real_sl] {pair}: {e}")

    if not candidates:
        try:
            for o in _get_open_algo_orders(pair):
                otype = (o.get('type') or o.get('orderType') or '').upper()
                # REV 23.1 — strict type match (empty type excluded).
                if otype not in ('STOP_MARKET', 'STOP'):
                    continue
                try:
                    v = float(o.get('triggerPrice') or o.get('stopPrice') or 0)
                except (TypeError, ValueError):
                    continue
                if v > 0 and v > entry * 0.5 and v < entry * 1.5:
                    candidates.append(v)
        except Exception:
            pass

    if not candidates:
        return None, []

    chosen = max(candidates) if is_long else min(candidates)
    return chosen, candidates


# ═════════════════════════════════════════════════════════════
#  SL level derivation (0=initial, 1=BE, 2=lock1, 3=lock2)
# ═════════════════════════════════════════════════════════════
def _derive_sl_level(is_long, actual_sl, entry, risk_unit=None, thresholds=None):
    if thresholds is None:
        thresholds = _VOL_CLASS_R_THRESHOLDS["MED"]
    be_r  = thresholds["be_stop_r"]
    lk1_r = thresholds["lock1_stop_r"]
    lk2_r = thresholds["lock2_stop_r"]

    if is_long:
        if risk_unit and risk_unit > 0:
            be_stop  = entry + risk_unit * be_r
            lk1_stop = entry + risk_unit * lk1_r
            lk2_stop = entry + risk_unit * lk2_r
            tol = risk_unit * 0.05
            if actual_sl >= lk2_stop - tol:  return 3
            if actual_sl >= lk1_stop - tol:  return 2
            if actual_sl >= be_stop  - tol:  return 1
            return 0
        # REV 23.1 — debug not warning: % fallback is by-design graceful.
        logger.debug(f"_derive_sl_level: no risk_unit for entry={entry}")
        if actual_sl >= entry * 1.005:   return 3
        elif actual_sl >= entry * 1.002: return 2
        elif actual_sl >= entry * 0.998: return 1
        else:                            return 0
    else:
        if risk_unit and risk_unit > 0:
            be_stop  = entry - risk_unit * be_r
            lk1_stop = entry - risk_unit * lk1_r
            lk2_stop = entry - risk_unit * lk2_r
            tol = risk_unit * 0.05
            if actual_sl <= lk2_stop + tol:  return 3
            if actual_sl <= lk1_stop + tol:  return 2
            if actual_sl <= be_stop  + tol:  return 1
            return 0
        logger.debug(f"_derive_sl_level: no risk_unit for entry={entry}")
        if actual_sl <= entry * 0.995:   return 3
        elif actual_sl <= entry * 0.998: return 2
        elif actual_sl <= entry * 1.002: return 1
        else:                            return 0


# ═════════════════════════════════════════════════════════════
#  get_trade_status — read-only UI helper
# ═════════════════════════════════════════════════════════════
def get_trade_status(symbol, pos: dict = None):
    """Rich trade status for the UI — SL, level, PnL, side, qty."""
    pair = symbol + 'USDT'
    client = _cl()
    if client is None:
        return None
    try:
        if pos is None:
            refresh_timestamp()
            pos_list = client.futures_position_information(symbol=pair)
            if not pos_list:
                return None
            pos = pos_list[0]

        amt = float(pos['positionAmt'])
        if amt == 0:
            return None
        entry = float(pos['entryPrice'])
        if entry <= 0:
            logger.debug(f"get_trade_status {symbol}: entry=0, skipping")
            return None
        mark = float(pos['markPrice'])
        pnl = float(pos['unRealizedProfit'])
        pnl_pct = ((mark - entry) / entry * 100) if amt > 0 else ((entry - mark) / entry * 100)
        is_long = amt > 0
        active = get_active_trade(symbol)
        risk_unit = _get_risk_unit(active) if active else None
        sl_price, _ = _pick_real_sl(pair, entry, is_long)

        if sl_price and sl_price > 0:
            if risk_unit and risk_unit > 0:
                th = _VOL_CLASS_R_THRESHOLDS.get(_vol_class(symbol),
                                                 _VOL_CLASS_R_THRESHOLDS["MED"])
                be_r  = th["be_stop_r"]
                lk1_r = th["lock1_stop_r"]
                lk2_r = th["lock2_stop_r"]
                tol = risk_unit * 0.05
                if is_long:
                    be_stop  = entry + risk_unit * be_r
                    lk1_stop = entry + risk_unit * lk1_r
                    lk2_stop = entry + risk_unit * lk2_r
                    if sl_price >= lk2_stop - tol:
                        sl_level = f"LOCK2 (+{risk_unit*lk2_r:.4f})"
                    elif sl_price >= lk1_stop - tol:
                        sl_level = f"LOCK1 (+{risk_unit*lk1_r:.4f})"
                    elif sl_price >= be_stop - tol:
                        sl_level = f"BE (+{risk_unit*be_r:.4f})"
                    else:
                        sl_level = f"INITIAL (${sl_price:.4f})"
                else:
                    be_stop  = entry - risk_unit * be_r
                    lk1_stop = entry - risk_unit * lk1_r
                    lk2_stop = entry - risk_unit * lk2_r
                    if sl_price <= lk2_stop + tol:
                        sl_level = f"LOCK2 (-{risk_unit*lk2_r:.4f})"
                    elif sl_price <= lk1_stop + tol:
                        sl_level = f"LOCK1 (-{risk_unit*lk1_r:.4f})"
                    elif sl_price <= be_stop + tol:
                        sl_level = f"BE (-{risk_unit*be_r:.4f})"
                    else:
                        sl_level = f"INITIAL (${sl_price:.4f})"
            else:
                if is_long:
                    if sl_price >= entry * 1.005:   sl_level = "LOCK2 (0.6%)"
                    elif sl_price >= entry * 1.002: sl_level = "LOCK1 (0.3%)"
                    elif sl_price >= entry * 0.998: sl_level = "BREAKEVEN"
                    else:                           sl_level = f"INITIAL (${sl_price:.4f})"
                else:
                    if sl_price <= entry * 0.995:   sl_level = "LOCK2 (0.6%)"
                    elif sl_price <= entry * 0.998: sl_level = "LOCK1 (0.3%)"
                    elif sl_price <= entry * 1.002: sl_level = "BREAKEVEN"
                    else:                           sl_level = f"INITIAL (${sl_price:.4f})"
        else:
            stored = active.get('sl_level', 0) if active else 0
            if stored == 1:   sl_level = "BREAKEVEN (stale?)"
            elif stored == 2: sl_level = "LOCK1 (stale?)"
            elif stored == 3: sl_level = "LOCK2 (stale?)"
            else:             sl_level = "INITIAL (no SL!)"

        return {
            'symbol': symbol, 'entry': entry, 'mark': mark,
            'pnl': pnl, 'pnl_pct': pnl_pct,
            'sl': sl_price, 'sl_level': sl_level,
            'side': 'LONG' if is_long else 'SHORT',
            'qty': abs(amt)
        }
    except Exception as e:
        logger.debug(f"get_trade_status error {symbol}: {type(e).__name__}: {e}")
        return None