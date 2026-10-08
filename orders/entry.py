"""
orders/entry.py — Order placement.

REV 1.9.10 (2026-10-08) — MIN-NOTIONAL CEIL + READ-LOCK COMPLETION:
  ✅ CRITICAL: min_notional floor/bump now CEIL to next step, not floor.
     Previously coarse-step coins (e.g. step=0.1, price=33,
     min_notional=$5) produced qty=0.1 → notional $3.30 < $5 →
     silent abort of otherwise-valid trades ("Still below min notional,
     aborting"). Reproduced with:
         min_notional=5, price=33, step=0.1, bump_mult=1.02
         qty = 5/33 * 1.02 = 0.154545
         old: floor to 0.1  → 0.1*33 = $3.30  ✗
         new: ceil  to 0.2  → 0.2*33 = $6.60  ✓
     Fix applied in TWO places:
       • _risk_size_with_floor_guard: floor_qty now ceils
       • place_order_fixed min_notional bump: qty_dec now ceils
     NOTE: the max_qty cap still FLOORS (rounds down) — that path
     must never exceed the exchange ceiling.

  ✅ HIGH: futures_account / futures_position_information /
     futures_change_margin_type / futures_change_leverage now wrapped
     in _read_lock / _write_lock respectively, completing the
     REV 1.9.3 lock migration. requests.Session is not thread-safe;
     without these locks, the entry thread and trade-manager
     background thread could corrupt the connection pool.

REV 1.9.8 (2026-10-08) — PRE-ENTRY RR GUARD (RR_COLLAPSE FIX):
  ✅ CRITICAL: predict post-cap RR BEFORE sending the market order.
     If the coin's TP1 cap + the signal's SL distance make min_rr
     mathematically impossible, SKIP the trade — never enter and
     then flatten on the post-fill RR_COLLAPSE check.

     Root cause (TRUMP, DOT, RENDER on 2026-10-06/07):
       • Signal engine emits wide ATR-based SL (e.g. 8.9% on TRUMP).
       • Coin's TP1 cap (coins_config.get_caps) is tighter (e.g. 9.7%).
       • Post-cap RR = 9.7/8.9 = 1.09 < min_rr 1.50 → COLLAPSE.
       • Old flow: enter → place SL → cap TP → RR check fails →
         immediate flatten. Paid spread + taker fees twice, and the
         strategy often re-emitted the same signal → churn.

     New flow: guard detects the impossibility pre-entry, releases
     the slot, returns False — no order is ever sent. Zero churn.

     Fail-open: if the guard itself raises, log a warning and
     proceed with the entry. A missed guard is preferable to a
     skipped valid trade. Downstream RR_COLLAPSE check remains as
     defence-in-depth for the (rare) case where realised slippage
     pushes a borderline-tolerable RR below the floor.

REV 1.9.9 (2026-10-08) — WRITE-LOCK ON 4 ORDER-PLACEMENT SITES:
  ✅ CRITICAL: every futures_create_order call now runs inside
     `_write_lock`. Previously 4 sites ran without it:
       • _place        (market entry)
       • _place_sl     (initial STOP_MARKET)
       • _place_tp1    (TAKE_PROFIT_MARKET #1)
       • _place_tp2    (TAKE_PROFIT_MARKET #2)
     These calls are dispatched through _run_with_timeout, which
     spawns a worker thread via a ThreadPoolExecutor. Without the
     lock, the entry worker and the trade-manager background thread
     could concurrently use the same requests.Session, corrupting
     the connection pool or interleaving order responses.

     Safety: _run_with_timeout does NOT acquire _write_lock itself
     (verified in core/client.py REV 11.7). _write_lock is an
     RLock (core/client.py line 233), so same-thread re-entry is
     safe. No deadlock risk — worker simply waits its turn if the
     trade manager holds the lock, which is the correct behavior.

REV 1.9.7 (2026-10-05) — BINANCE 36-CHAR CLIENT-ORDER-ID FIX:
  ✅ CRITICAL: `_client_order_id` previously produced cids up to 38
     chars for 12-char symbols (1000SHIBUSDT, 1000BONKUSDT,
     1000FLOKIUSDT, 1000PEPEUSDT, PUMPBTCUSDT, ...). Binance rejects
     any newClientOrderId > 36 chars with -4015, so every market entry
     on those coins was silently REJECTED. Observed live at 18:09:15
     on 2026-10-05:
         [1000SHIBUSDT] market order rejected:
           APIError(code=-4015): Client order id length should be
           less than 36 chars
     Fix (two parts, both inside `_client_order_id`):
       1. Strip the trailing 'USDT' from the symbol portion — the
          Binance API always pairs the cid with a `symbol=` param on
          lookup, so the suffix was redundant.
       2. Reduce uuid hex from 16 → 14 chars (2^56 entropy per
          attempt — still astronomically collision-free).
     New worst-case cid lengths:
         tb_entry_1000SHIB_<14hex>   = 32 chars
         tb_entry_1000FLOKI_<14hex>  = 33 chars
         tb_entry_PUMPBTC_<14hex>    = 31 chars
     All safely under the 36-char ceiling. SL/TP inline cids were
     already compliant (max 32) and are untouched.

REV 1.9.6 (2026-10-05) — STALE-SIGNAL SLIPPAGE GUARD + POST-FLATTEN COOLDOWN:
  ✅ CRITICAL (G1): the post-fill adverse-slippage check no longer
     fires on STALE signals. Root cause of the CRV churn loop observed
     in production (2026-10-05):

       The strategy emits a signal at price P_sig. By the time the
       order reaches the exchange, the price has already drifted to
       P_est (pre-check validated this gap as ≤ signal_drift_pct).
       The MARKET fill then lands at P_fill ≈ P_est + spread.

       The old check compared P_fill against P_sig and flattened
       whenever the TOTAL drift exceeded max_fill_slippage_pct — even
       though the pre-entry portion of that drift had ALREADY been
       validated by the SIGNAL DRIFT REJECT gate.

       Concrete failure (CRV, 2026-10-05, ~16:30):
         signal=0.380200  est=0.381700  fill=0.382200
         adverse_vs_signal = +0.526%   > 0.500%   → flatten
         adverse_vs_est    = +0.131%   << 0.500%   (fine!)
       The signal was ~1-3s stale; the strategy kept re-emitting the
       SAME signal on every scan (its own internal throttle did not
       cooldown after flatten), so the bot entered and immediately
       flattened ~6 times in 3 minutes, bleeding balance from
       spread + taker fees each cycle.

     Fix: the post-fill check now decides which reference price to
     use based on how stale the signal was at order time:

       sig_est_gap = |entry_price_est - signal_price| / signal_price

       • If sig_est_gap > 50% of max_fill_slippage_pct → signal is
         effectively stale. Use drift-vs-EST (measures execution
         slippage only). This is the correct metric — pre-entry
         drift was already validated by the drift gate above.
       • Otherwise → signal is fresh. Keep the original drift-vs-
         SIGNAL behaviour (catches fills that slipped beyond both
         estimates).

     Zero change when the signal is fresh. Only the stale-signal
     path changes, which is exactly the case that was broken.

  ✅ CRITICAL (G2): after a slippage-flatten, set a cooldown on the
     symbol. Without it, the next scan (15-30s later) re-emits the
     same still-active signal and immediately re-enters the same
     churning trade. Cooldown length is configurable via
     `cooldown_after_slippage_min` (default 5 min); it uses the
     existing `cooldown_until` dict from core.state, so no new
     state, no new locks, and the entry guard at the top of
     place_order_fixed already honours it.

     This is the DEFENCE-IN-DEPTH layer: even if G1's freshness
     heuristic is imperfect for a future symbol, the cooldown
     guarantees no more than one slippage-flatten per symbol per
     cooldown window.

  ✅ NEW (G2b) — ENV-FIRST COOLDOWN READ:
     Observed in production (2026-10-05 17:16): config_center rejected
     the new key with
         "[config_center] ⚠️ env override IGNORED:
          CC_COOLDOWN_AFTER_SLIPPAGE_MIN='5' — key
          'cooldown_after_slippage_min' not in target dict"
     because the key is not (yet) registered in config_center's
     target dict. The function therefore silently fell back to the
     hardcoded default (5 min) — functionally correct, but the
     operator lost the ability to tune the cooldown from .env.

     Fix: _set_post_flatten_cooldown() now reads the value with this
     precedence chain:

         1. os.environ['CC_COOLDOWN_AFTER_SLIPPAGE_MIN']  (env, direct)
         2. os.environ['COOLDOWN_AFTER_SLIPPAGE_MIN']     (env, no prefix)
         3. config_center GLOBAL['cooldown_after_slippage_min'] (whitelist)
         4. hardcoded 5 (last resort)

     Behaviour is unchanged when config_center eventually whitelists
     the key (env still wins, as it does for every other override in
     config_center). The env-first path just removes the operational
     dead-end observed on 2026-10-05.

REV 1.9.5 (2026-10-05) — SIZING BASE INCLUDES UNREALIZED PNL (H4b):
  ✅ MEDIUM (H4b): the wallet-balance base used for risk sizing is now
     `totalMarginBalance` (wallet + unrealized PnL) instead of
     `totalWalletBalance` (wallet only). Previously, whenever the
     account held underwater positions, the bot sized NEW entries
     against an inflated balance that ignored those losses.

     Example: wallet=$4500, open unrealized PnL=-$300. Real equity is
     $4200, but the old code sized the next trade off $4500 — a ~7%
     over-size relative to actual capital at risk. Over a losing
     streak with multiple concurrent positions, this compounds: each
     new entry assumes capital that has already been lost.

     Fix: read `totalMarginBalance` first (this is Binance's canonical
     "wallet + unrealized" figure and matches what the account cache
     and DD gate already use, via get_balance_or_last_good). Fallback
     chain preserves backward compatibility:
         totalMarginBalance
         → totalWalletBalance    (older API schemas)
         → availableBalance      (last-resort, existing behaviour)

     Zero change when the account has no open positions (unrealized
     PnL == 0 → totalMarginBalance == totalWalletBalance). Zero change
     for the DD gate, hold-time, or any other logic — this only
     affects the base used for the per-trade risk calculation.

REV 1.9.4 (2026-10-05) — POST-FILL CRASH SAFETY + HOLD-TIME ORIGIN FIX (retained):
  ✅ CRITICAL (C3): outer exception handler now flattens any residual
     position instead of leaving it naked.
  ✅ HIGH (H2): entry_time stamped AFTER market order, not before.

REV 1.9.3 (2026-10-05) — READ/WRITE LOCK MIGRATION (Phase 2, Step 6) (retained).
REV 1.9.2 (2026-10-04) — CRITICAL DOUBLE-ENTRY + SIZING GUARD FIXES (retained).
REV 1.9.1 (2026-10-04) — FILTER-BUST RECOMPUTE + MINOR CLEANUP (retained).
REV 1.9.0 (2026-10-03) — HARDENING PASS (retained).
REV 1.8.1 (2026-10-03) — ZERO-COERCION FIX (retained).
REV 1.8.0 (2026-10-02) — RUNTIME TOGGLE AWARENESS (retained).
"""
from __future__ import annotations

import os
import random
import threading
import time
import uuid
from decimal import Decimal
from datetime import datetime, timedelta
from typing import Optional

from binance.exceptions import BinanceAPIException

from core.client import (
    fetch_position_raw, get_filters, adjust_qty, adjust_price,
    invalidate_account_cache, refresh_timestamp,
    _run_with_timeout, send_telegram, logger,
    # ── REV 1.9.3 / 1.9.10 — explicit locks (Phase 2). ──
    # Every explicit lock site in this file wraps either a
    # futures_create_order (WRITE) or a read-only account/position
    # call (READ). The legacy _requests_lock alias is no longer
    # imported here on purpose: any missed migration site will raise
    # a NameError at runtime rather than silently over-serialize.
    _read_lock, _write_lock, VALID_SYMBOLS, _filters_ok_to_trade,
    handle_order_filter_error,
)
from core.state import (
    PKT, add_active_trade,
    bot_tracked_symbols, BOT_TRACKED_LOCK,
    cooldown_until, COOLDOWN_LOCK,
    CSV_FILE,
)
from market.indicators import get_trading_config
from core.coins_config import get_caps
from .utils import DRY_RUN, _cl
from .exit import emergency_close_retry, handle_trade_close

from core.config_center import get_config as _get_central_config
from core.config_center import get_min_rr as _get_min_rr_central
from core.config_center import get as _cc_get
from core.config_center import VOL_CLASS_QTY_MULT as _VOL_CLASS_QTY_MULT

try:
    from signals.decision_engine import COUNTER_TREND_STRATEGIES as _CT_STRATS
except Exception:
    _CT_STRATS = frozenset()

# ═════════════════════════════════════════════════════════════
#  REV 1.8.1 — SAFE CC NUMERIC READ
# ═════════════════════════════════════════════════════════════
def _cc_get_num(key: str, default):
    v = _cc_get(key, None)
    return default if v is None else v

# ═════════════════════════════════════════════════════════════
#  REV 1.9.0 — IDEMPOTENCY + AMBIGUITY RESOLUTION
#  REV 1.9.7 — BINANCE 36-CHAR CID LIMIT
# ═════════════════════════════════════════════════════════════
def _client_order_id(symbol: str, tag: str) -> str:
    """
    Deterministic per-attempt client order id. A RETRY with a new tag
    is a NEW order; an AMBIGUOUS OUTCOME is resolved by re-querying
    the SAME id.

    REV 1.9.7 (2026-10-05) — BINANCE 36-CHAR CID LIMIT:
      Binance rejects any newClientOrderId longer than 36 characters
      with -4015. The old format was:
          tb_{tag}_{symbol}_{uuid16}
      For 12-char symbols (1000SHIBUSDT, 1000BONKUSDT, 1000FLOKIUSDT,
      1000PEPEUSDT, PUMPBTCUSDT, etc.) plus a 9-char tag ("entry"),
      this produced 38 chars — every market entry on those coins was
      rejected with -4015.

      Observed live: 1000SHIB market entry REJECTED at 18:09:15 on
      2026-10-05 with cid
          tb_entry_1000SHIBUSDT_cae7b6becb104b48  (38 chars)

      Fix — two parts:
        1. Strip the trailing 'USDT' suffix from the symbol portion.
           The Binance API always pairs the cid with a `symbol=` param
           on lookup, so the suffix was redundant. 1000SHIBUSDT → 1000SHIB.
        2. Reduce uuid hex from 16 → 14 chars. Still 2^56 entropy per
           attempt — astronomically collision-free for the lifetimes
           involved (a single bot's order history).

      New worst-case length:
          tb_entry_1000SHIB_<14hex>   = 9 + 8 + 1 + 14 = 32 chars
          tb_entry_1000FLOKI_<14hex>  = 9 + 9 + 1 + 14 = 33 chars
          tb_entry_PUMPBTC_<14hex>    = 9 + 7 + 1 + 14 = 31 chars
      All safely under the 36-char ceiling.

      Backward compatibility: this function is ONLY called from the
      market-entry path. Existing SL/TP orders use their own inline
      format with 12-char uuid (max 32 chars, already compliant) and
      are NOT affected. No in-flight order lookup depends on the
      previous format.
    """
    short = symbol[:-4] if symbol.endswith('USDT') else symbol
    return f"tb_{tag}_{short}_{uuid.uuid4().hex[:14]}"

def _resolve_ambiguous_market(client, pair: str, cid: str,
                              timeout_s: float = 8.0):
    """
    After ANY exception/timeout on a market entry, the position is
    UNKNOWN. Poll by origClientOrderId until the order reaches a
    terminal or fill-bearing state.

    Returns (status, executed_qty, avg_price).
    status in {'FILLED', 'PARTIALLY_FILLED', 'CANCELED', 'EXPIRED',
               'REJECTED', 'NEW', 'UNKNOWN'}
    """
    deadline = time.time() + timeout_s
    last_status = 'UNKNOWN'
    while time.time() < deadline:
        try:
            o = client.futures_get_order(symbol=pair, origClientOrderId=cid)
            if not o:
                time.sleep(0.4)
                continue
            status = (o.get('status') or 'UNKNOWN').upper()
            last_status = status
            if status in ('FILLED', 'PARTIALLY_FILLED'):
                return (
                    status,
                    abs(float(o.get('executedQty', 0) or 0)),
                    float(o.get('avgPrice', 0) or 0),
                )
            if status in ('CANCELED', 'EXPIRED', 'REJECTED'):
                return status, 0.0, 0.0
            # NEW / PENDING_NEW — keep polling
        except Exception as e:
            # -2013 = order does not exist yet; keep polling
            if '-2013' not in str(e):
                logger.debug(f"[{pair}] resolve poll: {type(e).__name__}")
        time.sleep(0.4)
    return last_status, 0.0, 0.0

# ═════════════════════════════════════════════════════════════
#  REV 1.9.6 (G2 / G2b) — POST-FLATTEN COOLDOWN
# ═════════════════════════════════════════════════════════════
def _set_post_flatten_cooldown(symbol: str, reason: str = "flatten") -> None:
    """
    REV 1.9.6 (G2) — set a per-symbol cooldown after a defensive
    flatten.

    Prevents the churn loop where the strategy re-emits the same
    still-active signal on the next scan and immediately re-enters
    the same losing trade. Uses the existing `cooldown_until` dict
    (guarded by `COOLDOWN_LOCK`) which the entry guard at the top of
    place_order_fixed already honours — so no new state, no new
    locks, no race.

    REV 1.9.6 (G2b) — ENV-FIRST COOLDOWN READ:
      Config_center's env-override layer only accepts keys that are
      already registered in its target dict. This new key isn't, so
      a "⚠️ env override IGNORED ... key ... not in target dict"
      warning was emitted on startup and the .env value had no
      effect (the code silently fell back to the default 5 min).

      Fix: read the value in this precedence order:
        1. os.environ['CC_COOLDOWN_AFTER_SLIPPAGE_MIN']   (direct)
        2. os.environ['COOLDOWN_AFTER_SLIPPAGE_MIN']      (no prefix)
        3. config_center GLOBAL['cooldown_after_slippage_min']
        4. hardcoded default 5

      Set the cooldown to 0 to disable.

    This function is intentionally FAIL-OPEN: if anything goes
    wrong while setting the cooldown (config lookup, clock issue),
    the entry is not blocked — we only lose the churn protection.
    A missed cooldown is annoying; a missed entry is a bug.
    """
    try:
        # ── REV 1.9.6 (G2b) — env-first read ──
        _cd_min = None
        for _env_key in (
            "CC_COOLDOWN_AFTER_SLIPPAGE_MIN",
            "COOLDOWN_AFTER_SLIPPAGE_MIN",
        ):
            _env_val = os.environ.get(_env_key)
            if _env_val is not None and str(_env_val).strip() != "":
                try:
                    _cd_min = float(_env_val)
                    break
                except (TypeError, ValueError):
                    logger.warning(
                        f"[{symbol}] invalid {_env_key}="
                        f"{_env_val!r} — falling through to next source"
                    )
                    continue

        # ── Fall back to config_center, then hardcoded default ──
        if _cd_min is None:
            _cd_min = float(_cc_get_num("cooldown_after_slippage_min", 5))

        if _cd_min <= 0:
            return

        _until = datetime.now(PKT) + timedelta(minutes=_cd_min)
        with COOLDOWN_LOCK:
            cooldown_until[symbol] = _until
        logger.warning(
            f"[{symbol}] {_cd_min:.1f}m cooldown set after {reason} "
            f"(until {_until.strftime('%H:%M:%S')}) — prevents churn"
        )
    except Exception as e:
        logger.debug(f"[{symbol}] cooldown set failed ({reason}): {e}")

# ═════════════════════════════════════════════════════════════
#  REV 1.9.0 — RISK SIZING WITH FLOOR GUARD
#  REV 1.9.10 — FLOOR CEILS TO STEP (min_notional must be honored)
# ═════════════════════════════════════════════════════════════
def _risk_size_with_floor_guard(
    wallet_balance: float,
    risk_percent: float,
    ref_price: float,
    sl_price: float,
    step: Decimal,
    min_qty: Decimal,
    min_notional: float,
    max_qty: Optional[Decimal],
    max_oversize_mult: float = 1.20,
) -> Optional[Decimal]:
    """
    Returns a qty sized to risk_percent that respects exchange floors,
    OR None if the floor would force risk beyond max_oversize_mult ×
    budget.

    NEVER bumps qty up to satisfy exchange minimums. A skipped trade
    costs nothing; an oversized trade in a flash crash costs the
    account.

    REV 1.9.10 — FLOOR CEIL:
      `floor_qty` previously floored to step, which could UNDERSHOOT
      min_notional (e.g. step=0.1, price=33, min_notional=5 → old
      floor_qty = 0.1 → notional $3.30 < $5 → downstream abort).
      Now we ceil to the next step so the floor GUARANTEES
      notional ≥ min_notional.
    """
    try:
        wb = Decimal(str(wallet_balance))
        rp = Decimal(str(risk_percent))
        rpx = Decimal(str(ref_price))
        spx = Decimal(str(sl_price))
    except Exception:
        return None

    if wb <= 0 or rp <= 0 or rpx <= 0:
        return None

    risk_amount = wb * (rp / Decimal('100'))
    dist = abs(rpx - spx)
    if dist <= 0:
        return None

    qty = risk_amount / dist
    qty = (qty // step) * step
    if qty <= 0:
        return None

    if max_qty is not None and qty > max_qty:
        # NOTE: cap path FLOORS — must never exceed exchange maxQty.
        qty = (max_qty // step) * step

    # Floor: max of minQty and minNotional/price, step-aligned.
    floor_qty = max(min_qty, Decimal(str(min_notional)) / rpx)
    # ✅ REV 1.9.10 FIX: CEIL to next step — floor must GUARANTEE
    # notional >= min_notional, not undershoot it.
    _rem = floor_qty % step
    if _rem > 0:
        floor_qty = floor_qty - _rem + step
    if floor_qty <= 0:
        return None

    if qty < floor_qty:
        forced_risk = floor_qty * dist
        forced_pct = float(forced_risk / wb * Decimal('100'))
        budget_with_tol = risk_percent * max_oversize_mult
        if forced_pct > budget_with_tol:
            logger.warning(
                f"[SIZING] ABORT: exchange floor {floor_qty} forces "
                f"{forced_pct:.3f}% risk > {budget_with_tol:.3f}% budget"
            )
            return None
        logger.info(
            f"[SIZING] floor accepted: {float(qty)} → {floor_qty} "
            f"({forced_pct:.3f}% risk, target {risk_percent}%)"
        )
        qty = floor_qty

    # Final invariant check
    actual_pct = float(qty * dist / wb * Decimal('100'))
    if actual_pct > risk_percent * max_oversize_mult:
        logger.warning(
            f"[SIZING] ABORT: final risk {actual_pct:.3f}% exceeds "
            f"{risk_percent * max_oversize_mult:.3f}% budget"
        )
        return None
    return qty

# ═════════════════════════════════════════════════════════════
#  SIZING SCALERS (unchanged logic; live config reads)
# ═════════════════════════════════════════════════════════════
def _apply_vol_class_sizing(symbol: str, qty_dec: Decimal,
                            step: Decimal, min_qty: Decimal) -> Decimal:
    try:
        from core.coins_config import get_coin_vol_class
        vcls = get_coin_vol_class(symbol)
    except Exception:
        vcls = "MED"

    _mult_f = _VOL_CLASS_QTY_MULT.get(vcls, 0.70)
    mult = Decimal(str(_mult_f))
    try:
        scaled = qty_dec * mult
        scaled = (scaled // step) * step
        if scaled < min_qty:
            scaled = min_qty
        if scaled < qty_dec:
            logger.info(
                f"[{symbol}] vol-class {vcls} size {mult}x → {scaled} "
                f"(was {qty_dec})"
            )
        return scaled
    except Exception as e:
        logger.debug(f"[{symbol}] vol sizing failed: {e}")
        return qty_dec

def _apply_counter_trend_sizing(symbol: str, strategy: str,
                                qty_dec: Decimal, step: Decimal,
                                min_qty: Decimal) -> Decimal:
    if not _CT_STRATS:
        return qty_dec

    _strat = (strategy or "").strip()
    is_counter_trend = (
        _strat in _CT_STRATS
        or _strat == ""
        or _strat.upper() == "UNKNOWN"
    )
    if not is_counter_trend:
        return qty_dec

    try:
        _mult_f = float(_cc_get_num("counter_trend_size_mult", 0.60))
        mult = Decimal(str(_mult_f))
        scaled = qty_dec * mult
        scaled = (scaled // step) * step
        if scaled < min_qty:
            scaled = min_qty
        if scaled < qty_dec:
            _label = _strat if _strat else "UNKNOWN"
            logger.info(
                f"[{symbol}] counter-trend size {mult}x → {scaled} "
                f"(was {qty_dec}) [{_label}]"
            )
        return scaled
    except Exception as e:
        logger.debug(f"[{symbol}] counter-trend scaling failed: {e}")
        return qty_dec

def _snapshot_regime(symbol: str, strategy: str) -> tuple[str, int]:
    _regime_now = 'UNKNOWN'
    _hold_min = int(_cc_get_num('hold_minutes', 180))
    try:
        from market.indicators import get_cached_indicator
        _ind_1h = get_cached_indicator(symbol + 'USDT', '1h')
        if _ind_1h:
            _regime_now = _ind_1h.get('regime', 'UNKNOWN')
            _cfg_at_entry = _get_central_config(symbol, strategy, _regime_now)
            _hold_min = int(_cfg_at_entry.get('hold_minutes', _hold_min))
            logger.info(
                f"[{symbol}] REGIME SNAPSHOT: {_regime_now} "
                f"→ hold={_hold_min}m, "
                f"sl_atr={_cfg_at_entry.get('sl_atr')}, "
                f"tp1_atr={_cfg_at_entry.get('tp1_atr')}, "
                f"tp_mult={_cfg_at_entry.get('tp_mult')}"
            )
    except Exception as _re:
        logger.debug(f"[{symbol}] regime snapshot failed: {_re}")
    return _regime_now, _hold_min

def _get_regime_now(symbol: str) -> str:
    try:
        from market.indicators import get_cached_indicator
        _ind = get_cached_indicator(symbol + 'USDT', '1h')
        if _ind:
            return _ind.get('regime', 'UNKNOWN') or 'UNKNOWN'
    except Exception:
        pass
    return 'UNKNOWN'

# ═════════════════════════════════════════════════════════════
#  MARKET ORDER PLACEMENT WITH IDEMPOTENCY
#  REV 1.9.2 (C1) — no fresh cid on UNKNOWN ambiguity
#  REV 1.9.9 — market entry now runs inside _write_lock
# ═════════════════════════════════════════════════════════════
def _place_market_idempotent(client, pair: str, side: str, qty_str: str,
                             cid: str, max_attempts: int = 2):
    """
    Returns (status, exec_qty, avg_price, cid_used).
      status ∈ {'FILLED','PARTIALLY_FILLED','CANCELED','EXPIRED',
                'REJECTED','NEW','UNKNOWN'}

    REV 1.9.2 (C1) — DOUBLE-ENTRY GUARD:
      A NEW cid is generated ONLY when the resolver returns a TERMINAL
      non-fill status (CANCELED / EXPIRED / REJECTED). On 'UNKNOWN'
      (resolver could not confirm the order state), the function
      returns IMMEDIATELY with the original cid so the caller can
      register the trade as `unverified` and let reconcile handle it.
      Retrying with a fresh cid on UNKNOWN would risk placing a
      SECOND MARKET order if the first one actually landed but its
      ack was lost — doubling the position.

    REV 1.9.9 — WRITE-LOCK:
      The `_place` inner function now acquires `_write_lock` before
      calling `client.futures_create_order`. It runs inside a
      worker thread spawned by `_run_with_timeout`, which does NOT
      hold the lock itself (verified in core/client.py REV 11.7).
      `_write_lock` is an RLock, so if the caller happens to hold
      it (defensive), re-entry is safe. The lock serializes this
      market entry against the trade manager's SL updates, which
      use the same requests.Session.
    """
    for attempt in range(max_attempts):
        try:
            def _place():
                with _write_lock:
                    return client.futures_create_order(
                        symbol=pair, side=side, type='MARKET',
                        quantity=qty_str, newClientOrderId=cid,
                    )
            # REV 1.9.1 — 6s matches REV 11.2 executor cap.
            resp = _run_with_timeout(_place, 6, f"market:{pair}")
            if not resp:
                # No response but no exception — treat as ambiguous.
                s, eq, ap = _resolve_ambiguous_market(client, pair, cid)
                if eq > 0:
                    return s, eq, ap, cid
                # ── REV 1.9.2 (C1) ──
                # Only retry with a fresh cid on a TERMINAL non-fill.
                if s in ('CANCELED', 'EXPIRED', 'REJECTED'):
                    cid = _client_order_id(pair, "entry")
                    continue
                logger.critical(
                    f"[{pair}] market entry UNKNOWN "
                    f"(cid={cid}, status={s}) — NOT retrying with "
                    f"fresh cid to avoid double-entry"
                )
                return 'UNKNOWN', 0.0, 0.0, cid

            status = (resp.get('status') or 'NEW').upper()
            exec_qty = abs(float(resp.get('executedQty', 0) or 0))
            avg_price = float(resp.get('avgPrice', 0) or 0)

            # MARKET orders usually fill synchronously. If we got an
            # orderId but no fill info yet, poll for it.
            if exec_qty <= 0 and resp.get('orderId'):
                s, eq, ap = _resolve_ambiguous_market(
                    client, pair, cid, timeout_s=8.0
                )
                if eq > 0:
                    return s, eq, ap, cid
                status = s
                exec_qty = eq
                avg_price = ap

            return status, exec_qty, avg_price, cid

        except BinanceAPIException as e:
            # Rate limit → backoff + retry with same cid
            if e.code in (-1003, -1008):
                retry_after = None
                try:
                    r = getattr(e, 'response', None)
                    if r is not None and r.headers.get('Retry-After'):
                        retry_after = float(r.headers['Retry-After'])
                except Exception:
                    pass
                base = retry_after if retry_after is not None else (2 ** attempt)
                if e.code == -1008 and retry_after is None:
                    base *= 2
                sleep_s = min(base + random.uniform(0, 0.5), 15.0)
                logger.warning(
                    f"[{pair}] market rate-limit {e.code}, "
                    f"retry in {sleep_s:.2f}s"
                )
                time.sleep(sleep_s)
                try:
                    refresh_timestamp()
                except Exception:
                    pass
                continue

            # Timestamp out of window
            if e.code == -1021:
                logger.warning(f"[{pair}] -1021 timestamp; refresh & retry")
                try:
                    refresh_timestamp()
                except Exception:
                    pass
                time.sleep(0.5)
                continue

            # Filter mismatch → bust cache, re-round, retry once.
            # NOTE: the original order FAILED at the exchange (filter
            # reject), so it did NOT land. A fresh cid here is safe.
            if handle_order_filter_error(pair, e):
                f = get_filters(pair)
                qty_str = adjust_qty(
                    Decimal(qty_str), f['stepSize'], f['minQty']
                )
                cid = _client_order_id(pair, "entry")
                continue

            # Definitive reject with no fill — try to resolve anyway
            # (in case partial fill landed before reject).
            s, eq, ap = _resolve_ambiguous_market(client, pair, cid)
            if eq > 0:
                return s, eq, ap, cid
            logger.error(f"[{pair}] market order rejected: {e}")
            return 'REJECTED', 0.0, 0.0, cid

        except Exception as e:
            # Timeout or connection error — resolve ambiguity FIRST.
            s, eq, ap = _resolve_ambiguous_market(client, pair, cid)
            if eq > 0:
                return s, eq, ap, cid
            # ── REV 1.9.2 (C1) ──
            # Only retry with a fresh cid on a TERMINAL non-fill.
            if s in ('CANCELED', 'EXPIRED', 'REJECTED'):
                cid = _client_order_id(pair, "entry")
                continue
            logger.critical(
                f"[{pair}] market entry exception + UNKNOWN "
                f"(cid={cid}, status={s}, err={type(e).__name__}) — "
                f"NOT retrying with fresh cid to avoid double-entry"
            )
            return 'UNKNOWN', 0.0, 0.0, cid

    # Both attempts exhausted without a fill
    return 'UNKNOWN', 0.0, 0.0, cid

# ═════════════════════════════════════════════════════════════
#  MAIN ENTRY
# ═════════════════════════════════════════════════════════════
def place_order_fixed(symbol, side, quantity, sl_price, tp1_price, tp2_price,
                      entry_price_est, conf=50, rr=0, pattern="NONE", fg=50,
                      strategy="UNKNOWN", signal_price=None):
    """Place a market entry with attached SL/TP1/TP2."""
    pair = symbol + 'USDT'
    client = _cl()
    if client is None:
        logger.error(f"[{symbol}] place_order_fixed: client not initialized")
        return False

    if not _filters_ok_to_trade(pair):
        logger.warning(
            f"🚫 [{symbol}] Filters unverified (fallback × 2+) — skipping entry"
        )
        return False

    if DRY_RUN:
        logger.info(
            f"🧪 DRY RUN: would place {side} {pair} qty≈{quantity} "
            f"SL={sl_price} TP1={tp1_price} TP2={tp2_price} "
            f"strategy={strategy} signal_price={signal_price}"
        )
        return True

    # ─── LIVE config reads ───
    _leverage = int(_cc_get_num("leverage", 5))
    _risk_percent = float(_cc_get_num("risk_percent", 0.5))
    _max_open = int(_cc_get_num("max_open_positions", 3))
    _max_total_margin = float(_cc_get_num("max_total_margin_pct", 0.60))
    _margin_buffer_pct = float(_cc_get_num("margin_buffer_pct", 0.80))

    # ─── SIGNAL DRIFT CAP ───
    _regime_drift = _get_regime_now(symbol)
    _cfg_drift = _get_central_config(symbol, strategy, _regime_drift)
    _MAX_SIGNAL_DRIFT_PCT = float(_cfg_drift.get("signal_drift_pct", 0.5)) * 100
    if signal_price is not None and signal_price > 0 and entry_price_est > 0:
        try:
            _sig_p = float(signal_price)
            _live_p = float(entry_price_est)
            _drift_pct = abs(_live_p - _sig_p) / _sig_p * 100.0
            if _drift_pct > _MAX_SIGNAL_DRIFT_PCT:
                logger.warning(
                    f"[{symbol}] SIGNAL DRIFT REJECT: "
                    f"signal={_sig_p:.6f} live={_live_p:.6f} "
                    f"drift={_drift_pct:.3f}% > {_MAX_SIGNAL_DRIFT_PCT:.2f}% "
                    f"[regime={_regime_drift}]"
                )
                return False
        except (TypeError, ValueError) as _e:
            logger.debug(f"[{symbol}] drift check failed: {_e}")

    logger.info(
        f"🚀 Attempting to place {side} order for {pair} [strategy={strategy}]"
    )

    with COOLDOWN_LOCK:
        if symbol in cooldown_until and datetime.now(PKT) < cooldown_until[symbol]:
            remaining = (cooldown_until[symbol] - datetime.now(PKT)).seconds // 60
            logger.info(f" {symbol} cooldown {remaining}m left")
            return False

    with BOT_TRACKED_LOCK:
        if len(bot_tracked_symbols) >= _max_open:
            logger.warning(f" Max {_max_open} reached, skip {symbol}")
            return False
        if symbol in bot_tracked_symbols:
            logger.warning(f" {symbol} already tracked")
            return False
        bot_tracked_symbols.add(symbol)
        logger.info(
            f" Slot reserved for {symbol} "
            f"({len(bot_tracked_symbols)}/{_max_open})"
        )

    def _release_slot():
        with BOT_TRACKED_LOCK:
            bot_tracked_symbols.discard(symbol)

    try:
        if VALID_SYMBOLS and pair not in VALID_SYMBOLS:
            logger.error(f"Symbol {pair} not in VALID_SYMBOLS")
            _release_slot()
            return False

        # Pre-check: existing position
        # ── REV 1.9.10 — _read_lock (requests.Session not thread-safe). ──
        try:
            with _read_lock:
                pos_list = client.futures_position_information(symbol=pair)
            if pos_list and float(pos_list[0]['positionAmt']) != 0:
                logger.warning(f" {symbol} already in position")
                _release_slot()
                return False
        except Exception as e:
            logger.warning(f"Position pre-check failed for {pair}: {e}")

        # Account + margin checks
        # ── REV 1.9.10 — _read_lock. ──
        try:
            with _read_lock:
                account = client.futures_account()
            try:
                from core.client import _ACCOUNT_CACHE, _ACCOUNT_CACHE_LOCK
                with _ACCOUNT_CACHE_LOCK:
                    _ACCOUNT_CACHE['data'] = account
                    _ACCOUNT_CACHE['time'] = time.time()
            except Exception:
                pass

            available = float(account['availableBalance'])

            # ── REV 1.9.5 (H4b) — sizing base includes unrealized PnL. ──
            # `totalMarginBalance` = wallet + unrealized (what the DD
            # gate and account cache already use). Fall back to
            # `totalWalletBalance` on older schemas, then to
            # `availableBalance` as the last resort.
            wallet_balance = float(
                account.get(
                    'totalMarginBalance',
                    account.get('totalWalletBalance', available),
                )
            )

            required_margin = (float(quantity) * entry_price_est) / _leverage

            if required_margin > available * _margin_buffer_pct:
                logger.warning(f" Insufficient margin for {symbol}")
                _release_slot()
                return False

            try:
                used_margin_total = float(
                    account.get('totalInitialMargin',
                                wallet_balance - available)
                )
            except (TypeError, ValueError):
                used_margin_total = max(0.0, wallet_balance - available)

            projected_total = used_margin_total + required_margin
            max_total_allowed = wallet_balance * _max_total_margin
            if wallet_balance > 0 and projected_total > max_total_allowed:
                logger.warning(f" Aggregate exposure limit hit for {symbol}")
                _release_slot()
                return False
        except Exception as e:
            logger.warning(f"Account fetch failed: {e}")
            _release_slot()
            return False

        # Margin type / leverage
        # ── REV 1.9.10 — _write_lock (account-state mutation). ──
        try:
            with _write_lock:
                client.futures_change_margin_type(symbol=pair, marginType='ISOLATED')
        except Exception as e:
            if '-4046' not in str(e) and 'No need to change margin type' not in str(e):
                logger.debug(f"Margin type warning: {e}")

        try:
            with _write_lock:
                client.futures_change_leverage(symbol=pair, leverage=_leverage)
        except Exception as e:
            logger.error(f"Leverage change FAILED for {pair}: {e}")
            _release_slot()
            return False

        # Filters
        f = get_filters(pair)
        step = f['stepSize']
        min_qty = f['minQty']
        min_notional = f['minNotional']
        max_qty = f.get('maxQty')
        cfg = get_trading_config()

        # Final SL adjustment (min distance)
        final_sl_price = float(sl_price)
        min_dist = entry_price_est * cfg['min_dist_pct']
        if side == 'BUY':
            if final_sl_price >= entry_price_est - min_dist:
                final_sl_price = entry_price_est - min_dist
        else:
            if final_sl_price <= entry_price_est + min_dist:
                final_sl_price = entry_price_est + min_dist

        # Reference price = signal if available
        _ref_price = entry_price_est
        if signal_price is not None:
            try:
                _sp = float(signal_price)
                if _sp > 0:
                    _ref_price = _sp
            except (TypeError, ValueError):
                pass

        intended_dist = abs(_ref_price - final_sl_price)
        intended_dist_pct = (
            (intended_dist / _ref_price * 100) if _ref_price > 0 else 0
        )
        logger.info(
            f" [SIZE] Intended SL dist {intended_dist:.4f} "
            f"({intended_dist_pct:.3f}%) "
            f"[ref={_ref_price:.6f} est={entry_price_est:.6f}]"
        )

        # ═══════════════════════════════════════════════════════════
        #  REV 1.9.0 / 1.9.2 — RISK SIZING WITH FLOOR GUARD
        #  REV 1.9.2 (C5): abort threshold now read from a DEDICATED
        #  config key `risk_oversize_abort_mult` (default 1.20), NOT
        #  the warn-only `risk_oversize_warn_mult`. This prevents an
        #  operator from silently disabling the abort guard by tuning
        #  the warn key up.
        # ═══════════════════════════════════════════════════════════
        _oversize_mult = float(
            _cc_get_num("risk_oversize_abort_mult", 1.20)
        )
        try:
            qty_dec = _risk_size_with_floor_guard(
                wallet_balance=wallet_balance,
                risk_percent=_risk_percent,
                ref_price=_ref_price,
                sl_price=final_sl_price,
                step=step,
                min_qty=min_qty,
                min_notional=min_notional,
                max_qty=max_qty,
                max_oversize_mult=_oversize_mult,
            )
        except Exception as e:
            # ── REV 1.9.2 (C6) ──
            # Previously fell back to the caller's `quantity` and then
            # bumped sub-minimum sizes up to `min_qty` — silently
            # producing an oversized trade that bypassed the risk
            # budget. Now: reject cleanly. A skipped trade costs
            # nothing; an unbudgeted oversized trade can blow the
            # account.
            logger.exception(
                f"[{symbol}] sizing raised {type(e).__name__}: {e} — "
                f"REJECTING entry (never fall back to a bumped qty)"
            )
            _release_slot()
            return False

        if qty_dec is None:
            logger.warning(
                f"[{symbol}] SIZING ABORT: risk floor violation — "
                f"skipping entry (this is the correct behaviour; an "
                f"oversized trade is worse than a skipped one)"
            )
            _release_slot()
            return False

        logger.info(
            f" [C1] Qty finalized {qty_dec} from risk "
            f"${float(wallet_balance) * (_risk_percent / 100.0):.2f} "
            f"dist {abs(_ref_price - final_sl_price):.6f}"
        )

        # Down-scaling (vol class, counter-trend) — multiplicative
        qty_dec = _apply_counter_trend_sizing(
            symbol, strategy, qty_dec, step, min_qty
        )
        qty_dec = _apply_vol_class_sizing(symbol, qty_dec, step, min_qty)

        # Cap by maxQty (still aborts if cap < minQty)
        # NOTE: this path intentionally FLOORS (rounds down) — the cap
        # must never exceed the exchange ceiling.
        if max_qty is not None and qty_dec > max_qty:
            logger.warning(
                f"[{symbol}] qty {qty_dec} > exchange maxQty {max_qty} — capping"
            )
            qty_dec = max_qty
            qty_dec = (qty_dec // step) * step
            if qty_dec < min_qty:
                logger.error(
                    f"[{symbol}] capped qty {qty_dec} < minQty — abort"
                )
                _release_slot()
                return False

        qty_str = adjust_qty(qty_dec, step, min_qty)
        qty_dec = Decimal(qty_str)
        notional = float(qty_dec) * entry_price_est

        # min_notional bump (with 1.5× guard preserved)
        # ── REV 1.9.10 — CEIL to step so notional GUARANTEES ≥ min. ──
        if notional < min_notional:
            _pre_bump_qty = qty_dec
            _bump_mult = float(_cc_get_num("min_notional_bump_mult", 1.02))
            logger.warning(
                f"Notional {notional:.2f} < min {min_notional}, increasing qty"
            )
            qty_dec = Decimal(str(min_notional / entry_price_est * _bump_mult))
            # ✅ REV 1.9.10 FIX: ceil to next step (was floor — undershot).
            _rem = qty_dec % step
            if _rem > 0:
                qty_dec = qty_dec - _rem + step
            if qty_dec < min_qty:
                qty_dec = min_qty
            if max_qty is not None and qty_dec > max_qty:
                logger.error(
                    f"[{symbol}] min_notional bump pushed qty {qty_dec} > "
                    f"maxQty {max_qty} — aborting"
                )
                _release_slot()
                return False
            if _pre_bump_qty > 0 and qty_dec > _pre_bump_qty * Decimal("1.5"):
                _bump_ratio = float(qty_dec / _pre_bump_qty)
                _bumped_risk_usd = float(qty_dec) * abs(
                    _ref_price - final_sl_price
                )
                _bumped_risk_pct = (
                    (_bumped_risk_usd / wallet_balance * 100.0)
                    if wallet_balance > 0 else 0.0
                )
                logger.warning(
                    f"[{symbol}] min_notional bump {_pre_bump_qty} → {qty_dec} "
                    f"({_bump_ratio:.2f}×) would push risk to "
                    f"{_bumped_risk_pct:.3f}% — aborting entry"
                )
                _release_slot()
                return False
            qty_str = format(qty_dec, f'.{abs(step.as_tuple().exponent)}f')
            notional = float(qty_dec) * entry_price_est
            if notional < min_notional:
                logger.error("Still below min notional, aborting")
                _release_slot()
                return False

        # TP1 / TP2 quantity split
        tp1_ratio = Decimal(str(cfg['tp1_qty']))
        prec = abs(step.as_tuple().exponent)
        qty_tp1_dec = (qty_dec * tp1_ratio)
        qty_tp1_dec = (qty_tp1_dec // step) * step
        if qty_tp1_dec < min_qty:
            qty_tp1_dec = min_qty
        qty_tp2_dec = qty_dec - qty_tp1_dec
        qty_tp2_dec = (qty_tp2_dec // step) * step
        if qty_tp2_dec < min_qty:
            qty_tp1_dec = qty_dec
            qty_tp2_dec = Decimal('0')
        qty_tp1 = format(qty_tp1_dec, f'.{prec}f')
        qty_tp2 = format(qty_tp2_dec, f'.{prec}f') if qty_tp2_dec > 0 else '0'
        sl_price = final_sl_price

        # ═══════════════════════════════════════════════════════════
        #  REV 1.9.8 — PRE-ENTRY RR GUARD (RR_COLLAPSE FIX)
        #
        #  Predict what the post-cap RR will be BEFORE sending the
        #  market order. If the coin's TP cap + the signal's SL
        #  distance make min_rr mathematically impossible, SKIP
        #  the trade rather than entering and immediately flattening
        #  on the post-fill RR_COLLAPSE check.
        #
        #  Root cause (TRUMP/DOT/RENDER, 2026-10-06/07):
        #    Signal engine emits wide ATR-based SL (e.g. 8.9% on
        #    TRUMP), but the coin's TP1 cap (coins_config.get_caps)
        #    is tighter (e.g. 9.7%). Post-cap RR = 9.7/8.9 = 1.09
        #    < min_rr=1.50 → RR_COLLAPSE close. Guard detects the
        #    impossibility pre-entry → no order ever sent.
        #
        #  Fail-open: on any internal error, log and proceed — a
        #  missed guard is preferable to a skipped valid trade.
        #  The downstream RR_COLLAPSE check remains as defence-in-
        #  depth for realised-slippage edge cases.
        # ═══════════════════════════════════════════════════════════
        try:
            _caps_pre = get_caps(symbol)
            _ref_for_rr = _ref_price if _ref_price > 0 else entry_price_est
            _risk_pre = abs(_ref_for_rr - float(final_sl_price))
            if _risk_pre > 0 and _ref_for_rr > 0:
                _tp1_pre = float(tp1_price)
                if side == 'BUY':
                    _tp1_pre_capped = min(
                        _tp1_pre, _ref_for_rr * (1 + _caps_pre["tp1"])
                    )
                else:
                    _tp1_pre_capped = max(
                        _tp1_pre, _ref_for_rr * (1 - _caps_pre["tp1"])
                    )
                _reward_pre = abs(_tp1_pre_capped - _ref_for_rr)
                _pred_rr = _reward_pre / _risk_pre
                _min_rr_pre = _get_min_rr_central(strategy)
                _tol_pre = float(_cc_get_num("rr_collapse_tol", 0.02))
                if _pred_rr < _min_rr_pre - _tol_pre:
                    logger.info(
                        f"[{symbol}] ⏭️  PRE-ENTRY RR SKIP: predicted "
                        f"{_pred_rr:.3f} < min {_min_rr_pre:.2f} "
                        f"(tol {_tol_pre:.2f}) — "
                        f"SL dist {_risk_pre / _ref_for_rr * 100:.2f}%, "
                        f"TP cap {_caps_pre['tp1'] * 100:.2f}% "
                        f"[strategy={strategy}]"
                    )
                    _release_slot()
                    return False
        except Exception as _pre_e:
            logger.warning(
                f"[{symbol}] pre-entry RR guard error: "
                f"{type(_pre_e).__name__}: {_pre_e} — proceeding"
            )
        # ═══════════════════════════════════════════════════════════

        close_side = 'SELL' if side == 'BUY' else 'BUY'
        _regime_now, _hold_min = _snapshot_regime(symbol, strategy)

        # ═══════════════════════════════════════════════════════════
        #  REV 1.9.0 — IDEMPOTENT MARKET ENTRY + AMBIGUITY RESOLUTION
        # ═══════════════════════════════════════════════════════════
        cid = _client_order_id(pair, "entry")
        status, exec_qty, avg_price, cid_used = _place_market_idempotent(
            client, pair, side, qty_str, cid, max_attempts=2
        )
        logger.info(
            f"[{symbol}] market entry: status={status} "
            f"exec_qty={exec_qty} avg_price={avg_price} cid={cid_used}"
        )

        # ── REV 1.9.4 (H2) — entry_time stamped HERE, not before ──
        # Two effects:
        #   1. Hold-time origin = actual fill moment (used by the
        #      time-exit path in manage.py::manage_single_trade).
        #   2. Reconcile's unverified grace window
        #      (repair.py::_UNVERIFIED_FAST_RECONCILE_SEC = 10s) now
        #      starts at registration, not ~1-3s earlier. Previously
        #      the grace was mostly consumed by the time we registered,
        #      giving reconcile no quiet window on fast fills.
        _entry_time_str = datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')

        # Definitive reject with no fill → clean up
        if status in ('CANCELED', 'EXPIRED', 'REJECTED') and exec_qty <= 0:
            logger.error(
                f"[{symbol}] market entry {status} with no execution — "
                f"releasing slot"
            )
            _release_slot()
            return False

        # Unknown outcome — register as unverified and keep slot
        if status == 'UNKNOWN' and exec_qty <= 0:
            logger.critical(
                f"[{symbol}] market entry state UNKNOWN (cid={cid_used}) — "
                f"registering unverified. Guardian will reconcile."
            )
            add_active_trade(symbol, {
                'entry': entry_price_est, 'qty': qty_str, 'sl': '0',
                'tp1': '0', 'tp2': '0', 'side': side,
                'entry_time': _entry_time_str,
                'sl_id': 0, 'sl_level': 0,
                'unverified': True,
                'initial_sl': float(sl_price),
                'strategy': strategy,
                'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2,
                'regime_at_entry': _regime_now,
                'hold_time_minutes': _hold_min,
                'client_order_id': cid_used,
            })
            try:
                send_telegram(
                    f"🚨 {symbol} entry state UNKNOWN (cid {cid_used}) — "
                    f"manual check required"
                )
            except Exception:
                pass
            return False

        # ═══════════════════════════════════════════════════════════
        #  FILL CONFIRMED — determine actual entry + qty
        #  REV 1.9.1 — if exec_qty > 0 but avg_price == 0, try a single
        #  position fetch to recover entry price before falling to
        #  unverified. Binance occasionally returns partial responses.
        # ═══════════════════════════════════════════════════════════
        entry_price = avg_price if avg_price > 0 else None
        if exec_qty <= 0 or not entry_price:
            # Either no qty reported, or no avgPrice — fetch position
            # to recover. Short sleep to let the exchange settle.
            time.sleep(0.3)
            p = fetch_position_raw(symbol)
            if p is not None:
                _amt = float(p.get('positionAmt', 0) or 0)
                if _amt != 0:
                    if exec_qty <= 0:
                        exec_qty = abs(_amt)
                    _entry_from_pos = float(p.get('entryPrice', 0) or 0)
                    if _entry_from_pos > 0:
                        entry_price = _entry_from_pos

        if exec_qty <= 0 or not entry_price:
            # Cannot determine fill — but order reported FILLED/partial.
            logger.critical(
                f"[{symbol}] FILLED status but no exec_qty/price — "
                f"registering unverified for guardian"
            )
            add_active_trade(symbol, {
                'entry': entry_price_est, 'qty': qty_str, 'sl': '0',
                'tp1': '0', 'tp2': '0', 'side': side,
                'entry_time': _entry_time_str,
                'sl_id': 0, 'sl_level': 0,
                'unverified': True,
                'initial_sl': float(sl_price),
                'strategy': strategy,
                'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2,
                'regime_at_entry': _regime_now,
                'hold_time_minutes': _hold_min,
                'client_order_id': cid_used,
            })
            return False

        # Re-round qty to what actually filled
        qty_dec = (Decimal(str(exec_qty)) // step) * step
        if qty_dec < min_qty:
            # Position smaller than min_qty — a partial fill that's still
            # meaningful. Register unverified; guardian will close/SL it.
            logger.warning(
                f"[{symbol}] PARTIAL fill {exec_qty} < minQty — "
                f"registering unverified (not abandoning)"
            )
            add_active_trade(symbol, {
                'entry': entry_price, 'qty': str(exec_qty), 'sl': '0',
                'tp1': '0', 'tp2': '0', 'side': side,
                'entry_time': _entry_time_str,
                'sl_id': 0, 'sl_level': 0,
                'unverified': True,
                'initial_sl': float(sl_price),
                'strategy': strategy,
                'partial_fill': True,
                'client_order_id': cid_used,
            })
            return False
        qty_str = format(qty_dec, f'.{prec}f')

        # Recompute TP1/TP2 on actual filled qty
        qty_tp1_dec = (qty_dec * tp1_ratio)
        qty_tp1_dec = (qty_tp1_dec // step) * step
        if qty_tp1_dec < min_qty:
            qty_tp1_dec = min_qty
        qty_tp2_dec = qty_dec - qty_tp1_dec
        qty_tp2_dec = (qty_tp2_dec // step) * step
        if qty_tp2_dec < min_qty:
            qty_tp1_dec = qty_dec
            qty_tp2_dec = Decimal('0')
        qty_tp1 = format(qty_tp1_dec, f'.{prec}f')
        qty_tp2 = format(qty_tp2_dec, f'.{prec}f') if qty_tp2_dec > 0 else '0'

        logger.info(
            f"✅ Market {side} {pair} filled {qty_str} @ {entry_price} "
            f"(status={status}, cid={cid_used})"
        )

        # ── P0 RE-ANCHOR SL/TP TO ACTUAL FILL (preserve distances) ──
        _orig_risk = abs(_ref_price - final_sl_price)
        _orig_reward1 = abs(float(tp1_price) - _ref_price)
        _orig_reward2 = abs(float(tp2_price) - _ref_price)
        if _orig_risk > 0 and entry_price > 0:
            if side == 'BUY':
                sl_price = entry_price - _orig_risk
                tp1_price = entry_price + _orig_reward1
                tp2_price = entry_price + _orig_reward2
            else:
                sl_price = entry_price + _orig_risk
                tp1_price = entry_price - _orig_reward1
                tp2_price = entry_price - _orig_reward2
            logger.info(
                f"[{symbol}] P0 RE-ANCHOR: est {entry_price_est:.6f} → "
                f"fill {entry_price:.6f} | risk {_orig_risk:.6f} preserved"
            )
        else:
            logger.warning(
                f"[{symbol}] P0 RE-ANCHOR SKIPPED "
                f"(orig_risk={_orig_risk} entry={entry_price})"
            )

        # ── Post-fill slippage gate ──
        try:
            _sgn = 1.0 if side == 'BUY' else -1.0
            _drift_sig = (
                (entry_price - _ref_price) / _ref_price * 100.0 * _sgn
                if _ref_price > 0 else 0.0
            )
            _drift_est = (
                (entry_price - entry_price_est) / entry_price_est * 100.0 * _sgn
                if entry_price_est > 0 else 0.0
            )
            logger.info(
                f"TELEMETRY: {symbol} {side} strat={strategy} "
                f"signal={_ref_price:.6f} est={entry_price_est:.6f} "
                f"fill={entry_price:.6f} adverse_vs_signal={_drift_sig:+.3f}% "
                f"adverse_vs_est={_drift_est:+.3f}%"
            )

            # ═══════════════════════════════════════════════════════
            #  REV 1.9.6 (G1) — STALE-SIGNAL SLIPPAGE GUARD
            #
            #  The signal→est gap was ALREADY validated by the SIGNAL
            #  DRIFT REJECT gate at the top of place_order_fixed.
            #  Comparing the fill against the original signal price
            #  therefore double-counts that pre-entry drift. When the
            #  signal is stale (gap ≥ 50% of slip threshold), fall
            #  back to drift-vs-est — the correct measure of execution
            #  slippage.
            #
            #  Concrete failure this prevents (CRV, 2026-10-05):
            #    signal=0.380200  est=0.381700  fill=0.382200
            #    adverse_vs_signal = +0.526%  > 0.500% → OLD: flatten
            #    adverse_vs_est    = +0.131%  << 0.500%   (fine)
            #  The signal→est gap (0.394%) was already pre-validated;
            #  the fill was only 0.13% worse than expected. Old code
            #  flattened anyway, and the strategy immediately re-
            #  emitted the same signal → churn loop → balance drain.
            # ═══════════════════════════════════════════════════════
            _slip_max = float(_cc_get_num("max_fill_slippage_pct", 0.50))
            _sig_est_gap = (
                abs(entry_price_est - _ref_price) / _ref_price * 100.0
                if _ref_price > 0 else 0.0
            )
            _use_est_check = _sig_est_gap > (_slip_max * 0.5)

            if _use_est_check:
                _effective_drift = _drift_est
                _effective_label = "vs_est(stale_sig)"
                logger.info(
                    f"[{symbol}] signal→est gap {_sig_est_gap:.3f}% "
                    f"≥ {_slip_max * 0.5:.3f}% — signal treated as STALE, "
                    f"using drift-vs-est for slippage check "
                    f"(drift={_effective_drift:+.3f}%)"
                )
            else:
                _effective_drift = _drift_sig
                _effective_label = "vs_signal"

            if _effective_drift > _slip_max:
                logger.critical(
                    f"[{symbol}] ADVERSE SLIPPAGE "
                    f"{_effective_drift:.3f}% ({_effective_label}) > "
                    f"{_slip_max:.3f}% — flattening position "
                    f"[sig={_ref_price:.6f} est={entry_price_est:.6f} "
                    f"fill={entry_price:.6f}]"
                )
                try:
                    send_telegram(
                        f"🚨 {symbol} adverse slippage "
                        f"{_effective_drift:.2f}% ({_effective_label}) "
                        f"— flattening"
                    )
                except Exception:
                    pass
                try:
                    # ── REV 1.9.3 — write lock. ──
                    with _write_lock:
                        client.futures_create_order(
                            symbol=pair, side=close_side, type='MARKET',
                            quantity=qty_str, reduceOnly=True,
                        )
                except Exception as _ce:
                    logger.error(f"slippage flatten failed: {_ce}")
                    threading.Thread(
                        target=emergency_close_retry,
                        args=(symbol, pair, close_side),
                        daemon=True,
                    ).start()
                _release_slot()
                # ═══════════════════════════════════════════════════
                #  REV 1.9.6 (G2) — post-flatten cooldown
                #
                #  Without this, the next scan (~15-30s later) re-emits
                #  the same still-active signal and the bot re-enters
                #  the same churning trade. Cooldown is DEFENCE-IN-DEPTH
                #  on top of G1: even if G1's heuristic misses a future
                #  symbol, no more than one slippage-flatten per symbol
                #  per cooldown window.
                # ═══════════════════════════════════════════════════
                _set_post_flatten_cooldown(symbol, reason="slippage_flatten")
                return False
        except Exception as _te:
            logger.debug(f"[{symbol}] telemetry failed: {_te}")

        # ── Register active trade EARLY (before SL placement) ──
        add_active_trade(symbol, {
            'entry': entry_price, 'qty': qty_str, 'sl': '0',
            'tp1': '0', 'tp2': '0', 'side': side,
            'entry_time': _entry_time_str,
            'sl_id': 0, 'sl_level': 0,
            'unverified': True,
            'initial_sl': float(sl_price),
            'strategy': strategy,
            'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2,
            'regime_at_entry': _regime_now,
            'hold_time_minutes': _hold_min,
            'client_order_id': cid_used,
        })

        # ═══════════════════════════════════════════════════════════
        #  CROSSED SL CHECK — fill already through pre-anchor SL
        # ═══════════════════════════════════════════════════════════
        min_dist_post = entry_price * cfg['min_dist_pct']
        sl_price_f = float(sl_price)
        directional_dist = (
            (entry_price - sl_price_f) if side == 'BUY'
            else (sl_price_f - entry_price)
        )
        actual_dist_post = abs(entry_price - sl_price_f)

        if directional_dist <= 0:
            logger.critical(
                f"[{symbol}] Fill crossed SL! entry {entry_price:.6f} "
                f"SL {sl_price_f:.6f} — aborting"
            )
            try:
                send_telegram(
                    f"🚨 {symbol} CROSSED SL — closing\n"
                    f"Entry {entry_price:.4f} SL {sl_price_f:.4f}"
                )
            except Exception:
                pass
            closed_ok = False
            thread_launched = False

            def _sync_close() -> bool:
                """Attempt one market close; return True only if flat."""
                try:
                    # ── REV 1.9.3 — write lock. ──
                    with _write_lock:
                        client.futures_create_order(
                            symbol=pair, side=close_side, type='MARKET',
                            quantity=qty_str, reduceOnly=True,
                        )
                except BinanceAPIException as _ce:
                    # -2021 shouldn't happen on MARKET; log and treat as fail
                    logger.warning(
                        f"[{symbol}] crossed-SL close BinanceAPIException: "
                        f"{_ce.code}: {_ce}"
                    )
                    return False
                except Exception as _ce:
                    logger.warning(f"[{symbol}] crossed-SL close error: {_ce}")
                    return False
                time.sleep(0.6)
                p = fetch_position_raw(symbol)
                if p is None:
                    return False
                return abs(float(p.get('positionAmt', '0') or 0)) == 0

            try:
                for _ in range(3):
                    if _sync_close():
                        closed_ok = True
                        break
                    time.sleep(0.8)
                if not closed_ok:
                    logger.warning(
                        f"[{symbol}] crossed-SL sync close failed — "
                        f"launching emergency_close_retry"
                    )
                    threading.Thread(
                        target=emergency_close_retry,
                        args=(symbol, pair, close_side),
                        daemon=True,
                    ).start()
                    thread_launched = True
            except Exception as ce:
                logger.error(f"Crossed SL emergency close failed: {ce}")
                threading.Thread(
                    target=emergency_close_retry,
                    args=(symbol, pair, close_side),
                    daemon=True,
                ).start()
                thread_launched = True

            if thread_launched:
                return False
            try:
                time.sleep(2.5)
                handle_trade_close(symbol, pair, reason="CROSSED_SL")
            except Exception as htce:
                logger.warning(f"handle_trade_close CROSSED_SL failed: {htce}")
            return False

        # ═══════════════════════════════════════════════════════════
        #  IMMEDIATE PROTECTIVE STOP — placed ASAP after fill
        #  REV 1.9.1 — on filter cache-bust, recompute sl_adj with
        #  fresh tickSize. Previously only qty_str was re-rounded.
        #  REV 1.9.9 — wrapped in _write_lock.
        # ═══════════════════════════════════════════════════════════
        sl_adj = adjust_price(sl_price, f['tickSize'])
        sl_placed = False
        sl_id = None
        for attempt in range(3):
            try:
                def _place_sl():
                    with _write_lock:
                        return client.futures_create_order(
                            symbol=pair, side=close_side, type='STOP_MARKET',
                            stopPrice=sl_adj, quantity=qty_str,
                            reduceOnly=True, timeInForce='GTC',
                            workingType='MARK_PRICE',
                            newClientOrderId=f"tb_sl_{pair}_{uuid.uuid4().hex[:12]}",
                        )
                sl_resp = _run_with_timeout(_place_sl, 5, f"sl:{pair}")
                sl_id = sl_resp.get('orderId') or sl_resp.get('algoId')
                logger.info(f" SL placed at {sl_adj} (ID: {sl_id})")
                sl_placed = True
                break
            except BinanceAPIException as e:
                # -2021 → trigger crossed; close immediately
                if e.code == -2021:
                    logger.critical(
                        f"[{symbol}] SL -2021 would immediately trigger "
                        f"(stop {sl_adj}) — closing position"
                    )
                    try:
                        # ── REV 1.9.3 — write lock. ──
                        with _write_lock:
                            client.futures_create_order(
                                symbol=pair, side=close_side, type='MARKET',
                                quantity=qty_str, reduceOnly=True,
                            )
                    except Exception as ce:
                        logger.error(f"-2021 close failed: {ce}")
                    threading.Thread(
                        target=emergency_close_retry,
                        args=(symbol, pair, close_side),
                        daemon=True,
                    ).start()
                    time.sleep(0.5)
                    try:
                        handle_trade_close(symbol, pair, reason="SL_-2021")
                    except Exception as htce:
                        logger.warning(f"handle_trade_close SL_-2021: {htce}")
                    _release_slot()
                    return False
                # Filter mismatch → re-round qty AND recompute stop
                if handle_order_filter_error(pair, e):
                    f = get_filters(pair)
                    qty_str = adjust_qty(
                        Decimal(qty_str), f['stepSize'], f['minQty']
                    )
                    # REV 1.9.1 — recompute stop with the fresh tickSize.
                    sl_adj = adjust_price(sl_price, f['tickSize'])
                    continue
                logger.warning(f"SL attempt {attempt+1} failed: {e}")
                time.sleep(1.5)
            except Exception as e:
                logger.warning(f"SL attempt {attempt+1} failed: {e}")
                time.sleep(1.5)

        if not sl_placed:
            logger.error("SL placement FAILED - emergency close")
            threading.Thread(
                target=emergency_close_retry,
                args=(symbol, pair, close_side),
                daemon=True,
            ).start()
            _release_slot()
            try:
                send_telegram(f"🚨 SL FAILED {symbol}")
            except Exception:
                pass
            return False

        # ── Update active_trade with real SL ──
        add_active_trade(symbol, {
            'entry': entry_price, 'qty': qty_str, 'sl': sl_adj,
            'tp1': '0', 'tp2': '0', 'side': side,
            'entry_time': _entry_time_str,
            'sl_id': sl_id, 'sl_level': 0,
            'initial_sl': float(sl_adj),
            'strategy': strategy,
            'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2,
            'regime_at_entry': _regime_now,
            'hold_time_minutes': _hold_min,
            'client_order_id': cid_used,
            'unverified': False,
        })

        # ═══════════════════════════════════════════════════════════
        #  POST-SL REFINEMENT — tight / wide corrections
        # ═══════════════════════════════════════════════════════════
        _sl_tight_ratio = float(_cc_get_num("sl_tight_ratio", 0.70))

        if actual_dist_post < min_dist_post * _sl_tight_ratio:
            logger.warning(
                f"[{symbol}] SL too tight after fill "
                f"(dist {actual_dist_post:.6f} < {min_dist_post:.6f}), widening"
            )
            if side == 'BUY':
                sl_price = entry_price - min_dist_post
            else:
                sl_price = entry_price + min_dist_post
            sl_price_f = float(sl_price)
            actual_dist_post = min_dist_post
            directional_dist = min_dist_post
            try:
                risk_amount = float(wallet_balance) * (_risk_percent / 100.0)
                corrected_qty_tight = (
                    Decimal(str(risk_amount / actual_dist_post))
                    if actual_dist_post > 0 else qty_dec
                )
                corrected_qty_tight = (corrected_qty_tight // f['stepSize']) * f['stepSize']
                if corrected_qty_tight < f['minQty']:
                    corrected_qty_tight = f['minQty']
                if corrected_qty_tight < qty_dec:
                    reduce_qty_t = qty_dec - corrected_qty_tight
                    reduce_str_t = adjust_qty(reduce_qty_t, f['stepSize'], f['minQty'])
                    if Decimal(reduce_str_t) >= f['minQty']:
                        try:
                            # ── REV 1.9.3 — write lock. ──
                            with _write_lock:
                                client.futures_create_order(
                                    symbol=pair, side=close_side, type='MARKET',
                                    quantity=reduce_str_t, reduceOnly=True,
                                )
                            logger.info(
                                f"[{symbol}] TIGHT FIX: Reduced {reduce_str_t}, "
                                f"target {corrected_qty_tight}"
                            )
                            time.sleep(0.8)
                            p_check = fetch_position_raw(symbol)
                            settled = False
                            live_amt_t = 0.0
                            if p_check is not None:
                                live_amt_t = abs(float(p_check.get('positionAmt', '0') or 0))
                                if live_amt_t <= float(corrected_qty_tight) * 1.02:
                                    settled = True
                                else:
                                    logger.warning(
                                        f"[{symbol}] tight reduce not settled: "
                                        f"live {live_amt_t} > target {corrected_qty_tight}"
                                    )
                            if settled:
                                qty_dec = Decimal(str(live_amt_t)) if live_amt_t > 0 else corrected_qty_tight
                                qty_str = format(qty_dec, f'.{prec}f')
                                # Recompute TP qtys
                                qty_tp1_dec = qty_dec * Decimal(str(cfg['tp1_qty']))
                                qty_tp1_dec = (qty_tp1_dec // f['stepSize']) * f['stepSize']
                                if qty_tp1_dec < f['minQty']:
                                    qty_tp1_dec = f['minQty']
                                qty_tp2_dec = qty_dec - qty_tp1_dec
                                qty_tp2_dec = (qty_tp2_dec // f['stepSize']) * f['stepSize']
                                if qty_tp2_dec < f['minQty']:
                                    qty_tp1_dec = qty_dec
                                    qty_tp2_dec = Decimal('0')
                                qty_tp1 = format(qty_tp1_dec, f'.{prec}f')
                                qty_tp2 = format(qty_tp2_dec, f'.{prec}f') if qty_tp2_dec > 0 else '0'
                        except Exception as re_t:
                            logger.error(f"Tight branch reduce failed: {re_t}")
            except Exception as fix_t:
                logger.error(f"Tight branch fix failed: {fix_t}", exc_info=True)

        elif intended_dist > 0 and actual_dist_post > intended_dist * float(_cc_get_num("sl_wide_ratio", 1.05)):
            risk_multiplier = actual_dist_post / intended_dist
            logger.warning(
                f"[{symbol}] SL widened vs intended "
                f"({actual_dist_post:.6f} vs {intended_dist:.6f}) — "
                f"real risk {risk_multiplier:.2f}×"
            )
            try:
                send_telegram(
                    f"⚠️ {symbol} slippage: SL wider than intended, "
                    f"risk {risk_multiplier:.2f}x\n"
                    f"Entry {entry_price:.4f} SL {sl_price_f:.4f}"
                )
            except Exception:
                pass
            try:
                risk_amount = float(wallet_balance) * (_risk_percent / 100.0)
                _caps = get_caps(symbol)
                max_allowed_dist = entry_price * _caps["sl"]
                if actual_dist_post > max_allowed_dist:
                    logger.warning(
                        f"[{symbol}] Capping SL dist from "
                        f"{actual_dist_post:.6f} to {max_allowed_dist:.6f}"
                    )
                    if side == 'BUY':
                        sl_price = entry_price - max_allowed_dist
                    else:
                        sl_price = entry_price + max_allowed_dist
                    sl_price_f = float(sl_price)
                    actual_dist_post = max_allowed_dist
                    directional_dist = max_allowed_dist

                corrected_qty_dec = (
                    Decimal(str(risk_amount / actual_dist_post))
                    if actual_dist_post > 0 else qty_dec
                )
                corrected_qty_dec = (corrected_qty_dec // f['stepSize']) * f['stepSize']
                if corrected_qty_dec < f['minQty']:
                    corrected_qty_dec = f['minQty']
                _close_ratio = Decimal(str(_cc_get_num("corrected_qty_close_ratio", 0.50)))
                if corrected_qty_dec < (qty_dec * _close_ratio):
                    logger.critical(
                        f"[{symbol}] Corrected qty {corrected_qty_dec} < 50% of "
                        f"{qty_dec} — closing position"
                    )
                    try:
                        # ── REV 1.9.3 — write lock. ──
                        with _write_lock:
                            client.futures_create_order(
                                symbol=pair, side=close_side, type='MARKET',
                                quantity=qty_str, reduceOnly=True,
                            )
                    except Exception as ce:
                        logger.error(f"Emergency close failed: {ce}")
                    _release_slot()
                    return False

                if corrected_qty_dec < qty_dec:
                    reduce_qty = qty_dec - corrected_qty_dec
                    reduce_str = adjust_qty(reduce_qty, f['stepSize'], f['minQty'])
                    if Decimal(reduce_str) >= f['minQty']:
                        try:
                            # ── REV 1.9.3 — write lock. ──
                            with _write_lock:
                                client.futures_create_order(
                                    symbol=pair, side=close_side, type='MARKET',
                                    quantity=reduce_str, reduceOnly=True,
                                )
                            logger.info(
                                f"[{symbol}] FIXED RISK: Reduced {reduce_str}, "
                                f"new qty {corrected_qty_dec}"
                            )
                            time.sleep(0.8)
                            p_check = fetch_position_raw(symbol)
                            settled = False
                            live_amt = 0.0
                            if p_check is not None:
                                live_amt = abs(float(p_check.get('positionAmt', '0') or 0))
                                if live_amt <= float(corrected_qty_dec) * 1.02:
                                    settled = True
                                else:
                                    logger.warning(
                                        f"[{symbol}] wide reduce not settled: "
                                        f"live {live_amt} > target {corrected_qty_dec}"
                                    )
                            if settled:
                                qty_dec = Decimal(str(live_amt)) if live_amt > 0 else corrected_qty_dec
                                qty_str = format(qty_dec, f'.{prec}f')
                                qty_tp1_dec = qty_dec * Decimal(str(cfg['tp1_qty']))
                                qty_tp1_dec = (qty_tp1_dec // f['stepSize']) * f['stepSize']
                                if qty_tp1_dec < f['minQty']:
                                    qty_tp1_dec = f['minQty']
                                qty_tp2_dec = qty_dec - qty_tp1_dec
                                qty_tp2_dec = (qty_tp2_dec // f['stepSize']) * f['stepSize']
                                if qty_tp2_dec < f['minQty']:
                                    qty_tp1_dec = qty_dec
                                    qty_tp2_dec = Decimal('0')
                                qty_tp1 = format(qty_tp1_dec, f'.{prec}f')
                                qty_tp2 = format(qty_tp2_dec, f'.{prec}f') if qty_tp2_dec > 0 else '0'
                        except Exception as re:
                            logger.error(f"Reduce qty failed: {re}")
                if float(qty_dec) * entry_price < f['minNotional']:
                    logger.critical(
                        f"[{symbol}] post-fix notional "
                        f"{float(qty_dec)*entry_price:.2f} < minNotional "
                        f"{f['minNotional']} — aborting"
                    )
                    try:
                        # ── REV 1.9.3 — write lock. ──
                        with _write_lock:
                            client.futures_create_order(
                                symbol=pair, side=close_side, type='MARKET',
                                quantity=qty_str, reduceOnly=True,
                            )
                    except Exception as _pe:
                        logger.warning(f"[place_order_fixed] ignored API error: {type(_pe).__name__}: {_pe}")
                    _release_slot()
                    return False
            except Exception as fix_e:
                logger.error(f"SL fix logic failed: {fix_e}", exc_info=True)

        # ─── TP min-distance enforcement ───
        if side == 'BUY':
            if tp1_price <= entry_price + min_dist_post:
                tp1_price = entry_price + min_dist_post
            if tp2_price <= entry_price + min_dist_post:
                tp2_price = entry_price + min_dist_post
        else:
            if tp1_price >= entry_price - min_dist_post:
                tp1_price = entry_price - min_dist_post
            if tp2_price >= entry_price - min_dist_post:
                tp2_price = entry_price - min_dist_post

        # ─── Cap TPs by coin config ───
        _tp1_before_cap = float(tp1_price)
        _tp2_before_cap = float(tp2_price)
        _caps = get_caps(symbol)
        if side == 'BUY':
            tp1_price = min(tp1_price, entry_price * (1 + _caps["tp1"]))
            tp2_price = min(tp2_price, entry_price * (1 + _caps["tp2"]))
        else:
            tp1_price = max(tp1_price, entry_price * (1 - _caps["tp1"]))
            tp2_price = max(tp2_price, entry_price * (1 - _caps["tp2"]))

        # ─── RR re-check after caps ───
        _risk_final = abs(entry_price - float(sl_price))
        _reward_final = abs(float(tp1_price) - entry_price)
        _rr_final = (_reward_final / _risk_final) if _risk_final > 0 else 0.0
        _min_rr = _get_min_rr_central(strategy)
        _RR_COLLAPSE_TOL = float(_cc_get_num("rr_collapse_tol", 0.02))

        logger.info(
            f"[{symbol}] RR check: signal_rr={rr:.2f} → placed_rr={_rr_final:.3f} "
            f"(min {_min_rr:.2f}, tol {_RR_COLLAPSE_TOL}) [strategy={strategy}]"
        )

        if _rr_final < _min_rr - _RR_COLLAPSE_TOL:
            logger.critical(
                f"[{symbol}] RR COLLAPSE: {rr:.2f} → {_rr_final:.3f} — "
                f"closing position"
            )
            try:
                send_telegram(
                    f"🚨 {symbol} RR collapsed {rr:.2f}→{_rr_final:.2f} — closing"
                )
            except Exception:
                pass
            closed_ok = False
            thread_launched = False
            try:
                # ── REV 1.9.3 — write lock. ──
                with _write_lock:
                    client.futures_create_order(
                        symbol=pair, side=close_side, type='MARKET',
                        quantity=qty_str, reduceOnly=True,
                    )
                time.sleep(1.5)
                p = fetch_position_raw(symbol)
                if p is not None and abs(float(p.get('positionAmt', '0') or 0)) == 0:
                    closed_ok = True
            except Exception as ce:
                logger.error(f"[{symbol}] RR-collapse close failed: {ce}")
            if not closed_ok:
                threading.Thread(
                    target=emergency_close_retry,
                    args=(symbol, pair, close_side),
                    daemon=True,
                ).start()
                thread_launched = True
            if not thread_launched:
                try:
                    handle_trade_close(symbol, pair, reason="RR_COLLAPSE")
                except Exception:
                    pass
            _release_slot()
            return False

        if _rr_final < _min_rr:
            logger.warning(
                f"[{symbol}] RR boundary: {rr:.3f} → {_rr_final:.4f} "
                f"(within tol) — keeping position"
            )

        # ─── Final risk audit log ───
        try:
            _final_risk_usd = float(qty_dec) * abs(entry_price - float(sl_price))
            _final_risk_pct = (
                (_final_risk_usd / wallet_balance * 100.0)
                if wallet_balance > 0 else 0.0
            )
            logger.info(
                f"[{symbol}] RISK CHECK: actual {_final_risk_pct:.3f}% "
                f"(target {_risk_percent:.3f}%) | qty={qty_dec}"
            )
        except Exception as _rce:
            logger.debug(f"[{symbol}] risk-check log failed: {_rce}")

        # ═══════════════════════════════════════════════════════════
        #  PLACE TP1 / TP2
        #  REV 1.9.1 — recompute *_adj on filter cache-bust.
        #  REV 1.9.9 — both wrapped in _write_lock.
        # ═══════════════════════════════════════════════════════════
        tp1_adj = adjust_price(tp1_price, f['tickSize'])
        tp2_adj = adjust_price(tp2_price, f['tickSize'])

        tp1_id = None
        tp1_placed = False
        for tp1_attempt in range(2):
            try:
                def _place_tp1():
                    with _write_lock:
                        return client.futures_create_order(
                            symbol=pair, side=close_side, type='TAKE_PROFIT_MARKET',
                            stopPrice=tp1_adj, quantity=qty_tp1,
                            reduceOnly=True, timeInForce='GTC',
                            workingType='MARK_PRICE',
                            newClientOrderId=f"tb_tp1_{pair}_{uuid.uuid4().hex[:12]}",
                        )
                tp1_resp = _run_with_timeout(_place_tp1, 5, f"tp1:{pair}")
                tp1_id = tp1_resp.get('orderId') or tp1_resp.get('algoId')
                logger.info(f" TP1 at {tp1_adj} Qty {qty_tp1} (ID: {tp1_id})")
                tp1_placed = True
                break
            except BinanceAPIException as e:
                if handle_order_filter_error(pair, e):
                    f = get_filters(pair)
                    # REV 1.9.1 — recompute tp1_adj with fresh tickSize.
                    tp1_adj = adjust_price(tp1_price, f['tickSize'])
                    continue
                logger.warning(f"TP1 attempt {tp1_attempt+1} failed: {e}")
                time.sleep(1)
            except Exception as e:
                logger.warning(f"TP1 attempt {tp1_attempt+1} failed: {e}")
                time.sleep(1)
        if not tp1_placed:
            logger.error(f"TP1 FAILED for {symbol}")
            try:
                send_telegram(f"⚠️ TP1 FAILED for {symbol}")
            except Exception:
                pass

        tp2_id = None
        if Decimal(qty_tp2) > 0:
            tp2_placed = False
            for tp2_attempt in range(2):
                try:
                    def _place_tp2():
                        with _write_lock:
                            return client.futures_create_order(
                                symbol=pair, side=close_side,
                                type='TAKE_PROFIT_MARKET',
                                stopPrice=tp2_adj, quantity=qty_tp2,
                                reduceOnly=True, timeInForce='GTC',
                                workingType='MARK_PRICE',
                                newClientOrderId=f"tb_tp2_{pair}_{uuid.uuid4().hex[:12]}",
                            )
                    tp2_resp = _run_with_timeout(_place_tp2, 5, f"tp2:{pair}")
                    tp2_id = tp2_resp.get('orderId') or tp2_resp.get('algoId')
                    logger.info(f" TP2 at {tp2_adj} Qty {qty_tp2} (ID: {tp2_id})")
                    tp2_placed = True
                    break
                except BinanceAPIException as e:
                    if handle_order_filter_error(pair, e):
                        f = get_filters(pair)
                        # REV 1.9.1 — recompute tp2_adj with fresh tickSize.
                        tp2_adj = adjust_price(tp2_price, f['tickSize'])
                        continue
                    logger.warning(f"TP2 attempt {tp2_attempt+1} failed: {e}")
                    time.sleep(1)
                except Exception as e:
                    logger.warning(f"TP2 attempt {tp2_attempt+1} failed: {e}")
                    time.sleep(1)
            if not tp2_placed:
                logger.error(f"TP2 FAILED for {symbol}")

        # ─── Final state write ───
        add_active_trade(symbol, {
            'entry': entry_price, 'qty': qty_str, 'sl': sl_adj,
            'tp1': tp1_adj, 'tp2': tp2_adj, 'side': side,
            'entry_time': _entry_time_str,
            'sl_id': sl_id, 'sl_level': 0,
            'initial_sl': float(sl_adj),
            'tp1_id': tp1_id, 'tp2_id': tp2_id,
            'tp1_qty': qty_tp1, 'tp2_qty': qty_tp2,
            'strategy': strategy,
            'regime_at_entry': _regime_now,
            'hold_time_minutes': _hold_min,
            'client_order_id': cid_used,
            'unverified': False,
        })

        # ─── CSV log ───
        try:
            with open(CSV_FILE, 'a', encoding='utf-8') as logf:
                logf.write(
                    f"{datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')},"
                    f"{symbol},{side},{entry_price},{entry_price_est},"
                    f"{sl_adj},{tp1_adj},{tp2_adj},{qty_str},{conf:.1f},{rr:.2f},"
                    f"{pattern},{fg},{strategy}\n"
                )
        except Exception as e:
            logger.warning(f"CSV write failed: {e}")

        logger.info(
            f" {side} {pair} {qty_str} | SL {sl_adj} TP1 {tp1_adj} "
            f"TP2 {tp2_adj} | strategy={strategy}"
        )
        try:
            send_telegram(
                f" NEW TRADE [{strategy}]\n{symbol} {side}\n"
                f"Entry: ${entry_price:.2f}\nSL: {sl_adj}\n"
                f"TP1: {tp1_adj}\nTP2: {tp2_adj}"
            )
        except Exception:
            pass
        return True

    except Exception as e:
        # ═══════════════════════════════════════════════════════════
        #  REV 1.9.4 (C3) — POST-FILL CRASH SAFETY NET.
        #
        #  If an exception was raised AFTER the market order was
        #  submitted (anywhere past _place_market_idempotent), the
        #  position may exist on the exchange with NO SL, NO TP, and
        #  no bot-tracked record. Previously this handler just logged
        #  and released the slot, leaving a naked position until the
        #  next process restart. The orphan-watch would only alert
        #  (~60s), not adopt (auto_adopt defaults False).
        #
        #  Now: probe the exchange for a real position. If one exists
        #  — or if the probe itself fails (unknown state, assume the
        #  worst) — launch emergency_close_retry on a background
        #  thread. That routine uses a deterministic cid, retries up
        #  to ~40s, and cleans up on success.
        # ═══════════════════════════════════════════════════════════
        logger.error(
            f"Unexpected error in place_order for {symbol}: {e}",
            exc_info=True,
        )
        try:
            _p = fetch_position_raw(symbol)
            _naked = (
                _p is None
                or float(_p.get('positionAmt', 0) or 0) != 0
            )
        except Exception:
            # Cannot determine → conservative: assume filled.
            _naked = True

        if _naked:
            _close_side = 'SELL' if side == 'BUY' else 'BUY'
            logger.critical(
                f"[{symbol}] exception AFTER possible fill — "
                f"launching emergency_close_retry "
                f"(side={_close_side}) to flatten any residual position"
            )
            try:
                threading.Thread(
                    target=emergency_close_retry,
                    args=(symbol, pair, _close_side),
                    daemon=True,
                ).start()
            except Exception as _te:
                logger.error(
                    f"[{symbol}] could not launch emergency close "
                    f"thread: {_te} — MANUAL CHECK REQUIRED"
                )
            try:
                send_telegram(
                    f"🚨 {symbol} entry crashed post-fill — "
                    f"emergency flatten launched (side={_close_side})"
                )
            except Exception:
                pass

        _release_slot()
        return False