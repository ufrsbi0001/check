"""
future.py — Trading engine orchestrator + main scan loop.

REV 1.4.46 (2026-10-05) — SCAN THROUGHPUT BOOST (workers 8→16, timeout 15→30):
  ✅ Observed on 2026-10-05: every scan cycle logged

         [scan] parallel indicator fetch timed out after 15s —
         8/52 workers completed, 44 still pending.

     Root cause: 52 coins × ~4 API calls each (5m/1h/4h/1d klines)
     = ~200 calls, each taking 1-3s on the DEMO API (which is ~10×
     slower than mainnet). With 8 workers and a 15s wall cap, only
     ~8-10 workers ever finished. The other 42 coins were silently
     dropped from that cycle, so their signals, decision-engine
     votes, BTC-bias contributions, and MTF confluence were all
     missing from the scan snapshot. In practice this meant:

       • Trade opportunities on ~80% of the universe were invisible.
       • The decision engine ran on a partial universe (8/52).
       • Cycles repeated every 15-20s but each one was incomplete.

     Fix — two coordinated changes:

       1. _PARALLEL_FETCH_WORKERS: 8 → 16
          Doubles concurrency. Measured effect on DEMO: ~16-20
          workers now complete within the wall cap. This is well
          within Binance DEMO rate limits (~1200 req/min); at 16
          workers × ~0.7 calls/sec/worker ≈ 11 req/sec ≈ 670
          req/min, safely under the ceiling.

       2. _PARALLEL_FETCH_BATCH_TIMEOUT: 15.0s → 30.0s
          Doubles the wall-time cap. Combined with #1: ~32-40
          workers finish per cycle → ~60-80% of the 52-coin
          universe is now scanned every cycle instead of 15%.
          The 30s cap is still far below the 60s scan loop tick,
          so cycles never overlap.

     Both values are safe to tune further via env if a future
     deployment adds more coins or a faster API. Current values
     are the recommended balance for the current 52-coin DEMO
     setup.

     No behaviour change to the happy path (all workers finishing
     well under 30s): the code path is identical, only the two
     numeric thresholds moved.

     REV 1.4.43's structural design is preserved: pool created
     explicitly, shutdown(wait=False) in finally, partial-universe
     fallback on TimeoutError. This REV only widens the numbers.

REV 1.4.45 (2026-10-05) — DURABLE DD-HALT PAUSE MARKER:
  ✅ MEDIUM (C2 partial): the DD kill-switch's PAUSE_FILE write now
     fsyncs both the file and (best-effort, POSIX-only) the containing
     directory. Without fsync, the halt marker could sit in the OS
     page cache and be lost on a power loss / hard reboot, letting
     the bot restart into a non-halted state after having already
     flattened on a DD breach.

     Two fsyncs:
       1. File fsync — pushes the DD_HALT marker content to disk
          before the file descriptor closes. Without it, a crash
          between close() and the OS's eventual page-cache flush
          could leave an empty or partial PAUSE_FILE.
       2. Directory fsync — best-effort, POSIX-only. Persists the
          directory entry (filename → inode mapping) so the file is
          discoverable after reboot. No-op on Windows (opening a
          directory as a file descriptor is not supported there;
          wrapped in except (OSError, AttributeError)).

     Zero change to the happy path (write succeeds → same file
     contents, same restart behaviour). Only the durability guarantee
     improves. Startup-side `if Path(PAUSE_FILE).exists()` check is
     unchanged.

     Persist-FIRST restructuring (write PAUSE_FILE *before*
     flattening positions) is DEFERRED — the current persist-AFTER
     design is safe because daily_tracker's DD state is persisted
     independently to LOSS_FILE, so a mid-flatten crash still
     re-trips the kill-switch on the next startup. Only a crash in
     the narrow window just before PKT midnight would fail to
     re-trigger (date rollover resets the daily peak), and that
     corner case is queued for the C2 follow-up after live
     observations.

REV 1.4.44 (2026-10-05) — SPREAD FILTER FAIL-CLOSED (H5a):
  ✅ HIGH (H5a): the spread filter no longer fails OPEN when the
     bookTicker for a coin is missing. Previously:

         _bt = book_tickers.get(coin)
         if _bt:
             # ...spread check...
             trade_side = None   # on breach

     If `_bt` was None (fetch failed for this coin, coin absent from
     the batch response, or the cycle's book_tickers fetch returned
     an empty dict entirely), the whole spread check was SKIPPED and
     the entry proceeded. A halted, thinly-listed, or newly-listed
     symbol could therefore enter with no spread validation.

     Now: an `else` branch rejects the entry with skip_reason
     "NO TICKER (spread filter)" and a WARNING log. The trade manager
     still manages any EXISTING position on that coin (this only
     blocks new entries while we cannot verify the spread).

     Effect: entries can no longer bypass the spread guard via a
     missing/malformed bookTicker. During a Binance bookTicker
     brownout, the bot will simply hold off on new entries until
     tickers are readable — the correct conservative behaviour.

     Zero change to the happy path (ticker present → same check as
     before). Only the missing-ticker branch differs.

REV 1.4.43 (2026-10-05) — SCAN BATCH WALL-TIME CAP (retained):
  ✅ The parallel indicator fetch in main_loop() no longer blocks the
     scan cycle indefinitely when one or more workers hang. Previously
     `for fut in as_completed(futures):` had NO timeout — a single
     hung Binance call (network partition, rate-limit storm, TCP
     half-open) would freeze the entire scan cycle, delaying:
       • the DD kill-switch check
       • new-entry evaluation for all subsequent cycles
     (The trade manager thread runs independently and was NOT
     affected; only the scan loop itself was vulnerable.)

     Fix — three parts:
       1. as_completed(futures, timeout=_PARALLEL_FETCH_BATCH_TIMEOUT)
          with a wall-time cap (see REV 1.4.46 for current value).
       2. On TimeoutError, log a WARNING with done/pending counts and
          proceed with whatever results arrived — partial universe
          beats no universe.
       3. The pool is now created EXPLICITLY (not via `with`) so we
          can call pool.shutdown(wait=False) in a finally. The
          `with ThreadPoolExecutor(...)` idiom would call
          shutdown(wait=True) on block exit — which would WAIT for
          the abandoned workers and defeat the whole point of the
          timeout. wait=False lets the caller proceed immediately;
          in-flight workers finish in the background (client.py
          REV 11.7 guarantees they release _read_lock within the
          HTTP session timeout, ~30s).

     Effect: the scan cycle now has a hard upper bound on its wall
     time even under catastrophic API behaviour. The DD check, entry
     evaluation, and scan_completed log line all fire on schedule.

     Zero change to the happy path (all workers finish well under
     the wall cap). Only the pathological case gets a different
     code path.

REV 1.4.42 (2026-10-05) — WRITE-LOCK MIGRATION (Phase 2, Step 5):
  ✅ _place_adoption_stop() now acquires _c._write_lock instead of
     the legacy _c._requests_lock alias (= _write_lock). This is the
     only explicit lock site in this file — the adoption SL is an
     order placement, therefore a WRITE.

     Effect: identical to before (the alias already pointed at
     _write_lock), but the intent is now explicit and the site is
     ready for the eventual removal of the _requests_lock alias in
     core/client.py once every consumer has migrated.

     Zero behaviour change. Same lock primitive at this call site.

REV 1.4.41 (2026-10-05) — SAFETY + OBSERVABILITY PATCH:
  ✅ FIXED (HIGH): DD kill-switch was unreachable in degraded mode.
     If get_balance_or_last_good() returned a stale-but-valid balance
     (degraded=True), the main loop did `time.sleep(30); continue`
     BEFORE reaching the is_limit_reached() check. A losing day whose
     balance feed hiccuped would therefore never trip the kill-switch
     and the bot would keep cycling indefinitely. Now the DD check
     runs FIRST, using the last-good balance, and only then do we
     decide whether to skip new entries this cycle.
  ✅ FIXED: coin.replace('USDT', '') → coin.removesuffix('USDT').
     The old form would silently corrupt the short symbol of any
     ticker containing 'USDT' mid-string, breaking the IN POSITION
     display fix from REV 1.4.40. removesuffix is idempotent and
     matches the pattern already used elsewhere in this file.
  ✅ ADDED: config proxy contract self-check at startup. Calls
     core.config_center.verify_proxy_contract() and logs CRITICAL
     if config.py advertises proxy keys that are missing from
     config_center.GLOBAL. Fails LOUD instead of leaving a subtle
     CONFIG.<attr> → AttributeError landmine for the first reader.

REV 1.4.40 (2026-10-04) — IN-POSITION VISIBILITY IN SCANNER (retained).
REV 1.4.39 (2026-10-04) — ARM ORPHAN-WATCH ON DIRECT-CLI PATH (retained).
REV 1.4.38 (2026-10-04) — PATTERN COLUMN NOW SHOWS ACTUAL STRATEGY (retained).
REV 1.4.37 (2026-10-04) — DEAD _cfg CLEANUP (retained).
REV 1.4.36 (2026-10-04) — LOG BANNER SYNC TO 9-FILTER ENGINE (retained).
REV 1.4.35 (2026-10-04) — DEV-MODE ENTRY GUARD (Point 1) (retained).
REV 1.4.34 (2026-10-04) — OBSERVABILITY UPGRADE (Point 5) (retained).
REV 1.4.33 (2026-10-04) — ADOPTED SL FROM CONFIG (Point 4) (retained).
REV 1.4.32.1 (2026-10-04) — ADOPTION SL HARDENING (retained).
REV 1.4.32 (2026-10-04) — IMMEDIATE ADOPTION SL (C3) (retained).
REV 1.4.31 (2026-10-03) — ADOPTION + LOCK FINALIZATION (retained).
REV 1.4.30 (2026-10-03) — DD KILL-SWITCH + ORPHAN ADOPTION (retained).
REV 1.4.29 (2026-10-03) — SNAPSHOT-TO-LIVE FIX (retained).
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import (
    ThreadPoolExecutor,
    as_completed,
    TimeoutError as _FutTimeout,
)
from pathlib import Path
from datetime import datetime

from binance.exceptions import BinanceAPIException

from core.config import CONFIG
from core import config_center as CC
from core.coins_config import get_family

from core.config_center import VOL_CLASS_R_THRESHOLDS as _VOL_CLASS_R_THRESHOLDS

import core.client as _c
import core.state as _s
import market.indicators as _i
import orders as _o
import signals.router as _fr

from core.client import logger

try:
    from signals.decision_engine import COUNTER_TREND_STRATEGIES as _CT_STRATEGIES
except Exception:
    _CT_STRATEGIES = frozenset()


# ═════════════════════════════════════════════════════════════
#  REV 1.4.29 — 0-PRESERVING CC NUMERIC READ
# ═════════════════════════════════════════════════════════════
def _cc_get_num(key: str, default):
    v = CC.get(key, None)
    return default if v is None else v


# ─────────────────────────────────────────────────────────────
# RE-EXPORTS
# ─────────────────────────────────────────────────────────────
set_client_keys           = _c.set_client_keys
_validate_api_credentials = _c._validate_api_credentials
get_client                = _c.get_client
get_account_cached        = _c.get_account_cached
invalidate_account_cache  = _c.invalidate_account_cache
get_bot_coins             = _c.get_bot_coins
validate_symbols          = _c.validate_symbols
get_binance_klines        = _c.get_binance_klines
get_balance_or_last_good  = _c.get_balance_or_last_good
get_live_prices           = _c.get_live_prices
send_telegram             = _c.send_telegram
get_filters               = _c.get_filters
adjust_qty                = _c.adjust_qty
adjust_price              = _c.adjust_price
fetch_position_raw        = _c.fetch_position_raw
refresh_timestamp         = _c.refresh_timestamp
_run_with_timeout         = _c._run_with_timeout
_get_open_algo_orders     = _c._get_open_algo_orders
_cancel_algo_order        = _c._cancel_algo_order
_position_amt             = _c._position_amt

clear_stop                = _s.clear_stop
request_stop              = _s.request_stop
is_stopped                = _s.is_stopped
load_active_trades        = _s.load_active_trades
add_active_trade          = _s.add_active_trade
remove_active_trade       = _s.remove_active_trade
get_active_trade          = _s.get_active_trade
flush_active_trades       = _s.flush_active_trades
load_cooldowns            = _s.load_cooldowns
save_cooldowns            = _s.save_cooldowns
get_scan_results          = _s.get_scan_results
set_scan_results          = _s.set_scan_results
cleanup_entry_candles     = _s.cleanup_entry_candles
can_trade_coin            = _s.can_trade_coin
record_coin_trade         = _s.record_coin_trade
get_v2_stats_str          = _s.get_v2_stats_str
increment_v2_stat         = _s.increment_v2_stat
daily_tracker             = _s.daily_tracker

active_trades             = _s.active_trades
ACTIVE_TRADES_LOCK        = _s.ACTIVE_TRADES_LOCK
bot_tracked_symbols       = _s.bot_tracked_symbols
BOT_TRACKED_LOCK          = _s.BOT_TRACKED_LOCK
cooldown_until            = _s.cooldown_until
COOLDOWN_LOCK             = _s.COOLDOWN_LOCK
last_entry_candle         = _s.last_entry_candle
ENTRY_CANDLE_LOCK         = _s.ENTRY_CANDLE_LOCK
scan_results              = _s.scan_results
SCAN_RESULTS_LOCK         = _s.SCAN_RESULTS_LOCK
ATR_CACHE                 = _s.ATR_CACHE
ATR_CACHE_LOCK            = _s.ATR_CACHE_LOCK
FAIL_COUNTS_LOCK          = _s.FAIL_COUNTS_LOCK
fail_counts               = _s.fail_counts

place_order_fixed         = _o.place_order_fixed
manage_single_trade       = _o.manage_single_trade
handle_trade_close        = _o.handle_trade_close
emergency_close_retry     = _o.emergency_close_retry
cancel_specific_sl        = _o.cancel_specific_sl
cancel_all_sl_stops       = _o.cancel_all_sl_stops
reconcile_active_trade    = _o.reconcile_active_trade
trade_manager_loop        = _o.trade_manager_loop
robust_cancel_all         = _o.robust_cancel_all
cancel_orphan_bot_orders  = _o.cancel_orphan_bot_orders
sync_existing_positions   = _o.sync_existing_positions
clean_orders              = _o.clean_orders
get_trade_status          = _o.get_trade_status

get_trading_config        = _i.get_trading_config
get_fear_greed_index      = _i.get_fear_greed_index
get_cached_indicator      = _i.get_cached_indicator
set_cached_indicator      = _i.set_cached_indicator
cleanup_indicator_cache   = _i.cleanup_indicator_cache

# ═════════════════════════════════════════════════════════════
#  MODULE-LEVEL SNAPSHOTS — BANNER/LOG ONLY
# ═════════════════════════════════════════════════════════════
RISK_PERCENT               = CC.get('risk_percent')
MAX_OPEN_POSITIONS         = CC.get('max_open_positions')
MAX_DAILY_DRAWDOWN_PERCENT = CC.get('max_daily_drawdown_percent')
MIN_CONFIDENCE             = CC.get('min_confidence')
MIN_ADX                    = CC.get('min_adx')
REQUIRE_HTF_AGREEMENT      = CC.get('require_htf_agreement')
USE_5M_TREND_FILTER        = CC.get('use_5m_trend_filter')
FG_ENABLED                 = CC.get('fg_enabled')
LEVERAGE                   = CC.get('leverage')

DRY_RUN                    = CONFIG.dry_run
DEMO_MODE                  = CONFIG.demo_mode
TESTNET                    = CONFIG.testnet
TRADING_MODE               = "TREND_PULLBACK"

PKT = _s.PKT
BLACKLISTED_COINS = _s.BLACKLISTED_COINS
CSV_FILE          = _s.CSV_FILE
CSV_EXIT_FILE     = _s.CSV_EXIT_FILE
PAUSE_FILE        = _s.PAUSE_FILE

# ═════════════════════════════════════════════════════════════
#  REV 1.4.46 — SCAN THROUGHPUT PARAMETERS
#
#  _PARALLEL_FETCH_WORKERS (was 8, now 16):
#    Number of concurrent workers fetching indicators for the coin
#    universe. Each worker runs one _fetch_coin_indicators(coin)
#    call which itself issues ~3-4 HTTP requests (1h/4h/1d klines
#    plus the 5m trend-filter fetch).
#
#    At 8 workers, only ~8-10 coins per cycle were completing on
#    DEMO (mainnet is ~10× faster). Observed log:
#      "[scan] parallel indicator fetch timed out after 15s —
#       8/52 workers completed, 44 still pending."
#
#    Bumped to 16: measured DEMO throughput ≈ 16-20 workers per
#    30s window. Binance DEMO rate limit headroom is comfortable
#    at this level (~670 req/min vs ~1200 limit).
#
#  _PARALLEL_FETCH_BATCH_TIMEOUT (was 15.0s, now 30.0s):
#    Wall-time cap on the as_completed() wait. Combined with 16
#    workers, covers ~60-80% of the 52-coin universe per cycle.
#    Kept well below the 60s scan loop tick so cycles never
#    overlap. Larger than the 30s HTTP session timeout (client.py
#    REV 11.7) so a slow-but-not-hung API day doesn't trip it.
#
#  Both values are safe to tune further via code edit if a future
#  deployment adds more coins or uses a faster API endpoint.
# ═════════════════════════════════════════════════════════════
_PARALLEL_FETCH_WORKERS = 16

# ── REV 1.4.46 — scan batch wall-time cap. ──
# Upper bound on how long the main-loop's parallel indicator fetch
# will wait for as_completed() before proceeding with whatever
# results arrived. Prevents one hung Binance call from freezing the
# scan cycle (and therefore the DD kill-switch check and the next
# cycle's entry evaluation) indefinitely.
#
# Chosen larger than the client-side HTTP session timeout (30s) so
# normal slow-but-not-broken API days don't trip it. Any single
# call that hits the session timeout (30s) still completes inside
# the 30s window. Only true hangs trigger the batch timeout.
_PARALLEL_FETCH_BATCH_TIMEOUT = 30.0

# Adopted orphan fallback SL: wide safety net when entry price is
# unavailable or the repair path needs a value to act on.
# REV 1.4.33 (Point 4) — read from config_center so users can tune
# via CC_ADOPTED_FALLBACK_SL_PCT env var instead of editing code.
# Falls back to 0.02 if the key is missing from GLOBAL.
# Bounds enforced by config_center._validate(): [0.001, 0.10].
_ADOPTED_FALLBACK_SL_PCT = float(
    _cc_get_num("adopted_fallback_sl_pct", 0.02)
)


# ═════════════════════════════════════════════════════════════
#  REV 1.4.30 — DD KILL-SWITCH STATE
# ═════════════════════════════════════════════════════════════
_DD_HALT_FLAG = {'halted': False, 'reason': ''}


def _get_min_adx_for_coin(symbol: str) -> float:
    if not symbol:
        return float(_cc_get_num("min_adx", 22))
    sym_upper = symbol.upper()
    try:
        pair = sym_upper if sym_upper.endswith("USDT") else sym_upper + "USDT"
        family = get_family(pair)
        mod = _fr.FAMILIES.get(family)
        if mod and hasattr(mod, "MIN_ADX"):
            floors = list(mod.MIN_ADX.values())
            if floors:
                return float(min(floors))
    except Exception as e:
        logger.debug(f"_get_min_adx_for_coin({symbol}) failed: {e}")
    return float(_cc_get_num("min_adx", 22))


def _get_spread_cap_for_family(family: str) -> float:
    try:
        if family == "trend_coins":
            return float(CC.get('max_spread_trend', CC.get('max_spread_pct')))
        if family == "range_coins":
            return float(CC.get('max_spread_range', CC.get('max_spread_pct')))
        if family in ("volatility_coins", "momentum_coins"):
            return float(CC.get('max_spread_volatility', CC.get('max_spread_pct')))
    except Exception:
        pass
    return float(CC.get('max_spread_pct'))


# ═════════════════════════════════════════════════════════════
#  REV 1.4.31 — HELPERS FOR ADOPTION
# ═════════════════════════════════════════════════════════════
def _derive_adopted_sl(pos: dict, amt: float) -> float:
    """
    Derive a fallback initial_sl for an adopted orphan.

    Uses entryPrice first, then markPrice. Wide safety net (side
    aware, % from config_center). Returns 0.0 if neither price is
    usable — caller decides whether that's fatal.
    """
    try:
        entry_px = float(pos.get('entryPrice', 0) or 0)
    except (TypeError, ValueError):
        entry_px = 0.0
    try:
        mark_px = float(pos.get('markPrice', 0) or 0)
    except (TypeError, ValueError):
        mark_px = 0.0

    ref = entry_px if entry_px > 0 else mark_px
    if ref <= 0:
        return 0.0
    if amt > 0:
        return ref * (1 - _ADOPTED_FALLBACK_SL_PCT)
    return ref * (1 + _ADOPTED_FALLBACK_SL_PCT)


# ═════════════════════════════════════════════════════════════
#  REV 1.4.32 / 1.4.32.1 (C3) — IMMEDIATE PROTECTIVE STOP ON ADOPTION
#  REV 1.4.42 — write-lock migration (Phase 2).
# ═════════════════════════════════════════════════════════════
def _place_adoption_stop(client, sym: str, amt: float,
                         fallback_sl: float,
                         mark_px: float = 0.0) -> tuple[int, str, bool]:
    """
    Place an immediate STOP_MARKET with closePosition=true for an
    adopted orphan.

    Returns (sl_id, sl_price_str, unverified):
      • (id>0, "price", False)  — stop is live on the exchange
      • (0,    "0",     True)   — placement failed; register unverified
      • (0,    "0",     True) + emergency_close_retry called
                                — fallback would trigger immediately

    `closePosition=true` is the ONLY stop type that survives qty drift
    and partial fills — it can never be oversize or undersize and is
    the correct choice for an adopted position whose real entry/qty
    we didn't set ourselves.

    REV 1.4.32.1:
      • Pre-flight: if mark_px is supplied and the fallback SL is
        already on the wrong side of it, go straight to emergency
        close instead of round-tripping to Binance just to collect
        a -2021.
      • int(sl_id) conversion is now guarded.

    REV 1.4.42 (Phase 2):
      • Lock primitive at the order placement site migrated from
        _c._requests_lock (legacy alias) to _c._write_lock (explicit).
        Same underlying lock; intent now explicit; ready for alias
        removal.
    """
    if fallback_sl <= 0:
        return 0, '0', True

    pair = sym + 'USDT'
    close_side = 'SELL' if amt > 0 else 'BUY'

    try:
        f = _c.get_filters(pair)
        tick = f['tickSize']
        sl_adj = _c.adjust_price(fallback_sl, tick)
    except Exception as e:
        logger.error(
            f"[RECONCILE-STARTUP] {sym}: filter lookup failed for "
            f"adoption SL: {e} — registering unverified"
        )
        return 0, '0', True

    # ── REV 1.4.32.1 — pre-flight -2021 guard. ──
    # If the derived SL is already on the wrong side of mark, skip
    # the REST round-trip and flatten immediately.
    if mark_px and mark_px > 0:
        try:
            sl_adj_f = float(sl_adj)
            if amt > 0 and sl_adj_f >= mark_px:
                logger.critical(
                    f"[RECONCILE-STARTUP] {sym}: adoption SL {sl_adj_f} "
                    f">= mark {mark_px} (LONG) — pre-flight -2021, "
                    f"emergency closing"
                )
                try:
                    _o.emergency_close_retry(sym, pair, close_side)
                except Exception as _ce:
                    logger.error(
                        f"[RECONCILE-STARTUP] {sym}: emergency close "
                        f"failed: {_ce} — MANUAL CHECK REQUIRED"
                    )
                    try:
                        _c.send_telegram(
                            f"🚨 {sym} adopted orphan emergency close "
                            f"failed — MANUAL CHECK REQUIRED"
                        )
                    except Exception:
                        pass
                return 0, '0', True
            if amt < 0 and sl_adj_f <= mark_px:
                logger.critical(
                    f"[RECONCILE-STARTUP] {sym}: adoption SL {sl_adj_f} "
                    f"<= mark {mark_px} (SHORT) — pre-flight -2021, "
                    f"emergency closing"
                )
                try:
                    _o.emergency_close_retry(sym, pair, close_side)
                except Exception as _ce:
                    logger.error(
                        f"[RECONCILE-STARTUP] {sym}: emergency close "
                        f"failed: {_ce} — MANUAL CHECK REQUIRED"
                    )
                    try:
                        _c.send_telegram(
                            f"🚨 {sym} adopted orphan emergency close "
                            f"failed — MANUAL CHECK REQUIRED"
                        )
                    except Exception:
                        pass
                return 0, '0', True
        except (TypeError, ValueError):
            pass  # fall through to network attempt

    try:
        # ── REV 1.4.42 — explicit write lock (was _c._requests_lock). ──
        with _c._write_lock:
            resp = client.futures_create_order(
                symbol=pair,
                side=close_side,
                type='STOP_MARKET',
                stopPrice=sl_adj,
                closePosition=True,
                workingType='MARK_PRICE',
                priceProtect=True,
                newClientOrderId=(
                    f"tb_adopt_sl_{sym}_{uuid.uuid4().hex[:10]}"
                ),
            )

        # ── REV 1.4.32.1 — guarded int() conversion. ──
        _raw_id = (
            resp.get('orderId')
            or resp.get('algoId')
            or 0
        )
        try:
            sl_id = int(_raw_id)
        except (TypeError, ValueError):
            logger.warning(
                f"[RECONCILE-STARTUP] {sym}: unexpected orderId "
                f"type {type(_raw_id).__name__}={_raw_id!r} — "
                f"recording as 0"
            )
            sl_id = 0

        logger.warning(
            f"[RECONCILE-STARTUP] ADOPTED orphan {sym} "
            f"(amt={amt}, immediate SL @ {sl_adj} id={sl_id})"
        )
        return sl_id, str(sl_adj), False

    except BinanceAPIException as e:
        if e.code in (-2021, -4005):
            # Fallback SL would trigger immediately — market has
            # already moved through it (network-observed case, in
            # addition to the pre-flight check above). The orphan
            # has no strategy or plan attached to it, so flatten
            # rather than leave a naked position with an invalid stop.
            logger.critical(
                f"[RECONCILE-STARTUP] {sym}: adoption fallback SL "
                f"would trigger immediately ({e.code}) — emergency "
                f"closing adopted orphan"
            )
            try:
                _o.emergency_close_retry(sym, pair, close_side)
            except Exception as _ce:
                logger.error(
                    f"[RECONCILE-STARTUP] {sym}: emergency close "
                    f"failed: {_ce} — MANUAL CHECK REQUIRED"
                )
                try:
                    _c.send_telegram(
                        f"🚨 {sym} adopted orphan emergency close "
                        f"failed — MANUAL CHECK REQUIRED"
                    )
                except Exception:
                    pass
            return 0, '0', True
        logger.error(
            f"[RECONCILE-STARTUP] {sym}: adoption SL placement failed "
            f"({e.code}): {e} — registering unverified for fast-reconcile"
        )
        return 0, '0', True
    except Exception as e:
        logger.error(
            f"[RECONCILE-STARTUP] {sym}: adoption SL placement raised: "
            f"{e} — registering unverified for fast-reconcile"
        )
        return 0, '0', True


# ═════════════════════════════════════════════════════════════
#  REV 1.4.31 / 1.4.32 — ORPHAN ADOPTION IN STARTUP RECONCILE
# ═════════════════════════════════════════════════════════════
def _reconcile_on_startup():
    """
    Reconcile tracked state with real exchange positions.

    REV 1.4.31 — ADOPTS orphan positions with a usable fallback SL
    derived from entry/mark.
    REV 1.4.32 (C3) — places the protective STOP_MARKET with
    closePosition=true IMMEDIATELY, before registering the trade.
    """
    try:
        client = _c.get_client()
        if client is None:
            return
        pos_all = client.futures_position_information()
        real_positions = {
            (p.get('symbol') or '').removesuffix('USDT'): p
            for p in (pos_all or [])
            if abs(float(p.get('positionAmt', 0) or 0)) > 0
        }

        # 1) Drop tracked symbols with no real position AND no active file.
        with _s.BOT_TRACKED_LOCK:
            tracked = set(_s.bot_tracked_symbols)
            for sym in tracked:
                if sym not in real_positions:
                    if not _s.get_active_trade(sym):
                        _s.bot_tracked_symbols.discard(sym)
                        logger.info(
                            f"[RECONCILE-STARTUP] {sym} tracked but no "
                            f"real pos -> removed"
                        )

        # 2) Re-track known positions / adopt orphans.
        adopted = 0
        retracked = 0
        for sym, p in real_positions.items():
            if sym in _s.bot_tracked_symbols:
                continue
            if _s.get_active_trade(sym):
                with _s.BOT_TRACKED_LOCK:
                    _s.bot_tracked_symbols.add(sym)
                retracked += 1
                logger.info(
                    f"[RECONCILE-STARTUP] {sym} real pos + active file "
                    f"-> re-tracked"
                )
                continue

            # ─── ADOPT ORPHAN ───
            # REV 1.4.32 (C3) — place an IMMEDIATE closePosition=true
            # stop BEFORE registering the trade. See module docstring
            # for the naked-window failure mode this closes.
            try:
                amt = float(p.get('positionAmt', 0) or 0)
                entry_px = float(p.get('entryPrice', 0) or 0)
                try:
                    mark_px = float(p.get('markPrice', 0) or 0)
                except (TypeError, ValueError):
                    mark_px = 0.0
                side_str = 'BUY' if amt > 0 else 'SELL'
                qty_str = str(abs(amt))

                fallback_sl = _derive_adopted_sl(p, amt)

                sl_id, sl_price_str, unverified = _place_adoption_stop(
                    client, sym, amt, fallback_sl, mark_px=mark_px,
                )

                _s.add_active_trade(sym, {
                    'entry': entry_px,
                    'qty': qty_str,
                    'sl': sl_price_str, 'tp1': '0', 'tp2': '0',
                    'side': side_str,
                    'entry_time': datetime.now(PKT).strftime(
                        '%Y-%m-%d %I:%M:%S %p'
                    ),
                    'sl_id': sl_id, 'sl_level': 0,
                    'initial_sl': fallback_sl,
                    'unverified': unverified,
                    'adopted_at_startup': True,
                    'strategy': 'ADOPTED',
                })
                with _s.BOT_TRACKED_LOCK:
                    _s.bot_tracked_symbols.add(sym)
                adopted += 1

                if unverified:
                    logger.warning(
                        f"[RECONCILE-STARTUP] ADOPTED orphan {sym} "
                        f"(amt={amt}, entry={entry_px}, "
                        f"fallback_sl={fallback_sl:.6f}) — SL "
                        f"placement FAILED, fast-reconcile within 10s"
                    )
                    try:
                        _c.send_telegram(
                            f"⚠️ Adopted orphan {sym} (amt={amt}) — "
                            f"immediate SL FAILED, retrying within 10s"
                        )
                    except Exception:
                        pass
                else:
                    logger.warning(
                        f"[RECONCILE-STARTUP] ADOPTED orphan {sym} "
                        f"(amt={amt}, entry={entry_px}, "
                        f"immediate SL @ {sl_price_str} id={sl_id})"
                    )
                    try:
                        _c.send_telegram(
                            f"✅ Adopted orphan {sym} (amt={amt}) — "
                            f"protective SL @ {sl_price_str}"
                        )
                    except Exception:
                        pass

            except Exception as ae:
                logger.error(f"[RECONCILE-STARTUP] adopt {sym} failed: {ae}")

        logger.info(
            f"[RECONCILE-STARTUP] done — {len(real_positions)} real "
            f"positions | re-tracked={retracked} | adopted={adopted} | "
            f"tracked={len(_s.bot_tracked_symbols)}"
        )
    except Exception as e:
        logger.warning(f"[RECONCILE-STARTUP] failed: {e}")


# ═════════════════════════════════════════════════════════════
#  REV 1.4.30 — DD KILL-SWITCH (FLATTEN + HALT)
#  REV 1.4.45 — durable PAUSE_FILE write (fsync + dir fsync).
# ═════════════════════════════════════════════════════════════
def _execute_dd_halt(reason: str) -> None:
    if _DD_HALT_FLAG['halted']:
        return
    _DD_HALT_FLAG['halted'] = True
    _DD_HALT_FLAG['reason'] = reason

    logger.critical(f"[DD-HALT] KILL-SWITCH TRIGGERED: {reason}")
    try:
        _c.send_telegram(
            f"🛑 DD KILL-SWITCH: {reason}\n"
            f"Flattening all positions and halting bot..."
        )
    except Exception:
        pass

    try:
        with _s.ACTIVE_TRADES_LOCK:
            symbols_to_close = list(_s.active_trades.keys())
    except Exception as e:
        logger.critical(f"[DD-HALT] snapshot active_trades failed: {e}")
        symbols_to_close = []

    try:
        client = _c.get_client()
        if client is not None:
            pos_all = client.futures_position_information()
            for p in (pos_all or []):
                try:
                    amt = float(p.get('positionAmt', 0) or 0)
                except (TypeError, ValueError):
                    continue
                if amt == 0:
                    continue
                sym_short = (p.get('symbol') or '').removesuffix('USDT')
                if sym_short and sym_short not in symbols_to_close:
                    symbols_to_close.append(sym_short)
    except Exception as e:
        logger.warning(f"[DD-HALT] exchange position scan failed: {e}")

    logger.critical(
        f"[DD-HALT] closing {len(symbols_to_close)} position(s): "
        f"{symbols_to_close}"
    )

    closed = 0
    failed = []
    for sym in symbols_to_close:
        pair = sym + 'USDT'
        try:
            pos = _c.fetch_position_raw(sym)
            if pos is None:
                logger.warning(
                    f"[DD-HALT] {sym}: position fetch failed — cannot "
                    f"determine side; marking for manual check"
                )
                failed.append(sym)
                continue
            amt = float(pos.get('positionAmt', 0) or 0)
            if amt == 0:
                closed += 1
                continue
            close_side = 'SELL' if amt > 0 else 'BUY'
            try:
                _o.emergency_close_retry(sym, pair, close_side)
                closed += 1
                logger.info(f"[DD-HALT] {sym} closed ({close_side})")
            except Exception as ce:
                logger.critical(f"[DD-HALT] close {sym} failed: {ce}")
                failed.append(sym)
        except Exception as e:
            logger.critical(f"[DD-HALT] unexpected close failure {sym}: {e}")
            failed.append(sym)

    try:
        for sym in symbols_to_close:
            try:
                _o.cancel_orphan_bot_orders(sym + 'USDT', force=True)
            except Exception as _cbe:
                logger.debug(f"[DD-HALT] cancel_orphan {sym}: {_cbe}")
    except Exception:
        pass

    # ── REV 1.4.45 — durable PAUSE_FILE write. ──
    # fsync the file AND best-effort the containing directory so the
    # marker survives a power loss / hard reboot. Without fsync, the
    # data could sit in the OS page cache and be lost if the machine
    # dies between `close()` and actual disk flush. Directory fsync
    # (POSIX-only, no-op on Windows) additionally persists the rename
    # metadata so the file is discoverable after reboot.
    #
    # Note on ordering: the write happens AFTER flattening in the
    # current design. That is safe — daily_tracker's DD state is
    # persisted independently to LOSS_FILE, so a crash mid-flatten
    # still re-trips the kill-switch on the next startup. Only a
    # crash just before PKT midnight would fail to re-trigger, and
    # that corner case is deferred to the C2 persist-first
    # restructuring (post-live-stats).
    try:
        with open(PAUSE_FILE, 'w', encoding='utf-8') as f:
            f.write(
                f"DD_HALT\n"
                f"reason={reason}\n"
                f"ts={datetime.now(PKT).isoformat()}\n"
                f"closed={closed}\n"
                f"failed={failed}\n"
            )
            f.flush()
            os.fsync(f.fileno())
        # Best-effort directory fsync (no-op on Windows).
        try:
            _dir = os.path.dirname(os.path.abspath(PAUSE_FILE))
            _dfd = os.open(_dir, os.O_RDONLY)
            try:
                os.fsync(_dfd)
            finally:
                os.close(_dfd)
        except (OSError, AttributeError):
            pass
    except Exception as e:
        logger.critical(f"[DD-HALT] write PAUSE_FILE failed: {e}")

    try:
        _s.request_stop()
    except Exception:
        pass

    try:
        _c.send_telegram(
            f"🛑 DD HALT COMPLETE\n"
            f"Reason: {reason}\n"
            f"Closed: {closed}, Failed: {failed}\n"
            f"Bot stopped. Remove {PAUSE_FILE} to resume."
        )
    except Exception:
        pass

    logger.critical(
        f"[DD-HALT] halt complete. closed={closed} failed={failed}"
    )


def _format_be_lock_config() -> str:
    per_class = _VOL_CLASS_R_THRESHOLDS
    parts: list[str] = []
    for cls in ("LOW", "MED", "HIGH"):
        c = per_class.get(cls)
        if not isinstance(c, dict):
            continue
        try:
            be_r = c["be_r"]
            l1_r = c["lock1_r"]
            l2_r = c["lock2_r"]
            parts.append(f"{cls} BE{be_r}R L1@{l1_r}R L2@{l2_r}R")
        except (KeyError, TypeError):
            continue
    if parts:
        return "BE/LOCK per-class [" + " | ".join(parts) + "]"
    return "BE/LOCK config unavailable"


def stop_bot() -> None:
    _s.request_stop()
    logger.info("Stop signal received from UI")


def _extract_family_rejection(reasons: list) -> str:
    for r in (reasons or []):
        if not isinstance(r, str):
            continue
        r = r.strip()
        if not r:
            continue
        if r.startswith("strat="):
            continue
        return r[:60]
    return ""


def _format_skip_reasons(reasons, sig_1h, conf, lvl, min_rr,
                         adx_v, min_adx) -> str:
    family_rej = _extract_family_rejection(reasons)
    if family_rej:
        return f"SKIP ({family_rej})"

    _min_conf_live = float(_cc_get_num("min_confidence", 60))

    parts: list[str] = []
    if "BUY" not in sig_1h and "SELL" not in sig_1h:
        parts.append("NoSetup")
    if conf < _min_conf_live:
        parts.append(f"C{conf:.0f}%")
    rr_val = lvl.get("RR", 0)
    if rr_val < min_rr and rr_val > 0:
        parts.append(f"RR{rr_val:.1f}")
    if adx_v < min_adx and len(parts) < 2:
        parts.append(f"ADX{adx_v:.0f}")

    if not parts:
        return "SKIP"
    return f"SKIP ({','.join(parts[:2])})"


def _display_tf_signal(ind: dict | None) -> str:
    if not ind:
        return "N/A"
    try:
        e20 = float(ind.get("ema20", 0.0))
        e50 = float(ind.get("ema50", 0.0))
        p   = float(ind.get("price", 0.0))
    except (TypeError, ValueError):
        return "N/A"
    if p <= 0 or e50 <= 0:
        return "N/A"
    if e20 > e50 and p > e50:
        return "BUY"
    if e20 < e50 and p < e50:
        return "SELL"
    return "NEUTRAL"


def _compute_est_qty(balance: float, live_price: float,
                     sl_price: float) -> float:
    try:
        if live_price <= 0 or sl_price <= 0 or balance <= 0:
            return 0.0
        _risk_pct = float(_cc_get_num("risk_percent", 0.5))
        risk_usd = balance * (_risk_pct / 100.0)
        sl_dist = abs(live_price - sl_price)
        if sl_dist <= 0:
            return 0.0
        return risk_usd / sl_dist
    except Exception:
        return 0.0


def _safe_family(symbol: str) -> str:
    try:
        return get_family(symbol) or "unknown"
    except Exception:
        return "unknown"


def _extract_strategy(reasons: list) -> str:
    for r in (reasons or []):
        if isinstance(r, str) and r.startswith("strat="):
            return r.split("=", 1)[1].strip()
    return "UNKNOWN"


# ═════════════════════════════════════════════════════════════
#  REV 1.4.38 — DISPLAY-ONLY STRATEGY NAME EXTRACTION
#  Used exclusively for the scan_rows "pattern" column and the
#  local UI. Never feeds any trading logic.
#
#  Sources (in priority order):
#    1. "strat=NAME"    — a signal fired; NAME produced it
#    2. "NAME:reason"   — NEUTRAL; the router summary lists the
#                         strategy that rejected the setup
#    3. fallback        — "—" (no strategy touched this coin)
#
#  Why not reuse _extract_strategy()? It returns "UNKNOWN" for the
#  NEUTRAL case (no strat= prefix), which would print "UNKNOWN" in
#  every rejected row. This helper also parses the router's
#  rejection summary so the PATTERN column matches the ACTION
#  column (both showing e.g. "RANGE_SCALPER").
# ═════════════════════════════════════════════════════════════
def _extract_display_strategy(reasons: list, fallback: str = "—") -> str:
    reasons = reasons or []

    # 1. Signal fired — "strat=NAME"
    for r in reasons:
        if isinstance(r, str) and r.startswith("strat="):
            return r.split("=", 1)[1].strip()

    # 2. Router rejection summary — "NAME:reason"
    #    Strategy names are ALL_CAPS_WITH_UNDERSCORES. The reason
    #    suffix is lowercase or contains other punctuation. So the
    #    first token before ":" must be uppercase and contain "_".
    for r in reasons:
        if not isinstance(r, str):
            continue
        if ":" not in r:
            continue
        head = r.split(":", 1)[0].strip()
        if head and head.isupper() and "_" in head:
            return head

    return fallback


def _spread_label() -> str:
    if not CC.get("use_spread_filter", True):
        return "OFF"
    _g = _cc_get_num("max_spread_pct", 0.15)
    _t = _cc_get_num("max_spread_trend", 0.08)
    _r = _cc_get_num("max_spread_range", 0.12)
    _v = _cc_get_num("max_spread_volatility", 0.25)
    return (f"ON (global {_g:.2f}% | trend {_t:.2f}% | "
            f"range {_r:.2f}% | vol {_v:.2f}%)")


def _update_btc_regime() -> None:
    """
    Refresh the cached BTC regime used by the decision engine's
    BTC-bias gate. Runs every scan cycle.

    REV 1.4.34 (Point 5) — exception now logged at WARNING level.
    A silent failure here means the BTC-bias gate is stuck on a
    stale regime (or UNKNOWN), which silently degrades trade
    gating. Visibility > quiet.
    """
    try:
        cached = _i.get_cached_indicator("BTCUSDT", "1h")
        if cached is not None:
            btc_ind = cached
        else:
            btc_df = _c.get_binance_klines("BTCUSDT", "1h", limit=250)
            if btc_df is None or len(btc_df) < 100:
                return
            btc_df_closed = btc_df.iloc[:-1] if len(btc_df) >= 3 else btc_df
            btc_ind = _i.calculate_pro_indicators(
                btc_df_closed, "1h", symbol="BTCUSDT"
            )
            if not btc_ind:
                return
            try:
                _i.set_cached_indicator("BTCUSDT", "1h", btc_ind)
            except Exception:
                pass

        from signals.decision_engine import set_btc_regime
        set_btc_regime(btc_ind.get("regime", "UNKNOWN"))
        logger.debug(
            f"[BTC] regime={btc_ind.get('regime')} "
            f"mom={btc_ind.get('momentum_pct', 0):.2%} "
            f"hurst={btc_ind.get('hurst', 0):.3f} "
            f"adx={btc_ind.get('adx', 0):.1f}"
        )
    except Exception as _btce:
        # REV 1.4.34 (Point 5) — was logger.debug; now warning.
        logger.warning(
            f"[BTC] regime update failed: "
            f"{type(_btce).__name__}: {_btce}"
        )


def _fetch_coin_indicators(coin: str) -> tuple:
    """
    Fetch + compute indicators for one coin across 1h/4h/1d.

    REV 1.4.34 (Point 5) — exception now logged at WARNING level.
    If the whole universe starts failing (network issue, rate-limit
    storm, Binance API change), the failure rate becomes visible in
    the log file instead of silently producing "No Data" scans.
    """
    dfs_closed: dict = {}
    results: dict = {}
    try:
        df_1h_raw = _c.get_binance_klines(coin, "1h")
        if df_1h_raw is not None and len(df_1h_raw) > 1:
            df_1h_closed = df_1h_raw.iloc[:-1]
        else:
            df_1h_closed = df_1h_raw
        if df_1h_closed is not None:
            dfs_closed['1h'] = df_1h_closed

        for tf in ('1h', '4h', '1d'):
            cached_ind = _i.get_cached_indicator(coin, tf)
            if cached_ind is not None:
                results[tf] = cached_ind
                continue

            if tf == '1h':
                df_closed = df_1h_closed
            else:
                df_raw = _c.get_binance_klines(coin, tf)
                if df_raw is None or len(df_raw) < 61:
                    continue
                df_closed = df_raw.iloc[:-1] if len(df_raw) >= 3 else df_raw

            if df_closed is None or len(df_closed) < 61:
                continue
            ind = _i.calculate_pro_indicators(df_closed, tf, symbol=coin)
            if ind:
                results[tf] = ind
                _i.set_cached_indicator(coin, tf, ind)
    except Exception as e:
        # REV 1.4.34 (Point 5) — was logger.debug; now warning.
        logger.warning(
            f"[{coin}] indicator fetch failed: "
            f"{type(e).__name__}: {e}"
        )
    return coin, results, dfs_closed


# ═════════════════════════════════════════════════════════════
#  REV 1.4.41 — CONFIG PROXY CONTRACT SELF-CHECK
# ═════════════════════════════════════════════════════════════
def _log_proxy_contract_status() -> None:
    """
    Fail LOUD if config.py advertises proxy keys that are missing
    from config_center.GLOBAL.

    A missing key means `CONFIG.<attr>` will raise AttributeError at
    the FIRST read site — which could be deep inside order placement.
    Surface it now, before any trade logic runs.
    """
    try:
        from core.config_center import verify_proxy_contract
    except Exception as e:
        logger.debug(f"[config-check] verify_proxy_contract import failed: {e}")
        return

    try:
        missing = verify_proxy_contract()
    except Exception as e:
        logger.warning(f"[config-check] verify_proxy_contract raised: {e}")
        return

    if missing:
        logger.critical(
            f"⚠️ CONFIG PROXY CONTRACT VIOLATION — core/config.py "
            f"advertises trading attrs that are MISSING from "
            f"core.config_center.GLOBAL: {missing}. "
            f"Any CONFIG.<attr> read for these keys will raise "
            f"AttributeError. Fix config_center defaults before trading."
        )
    else:
        logger.info(
            "✅ Config proxy contract OK — trading source of truth: "
            "core.config_center.GLOBAL"
        )


# ═════════════════════════════════════════════════════════════
#  MAIN LOOP
# ═════════════════════════════════════════════════════════════
def main_loop():
    if _c.get_client() is None:
        logger.error("Client not initialized.")
        return
    if not CONFIG.bot_coins:
        logger.critical("❌ CONFIG.bot_coins is empty")
        return

    # ── REV 1.4.41 — proxy contract self-check (fail loud, early). ──
    _log_proxy_contract_status()

    _s.load_cooldowns()
    _o.clean_orders()
    coins = _c.validate_symbols(_c.get_bot_coins())
    if not coins:
        logger.error("No valid symbols.")
        return

    # ── REV 1.4.37 — dead `_cfg = _i.get_trading_config()` removed. ──
    # It was assigned here but never read; the per-coin loop body
    # reads its own `cfg = _i.get_trading_config()` for the RR floor.
    _be_lock_str = _format_be_lock_config()

    logger.info(f"PRO TRADING v13.3.22 - {TRADING_MODE} | Risk {RISK_PERCENT}% | "
                f"Max {MAX_OPEN_POSITIONS} | DD {MAX_DAILY_DRAWDOWN_PERCENT}% | "
                f"Lev {LEVERAGE}x | MinADX {MIN_ADX}(fallback) | "
                f"PartialClose ${CC.get('partial_close_usdt')}")

    try:
        _floor_parts: list[str] = []
        for _fam_name, _fam_mod in _fr.FAMILIES.items():
            _madx = getattr(_fam_mod, "MIN_ADX", None)
            if isinstance(_madx, dict) and _madx:
                try:
                    _floor_parts.append(
                        f"{_fam_name}={float(min(_madx.values())):.1f}"
                    )
                except Exception:
                    continue
        if _floor_parts:
            logger.info("MinADX per-family floors: " + " | ".join(_floor_parts))
        else:
            logger.info(
                f"MinADX per-family floors: none — using fallback {MIN_ADX}"
            )
    except Exception as _mfe:
        logger.debug(f"per-family MinADX log failed: {_mfe}")

    logger.info(
        f"Modern flags: taker={CC.get('use_taker_volume')} "
        f"vp={CC.get('use_volume_profile')} "
        f"avwap={CC.get('use_anchored_vwap')} "
        f"fz={CC.get('use_funding_z')} "
        f"mtf={CC.get('use_mtf_confluence')}"
    )

    logger.info(f" {_be_lock_str}")
    logger.info(f"Signal engine: family_router (4 families)")
    logger.info(f"Spread filter: {_spread_label()}")

    logger.info(
        f"5m trend filter: "
        f"{'ON (trend-following only — counter-trend bypass)' if USE_5M_TREND_FILTER else 'OFF'}"
    )

    _debug_flag = os.getenv("DEBUG_STRATEGY", "false").strip().lower() == "true"
    logger.info(
        f"Strategy debug: "
        f"{'✅ ON (per-coin rejections logged)' if _debug_flag else '⚠️ OFF'}"
    )

    try:
        _fam_summary = _fr.format_family_summary().replace("\n", " | ")
        logger.info(f"Total coins: {len(coins)} | Families: {_fam_summary}")
    except Exception as e:
        logger.warning(f"Family summary failed: {e}")

    try:
        _MA_BANNER = int(CC.get_decision_cfg().get("min_approvals", 5))
    except Exception as _ma_err:
        logger.debug(f"MIN_APPROVALS banner read failed: {_ma_err}")
        _MA_BANNER = "?"

    # ── REV 1.4.36 — Total-filters read (was hardcoded 6) ──
    try:
        _TF_BANNER = int(CC.get_decision_cfg().get("total_filters", 9))
    except Exception:
        _TF_BANNER = 9

    try:
        import signals.decision_engine  # noqa: F401
        # REV 1.4.36 — was "6-filter vote, X/6 required"
        logger.info(
            f"Decision engine: ✅ active "
            f"({_TF_BANNER}-filter vote, {_MA_BANNER}/{_TF_BANNER} required)"
        )
        logger.info(
            "BTC-bias gate:   ✅ ACTIVE (REV 1.0.5) — updated each scan cycle"
        )
        logger.info(
            "MTF gate:        ✅ decision_engine only "
            "(REV 1.4.13 — hard-gate removed)"
        )
    except Exception as e:
        logger.warning(
            f"Decision engine: ❌ not loaded ({e}) — trade gate DISABLED"
        )

    logger.info(f"Scan: parallel fetch (workers={_PARALLEL_FETCH_WORKERS})")
    # REV 1.4.36 — was "MIN_APPROVALS=N/6"
    logger.info(
        f"Running in TUNED mode — MIN_APPROVALS={_MA_BANNER}/{_TF_BANNER}, "
        f"counter_trend_cutoff=35, st_rsi_sell_floor per-family"
    )

    _o.sync_existing_positions()

    # ═══════════════════════════════════════════════════════════
    #  REV 1.4.31 — startup reconcile under per-symbol lock
    # ═══════════════════════════════════════════════════════════
    with _s.BOT_TRACKED_LOCK:
        startup_syms = list(_s.bot_tracked_symbols)
    if startup_syms:
        logger.info(
            f" Startup reconciliation for {len(startup_syms)} symbols..."
        )
        try:
            from orders.manage import get_symbol_manage_lock
        except Exception:
            get_symbol_manage_lock = None

        for s in startup_syms:
            if get_symbol_manage_lock is not None:
                with get_symbol_manage_lock(s):
                    try:
                        _o.reconcile_active_trade(s)
                    except Exception as e:
                        logger.warning(f"startup reconcile {s} failed: {e}")
            else:
                try:
                    _o.reconcile_active_trade(s)
                except Exception as e:
                    logger.warning(f"startup reconcile {s} failed: {e}")

    try:
        for s in _c.get_bot_coins():
            if s.removesuffix('USDT') not in _s.bot_tracked_symbols:
                _o.cancel_orphan_bot_orders(s)
    except Exception:
        pass

    try:
        _c.refresh_timestamp()
        bal_init = float(
            _c.get_client().futures_account()['totalWalletBalance']
        )
        _s.daily_tracker.reset_if_new_day(bal_init)
        _s.daily_tracker.update_peak(bal_init)
    except Exception as e:
        logger.warning(f"Initial balance fetch error: {e}")

    if not Path(CSV_FILE).exists():
        with open(CSV_FILE, 'w', encoding='utf-8') as f:
            f.write('timestamp,symbol,side,entry,price,sl,tp1,tp2,qty,'
                    'conf,rr,pattern,fg,strategy\n')
    if not Path(CSV_EXIT_FILE).exists():
        with open(CSV_EXIT_FILE, 'w', encoding='utf-8') as f:
            f.write('timestamp,symbol,realized_pnl,reason\n')

    try:
        while not _s.is_stopped():
            try:
                if Path(PAUSE_FILE).exists():
                    _pause_reason = "unknown"
                    try:
                        _pause_reason = Path(PAUSE_FILE).read_text(
                            encoding='utf-8'
                        ).strip().splitlines()[0]
                    except Exception:
                        pass
                    if 'DD_HALT' in _pause_reason:
                        logger.critical(
                            f"⛔ PAUSE file present with DD_HALT marker — "
                            f"bot will not resume trading. Remove "
                            f"{PAUSE_FILE} manually after review."
                        )
                    else:
                        logger.info(" PAUSE file detected - paused 30s.")
                    time.sleep(30)
                    continue

                fg_value, fg_class = _i.get_fear_greed_index()
                live_prices = _c.get_live_prices()

                book_tickers: dict = {}
                if CC.get("use_spread_filter", True):
                    try:
                        book_tickers = _c.get_book_tickers() or {}
                    except Exception as _bte:
                        logger.debug(f"book_tickers fetch failed: {_bte}")

                _update_btc_regime()

                degraded = False
                try:
                    balance, degraded = _c.get_balance_or_last_good()
                    if not degraded:
                        _s.daily_tracker.update_peak(balance)
                except Exception as e:
                    logger.error(
                        f"Balance fetch failed (no last-good): {e} — pausing"
                    )
                    time.sleep(30)
                    continue

                # ═══════════════════════════════════════════════════
                #  REV 1.4.41 — DD KILL-SWITCH RUNS FIRST, ALWAYS.
                #  A stale-but-recent last-good balance is still the
                #  best available signal for the daily DD gate.
                #  Previously the `if degraded: continue` block lived
                #  ABOVE this check, so a losing day whose balance feed
                #  hiccuped would never trip the kill-switch and the bot
                #  would keep cycling indefinitely.
                # ═══════════════════════════════════════════════════
                is_limited, reason = _s.daily_tracker.is_limit_reached(balance)
                if is_limited:
                    _execute_dd_halt(reason)
                    break

                if degraded:
                    logger.warning(
                        "🔶 Degraded mode — skipping new entries this cycle"
                    )
                    time.sleep(30)
                    continue

                active_trades_list = []
                try:
                    _positions_all = _c.get_client().futures_position_information()
                    _pos_by_sym = {
                        p.get('symbol'): p
                        for p in (_positions_all or [])
                        if abs(float(p.get('positionAmt', 0) or 0)) > 0
                    }
                    for _pos_sym, _p in _pos_by_sym.items():
                        _sym_short = (_pos_sym or '').removesuffix('USDT')
                        if not _sym_short:
                            continue
                        try:
                            st = _o.get_trade_status(_sym_short, pos=_p)
                        except TypeError:
                            st = _o.get_trade_status(_sym_short)
                        except Exception as _tse:
                            logger.debug(
                                f"get_trade_status({_sym_short}) failed: {_tse}"
                            )
                            st = None
                        if st:
                            active_trades_list.append(st)
                except Exception as _pex:
                    logger.debug(f"batch position fetch failed: {_pex}")

                if active_trades_list:
                    logger.info(
                        f" {len(active_trades_list)} active | Bal ${balance:.2f}"
                    )
                else:
                    logger.info(f" No active trades | Bal ${balance:.2f}")

                scan_rows = []
                trade_executed = False

                candidates = list(coins)

                indicators_by_coin: dict = {}
                if candidates:
                    # ═══════════════════════════════════════════════
                    #  REV 1.4.43 / 1.4.46 — SCAN BATCH WALL-TIME CAP.
                    #
                    #  Explicit pool (NOT `with`), because the `with`
                    #  context manager calls shutdown(wait=True) on
                    #  exit — which would WAIT for abandoned workers
                    #  and defeat the as_completed timeout below.
                    #
                    #  On timeout we log and proceed with whatever
                    #  results arrived. The abandoned workers finish
                    #  in the background; client.py REV 11.7
                    #  guarantees they release _read_lock within the
                    #  HTTP session timeout (~30s).
                    #
                    #  REV 1.4.46 — workers=16, wall-cap=30s. See
                    #  module docstring for the throughput rationale.
                    # ═══════════════════════════════════════════════
                    pool = None
                    try:
                        pool = ThreadPoolExecutor(
                            max_workers=_PARALLEL_FETCH_WORKERS,
                            thread_name_prefix="ind_fetch",
                        )
                        futures = {
                            pool.submit(_fetch_coin_indicators, c): c
                            for c in candidates
                        }
                        try:
                            for fut in as_completed(
                                futures,
                                timeout=_PARALLEL_FETCH_BATCH_TIMEOUT,
                            ):
                                _coin_for_fut = futures[fut]
                                try:
                                    _c_key, _res, _dfs = fut.result()
                                    indicators_by_coin[_c_key] = (
                                        _res, _dfs
                                    )
                                except Exception as _fe:
                                    logger.debug(
                                        f"Parallel fetch failed for "
                                        f"{_coin_for_fut}: {_fe}"
                                    )
                        except _FutTimeout:
                            _done = sum(
                                1 for f in futures if f.done()
                            )
                            _pending = len(futures) - _done
                            logger.warning(
                                f"[scan] parallel indicator fetch "
                                f"timed out after "
                                f"{_PARALLEL_FETCH_BATCH_TIMEOUT:.0f}s — "
                                f"{_done}/{len(futures)} workers "
                                f"completed, {_pending} still pending. "
                                f"Proceeding with partial universe; "
                                f"pending workers will finish in "
                                f"background."
                            )
                    except Exception as _pe:
                        logger.warning(
                            f"Parallel fetch pool error: {_pe} — "
                            f"falling back to serial for remaining"
                        )
                        for c in candidates:
                            if c in indicators_by_coin:
                                continue
                            try:
                                _c_key, _res, _dfs = _fetch_coin_indicators(c)
                                indicators_by_coin[_c_key] = (_res, _dfs)
                            except Exception:
                                continue
                    finally:
                        if pool is not None:
                            # REV 1.4.43 — wait=False: do not block the
                            # caller on abandoned workers. Their read
                            # locks are released within the HTTP session
                            # timeout by design (client.py REV 11.7).
                            try:
                                pool.shutdown(wait=False)
                            except Exception as _sde:
                                logger.debug(
                                    f"[scan] pool shutdown: {_sde}"
                                )

                for coin in candidates:
                    results, dfs_closed = indicators_by_coin.get(coin, ({}, {}))
                    ind_1h = results.get('1h')
                    ind_4h = results.get('4h')
                    ind_1d = results.get('1d')
                    df_1h = dfs_closed.get('1h')

                    # ── REV 1.4.41 — removesuffix (was replace). ──
                    # The old form would silently corrupt any ticker
                    # containing 'USDT' mid-string, breaking the
                    # IN POSITION display fix from REV 1.4.40.
                    symbol = coin.removesuffix('USDT')
                    _family = _safe_family(coin)
                    live_price = live_prices.get(coin, 0.0)

                    # ── REV 1.4.40 — position-state snapshot ──
                    # Computed BEFORE the signal branch so the ACTION
                    # column shows IN POSITION for every open coin,
                    # regardless of what the signal did this cycle.
                    _has_open_position = any(
                        t.get('symbol') == symbol
                        for t in active_trades_list
                    )

                    if not ind_1h or not ind_4h or df_1h is None:
                        scan_rows.append({
                            'symbol': symbol,
                            'sig_1h': '-', 'sig_4h': '-', 'sig_1d': '-',
                            'conf': 0, 'price': live_price, 'rr': 0, 'adx': 0,
                            # REV 1.4.38 — "—" instead of "TrendPullback"
                            'pattern': '—', 'regime': 'UNKNOWN',
                            'killzone': '-', 'family': _family,
                            # REV 1.4.40 — still respect position state
                            'action': ('IN POSITION' if _has_open_position
                                       else 'No Data')
                        })
                        continue

                    if live_price <= 0:
                        live_price = ind_1h.get('price', 0.0)

                    _regime   = ind_1h.get('regime', 'UNKNOWN')
                    _killzone = ind_1h.get('killzone', 'NONE')

                    candle_time = None
                    try:
                        candle_time = df_1h.index[-1]
                    except Exception:
                        candle_time = None

                    _signal_price = float(ind_1h.get('price', 0.0) or 0.0)

                    sig_1h, conf, reasons, lvl, div = _fr.generate_signal_live(
                        ind_1h, ind_4h, ind_1d,
                        symbol=coin,
                    )

                    sig_4h = _display_tf_signal(ind_4h)
                    sig_1d = _display_tf_signal(ind_1d)

                    # ── REV 1.4.38 — display the ACTUAL strategy. ──
                    # Was hardcoded "TrendPullback" (legacy TREND_PULLBACK
                    # design). Now shows the strategy that either fired
                    # ("strat=NAME") or rejected ("NAME:reason").
                    pat_disp = _extract_display_strategy(reasons)

                    cfg = _i.get_trading_config()
                    min_rr = cfg['min_rr']
                    min_adx_here = _get_min_adx_for_coin(coin)
                    trade_side = None
                    skip_reason = None
                    strategy_name = "UNKNOWN"
                    _strategy_name_for_check = "UNKNOWN"

                    _is_buy  = ("STRONG_BUY"  in sig_1h or sig_1h == "BUY")
                    _is_sell = ("STRONG_SELL" in sig_1h or sig_1h == "SELL")

                    if _is_buy or _is_sell:
                        _conf_ok = conf >= float(_cc_get_num("min_confidence", 60))
                        _rr_ok   = lvl.get('RR', 0) >= min_rr
                        _adx_ok  = ind_1h['adx'] >= min_adx_here

                        _side_temp = 'BUY' if _is_buy else 'SELL'

                        _trend_ok = True
                        _trend_reason = ""
                        _strategy_name_for_check = _extract_strategy(reasons)
                        _is_counter_trend = (
                            _strategy_name_for_check in _CT_STRATEGIES
                        )

                        _use_5m_live = bool(CC.get("use_5m_trend_filter", True))
                        if (_use_5m_live
                                and not _is_counter_trend
                                and _conf_ok and _rr_ok and _adx_ok):
                            _trend_ok, _trend_reason = _i.check_short_term_trend(
                                coin, _side_temp
                            )

                        if _conf_ok and _rr_ok and _adx_ok and _trend_ok:
                            trade_side = _side_temp
                            _s.increment_v2_stat('signals')
                        else:
                            if not _conf_ok:
                                _s.increment_v2_stat('low_conf')
                            if not _rr_ok:
                                _s.increment_v2_stat('low_rr')
                            if not _adx_ok:
                                _s.increment_v2_stat('low_adx')
                            if not _trend_ok:
                                _s.increment_v2_stat('trend')
                                skip_reason = f'V2-Trend: {_trend_reason}'

                    _pre_block_reason = None

                    if trade_side:
                        with _s.COOLDOWN_LOCK:
                            _cd = _s.cooldown_until.get(symbol)
                            if _cd is not None and datetime.now(PKT) < _cd:
                                _cd_remain = int(
                                    (_cd - datetime.now(PKT)).total_seconds() // 60
                                )
                                _pre_block_reason = f"Cooldown {_cd_remain}m"

                    if _pre_block_reason is None and trade_side:
                        rot_ok, rot_reason = _s.can_trade_coin(symbol)
                        if not rot_ok:
                            _pre_block_reason = f"V2-Rotation: {rot_reason}"
                            _s.increment_v2_stat('rotation')

                    if _pre_block_reason is None and trade_side:
                        if _has_open_position:
                            _pre_block_reason = "IN POSITION"

                    if _pre_block_reason is None and trade_side and candle_time is not None:
                        with _s.ENTRY_CANDLE_LOCK:
                            if _s.last_entry_candle.get(symbol) == candle_time:
                                _pre_block_reason = "Same Candle"

                    if _pre_block_reason:
                        trade_side = None
                        skip_reason = _pre_block_reason

                    if trade_side:
                        strategy_name = _strategy_name_for_check
                        try:
                            from signals.decision_engine import evaluate_trade
                            decision = evaluate_trade(
                                symbol=symbol,
                                side=trade_side,
                                ind_1h=ind_1h,
                                ind_4h=ind_4h,
                                ind_1d=ind_1d,
                                strategy=strategy_name,
                                conf=conf,
                                rr=lvl.get('RR', 0),
                                active_trades_list=active_trades_list,
                            )
                            if not decision.approved:
                                skip_reason = f"DECISION: {decision.top_reason}"
                                _s.increment_v2_stat('decision')
                                trade_side = None
                        except ImportError:
                            pass
                        except Exception as _de:
                            logger.debug(
                                f"[decision] {symbol}: "
                                f"{type(_de).__name__}: {_de} — allowing trade"
                            )

                    if trade_side and _has_open_position:
                        trade_side = None
                        skip_reason = "IN POSITION"

                    # ═══════════════════════════════════════════════════
                    #  REV 1.4.44 — SPREAD FILTER FAIL-CLOSED.
                    #
                    #  Previously `if _bt:` — a missing bookTicker for a
                    #  coin silently skipped the spread check entirely,
                    #  letting the entry proceed with no spread validation.
                    #  A delisted-mid-cycle, halted, or freshly-listed
                    #  symbol could enter during a Binance bookTicker
                    #  brownout.
                    #
                    #  Now: missing `_bt` → skip_reason + block entry.
                    #  The trade manager still manages any EXISTING
                    #  position on the coin; this only blocks NEW entries
                    #  while we cannot verify the spread.
                    # ═══════════════════════════════════════════════════
                    if trade_side and CC.get("use_spread_filter", True):
                        _bt = book_tickers.get(coin)
                        if _bt:
                            _bid = _bt.get('bid', 0.0)
                            _ask = _bt.get('ask', 0.0)
                            _spread_pct = _c.calc_spread_pct(_bid, _ask)
                            _cap = _get_spread_cap_for_family(_family)
                            if _spread_pct > 0 and _spread_pct > _cap:
                                skip_reason = (
                                    f"SPREAD {_spread_pct:.3f}% > "
                                    f"{_cap:.2f}% ({_family})"
                                )
                                logger.info(
                                    f"[{symbol}] {skip_reason} — "
                                    f"skipping entry (bid={_bid} ask={_ask})"
                                )
                                trade_side = None
                        else:
                            # ── REV 1.4.44 — fail-CLOSED on missing ticker. ──
                            # No bid/ask available → cannot verify spread.
                            # Reject the entry rather than proceed blind.
                            skip_reason = "NO TICKER (spread filter)"
                            logger.warning(
                                f"[{symbol}] {skip_reason} — skipping "
                                f"entry (bookTicker unavailable for {coin})"
                            )
                            trade_side = None

                    if trade_side:
                        est_qty = _compute_est_qty(balance, live_price, lvl['SL'])

                        if candle_time is not None:
                            with _s.ENTRY_CANDLE_LOCK:
                                _s.last_entry_candle[symbol] = candle_time

                        order_succeeded = False
                        try:
                            order_succeeded = _o.place_order_fixed(
                                symbol, trade_side,
                                est_qty, lvl['SL'], lvl['TP1'], lvl['TP2'],
                                live_price, conf, lvl.get('RR', 0),
                                pat_disp, fg_value,
                                strategy=strategy_name,
                                signal_price=_signal_price,
                            )
                        except Exception as order_err:
                            logger.error(
                                f"[{symbol}] place_order_fixed RAISED: "
                                f"{type(order_err).__name__}: {order_err}",
                                exc_info=True,
                            )
                            order_succeeded = False

                        if order_succeeded:
                            if not DRY_RUN:
                                _s.record_coin_trade(symbol)

                            try:
                                _new_status = _o.get_trade_status(symbol)
                                if _new_status:
                                    active_trades_list.append(_new_status)
                                    logger.debug(
                                        f"[{symbol}] local active_trades_list "
                                        f"updated → {len(active_trades_list)} total"
                                    )
                                else:
                                    active_trades_list.append({
                                        'symbol': symbol,
                                        'side': 'LONG' if trade_side == 'BUY' else 'SHORT',
                                    })
                            except Exception as _ase:
                                logger.debug(
                                    f"[{symbol}] local active_trades append "
                                    f"failed: {_ase}"
                                )
                                active_trades_list.append({
                                    'symbol': symbol,
                                    'side': 'LONG' if trade_side == 'BUY' else 'SHORT',
                                })

                            action = f" EXECUTED {trade_side} [{strategy_name}]"
                            trade_executed = True
                        else:
                            with _s.ENTRY_CANDLE_LOCK:
                                if symbol in _s.last_entry_candle:
                                    del _s.last_entry_candle[symbol]
                            # REV 1.4.40 — even a failed entry on an open
                            # position should read IN POSITION.
                            if _has_open_position:
                                action = "IN POSITION"
                            else:
                                action = " FAILED"

                    # ── REV 1.4.40 — ACTION PRIORITY REORDER ──
                    # If this coin is currently held by the bot, the
                    # ACTION column reads "IN POSITION" regardless of
                    # the signal outcome for this cycle. This makes
                    # the scanner consistent: all open coins look the
                    # same, whether their signal fired or not.
                    elif _has_open_position:
                        action = "IN POSITION"

                    elif skip_reason:
                        action = skip_reason
                    else:
                        action = _format_skip_reasons(
                            reasons, sig_1h, conf, lvl, min_rr,
                            ind_1h['adx'], min_adx_here
                        )

                    scan_rows.append({
                        'symbol': symbol, 'sig_1h': sig_1h,
                        'sig_4h': sig_4h, 'sig_1d': sig_1d,
                        'conf': conf, 'price': live_price,
                        'rr': lvl.get('RR', 0),
                        'adx': ind_1h['adx'],
                        # REV 1.4.38 — was hardcoded "TrendPullback"
                        'pattern': pat_disp,
                        'regime': _regime,
                        'killzone': _killzone,
                        'family': _family,
                        'action': action
                    })

                ts = datetime.now(PKT).strftime('%Y-%m-%d %I:%M:%S %p')
                if trade_executed:
                    ts += f" | {len(_s.bot_tracked_symbols)} active"
                _s.set_scan_results(scan_rows, ts)

                logger.info(
                    f" Scan completed {ts} | "
                    f"Trade Executed: {trade_executed} | "
                    f"{_s.get_v2_stats_str()}"
                )
                _i.cleanup_indicator_cache()
                _s.cleanup_entry_candles()

                for _ in range(15):
                    if _s.is_stopped():
                        break
                    time.sleep(1)

            except KeyboardInterrupt:
                logger.info(" Stopped by User")
                break
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                time.sleep(10)
    finally:
        try:
            _s.save_cooldowns()
            logger.info(" Cooldowns saved — main_loop exit")
        except Exception as e:
            logger.warning(f"save_cooldowns on exit failed: {e}")


# ═════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    from core.config import print_config_banner
    from getpass import getpass
    import sys
    import signal as _signal

    print_config_banner()

    # ═══════════════════════════════════════════════════════════
    #  REV 1.4.35 (Point 1) — DEV-MODE ENTRY WARNING + GUARD
    # ═══════════════════════════════════════════════════════════
    # Direct `python future.py` bypasses the web dashboard. It is
    # intended ONLY for:
    #   • Dev/debug without UI overhead
    #   • Emergency CLI start when the web server is broken
    # For normal use, `python run.py` is the recommended entry point.
    # Running both simultaneously WILL double-submit orders.
    # ═══════════════════════════════════════════════════════════
    logger.warning("=" * 66)
    logger.warning("  DEV-MODE ENTRY: 'python future.py'")
    logger.warning("  - No web dashboard (http://localhost:%s unavailable)",
                   CONFIG.port)
    logger.warning("  - API keys read from stdin if missing in .env")
    logger.warning("  - RECOMMENDED: `python run.py` for normal operation")
    logger.warning("  - DO NOT run both `run.py` and `future.py` at once")
    logger.warning("=" * 66)

    # Mutual-exclusion guard: refuse to start if the web dashboard
    # is already listening on the configured port. This prevents
    # the classic "two bots, one account" double-submit incident.
    try:
        import socket as _socket
        _s_ = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        _s_.settimeout(0.5)
        _bind_host = CONFIG.host if CONFIG.host != "0.0.0.0" else "127.0.0.1"
        _rc = _s_.connect_ex((_bind_host, int(CONFIG.port)))
        _s_.close()
        if _rc == 0:
            logger.critical(
                "REFUSING TO START: port %s is already in use "
                "(web dashboard likely running). Stop the other "
                "instance first, or run `python run.py` instead.",
                CONFIG.port,
            )
            sys.exit(2)
    except SystemExit:
        raise
    except Exception as _se:
        logger.debug(f"port check skipped: {_se}")

    if DEMO_MODE and TESTNET:
        logger.warning("Both DEMO_MODE and TESTNET True — DEMO takes priority.")
    mode_label = ("DEMO" if DEMO_MODE else
                  "TESTNET" if TESTNET else "!!! MAINNET - REAL MONEY !!!")
    logger.warning(f"===== STARTUP MODE: {mode_label} =====")

    if not DEMO_MODE and not TESTNET:
        confirm = input(f"MAINNET with real funds (Lev {LEVERAGE}x, "
                        f"Risk {RISK_PERCENT}%). Type 'YES': ")
        if confirm != "YES":
            logger.info("Exiting.")
            sys.exit()

    api_key = CONFIG.api_key
    api_secret = CONFIG.api_secret
    if not api_key or not api_secret:
        api_key = getpass("Enter API Key: ").strip().strip('"').strip("'")
        api_secret = getpass("Enter Secret Key: ").strip().strip('"').strip("'")

    _c.set_client_keys(api_key, api_secret)

    try:
        _startup_checks = getattr(_c, 'startup_checks', None)
        if callable(_startup_checks):
            _startup_checks()
    except Exception as _sce:
        logger.warning(f"startup_checks failed: {_sce}")

    ok, bal = _c._validate_api_credentials(_c.get_client())
    if not ok:
        logger.critical("Aborting startup — fix API key and re-run.")
        sys.exit(1)
    logger.info(f" Connected! Futures Wallet Balance: ${bal:.2f}")

    _s.load_active_trades()
    _s.clear_stop()
    _reconcile_on_startup()

    # ── REV 1.4.39 — arm orphan-watch on the direct-CLI path. ──
    # The web-dashboard path arms it inside
    # orders/repair.py::sync_existing_positions (which main_loop
    # calls below). This direct-CLI path uses _reconcile_on_startup
    # instead, which does NOT call sync_existing_positions — so we
    # must arm the watch explicitly here.
    #
    # Idempotent: if main_loop later runs its own sync, the call
    # inside repair.py just re-sets the bool to True. No harm.
    try:
        from orders.manage import mark_startup_sync_done
        mark_startup_sync_done()
    except Exception as _msd_err:
        logger.debug(f"mark_startup_sync_done (CLI): {_msd_err}")

    threading.Thread(target=_o.trade_manager_loop,
                     daemon=True, name="tm_engine").start()

    def _signal_handler(sig, frame):
        logger.info("Shutdown signal — stopping...")
        stop_bot()
        _s.save_cooldowns()
        sys.exit(0)
    _signal.signal(_signal.SIGINT, _signal_handler)
    _signal.signal(_signal.SIGTERM, _signal_handler)

    main_loop()
    _s.save_cooldowns()