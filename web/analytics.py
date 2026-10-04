"""
analytics.py — Accurate trade analytics module for the bot's UI.

Provides SAME data as history.py's 4 CSVs, but kept IN MEMORY
(no CSV files written). Served via app.py's /api/analytics/* routes
and consumed by the existing /api/history and /api/history/detailed
endpoints for accurate UI display.

Data is refreshed by a background worker every 5 minutes.
Uses bot's existing Binance client (no extra session).

REV 1.9 (2026-10-03) — SHARED-SESSION SAFETY:
  ✅ All Binance API calls now serialised under core.client's
     _requests_lock. The python-binance Client's requests.Session is
     NOT thread-safe (cookie jar, connection-pool bookkeeping), and
     analytics previously called futures_income_history /
     futures_account_trades / futures_get_all_orders WITHOUT the lock
     — mixed with the trading engine's own locked calls on the same
     session. Under sustained load this produced intermittent
     TimeoutError / corrupted HTTP state (the empty "Balance fetch
     error:" messages in the demo logs were a downstream symptom).
     The lock is taken PER API CALL (not per pagination loop), so
     the trading engine's order path can interleave between pages.
     Expected impact: analytics refresh slows ~10-20% (already ~45s);
     the trading engine is never blocked for the full refresh.
  ✅ Defensive import of _requests_lock — if a future/old client.py
     doesn't expose it, a local RLock is used so analytics still
     works (just not coordinated with the trading engine).

REV 1.8 (2026-10-03) — PERF + CONFIG + ROBUSTNESS:
  ✅ FIXED: _summarize_trade() was O(trades × income). For every trade
     it linearly scanned ALL income rows to find matching FUNDING_FEE
     events — 5000 trades × 10000 income = 50M iterations per refresh.
     Now a per-symbol funding index is built once and reused, reducing
     to O(income + trades × per_symbol_funding). Typical speedup:
     5-10× on accounts with heavy funding history.
  ✅ PAGE_SIZE now sourced from CONFIG.income_page_size (was a
     hardcoded 1000 constant duplicating the value). Same default,
     but a single source of truth — set CC_INCOME_PAGE_SIZE to tune.
  ✅ MAX_PAGES retained as an analytics-specific safety ceiling (500).
  ✅ FIXED: _fetch_fills() / _fetch_orders() — `int(resp[-1]["id"])`
     could raise KeyError/ValueError on malformed API responses
     (defensive parsing added).
  ✅ refresh_now() logs at DEBUG when the non-blocking refresh lock
     is already held.
  ✅ INCOME_LOOKBACK_DAYS comment clarified: 89 is Binance's protocol
     cap (retention limit), NOT a user-tunable preference.

REV 1.7 (2026-09-28) — FALLBACK LOOP CONSISTENCY FIX.
REV 1.6 (2026-09-28) — SILENT CLIENT-WAIT FOR WORKER.
REV 1.5 (2026-09-26) — FILLS/ORDERS RATE-LIMIT PAGE-SKIP FIX.
REV 1.4 (2026-09-26) — PAGINATION ROBUSTNESS.
REV 1.3 (2026-09-26) — ALIGNED WITH history.py REV 3.7.
REV 1.2 (2026-09-26) — aligned with history.py REV 3.3.
REV 1.1 (2026-09-24) — double-fetch fix.
REV 1.0 (2026-09-24) — initial release.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from core.config import CONFIG
from core.client import get_client, logger

# ── REV 1.9 — shared _requests_lock (defensive import) ──
try:
    from core.client import _requests_lock
except ImportError:
    # Older/unknown client.py — fall back to a local lock. Analytics
    # still works, just not coordinated with the trading engine's own
    # session use. Log once so operators know the fix isn't active.
    _requests_lock = threading.RLock()
    logger.warning(
        "[analytics] core.client._requests_lock not available — "
        "using local lock; shared-session coordination DISABLED"
    )

try:
    from binance.exceptions import BinanceAPIException
except ImportError:
    class BinanceAPIException(Exception):
        code = 0
        message = "no python-binance"


# ═════════════════════════════════════════════════════════════
#  TUNABLES
# ═════════════════════════════════════════════════════════════
REFRESH_INTERVAL_SEC = 300    # background full refresh every 5 min

# REV 1.8 — page size from config_center-adjacent env (core.config).
PAGE_SIZE            = int(CONFIG.income_page_size)

PAGE_SLEEP_SEC       = 0.15

# REV 1.8 — Analytics-specific safety ceiling (NOT CONFIG.max_income_pages).
MAX_PAGES            = 500
MAX_CONSECUTIVE_RL   = 10     # REV 1.5: bail after this many back-to-back RL

# REV 1.8 — 89 = Binance's protocol retention cap for futures income
# history. NOT user preference — /api/history applies
# CONFIG.history_lookback_days for DISPLAY filtering.
INCOME_LOOKBACK_DAYS = 89


# ═════════════════════════════════════════════════════════════
#  IN-MEMORY CACHE
# ═════════════════════════════════════════════════════════════
_CACHE: dict[str, Any] = {
    "fills":    [],
    "orders":   [],
    "income":   [],
    "trades":   [],
    "summary":  {},
    "last_refresh_ms": 0,
    "refresh_in_progress": False,
    "last_error": "",
}
_CACHE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_WORKER_THREAD: Optional[threading.Thread] = None
_WORKER_STOP = threading.Event()

# REV 1.3: page-param support probe cache (process lifetime)
_PAGE_SUPPORT_CACHE: Optional[bool] = None


# ═════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════
def _fmt_ts(ms: int) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(
        ms / 1000, tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def _is_buyer(t: dict) -> bool:
    return bool(t.get("isBuyer", t.get("buyer", False)))


def _is_maker(t: dict) -> bool:
    return bool(t.get("isMaker", t.get("maker", False)))


def _f(v: Any) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _income_key(r: dict) -> tuple:
    """Composite dedup key. tranId is shared by REALIZED_PNL and
    COMMISSION rows of the same fill, so incomeType + income are needed."""
    return (
        str(r.get("tranId") or ""),
        int(r.get("time") or 0),
        r.get("incomeType") or "",
        str(r.get("income") or ""),
        r.get("symbol") or "",
        r.get("asset") or "",
    )


def _safe_int(v: Any, default: int = 0) -> int:
    """REV 1.8 — defensive int coercion for paginator cursors."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ═════════════════════════════════════════════════════════════
#  PAGE-SUPPORT PROBE  (REV 1.3, revised REV 1.4 / 1.9)
# ═════════════════════════════════════════════════════════════
def _probe_page_support(client) -> bool:
    """
    Informational probe: does this client/endpoint ACCEPT the `page`
    param without raising?

    NOTE (REV 1.4): python-binance forwards unknown kwargs as query
    params — Binance may silently IGNORE `page` without raising. So
    a True return here does NOT prove the endpoint honours pagination.
    The runtime auto-fallback in _fetch_income_window() is the real
    safety net.

    REV 1.9 — wrapped in _requests_lock to serialise with the trading
    engine's own shared-session use.
    """
    global _PAGE_SUPPORT_CACHE
    if _PAGE_SUPPORT_CACHE is not None:
        return _PAGE_SUPPORT_CACHE

    now_ms = int(time.time() * 1000)
    try:
        with _requests_lock:
            client.futures_income_history(
                startTime=now_ms - 24 * 60 * 60 * 1000,
                endTime=now_ms,
                limit=10,
                page=0,
            )
        _PAGE_SUPPORT_CACHE = True
    except BinanceAPIException:
        _PAGE_SUPPORT_CACHE = False
    except Exception:
        _PAGE_SUPPORT_CACHE = False

    mode = "page-param" if _PAGE_SUPPORT_CACHE else "time-cursor fallback"
    logger.info(f"[analytics] income pagination mode: {mode} (probe; "
                f"runtime auto-fallback also active)")
    return _PAGE_SUPPORT_CACHE


# ═════════════════════════════════════════════════════════════
#  FETCHERS
# ═════════════════════════════════════════════════════════════
def _fetch_fills(client, symbol: str) -> list[dict]:
    """Fetch account trades for `symbol`, paging with fromId.

    REV 1.5 — RATE-LIMIT RETRY FIX.
    REV 1.8 — defensive int() on last id.
    REV 1.9 — API call wrapped in _requests_lock.
    """
    out: list[dict] = []
    from_id = 0
    page = 0
    consecutive_rl = 0

    while page < MAX_PAGES:
        try:
            with _requests_lock:
                resp = client.futures_account_trades(
                    symbol=symbol, limit=PAGE_SIZE, fromId=from_id,
                )
            consecutive_rl = 0
        except BinanceAPIException as e:
            if e.code in (-1003, -1015):
                time.sleep(3)
                consecutive_rl += 1
                if consecutive_rl > MAX_CONSECUTIVE_RL:
                    logger.warning(
                        f"[analytics] fills {symbol}: "
                        f">{MAX_CONSECUTIVE_RL} consecutive rate limits "
                        f"— aborting"
                    )
                    break
                continue
            logger.debug(f"[analytics] fills {symbol} {e.code}")
            break
        except Exception as e:
            logger.debug(f"[analytics] fills {symbol}: {type(e).__name__}")
            break

        if not resp:
            break
        out.extend(resp)
        if len(resp) < PAGE_SIZE:
            break

        # REV 1.8 — defensive last-id parse.
        last_id = _safe_int(resp[-1].get("id"), default=-1)
        if last_id < 0:
            logger.warning(
                f"[analytics] fills {symbol}: missing/invalid 'id' on "
                f"last record — stopping pagination"
            )
            break
        from_id = last_id + 1
        page += 1
        time.sleep(PAGE_SLEEP_SEC)

    return out


def _fetch_orders(client, symbol: str, from_order_id: int) -> list[dict]:
    """Fetch all orders for `symbol`, paging with fromId.

    REV 1.5 — same rate-limit retry fix as _fetch_fills().
    REV 1.8 — defensive int() on last orderId.
    REV 1.9 — API call wrapped in _requests_lock.
    """
    out: list[dict] = []
    from_id = from_order_id
    page = 0
    consecutive_rl = 0

    while page < MAX_PAGES:
        try:
            with _requests_lock:
                resp = client.futures_get_all_orders(
                    symbol=symbol, limit=PAGE_SIZE, fromId=from_id,
                )
            consecutive_rl = 0
        except BinanceAPIException as e:
            if e.code in (-1003, -1015):
                time.sleep(3)
                consecutive_rl += 1
                if consecutive_rl > MAX_CONSECUTIVE_RL:
                    logger.warning(
                        f"[analytics] orders {symbol}: "
                        f">{MAX_CONSECUTIVE_RL} consecutive rate limits "
                        f"— aborting"
                    )
                    break
                continue
            logger.debug(f"[analytics] orders {symbol} {e.code}")
            break
        except Exception as e:
            logger.debug(f"[analytics] orders {symbol}: {type(e).__name__}")
            break

        if not resp:
            break
        out.extend(resp)
        if len(resp) < PAGE_SIZE:
            break

        # REV 1.8 — defensive last-id parse.
        last_oid = _safe_int(resp[-1].get("orderId"), default=-1)
        if last_oid < 0:
            logger.warning(
                f"[analytics] orders {symbol}: missing/invalid 'orderId' "
                f"on last record — stopping pagination"
            )
            break
        from_id = last_oid + 1
        page += 1
        time.sleep(PAGE_SLEEP_SEC)

    return out


# ── REV 1.4: robust income fetchers ──
def _fetch_income_window(client, start_ms: int, end_ms: int,
                         max_pages: int = 500) -> list[dict]:
    """
    Page-based income fetch.

    REV 1.4 fixes:
      • Rate-limit retries no longer consume the `page` counter.
      • Silent page-ignore fallback to time-cursor.
      • Bail after MAX_CONSECUTIVE_RL consecutive rate limits.

    REV 1.5: hardcoded 1000 → PAGE_SIZE.
    REV 1.9: API call wrapped in _requests_lock.
    """
    out: list[dict] = []
    seen_keys: set = set()
    page = 0
    consecutive_rl = 0

    while page < max_pages:
        try:
            with _requests_lock:
                batch = client.futures_income_history(
                    startTime=start_ms, endTime=end_ms,
                    limit=PAGE_SIZE, page=page,
                )
            consecutive_rl = 0
        except BinanceAPIException as e:
            if e.code in (-1003, -1015):
                time.sleep(3)
                consecutive_rl += 1
                if consecutive_rl > MAX_CONSECUTIVE_RL:
                    logger.warning(
                        "[analytics] page fetch: "
                        f">{MAX_CONSECUTIVE_RL} consecutive rate limits "
                        "— abandoning page mode"
                    )
                    break
                continue
            raise

        if not batch:
            break

        fresh = 0
        for r in batch:
            k = _income_key(r)
            if k in seen_keys:
                continue
            seen_keys.add(k)
            out.append(r)
            fresh += 1

        if len(batch) < PAGE_SIZE:
            break

        if fresh == 0 and page > 0:
            # Page param is being silently ignored. Fall back to
            # time-cursor mode for the remainder of the range.
            logger.info(
                f"[analytics] page param appears unsupported "
                f"(0 fresh on page {page}) — switching to "
                f"time-cursor for remainder"
            )
            last_seen_ms = max(
                (int(r.get("time") or 0) for r in out),
                default=start_ms,
            )
            tail = _fetch_income_window_fallback(
                client, last_seen_ms, end_ms,
            )
            for r in tail:
                k = _income_key(r)
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                out.append(r)
            return out

        page += 1
        time.sleep(PAGE_SLEEP_SEC)

    return out


def _fetch_income_window_fallback(client, start_ms: int,
                                   end_ms: int) -> list[dict]:
    """Time-cursor income fetch. cursor = last_time (WITHOUT +1) with
    composite-key dedup; advances by +1 only on stall.

    REV 1.5: hardcoded 1000 → PAGE_SIZE.
    REV 1.7 — RATE-LIMIT PAGE-COUNTER FIX.
    REV 1.9 — API call wrapped in _requests_lock.
    """
    out: list[dict] = []
    seen_keys: set = set()
    cursor = start_ms
    stall_count = 0
    consecutive_rl = 0
    page = 0

    while page < MAX_PAGES:
        try:
            with _requests_lock:
                batch = client.futures_income_history(
                    startTime=cursor, endTime=end_ms, limit=PAGE_SIZE,
                )
            consecutive_rl = 0
        except BinanceAPIException as e:
            if e.code in (-1003, -1015):
                time.sleep(3)
                consecutive_rl += 1
                if consecutive_rl > MAX_CONSECUTIVE_RL:
                    logger.warning(
                        "[analytics] income fallback: "
                        f">{MAX_CONSECUTIVE_RL} consecutive rate limits "
                        "— aborting"
                    )
                    break
                continue
            raise
        if not batch:
            break

        fresh = 0
        for r in batch:
            k = _income_key(r)
            if k in seen_keys:
                continue
            seen_keys.add(k)
            out.append(r)
            fresh += 1

        if len(batch) < PAGE_SIZE:
            break

        last_time = int(batch[-1]["time"])
        new_cursor = last_time
        if fresh == 0 or new_cursor <= cursor:
            new_cursor = cursor + 1
            stall_count += 1
        else:
            stall_count = 0
        if stall_count > 10:
            logger.warning("[analytics] income pagination stalled — breaking")
            break
        cursor = new_cursor
        page += 1        # REV 1.7: advance ONLY on a successful page
        if cursor >= end_ms:
            break
        time.sleep(PAGE_SLEEP_SEC)

    if page >= MAX_PAGES and cursor < end_ms:
        logger.warning(
            f"[analytics] income fallback: reached MAX_PAGES={MAX_PAGES} "
            f"with data pending (cursor={cursor} < end={end_ms}) "
            f"— results may be truncated"
        )

    return out


def _fetch_income_range(client, start_ms: int, end_ms: int) -> list[dict]:
    """Dispatch based on one-time probe."""
    if _probe_page_support(client):
        try:
            return _fetch_income_window(client, start_ms, end_ms)
        except BinanceAPIException:
            logger.warning("[analytics] page fetch failed mid-run, "
                           "switching to time-cursor fallback")
            global _PAGE_SUPPORT_CACHE
            _PAGE_SUPPORT_CACHE = False
            return _fetch_income_window_fallback(client, start_ms, end_ms)
    else:
        return _fetch_income_window_fallback(client, start_ms, end_ms)


def _fetch_income(client) -> list[dict]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - INCOME_LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    return _fetch_income_range(client, start_ms, now_ms)


# ═════════════════════════════════════════════════════════════
#  PAIRING ENGINE  (REV 1.3 — aligned with history.py REV 3.4)
# ═════════════════════════════════════════════════════════════
def _new_trade(fill: dict, signed_qty: float) -> dict:
    """Create a fresh trade. funding_start_ms = entry time."""
    t = int(fill["time"])
    return {
        "side": "LONG" if signed_qty > 0 else "SHORT",
        "entry_fills": [fill],
        "exit_fills": [],
        "entry_time_ms": t,
        "exit_time_ms": None,
        "funding_start_ms": t,
        "status": "OPEN",
    }


def _scale_fills(fills: list[dict], factor: float) -> list[dict]:
    """Return copies of fills with qty & commission scaled by `factor`."""
    out = []
    for f in fills:
        nf = dict(f)
        nf["qty"] = str(_f(f.get("qty")) * factor)
        nf["commission"] = str(_f(f.get("commission")) * factor)
        out.append(nf)
    return out


def _pair_fills(fills: list[dict]) -> list[dict]:
    """
    Walk fills chronologically, pair entry/exit into round-trip trades.

    REV 1.3 — Semantics (identical to history.py REV 3.3/3.4).
    """
    fills_sorted = sorted(fills, key=lambda x: (int(x["time"]), int(x["id"])))
    trades: list[dict] = []
    open_trade: Optional[dict] = None
    position = 0.0

    for f in fills_sorted:
        side = "BUY" if _is_buyer(f) else "SELL"
        qty = _f(f.get("qty"))
        signed = qty if side == "BUY" else -qty

        if abs(position) < 1e-9:
            open_trade = _new_trade(f, signed)
            position = signed
            continue

        if (position > 0 and signed > 0) or (position < 0 and signed < 0):
            open_trade["entry_fills"].append(f)
            position += signed
            continue

        # Opposite direction
        if abs(signed) <= abs(position) + 1e-9:
            open_trade["exit_fills"].append(f)
            position += signed
            if abs(position) < 1e-9:
                open_trade["status"] = "CLOSED"
                open_trade["exit_time_ms"] = int(f["time"])
                trades.append(open_trade)
                open_trade = None
                position = 0.0
            # else: partial exit — keep accumulating (do NOT split)
        else:
            # ── FLIP ──
            close_qty = abs(position)
            factor = close_qty / qty if qty else 1.0

            exit_portion = dict(f)
            exit_portion["qty"] = str(close_qty)
            exit_portion["realizedPnl"] = f.get("realizedPnl", "0")
            exit_portion["commission"]  = str(_f(f.get("commission")) * factor)
            open_trade["exit_fills"].append(exit_portion)
            open_trade["status"] = "CLOSED"
            open_trade["exit_time_ms"] = int(f["time"])
            trades.append(open_trade)

            remainder = abs(signed) - close_qty
            new_fill = dict(f)
            new_fill["qty"] = str(remainder)
            new_fill["realizedPnl"] = "0"
            new_fill["commission"]  = str(_f(f.get("commission")) * (1 - factor))
            open_trade = _new_trade(new_fill, remainder if signed > 0 else -remainder)
            position = remainder if signed > 0 else -remainder

    # ── End of fills: handle leftover open_trade ──
    if open_trade is not None:
        if open_trade["exit_fills"]:
            entry_qty = sum(_f(f.get("qty")) for f in open_trade["entry_fills"])
            exit_qty  = sum(_f(f.get("qty")) for f in open_trade["exit_fills"])
            remaining = entry_qty - exit_qty

            if remaining > 1e-9 and exit_qty > 1e-9:
                factor = exit_qty / entry_qty
                last_exit_ms = int(open_trade["exit_fills"][-1]["time"])

                trades.append({
                    "side": open_trade["side"],
                    "entry_fills": _scale_fills(open_trade["entry_fills"], factor),
                    "exit_fills": open_trade["exit_fills"],
                    "entry_time_ms": open_trade["entry_time_ms"],
                    "exit_time_ms": last_exit_ms,
                    "funding_start_ms": open_trade["funding_start_ms"],
                    "status": "CLOSED",
                })
                trades.append({
                    "side": open_trade["side"],
                    "entry_fills": _scale_fills(open_trade["entry_fills"], 1 - factor),
                    "exit_fills": [],
                    "entry_time_ms": open_trade["entry_time_ms"],
                    "exit_time_ms": None,
                    "funding_start_ms": last_exit_ms,
                    "status": "OPEN",
                })
            elif remaining < 1e-9:
                open_trade["status"] = "CLOSED"
                open_trade["exit_time_ms"] = int(open_trade["exit_fills"][-1]["time"])
                trades.append(open_trade)
            else:
                open_trade["status"] = "OPEN"
                trades.append(open_trade)
        else:
            open_trade["status"] = "OPEN"
            trades.append(open_trade)

    return trades


# ═════════════════════════════════════════════════════════════
#  REV 1.8 — FUNDING INDEX (per-symbol)
# ═════════════════════════════════════════════════════════════
def _build_funding_index(all_income: list[dict]) -> dict[str, list[tuple[int, float]]]:
    idx: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for inc in all_income:
        if inc.get("incomeType") != "FUNDING_FEE":
            continue
        sym = inc.get("symbol")
        if not sym:
            continue
        t = int(inc.get("time") or 0)
        idx[sym].append((t, _f(inc.get("income"))))
    # Sort by time so we can early-break within a range.
    for sym in idx:
        idx[sym].sort(key=lambda x: x[0])
    return idx


def _summarize_trade(t: dict,
                     funding_index: Optional[dict[str, list[tuple[int, float]]]] = None,
                     all_income: Optional[list[dict]] = None) -> dict:
    """
    Build one summary row.

    REV 1.8 — funding lookup now O(per_symbol_events) via index
    (built once per refresh). Backward-compat path with all_income
    is kept for any external caller but is no longer used by
    _compute_all().
    """
    ef = t["entry_fills"]; xf = t["exit_fills"]
    is_closed = (t["status"] == "CLOSED")

    entry_qty = sum(_f(f.get("qty")) for f in ef)
    exit_qty  = sum(_f(f.get("qty")) for f in xf)
    entry_vwap = (sum(_f(f.get("price")) * _f(f.get("qty")) for f in ef) / entry_qty) if entry_qty else 0.0
    exit_vwap  = (sum(_f(f.get("price")) * _f(f.get("qty")) for f in xf) / exit_qty) if exit_qty else 0.0

    gross_pnl = (sum(_f(f.get("realizedPnl")) for f in ef)
                 + sum(_f(f.get("realizedPnl")) for f in xf))
    commission = -sum(_f(f.get("commission")) for f in (ef + xf))

    sym = ef[0]["symbol"]
    entry_ms = int(ef[0]["time"])
    exit_ms  = int(xf[-1]["time"]) if (is_closed and xf) else int(time.time() * 1000)

    funding_start = t.get("funding_start_ms", entry_ms)
    funding = 0.0

    # REV 1.8 — prefer index; fall back to linear scan only if caller
    # explicitly passes all_income (legacy path).
    if funding_index is not None:
        events = funding_index.get(sym, ())
        for it, amt in events:
            if it < funding_start:
                continue
            if it > exit_ms:
                break  # sorted by time
            funding += amt
    elif all_income is not None:
        for inc in all_income:
            if inc.get("symbol") != sym:
                continue
            if inc.get("incomeType") != "FUNDING_FEE":
                continue
            it = int(inc.get("time") or 0)
            if funding_start <= it <= exit_ms:
                funding += _f(inc.get("income"))
    # else: no funding data available — leaves funding at 0.0

    net_pnl = gross_pnl + commission + funding
    holding_min = (exit_ms - entry_ms) / 60000.0 if exit_ms > entry_ms else 0.0
    entry_orders = sorted(set(str(f.get("orderId")) for f in ef if f.get("orderId")))
    exit_orders  = sorted(set(str(f.get("orderId")) for f in xf if f.get("orderId")))

    return {
        "symbol": sym, "side": t["side"], "status": t["status"],
        "entry_time_utc": _fmt_ts(entry_ms),
        "exit_time_utc":  _fmt_ts(exit_ms) if (is_closed and xf) else "",
        "holding_minutes": round(holding_min, 2) if is_closed else 0.0,
        "entry_price": entry_vwap,
        "exit_price":  exit_vwap if (is_closed and xf) else 0.0,
        "qty": entry_qty,
        "notional_usdt": entry_vwap * entry_qty,
        "gross_pnl": gross_pnl,
        "commission": commission,
        "funding": funding,
        "net_pnl": net_pnl,
        "entry_fill_count": len(ef),
        "exit_fill_count": len(xf),
        "entry_orders": entry_orders,
        "exit_orders": exit_orders if is_closed else [],
    }


# ═════════════════════════════════════════════════════════════
#  ROW SHAPERS
# ═════════════════════════════════════════════════════════════
def _fill_row(t: dict) -> dict:
    return {
        "id": t.get("id", ""),
        "datetime_utc": _fmt_ts(int(t["time"])) if t.get("time") else "",
        "time_ms": int(t.get("time") or 0),
        "symbol": t.get("symbol", ""),
        "side": "BUY" if _is_buyer(t) else "SELL",
        "price": _f(t.get("price")),
        "qty": _f(t.get("qty")),
        "quoteQty": _f(t.get("quoteQty")),
        "commission": _f(t.get("commission")),
        "commissionAsset": t.get("commissionAsset", ""),
        "realizedPnl": _f(t.get("realizedPnl")),
        "orderId": t.get("orderId", ""),
        "buyer": _is_buyer(t),
        "maker": _is_maker(t),
    }


def _order_row(o: dict) -> dict:
    return {
        "orderId": o.get("orderId", ""),
        "time_utc": _fmt_ts(int(o["time"])) if o.get("time") else "",
        "update_utc": _fmt_ts(int(o["updateTime"])) if o.get("updateTime") else "",
        "symbol": o.get("symbol", ""),
        "side": o.get("side", ""),
        "type": o.get("type", ""),
        "status": o.get("status", ""),
        "origQty": _f(o.get("origQty")),
        "executedQty": _f(o.get("executedQty")),
        "avgPrice": _f(o.get("avgPrice")),
        "stopPrice": _f(o.get("stopPrice")),
        "reduceOnly": bool(o.get("reduceOnly", False)),
        "closePosition": bool(o.get("closePosition", False)),
        "workingType": o.get("workingType", ""),
        "clientOrderId": o.get("clientOrderId", ""),
    }


def _income_row(inc: dict) -> dict:
    return {
        "time_utc": _fmt_ts(int(inc["time"])) if inc.get("time") else "",
        "time_ms": int(inc.get("time") or 0),
        "symbol": inc.get("symbol", ""),
        "incomeType": inc.get("incomeType", ""),
        "income": _f(inc.get("income")),
        "asset": inc.get("asset", ""),
        "tranId": inc.get("tranId", ""),
    }


# ═════════════════════════════════════════════════════════════
#  SYMBOL DISCOVERY
# ═════════════════════════════════════════════════════════════
def _discover_symbols(client, all_income: list[dict] | None = None) -> list[str]:
    """
    Discover all symbols this account has traded.
    `all_income` allows the caller to pass an already-fetched income
    list, avoiding a redundant fetch.
    """
    syms: set[str] = set()

    # 1. Configured bot coins (fast, no API call)
    for c in CONFIG.bot_coins:
        s = c.upper()
        if not s.endswith("USDT"):
            s += "USDT"
        syms.add(s)

    # 2. Any symbol seen in income history (covers manual trades)
    try:
        income = all_income if all_income is not None else _fetch_income(client)
        for inc in income:
            s = inc.get("symbol")
            if s and s.endswith("USDT"):
                syms.add(s)
    except Exception:
        pass

    return sorted(syms)


# ═════════════════════════════════════════════════════════════
#  PER-SYMBOL DRIFT DIAGNOSTIC  (REV 1.3)
# ═════════════════════════════════════════════════════════════
def _report_per_symbol_drift(all_fills: list[dict],
                             all_income: list[dict]) -> dict:
    """
    Compute per-symbol realizedPnl & commission drift.
    Returns a compact dict. Also logs warnings for any non-zero delta.
    """
    fills_rp: dict[str, float] = defaultdict(float)
    fills_cm: dict[str, float] = defaultdict(float)
    for f in all_fills:
        sym = f.get("symbol") or ""
        fills_rp[sym] += _f(f.get("realizedPnl"))
        fills_cm[sym] += _f(f.get("commission"))  # positive

    inc_rp: dict[str, float] = defaultdict(float)
    inc_cm: dict[str, float] = defaultdict(float)
    for i in all_income:
        sym = i.get("symbol") or ""
        t = i.get("incomeType")
        if t == "REALIZED_PNL":
            inc_rp[sym] += _f(i.get("income"))
        elif t == "COMMISSION":
            inc_cm[sym] += _f(i.get("income"))  # negative

    symbols = sorted(set(list(fills_rp.keys()) + list(inc_rp.keys())))
    per_sym: dict[str, dict] = {}
    total_d_rp = 0.0
    total_d_cm = 0.0

    for sym in symbols:
        d_rp = fills_rp[sym] - inc_rp[sym]
        d_cm = fills_cm[sym] + inc_cm[sym]   # signs cancel
        total_d_rp += d_rp
        total_d_cm += d_cm
        per_sym[sym] = {
            "fill_rpnl":  round(fills_rp[sym], 4),
            "inc_rpnl":   round(inc_rp[sym], 4),
            "delta_rpnl": round(d_rp, 4),
            "fill_comm":  round(fills_cm[sym], 4),
            "inc_comm":   round(inc_cm[sym], 4),
            "delta_comm": round(d_cm, 4),
        }
        if abs(d_rp) > 0.01 or abs(d_cm) > 0.01:
            logger.warning(
                f"[analytics] drift {sym}: "
                f"ΔRPNL={d_rp:+.4f}  ΔCOMM={d_cm:+.4f}"
            )

    if abs(total_d_rp) < 0.01 and abs(total_d_cm) < 0.01:
        logger.info(f"[analytics] per-symbol drift: CLEAN "
                    f"(ΔRPNL={total_d_rp:+.4f}, ΔCOMM={total_d_cm:+.4f})")
    else:
        logger.warning(f"[analytics] per-symbol drift TOTAL: "
                       f"ΔRPNL={total_d_rp:+.4f}, ΔCOMM={total_d_cm:+.4f}")

    return {
        "per_symbol": per_sym,
        "total_delta_rpnl": round(total_d_rp, 4),
        "total_delta_comm": round(total_d_cm, 4),
    }


# ═════════════════════════════════════════════════════════════
#  CORE REFRESH
# ═════════════════════════════════════════════════════════════
def _compute_all() -> dict:
    client = get_client()
    if client is None:
        raise RuntimeError("no client")

    t0 = time.time()
    logger.info("[analytics] full refresh starting")

    # Probe page-param support once (informational).
    _probe_page_support(client)

    # Fetch income once, then reuse it for symbol discovery.
    all_income = _fetch_income(client)
    symbols = _discover_symbols(client, all_income)

    all_fills: list[dict] = []
    per_symbol: dict[str, list[dict]] = defaultdict(list)
    for sym in symbols:
        fills = _fetch_fills(client, sym)
        if fills:
            per_symbol[sym] = fills
            all_fills.extend(fills)

    all_orders: list[dict] = []
    for sym, fills in per_symbol.items():
        try:
            min_oid = min(int(f["orderId"]) for f in fills)
        except Exception:
            continue
        all_orders.extend(_fetch_orders(client, sym, min_oid))

    all_trades_raw: list[dict] = []
    for sym, fills in per_symbol.items():
        all_trades_raw.extend(_pair_fills(fills))
    all_trades_raw.sort(key=lambda t: t["entry_time_ms"])

    # ── REV 1.8 — build funding index ONCE, reuse for every trade ──
    funding_index = _build_funding_index(all_income)
    trades_summary = [
        _summarize_trade(t, funding_index=funding_index)
        for t in all_trades_raw
    ]

    fills_rows = [_fill_row(f) for f in all_fills]
    fills_rows.sort(key=lambda r: (r["time_ms"], str(r["id"])), reverse=True)
    order_rows = [_order_row(o) for o in all_orders]
    order_rows.sort(key=lambda r: r["time_utc"], reverse=True)
    inc_rows = [_income_row(i) for i in all_income]
    inc_rows.sort(key=lambda r: r["time_ms"], reverse=True)
    trades_summary.sort(key=lambda r: r["entry_time_utc"], reverse=True)

    # ── Aggregate stats + cross-check (REV 1.3: parity with income) ──
    closed = [t for t in trades_summary if t["status"] == "CLOSED"]
    open_  = [t for t in trades_summary if t["status"] == "OPEN"]

    closed_gross = sum(t["gross_pnl"] for t in closed)
    closed_comm  = sum(t["commission"] for t in closed)
    closed_fund  = sum(t["funding"]    for t in closed)
    open_comm    = sum(t["commission"] for t in open_)
    open_fund    = sum(t["funding"]    for t in open_)

    total_gross = closed_gross
    total_comm  = closed_comm + open_comm
    total_fund  = closed_fund + open_fund
    total_net   = total_gross + total_comm + total_fund

    income_realized   = sum(_f(i.get("income")) for i in all_income if i.get("incomeType") == "REALIZED_PNL")
    income_commission = sum(_f(i.get("income")) for i in all_income if i.get("incomeType") == "COMMISSION")
    income_funding    = sum(_f(i.get("income")) for i in all_income if i.get("incomeType") == "FUNDING_FEE")
    income_net        = income_realized + income_commission + income_funding

    wins   = sum(1 for t in closed if t["net_pnl"] > 0)
    losses = sum(1 for t in closed if t["net_pnl"] < 0)
    be     = sum(1 for t in closed if abs(t["net_pnl"]) < 1e-6)
    total  = len(closed)
    avg_win  = (sum(t["net_pnl"] for t in closed if t["net_pnl"] > 0) / wins) if wins else 0.0
    avg_loss = (sum(t["net_pnl"] for t in closed if t["net_pnl"] < 0) / losses) if losses else 0.0

    first_ms = min((int(f["time"]) for f in all_fills), default=0)
    last_ms  = max((int(f["time"]) for f in all_fills), default=0)

    drift = total_net - income_net

    diagnostic = _report_per_symbol_drift(all_fills, all_income)

    summary = {
        "coverage_start": _fmt_ts(first_ms),
        "coverage_end":   _fmt_ts(last_ms),
        "span_days":      round((last_ms - first_ms) / 86400000, 2) if first_ms else 0,
        "symbols":        symbols,
        "symbol_count":   len(symbols),
        "fills_count":    len(fills_rows),
        "orders_count":   len(order_rows),
        "income_count":   len(inc_rows),
        "trades_total":   len(trades_summary),
        "trades_closed":  total,
        "trades_open":    len(open_),
        "from_trades": {
            "gross_pnl":  round(total_gross, 4),
            "commission": round(total_comm, 4),
            "funding":    round(total_fund, 4),
            "net_pnl":    round(total_net, 4),
        },
        "from_income": {
            "realized":   round(income_realized, 4),
            "commission": round(income_commission, 4),
            "funding":    round(income_funding, 4),
            "net_pnl":    round(income_net, 4),
        },
        "drift": round(drift, 4),
        "wins":   wins,
        "losses": losses,
        "be":     be,
        "win_rate": round(wins / total * 100, 2) if total else 0.0,
        "avg_win":  round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "wl_rr":    round(abs(avg_win / avg_loss), 2) if avg_loss else 0.0,
        "diagnostic": diagnostic,
    }

    elapsed = time.time() - t0
    logger.info(f"[analytics] refresh done in {elapsed:.1f}s — "
                f"fills={len(fills_rows)} orders={len(order_rows)} "
                f"income={len(inc_rows)} trades={len(trades_summary)} "
                f"drift={drift:+.4f}")

    return {
        "fills": fills_rows,
        "orders": order_rows,
        "income": inc_rows,
        "trades": trades_summary,
        "summary": summary,
    }


# ═════════════════════════════════════════════════════════════
#  PUBLIC API
# ═════════════════════════════════════════════════════════════
def refresh_now() -> bool:
    """Synchronous full refresh. Returns True on success.

    REV 1.6 — QUIET NO-CLIENT.
    REV 1.8 — logs DEBUG when the refresh lock is already held.
    """
    if not _REFRESH_LOCK.acquire(blocking=False):
        logger.debug("[analytics] refresh_now skipped — already in progress")
        return False
    try:
        with _CACHE_LOCK:
            _CACHE["refresh_in_progress"] = True
        try:
            if get_client() is None:
                return False

            result = _compute_all()
            with _CACHE_LOCK:
                _CACHE.update(result)
                _CACHE["last_refresh_ms"] = int(time.time() * 1000)
                _CACHE["last_error"] = ""
            return True
        except Exception as e:
            logger.warning(f"[analytics] refresh failed: {e}")
            with _CACHE_LOCK:
                _CACHE["last_error"] = str(e)
            return False
        finally:
            with _CACHE_LOCK:
                _CACHE["refresh_in_progress"] = False
    finally:
        _REFRESH_LOCK.release()


def snapshot() -> dict:
    """Return a thread-safe snapshot of the cache.

    NOTE: list() / dict() are SHALLOW copies. Trade/fill/order dicts
    inside are shared references with the cache. All current callers
    (web/app.py) are read-only, so this is safe — but external code
    MUST NOT mutate the returned dicts in place.
    """
    with _CACHE_LOCK:
        return {
            "fills":    list(_CACHE["fills"]),
            "orders":   list(_CACHE["orders"]),
            "income":   list(_CACHE["income"]),
            "trades":   list(_CACHE["trades"]),
            "summary":  dict(_CACHE["summary"]),
            "last_refresh_ms": _CACHE["last_refresh_ms"],
            "refresh_in_progress": _CACHE["refresh_in_progress"],
            "last_error": _CACHE["last_error"],
        }


def schedule_refresh() -> None:
    """Kick off a background refresh (non-blocking).

    REV 1.6: if no client is set, this is a silent no-op.
    """
    if get_client() is None:
        return

    def _run():
        try:
            refresh_now()
        except Exception as e:
            logger.debug(f"[analytics] scheduled refresh: {e}")
    threading.Thread(target=_run, daemon=True, name="analytics_oneshot").start()


def start_background_worker() -> None:
    """Start periodic full-refresh worker. Idempotent."""
    global _WORKER_THREAD
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return
    _WORKER_STOP.clear()
    _WORKER_THREAD = threading.Thread(
        target=_worker_loop, daemon=True, name="analytics_worker"
    )
    _WORKER_THREAD.start()
    logger.info(f"[analytics] background worker started (every {REFRESH_INTERVAL_SEC}s)")


def stop_background_worker() -> None:
    _WORKER_STOP.set()


def _worker_loop() -> None:
    """Background refresh loop.

    REV 1.6 — SILENT CLIENT-WAIT.
    """
    # Give Flask / config loading a moment.
    time.sleep(5)

    if _WORKER_STOP.is_set():
        return

    # ── Phase 1: quietly wait for the client ──
    if get_client() is None:
        logger.info("[analytics] worker idle — waiting for bot to start")

    while get_client() is None:
        if _WORKER_STOP.is_set():
            return
        for _ in range(5):
            if _WORKER_STOP.is_set():
                return
            time.sleep(1)

    if _WORKER_STOP.is_set():
        return

    logger.info("[analytics] worker starting refresh cycle")

    # ── Phase 2: normal refresh loop ──
    while not _WORKER_STOP.is_set():
        try:
            refresh_now()
        except Exception as e:
            logger.warning(f"[analytics] worker refresh error: {e}")
        for _ in range(REFRESH_INTERVAL_SEC):
            if _WORKER_STOP.is_set():
                return
            time.sleep(1)