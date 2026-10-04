"""
orders/exit.py — Trade close and emergency close.

REV 1.7.1 (2026-10-04) — DETERMINISTIC EMERGENCY-CLOSE CID (C4):
  ✅ CRITICAL: emergency_close_retry() now uses a DETERMINISTIC
     newClientOrderId derived from (pair, close_side, qty_str) via
     SHA-256, instead of a fresh UUID per attempt. Previous behaviour
     used `_new_cid("emg", pair)` on every attempt — if attempt #1's
     HTTP ack was lost but the order landed on Binance, attempt #2
     fired a SECOND MARKET reduceOnly. Binance's idempotency
     guarantee (same cid + same params = same order, no duplicate)
     is now active: a retry of the SAME (pair, side, qty) resolves
     to the SAME order, never a duplicate.
  ✅ The cid changes only when qty_str changes (partial fill settled
     → new qty → new cid). That's correct: the params changed, so
     a new order is legitimate.
  ✅ _new_cid() retained for non-emergency call sites (unchanged).

REV 1.7.0 (2026-10-03) — FAILURE-PATH HARDENING (retained).
REV 1.6.1 (2026-10-03) — CORRECTNESS HARDENING (retained).
REV 1.6.0 (2026-10-02) — RUNTIME TOGGLE AWARENESS (retained).
REV 1.5.6 (2026-10-02) — DAILY-LOSS COUNT CLARITY (retained).
REV 1.5.5 (2026-09-29) — DUPLICATE-CLOSE REASON TRACKING (retained).
"""
from __future__ import annotations

import hashlib
import random
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from binance.exceptions import BinanceAPIException

from core.client import (
    fetch_position_raw, get_filters, adjust_qty,
    refresh_timestamp, _get_open_algo_orders, _cancel_algo_order,
    invalidate_account_cache, send_telegram, logger,
    _requests_lock,
    handle_order_filter_error,   # REV 1.7.0
)
from core.config import CONFIG
from core.state import (
    PKT, get_active_trade, remove_active_trade,
    bot_tracked_symbols, BOT_TRACKED_LOCK,
    cooldown_until, COOLDOWN_LOCK, save_cooldowns,
    fail_counts, FAIL_COUNTS_LOCK,
    daily_tracker,
    CSV_EXIT_FILE,
)

from core.config_center import get as _cc_get

from .utils import _cl, _JUST_CLOSED, _JUST_CLOSED_LOCK


# ═════════════════════════════════════════════════════════════
#  DATA_DIR-ANCHORED PATHS
# ═════════════════════════════════════════════════════════════
_UNMANAGED_POSITIONS_FILE = str(
    Path(CSV_EXIT_FILE).parent / "unmanaged_positions.json"
)


# ═════════════════════════════════════════════════════════════
#  Reason classification
# ═════════════════════════════════════════════════════════════
_MANUAL_UI_REASONS = frozenset({"MANUAL_UI"})


# ═════════════════════════════════════════════════════════════
#  FIRST-REASON TRACKER (diagnostic only)
# ═════════════════════════════════════════════════════════════
_CLOSED_REASONS: dict[str, str] = {}


# ═════════════════════════════════════════════════════════════
#  REV 1.7.0 — TUNING CONSTANTS
# ═════════════════════════════════════════════════════════════
# Backoff ladder for emergency close attempts (seconds).
_EMG_BACKOFF = (0.0, 1.0, 2.0, 4.0, 8.0, 12.0)
# Adaptive delays for PnL settlement polling.
_PNL_RETRY_DELAYS = (1.0, 1.5, 2.0, 3.0, 4.0)


# ═════════════════════════════════════════════════════════════
#  SAFE CC NUMERIC READ (0-preserving)
# ═════════════════════════════════════════════════════════════
def _cc_get_num(key: str, default):
    v = _cc_get(key, None)
    return default if v is None else v


def _new_cid(tag: str, pair: str) -> str:
    """Random per-attempt client order id (non-emergency call sites)."""
    return f"tb_{tag}_{pair}_{uuid.uuid4().hex[:12]}"


# ═════════════════════════════════════════════════════════════
#  REV 1.7.1 (C4) — DETERMINISTIC EMERGENCY-CLOSE CID
# ═════════════════════════════════════════════════════════════
def _emg_cid(pair: str, close_side: str, qty_str: str) -> str:
    """
    Deterministic clientOrderId for an emergency-close MARKET order.

    Derived from (pair, close_side, qty_str) so that a RETRY of the
    SAME close request reuses the SAME cid. Binance's idempotency
    guarantee (same cid + same params → same order, no duplicate)
    is therefore active: even if attempt #1's ack was lost but the
    order landed, attempt #2 with the same cid returns the original
    order instead of placing a second MARKET reduceOnly.

    If qty_str changes (partial fill settled between attempts), the
    cid changes — that's correct, because the params changed and a
    new order is legitimate.

    Binance caps clientOrderId at 36 characters. We truncate the
    tail if the pair is long (e.g. 1000PEPEUSDT).
    """
    payload = f"EMG|{pair}|{close_side}|{qty_str}".encode()
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"tb_emg_{pair}_{digest}"[:36]


# ═════════════════════════════════════════════════════════════
#  REV 1.7.0 — EMERGENCY CLOSE SUCCESS PATH (idempotent)
# ═════════════════════════════════════════════════════════════
def _emergency_close_success(symbol, pair):
    logger.info(f"✅ EMERGENCY CLOSE verified for {symbol}")
    try:
        send_telegram(f"✅ EMERGENCY CLOSE {symbol}")
    except Exception:
        pass
    try:
        handle_trade_close(symbol, pair, reason="emergency_close")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════
#  Emergency close retry loop
#  REV 1.7.0 — rewritten
#  REV 1.7.1 (C4) — deterministic cid per (pair, side, qty)
# ═════════════════════════════════════════════════════════════
def emergency_close_retry(symbol, pair, close_side):
    """
    Close a position with MARKET reduceOnly, with bounded retries.

    Budget: sum(_EMG_BACKOFF) ≈ 27s of inter-attempt waits, plus
    ~2s verify per attempt → worst case ~40s. Under normal conditions
    the first attempt succeeds and verify returns within ~1s.

    Idempotent: safe to call from multiple places (guarded at the
    handle_trade_close layer by _JUST_CLOSED).

    REV 1.7.1 (C4) — every attempt that targets the SAME (pair,
    close_side, qty) reuses the SAME cid. This makes the operation
    truly idempotent at the exchange: a lost-ack retry resolves to
    the original order rather than placing a duplicate MARKET close.
    """
    client = _cl()
    if client is None:
        logger.error(
            f"[{symbol}] emergency_close_retry: client not initialized"
        )
        return

    max_attempts = len(_EMG_BACKOFF)

    for attempt in range(max_attempts):
        try:
            refresh_timestamp()
            with _requests_lock:
                pos_arr = client.futures_position_information(symbol=pair)

            if not pos_arr:
                # Don't stall; short backoff and retry.
                time.sleep(1.0 + random.uniform(0, 0.5))
                continue

            amt = abs(float(pos_arr[0].get('positionAmt', 0) or 0))
            if amt == 0:
                _emergency_close_success(symbol, pair)
                return

            f = get_filters(pair)
            qty_str = adjust_qty(amt, f['stepSize'], f['minQty'])

            # ── REV 1.7.1 (C4) — deterministic cid per (pair, side, qty).
            # Retries with the SAME qty reuse the SAME cid, activating
            # Binance's idempotency guarantee.
            emg_cid = _emg_cid(pair, close_side, qty_str)

            # ── Place the close ──
            try:
                with _requests_lock:
                    client.futures_create_order(
                        symbol=pair, side=close_side, type='MARKET',
                        quantity=qty_str, reduceOnly=True,
                        newClientOrderId=emg_cid,
                    )
            except BinanceAPIException as e:
                if e.code in (-1003, -1008):
                    retry_after = None
                    try:
                        r = getattr(e, 'response', None)
                        if r is not None and r.headers.get('Retry-After'):
                            retry_after = float(r.headers['Retry-After'])
                    except Exception:
                        pass
                    base = retry_after if retry_after is not None \
                        else (2 ** attempt)
                    if e.code == -1008 and retry_after is None:
                        base *= 2
                    sleep_s = min(base + random.uniform(0, 0.5), 15.0)
                    logger.warning(
                        f"[{symbol}] emergency close rate-limit "
                        f"{e.code}, sleep {sleep_s:.1f}s"
                    )
                    time.sleep(sleep_s)
                    continue

                if e.code == -1021:
                    logger.warning(
                        f"[{symbol}] emergency close -1021; "
                        f"refresh timestamp and retry"
                    )
                    try:
                        refresh_timestamp()
                    except Exception:
                        pass
                    time.sleep(0.5)
                    continue

                if handle_order_filter_error(pair, e):
                    # Cache busted; next iteration re-rounds with fresh
                    # filters — qty_str may change → new deterministic
                    # cid. That is correct (params changed).
                    continue

                if e.code == -2021:
                    # MARKET orders should never trigger this; log it
                    # and treat as transient.
                    logger.warning(
                        f"[{symbol}] emergency close -2021 "
                        f"(unexpected for MARKET) — retrying"
                    )
                    time.sleep(0.5)
                    continue

                # ── REV 1.7.1 (C4) — duplicate-cid resolution ──
                # -2022: ReduceOnly rejected (position already flat).
                # -2011: order not found (rare for reduceOnly retry).
                # In either case, verify position before reacting.
                if e.code == -2022:
                    try:
                        with _requests_lock:
                            _chk = client.futures_position_information(
                                symbol=pair
                            )
                        if _chk and abs(float(
                            _chk[0].get('positionAmt', 0) or 0
                        )) == 0:
                            _emergency_close_success(symbol, pair)
                            return
                    except Exception as _pe:
                        logger.warning(f"[emergency_close_retry] ignored API error: {type(_pe).__name__}: {_pe}")

                logger.warning(
                    f"[{symbol}] emergency close attempt {attempt+1} "
                    f"({e.code}): {e}"
                )
            except Exception as e:
                logger.warning(
                    f"[{symbol}] emergency close attempt {attempt+1} "
                    f"failed: {e}"
                )

            logger.info(f" EMERGENCY CLOSE attempt {attempt+1} {symbol}")

            # ── Verify with a short poll ──
            for _ in range(4):
                time.sleep(0.5 + random.uniform(0, 0.3))
                try:
                    with _requests_lock:
                        pos = client.futures_position_information(
                            symbol=pair
                        )[0]
                    if float(pos['positionAmt']) == 0:
                        _emergency_close_success(symbol, pair)
                        return
                except Exception as _pe:
                    logger.warning(f"[emergency_close_retry] ignored API error: {type(_pe).__name__}: {_pe}")

        except Exception as e:
            logger.warning(
                f"Emergency close attempt {attempt+1} outer: {e}"
            )

        # Backoff between attempts
        if attempt < max_attempts - 1:
            time.sleep(_EMG_BACKOFF[attempt] + random.uniform(0, 0.5))

    # ── All attempts failed ──
    logger.critical(f"❌ Could not close {symbol}")
    try:
        with open(_UNMANAGED_POSITIONS_FILE, "a", encoding='utf-8') as f:
            f.write(
                f"{datetime.now(PKT).isoformat()} - {symbol} - {pair} - "
                f"MANUAL CLOSE REQUIRED\n"
            )
    except Exception as e:
        logger.warning(f"Failed to log unmanaged position {symbol}: {e}")
    try:
        send_telegram(f"🚨 CRITICAL {symbol} – MANUAL CHECK REQUIRED")
    except Exception:
        pass


# ═════════════════════════════════════════════════════════════
#  robust_cancel_all
#  REV 1.7.0 — jitter + rate-limit awareness
# ═════════════════════════════════════════════════════════════
def robust_cancel_all(symbol_pair):
    client = _cl()
    if client is None:
        return False
    for attempt in range(3):
        try:
            refresh_timestamp()
            with _requests_lock:
                try:
                    client.futures_cancel_all_open_orders(
                        symbol=symbol_pair
                    )
                except BinanceAPIException as e:
                    if e.code in (-1003, -1008):
                        logger.debug(
                            f"robust_cancel {symbol_pair}: rate limit "
                            f"{e.code}"
                        )
                    else:
                        logger.debug(
                            f"robust_cancel {symbol_pair}: {e.code}: {e}"
                        )
                except Exception as e:
                    logger.debug(f"robust_cancel {symbol_pair}: {e}")

                for o in _get_open_algo_orders(symbol_pair):
                    oid = o.get('algoId') or o.get('orderId')
                    if oid:
                        _cancel_algo_order(symbol_pair, oid)

                try:
                    open_orders = client.futures_get_open_orders(
                        symbol=symbol_pair
                    )
                    for o in open_orders:
                        try:
                            client.futures_cancel_order(
                                symbol=symbol_pair, orderId=o['orderId']
                            )
                        except Exception as _pe:
                            logger.warning(f"[robust_cancel_all] ignored API error: {type(_pe).__name__}: {_pe}")
                except Exception as _pe:
                    logger.warning(f"[robust_cancel_all] ignored API error: {type(_pe).__name__}: {_pe}")

                for o in _get_open_algo_orders(symbol_pair):
                    oid = o.get('algoId') or o.get('orderId')
                    if oid:
                        _cancel_algo_order(symbol_pair, oid)

            time.sleep(0.3)

            with _requests_lock:
                remaining = client.futures_get_open_orders(
                    symbol=symbol_pair
                )
            remaining_algo = _get_open_algo_orders(symbol_pair)
            if not remaining and not remaining_algo:
                logger.info(f"  [{symbol_pair}] All orphaned orders cancelled")
                return True
        except Exception as e:
            logger.debug(f"robust_cancel {symbol_pair} {attempt+1}: {e}")
        time.sleep(1.0 + random.uniform(0, 0.5))
    return False


# ═════════════════════════════════════════════════════════════
#  get_total_pnl
#  REV 1.7.0 — API call under _requests_lock
# ═════════════════════════════════════════════════════════════
def get_total_pnl(symbol, entry_time_ms):
    pair = symbol + 'USDT'
    client = _cl()
    if client is None:
        return 0.0
    total = 0.0
    page_size = int(CONFIG.income_page_size)
    max_pages = int(CONFIG.max_income_pages)
    try:
        refresh_timestamp()
        cursor = int(entry_time_ms)
        for _page in range(max_pages):
            with _requests_lock:
                income = client.futures_income_history(
                    symbol=pair, startTime=cursor, limit=page_size
                )
            if not income:
                break
            for entry in income:
                if entry['symbol'] == pair and entry['incomeType'] in (
                    'REALIZED_PNL', 'COMMISSION', 'FUNDING_FEE'
                ):
                    total += float(entry['income'])
            if len(income) < page_size:
                break
            last_time = max(int(e.get('time', cursor)) for e in income)
            if last_time <= cursor:
                break
            cursor = last_time + 1
    except BinanceAPIException as e:
        if e.code in (-1003, -1008):
            logger.warning(
                f"[{symbol}] income history rate limit {e.code}; "
                f"returning partial sum {total:.4f}"
            )
        else:
            logger.debug(f"Income sum error {symbol} ({e.code}): {e}")
    except Exception as e:
        logger.debug(f"Income sum error {symbol}: {e}")
    return total


# ═════════════════════════════════════════════════════════════
#  handle_trade_close
#  REV 1.7.0 — adaptive PnL retry window
# ═════════════════════════════════════════════════════════════
def handle_trade_close(symbol, pair=None, reason="closed"):
    if pair is None:
        pair = symbol + 'USDT'

    if not reason:
        reason = "closed"

    client = _cl()

    # ── DUPLICATE-GUARD WITH FIRST-REASON LOOKUP ──
    with _JUST_CLOSED_LOCK:
        now = time.time()
        stale = [k for k, v in list(_JUST_CLOSED.items()) if now - v > 60]
        for k in stale:
            del _JUST_CLOSED[k]
            _CLOSED_REASONS.pop(k, None)

        if symbol in _JUST_CLOSED:
            first_reason = _CLOSED_REASONS.get(symbol, "unknown")
            logger.info(
                f"[{symbol}] handle_trade_close skip — already closed "
                f"(first_reason={first_reason}, this_call={reason}, "
                f"within 60s)"
            )
            return

        _JUST_CLOSED[symbol] = now
        _CLOSED_REASONS[symbol] = reason

    try:
        if not robust_cancel_all(pair):
            logger.warning(
                f"[{symbol}] robust_cancel_all did not confirm full "
                f"cleanup — proceeding with close (monitor will reconcile)"
            )
    except Exception as e:
        logger.debug(f"robust_cancel_all in handle_trade_close failed: {e}")

    active = get_active_trade(symbol)
    entry_time_str = active.get('entry_time')
    entry_time_ms = 0
    if entry_time_str:
        try:
            dt = datetime.strptime(
                entry_time_str, '%Y-%m-%d %I:%M:%S %p'
            )
            entry_time_ms = int(dt.replace(tzinfo=PKT).timestamp() * 1000)
        except Exception as e:
            logger.warning(f"entry_time parse failed {symbol}: {e}")

    if entry_time_ms == 0:
        pnl = 0.0
        reason = "MANUAL"
    else:
        pnl = get_total_pnl(symbol, entry_time_ms)

    # ── PnL retry window (REV 1.7.0 — adaptive, ~12s cap) ──
    if pnl == 0.0 and reason != "MANUAL":
        max_retries = 3 if reason in _MANUAL_UI_REASONS \
            else len(_PNL_RETRY_DELAYS)
        for _retry in range(max_retries):
            delay = _PNL_RETRY_DELAYS[_retry] + random.uniform(0, 0.3)
            time.sleep(delay)
            pnl_retry = get_total_pnl(symbol, entry_time_ms)
            if pnl_retry != 0.0:
                pnl = pnl_retry
                logger.info(
                    f"[{symbol}] PnL retry {_retry+1} got "
                    f"{pnl_retry:.4f} for {reason}"
                )
                break

    if pnl == 0.0:
        reason = "MANUAL" if reason in ("closed", "detected amt=0") else reason
    else:
        if reason in ("closed", "detected amt=0", "emergency_close"):
            reason = "SL" if pnl < 0 else "TP"

    # ── R-multiple of this close and "real loss" flag ──
    _r_mult = None
    try:
        _e = float(active.get('entry', 0) or 0)
        _isl = float(active.get('initial_sl', 0) or 0)
        _q = float(active.get('qty', 0) or 0)
        _risk_usd = abs(_e - _isl) * _q
        if _risk_usd > 0:
            _r_mult = pnl / _risk_usd
    except Exception:
        _r_mult = None

    _tiny_loss_r = float(_cc_get_num("tiny_loss_r", 0.30))
    _is_real_loss = pnl < 0 and (
        reason != "TIME_EXIT"
        or _r_mult is None
        or _r_mult <= -_tiny_loss_r
    )

    output_reason = "MANUAL" if reason in _MANUAL_UI_REASONS else reason
    is_manual_close = reason in _MANUAL_UI_REASONS

    try:
        with open(CSV_EXIT_FILE, 'a', encoding='utf-8') as ef:
            ef.write(
                f"{datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')},"
                f"{symbol},{pnl:.2f},{output_reason}\n"
            )
    except Exception as e:
        logger.warning(f"Exit log write failed: {e}")

    # ── Feed decision engine memory ──
    try:
        from signals.decision_engine import record_trade_result
        strategy_name = (
            active.get('strategy', 'UNKNOWN') if active else 'UNKNOWN'
        )
        if pnl < 0 and not _is_real_loss:
            logger.info(
                f"[{symbol}] tiny TIME_EXIT loss "
                f"(R={_r_mult if _r_mult is None else round(_r_mult, 2)}) "
                f"- not fed to loss-streak guard"
            )
        else:
            record_trade_result(strategy_name, pnl)
    except Exception as _de:
        logger.debug(f"[decision] record_trade_result failed: {_de}")

    _r_txt = "" if _r_mult is None else f" ({_r_mult:+.2f}R)"

    _cooldown_sl = int(_cc_get_num("cooldown_after_sl_min", 20))
    _cooldown_tp = int(_cc_get_num("cooldown_after_tp_min", 15))

    if pnl < 0:
        if _is_real_loss:
            daily_tracker.add_loss()
        cooldown_min = _cooldown_sl
        if is_manual_close:
            try:
                send_telegram(f"🔻 MANUAL CLOSE {symbol}\nPnL: ${pnl:.2f}")
            except Exception:
                pass
        elif reason == "TIME_EXIT":
            try:
                send_telegram(
                    f"⏰ TIME_EXIT {symbol}\nPnL: ${pnl:.2f}{_r_txt}"
                )
            except Exception:
                pass
        else:
            try:
                send_telegram(f" SL HIT {symbol}\nPnL: ${pnl:.2f}")
            except Exception:
                pass
    elif pnl > 0:
        cooldown_min = _cooldown_tp
        if is_manual_close:
            try:
                send_telegram(f"💰 MANUAL CLOSE {symbol}\nPnL: ${pnl:.2f}")
            except Exception:
                pass
        elif reason == "TIME_EXIT":
            try:
                send_telegram(
                    f"⏰ TIME_EXIT {symbol}\nPnL: ${pnl:.2f}{_r_txt}"
                )
            except Exception:
                pass
        else:
            try:
                send_telegram(f" TP HIT {symbol}\nPnL: ${pnl:.2f}")
            except Exception:
                pass
    else:
        cooldown_min = _cooldown_tp
        if is_manual_close:
            try:
                send_telegram(f"➖ MANUAL CLOSE {symbol} (BE)")
            except Exception:
                pass
        else:
            try:
                send_telegram(f" CLOSED {symbol} (BE)")
            except Exception:
                pass

    with COOLDOWN_LOCK:
        cooldown_until[symbol] = (
            datetime.now(PKT) + timedelta(minutes=cooldown_min)
        )
    try:
        save_cooldowns()
    except Exception as e:
        logger.debug(f"save_cooldowns failed in close: {e}")

    with BOT_TRACKED_LOCK:
        bot_tracked_symbols.discard(symbol)

    remove_active_trade(symbol)

    with FAIL_COUNTS_LOCK:
        fail_counts.pop(symbol, None)

    try:
        invalidate_account_cache()
    except Exception:
        pass

    logger.info(f" [{symbol}] Closed OFF ({output_reason})")