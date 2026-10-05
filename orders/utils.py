"""
orders/utils.py — Shared constants and low-level helpers.

REV 23.3 (2026-10-05) — BINANCE UI PARITY VIA BREAK-EVEN PRICE:
  ✅ `get_trade_status()` computes PnL from `breakEvenPrice` (which
     includes entry commission), NOT `entryPrice`. This is what
     Binance Futures UI does.
  ✅ `pnl_pct` and `roi_pct` both derive from the break-even basis.
  ✅ Falls back to `entryPrice` if `breakEvenPrice` is missing
     (older demo API schemas) — zero behaviour change in that case.
  ✅ Sign-safe for both LONG and SHORT.
  ✅ Adds `break_even` field to the response for UI/debug visibility.

REV 23.2.1 (2026-10-05) — LEVERAGE FALLBACK TO CONFIG.
REV 23.2 (2026-10-05) — LEVERAGED ROI IN TRADE STATUS.
REV 23.1 (2026-10-03) — DEAD CONSTANT CLEANUP + SL SELECTION CLARITY.
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
# ═════════════════════════════════════════════════════════════
def _vol_class(symbol: str) -> str:
    try:
        return get_coin_vol_class(symbol)
    except Exception as e:
        logger.debug(f"_vol_class({symbol}) failed: {e}, defaulting to MED")
        return "MED"


def _r_thresholds_per_class(symbol: str, cfg: dict) -> dict:
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
    """Return the most-advanced SL on the book."""
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
    """
    Rich trade status for the UI — SL, level, PnL, side, qty.

    REV 23.2 — adds `roi_pct` (leverage × price move) and `leverage`.
    REV 23.2.1 — leverage falls back to config_center.leverage.
    REV 23.3 — PnL now computed from breakEvenPrice (Binance UI parity).
    """
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
        is_long = amt > 0

        # ── REV 23.3 — Binance UI parity via break-even price ──
        try:
            be_price = float(pos.get('breakEvenPrice', 0) or 0)
        except (TypeError, ValueError):
            be_price = 0.0
        if be_price <= 0:
            be_price = entry

        if is_long:
            pnl = (mark - be_price) * abs(amt)
            pnl_pct = (mark - be_price) / entry * 100
        else:
            pnl = (be_price - mark) * abs(amt)
            pnl_pct = (be_price - mark) / entry * 100

        # ── REV 23.2 / 23.2.1 — leveraged ROI on margin ──
        leverage = None
        try:
            _lev_raw = pos.get('leverage', None)
            if _lev_raw not in (None, ''):
                _lv = int(float(_lev_raw))
                if _lv >= 1:
                    leverage = _lv
        except (TypeError, ValueError):
            leverage = None

        if leverage is None:
            try:
                from core import config_center as _CC
                leverage = int(_CC.get('leverage', 1) or 1)
                if leverage < 1:
                    leverage = 1
            except Exception:
                leverage = 1

        roi_pct = pnl_pct * leverage

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
            'roi_pct': roi_pct,
            'leverage': leverage,
            'break_even': be_price,
            'sl': sl_price, 'sl_level': sl_level,
            'side': 'LONG' if is_long else 'SHORT',
            'qty': abs(amt)
        }
    except Exception as e:
        logger.debug(f"get_trade_status error {symbol}: {type(e).__name__}: {e}")
        return None