"""
orders/entry.py — Order placement.

REV 1.9.3 (2026-10-05) — READ/WRITE LOCK MIGRATION (Phase 2, Step 6):
  ✅ Migrated all 9 explicit `_requests_lock` sites to `_write_lock`.
     Every one of them wraps a `futures_create_order` MARKET close:
       • adverse-slippage flatten
       • crossed-SL close (sync + retry)
       • SL -2021 emergency close
       • tight-branch corrective reduce
       • wide-branch >50%-of-qty emergency close
       • wide-branch corrective reduce
       • post-fix notional below-min close
       • RR-collapse close
     All are WRITES — the correct lock is _write_lock.

     Effect: entry.py's market closes no longer compete with read-only
     probes (klines, position fetches, account snapshots) for the same
     lock. With the alias `_requests_lock = _write_lock` in
     core.client, the migration is behaviour-neutral — but the intent
     is now explicit and this file is ready for the eventual alias
     removal.

     Note: this file has NO read-lock sites to migrate — every
     existing explicit lock wraps an order create. The unlocked
     read calls (futures_position_information, futures_account,
     futures_get_order inside _resolve_ambiguous_market) are pre-
     existing behaviour and were NOT changed by this revision.

     Zero behaviour change. Same lock primitive at each call site.

REV 1.9.2 (2026-10-04) — CRITICAL DOUBLE-ENTRY + SIZING GUARD FIXES:
  ✅ CRITICAL (C1): `_place_market_idempotent` no longer retries with
     a FRESH clientOrderId when the ambiguous-market resolver returns
     status='UNKNOWN'. Previously, if Binance accepted the first order
     but its ack hadn't propagated by the time the 8s poll gave up,
     the code generated a new cid and placed a SECOND MARKET order —
     opening 2× the intended position. Now: fresh cid ONLY on terminal
     status (CANCELED/EXPIRED/REJECTED). On UNKNOWN, return to caller
     immediately so the trade is registered as `unverified` and
     reconciled by the guardian.
  ✅ CRITICAL (C5): `max_oversize_mult` now reads from a DEDICATED
     config key `risk_oversize_abort_mult` (default 1.20). Previously
     it read `risk_oversize_warn_mult` — the same key used for the
     log-only warn threshold. An operator tuning the warn key up
     (e.g. to 2.0, thinking it's just logging) silently disabled the
     sizing abort guard. Requires a matching key in
     core/config_center.py::GLOBAL — falls back to 1.20 if absent.
  ✅ CRITICAL (C6): the outer `except` around `_risk_size_with_floor_guard`
     no longer bumps qty up to `min_qty` on failure. Previously any
     unexpected exception in the sizing function silently produced an
     oversized (min-qty) position that bypassed the risk budget. Now
     the entry is rejected cleanly (`_release_slot(); return False`).

REV 1.9.1 (2026-10-04) — FILTER-BUST RECOMPUTE + MINOR CLEANUP (retained).
REV 1.9.0 (2026-10-03) — HARDENING PASS (retained).
REV 1.8.1 (2026-10-03) — ZERO-COERCION FIX (retained).
REV 1.8.0 (2026-10-02) — RUNTIME TOGGLE AWARENESS (retained).
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from decimal import Decimal
from datetime import datetime
from typing import Optional

from binance.exceptions import BinanceAPIException

from core.client import (
    fetch_position_raw, get_filters, adjust_qty, adjust_price,
    invalidate_account_cache, refresh_timestamp,
    _run_with_timeout, send_telegram, logger,
    # ── REV 1.9.3 — explicit write lock (Phase 2). ──
    # Every explicit lock site in this file wraps a futures_create_order
    # MARKET close, i.e. a WRITE. The legacy _requests_lock alias is
    # no longer imported here on purpose: any missed migration site
    # will raise a NameError at runtime rather than silently
    # over-serialize.
    _write_lock, VALID_SYMBOLS, _filters_ok_to_trade,
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
# ═════════════════════════════════════════════════════════════
def _client_order_id(symbol: str, tag: str) -> str:
    """
    Deterministic per-attempt client order id. A RETRY with a new tag
    is a NEW order; an AMBIGUOUS OUTCOME is resolved by re-querying
    the SAME id.
    """
    return f"tb_{tag}_{symbol}_{uuid.uuid4().hex[:16]}"


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
#  REV 1.9.0 — RISK SIZING WITH FLOOR GUARD
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
        qty = (max_qty // step) * step

    # Floor: max of minQty and minNotional/price, step-aligned.
    floor_qty = max(min_qty, Decimal(str(min_notional)) / rpx)
    floor_qty = (floor_qty // step) * step
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
    """
    for attempt in range(max_attempts):
        try:
            def _place():
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
        try:
            pos_list = client.futures_position_information(symbol=pair)
            if pos_list and float(pos_list[0]['positionAmt']) != 0:
                logger.warning(f" {symbol} already in position")
                _release_slot()
                return False
        except Exception as e:
            logger.warning(f"Position pre-check failed for {pair}: {e}")

        # Account + margin checks
        try:
            account = client.futures_account()
            try:
                from core.client import _ACCOUNT_CACHE, _ACCOUNT_CACHE_LOCK
                with _ACCOUNT_CACHE_LOCK:
                    _ACCOUNT_CACHE['data'] = account
                    _ACCOUNT_CACHE['time'] = time.time()
            except Exception:
                pass

            available = float(account['availableBalance'])
            wallet_balance = float(account.get('totalWalletBalance', available))
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
        try:
            client.futures_change_margin_type(symbol=pair, marginType='ISOLATED')
        except Exception as e:
            if '-4046' not in str(e) and 'No need to change margin type' not in str(e):
                logger.debug(f"Margin type warning: {e}")

        try:
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
        if notional < min_notional:
            _pre_bump_qty = qty_dec
            _bump_mult = float(_cc_get_num("min_notional_bump_mult", 1.02))
            logger.warning(
                f"Notional {notional:.2f} < min {min_notional}, increasing qty"
            )
            qty_dec = Decimal(str(min_notional / entry_price_est * _bump_mult))
            qty_dec = (qty_dec // step) * step
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

        close_side = 'SELL' if side == 'BUY' else 'BUY'
        _shared_entry_time_str = datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')
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
                'entry_time': _shared_entry_time_str,
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
                'entry_time': _shared_entry_time_str,
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
                'entry_time': _shared_entry_time_str,
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
            # REV 1.9.0 — adverse slippage abort
            _slip_max = float(_cc_get_num("max_fill_slippage_pct", 0.50))
            if _drift_sig > _slip_max:
                logger.critical(
                    f"[{symbol}] ADVERSE SLIPPAGE {_drift_sig:.3f}% > "
                    f"{_slip_max:.3f}% — flattening position"
                )
                try:
                    send_telegram(
                        f"🚨 {symbol} adverse slippage {_drift_sig:.2f}% "
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
                return False
        except Exception as _te:
            logger.debug(f"[{symbol}] telemetry failed: {_te}")

        # ── Register active trade EARLY (before SL placement) ──
        add_active_trade(symbol, {
            'entry': entry_price, 'qty': qty_str, 'sl': '0',
            'tp1': '0', 'tp2': '0', 'side': side,
            'entry_time': _shared_entry_time_str,
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
        # ═══════════════════════════════════════════════════════════
        sl_adj = adjust_price(sl_price, f['tickSize'])
        sl_placed = False
        sl_id = None
        for attempt in range(3):
            try:
                def _place_sl():
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
            'entry_time': _shared_entry_time_str,
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
        # ═══════════════════════════════════════════════════════════
        tp1_adj = adjust_price(tp1_price, f['tickSize'])
        tp2_adj = adjust_price(tp2_price, f['tickSize'])

        tp1_id = None
        tp1_placed = False
        for tp1_attempt in range(2):
            try:
                def _place_tp1():
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
            'entry_time': _shared_entry_time_str,
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
        logger.error(
            f"Unexpected error in place_order for {symbol}: {e}",
            exc_info=True,
        )
        _release_slot()
        return False