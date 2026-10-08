"""
client.py — Binance Futures client, exchange metadata, filters, klines,
positions, account cache, Telegram. Lowest layer of the trading engine.

Does NOT know about strategies or trade lifecycle.

REV 11.8 (2026-10-05) — ALGO-ORDER FETCH FAIL-CLOSED (H5b):
  ✅ MEDIUM (H5b): _get_open_algo_orders() now returns None on error
     instead of []. Previously any failure (network hiccup, rate-limit
     storm, missing SDK method) returned an empty list — which callers
     read as "no protective stops exist". Consequences:

       • robust_cancel_all() in exit.py could log
         "All orphaned orders cancelled" when it had not actually been
         able to read the algo-order book.
       • orphan-cancel paths in repair.py could report 0 cancelled
         when they had silently skipped every check.
       • manage.py's TP1-detection could conclude "no TP1 open"
         (i.e. TP1 filled) on the basis of a failed fetch, and
         promote SL to BE prematurely.

     Return contract is now:

         list of dicts — success (may be empty; means "no orders")
         None          — the fetch itself failed; state is UNKNOWN

     Callers MUST branch on None and treat it as "cannot determine"
     rather than "empty". Existing callers that do
     `for o in _get_open_algo_orders(pair):` inside a try/except will
     silently get a TypeError (caught), so the bot does not crash —
     but to fully realise the fix, callers should be migrated to:

         _algo = _get_open_algo_orders(pair)
         if _algo is None:
             # unknown — do NOT assume empty
             ...
         else:
             for o in _algo:
                 ...

     A legacy shim is deliberately NOT provided: a shim would let
     un-migrated callers keep the fail-open behaviour silently, which
     is the exact class of bug this change eliminates. Prefer to
     migrate the callers (see the "caller impact" notes in the
     REV 11.8 summary).

     Zero change to the happy path: when the fetch succeeds, the
     return value is a list (possibly empty) exactly as before.

REV 11.7 (2026-10-05) — TIMEOUT HONESTY + DECIMAL FALLBACK PRECISION (retained):
  ✅ CRITICAL (A): _run_with_timeout() no longer abandons lock-holding
     workers on the best-effort path. Previously, a 6s caller cap on
     futures_account / exchange_info / any non-order call could fire
     BEFORE the HTTP session timeout (10s), leaving the worker thread
     alive and HOLDING _read_lock. With the Phase-2 read/write split,
     _read_lock is contended by analytics, orphan-watch, get_trade_status,
     _pick_real_sl, and every klines fetch — so one abandoned worker
     queued every other reader behind it, silently converting a single
     slow call into a reader-wide stall.

     Fix: the effective timeout for best-effort callers is now
     max(caller, HTTP session timeout) — the SAME floor critical
     callers already had. The 6s cap is gone. The caller now waits
     honestly for the worker to finish (or for the socket layer to
     kill the request at 10s), after which the lock is provably
     released.

     Trade-off: individual best-effort calls may block their caller
     for up to 10s instead of 6s in the worst case. That is the
     correct trade — the previous behaviour only gave the ILLUSION
     of a shorter wait; the lock was still held for the full 10s.

  ✅ MEDIUM (B): adjust_qty() / adjust_price() fallback paths now
     re-format the value via Decimal using step/tick's exponent,
     matching the primary path's output contract. Previously the
     fallback returned `str(min_qty)` / `str(price)` verbatim, which
     could violate Binance's strict -1111 precision rules if the
     input's decimal representation differed from the step's
     (e.g. min_qty=Decimal('0.0010') → "0.0010" while the exchange
     expects "0.001"). The new fallback is a best-effort repair, not
     a full floor-to-step, but it is precision-correct.

  ✅ Removed the now-unused `_TIMEOUT_EXECUTOR_CAP` constant. Its
     only purpose was the cap that A eliminates.

REV 11.6 (2026-10-05) — DEPRECATED ALIAS REMOVED (Phase 2 cleanup):
  ✅ The deprecated `_requests_lock = _write_lock` alias has been
     REMOVED. Every consumer file has been migrated to the explicit
     read/write split introduced in REV 11.5:
       • future.py, orders/{manage,entry,exit,repair}.py,
         web/{app,analytics}.py — all use _read_lock / _write_lock
         directly.
     The alias outlived its usefulness as a safety net; keeping it
     would let future contributors accidentally reintroduce the old
     "one lock for everything" pattern without realising.

  ✅ Added a module-level `__getattr__` guard. Any code that still
     references `_requests_lock` now fails with a CLEAR, actionable
     AttributeError naming the correct replacement — instead of a
     generic "module has no attribute" or a silent re-serialization.
     This is the standard Python idiom (PEP 562) for graceful removal
     of a public-ish name.

  ✅ Verification before this change: `grep -rn _requests_lock` across
     the whole repo returned exactly one hit — this file's own alias
     definition. Zero external consumers. Removal is therefore safe.

  ✅ Zero behaviour change for every migrated call site. Same locks,
     same semantics. The only observable difference is that a
     hypothetical missed migration now fails LOUD instead of quietly
     over-serializing.

REV 11.5 (2026-10-05) — READ/WRITE LOCK SPLIT (Phase 2) (retained):
  ✅ The single _requests_lock (which serialized EVERY Binance call —
     reads AND writes) has been split into two independent RLocks:
        _read_lock  — positions, klines, account, tickers, exchangeInfo,
                      open orders, server time, algo-order LIST.
        _write_lock — futures_create_order, futures_cancel_order,
                      futures_cancel_algo_order.
     Reads no longer block other reads. Writes still serialize against
     other writes. Reads and writes are INDEPENDENT.

  ✅ NO NESTING between the two locks anywhere in the codebase. If a
     future path ever needs both, acquire _read_lock FIRST (canonical
     order) — otherwise deadlock risk.

  ✅ retry_on_rate_limit() accepts `is_write=` keyword:
        @retry_on_rate_limit                     # read (default)
        @retry_on_rate_limit(is_write=True)      # write
     The lock is re-entered per retry attempt; it is NOT held across
     the exponential-backoff sleep, so other readers/writers proceed
     between retries.

REV 11.4 (2026-10-04) — HEDGE-MODE FAIL-FAST (retained):
  ✅ CRITICAL: set_client_keys() and startup_checks() now RAISE a
     RuntimeError when the account is in HEDGE mode
     (dualSidePosition=True). Previously check_position_mode() only
     LOGGED a CRITICAL and returned the bool — which both callers
     ignored. The bot booted successfully into HEDGE mode, then every
     order failed at the exchange with cryptic codes (-4061 / -4046 /
     "reduceOnly rejected"), wasting time and creating confusion.
     Now the failure is immediate and actionable, at the exact point
     where the mode is discovered.
  ✅ check_position_mode() contract unchanged: returns True (HEDGE),
     False (ONE-WAY), or None (check itself failed — e.g. API error).
     Callers decide what to do with None; only True is a hard fail.
  ✅ Zero behaviour change for ONE-WAY accounts (the common case).

REV 11.3 (2026-10-04) — CRITICAL-PATH TIMEOUT ISOLATION (A1) (retained).
REV 11.2 (2026-10-03) — EXECUTOR POOL HARDENING (retained).
REV 11.1 (2026-10-03) — HARDENING PASS (retained).
REV 11.0 (2026-10-03) — KLINES CACHE + LOCK HYGIENE (retained).
REV 10.9 (2026-10-02) — COSMETIC CLEANUP (retained).
"""

from __future__ import annotations

import concurrent.futures
import logging
import random
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import requests
from binance.client import Client
from binance.exceptions import BinanceAPIException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import CONFIG

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
from logging.handlers import RotatingFileHandler

_log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _log_path = Path(CONFIG.log_dir) / "bot.log"
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_handlers.insert(0, RotatingFileHandler(
        str(_log_path), maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8'
    ))
except Exception:
    pass

logging.basicConfig(
    level=getattr(logging, CONFIG.log_level, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=_log_handlers,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────────────────────
_global_client: Optional[Client] = None
_CLIENT_INIT_LOCK = threading.RLock()

# ═════════════════════════════════════════════════════════════
#  REV 11.5 / 11.6 — READ / WRITE LOCK SPLIT
# ═════════════════════════════════════════════════════════════
# _read_lock  — shared. Every read API call acquires this.
#               Reads do NOT block other reads.
#
# _write_lock — exclusive. Every state-mutating API call
#               (create/cancel order, cancel algo order) acquires
#               this. Writes serialize against each other.
#
# Reads and writes are INDEPENDENT. This is the whole point:
# a slow position fetch must not stall an SL update, and a slow
# klines fetch must not stall an entry.
#
# NO NESTING anywhere in the current codebase. If a future path
# needs both, acquire _read_lock FIRST (canonical order) to
# avoid deadlock with any future path that does the same.
#
# REV 11.6 — the deprecated `_requests_lock` alias has been
# REMOVED. Every consumer file migrated in REV 11.5–11.10 of the
# respective modules. A module-level __getattr__ below catches any
# straggler and gives an actionable error instead of a generic
# "module has no attribute" message.
# ═════════════════════════════════════════════════════════════
_read_lock = threading.RLock()
_write_lock = threading.RLock()


# ═════════════════════════════════════════════════════════════
#  REV 11.6 — REMOVED-NAME GUARD (PEP 562)
# ═════════════════════════════════════════════════════════════
def __getattr__(name: str):
    """
    Module-level attribute access hook.

    Only fires for names NOT defined at module scope. Used to give a
    clear, actionable error for the one symbol we deliberately
    removed in REV 11.6 (`_requests_lock`), so a missed migration
    surfaces with instructions rather than a bare NameError.

    Any OTHER missing attribute gets the standard Python message,
    preserving the normal error contract for tools and callers.
    """
    if name == "_requests_lock":
        raise AttributeError(
            "core.client._requests_lock was REMOVED in REV 11.6. "
            "Migrate to the explicit split: "
            "_read_lock for read paths "
            "(positions / klines / account / tickers / open-orders / "
            "algo-order list / server time), "
            "_write_lock for write paths "
            "(futures_create_order / futures_cancel_order / "
            "futures_cancel_algo_order). "
            "See core/client.py docstring (REV 11.5–11.6)."
        )
    raise AttributeError(
        f"module {__name__!r} has no attribute {name!r}"
    )


_TS_LOCK = threading.Lock()
_TS_OFFSET = {'value': 0, 'time': 0.0}
_TS_TTL = 60.0

# REV 11.3 (A1) / REV 11.7 (A) — two pools, split by consequence of failure.
#
#   _TIMEOUT_EXECUTOR           — best-effort pool.
#     Used for: futures_account, exchangeInfo, get_open_orders probes,
#     and any other non-order call. 32 workers.
#
#   _CRITICAL_TIMEOUT_EXECUTOR  — order-placement pool.
#     Used for: futures_create_order (market entries, STOP_MARKET
#     SL, TAKE_PROFIT_MARKET TP1/TP2, SL updates, emergency closes).
#     8 workers.
#
#   REV 11.7: BOTH pools now floor the effective wait at the HTTP
#   session timeout (30s). Previously best-effort was capped at 6s,
#   which caused the CALLER to give up while the WORKER was still in
#   flight — still holding _read_lock for up to the full session
#   timeout. The cap only gave the illusion of a shorter wait.
#   Now: the caller waits the full session timeout and the worker is
#   guaranteed to have returned (or been killed by the socket layer)
#   before control returns. Bounded lock-hold time, honest wait.
_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="apicl"
)
_CRITICAL_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="apicrit"
)

# REV 11.7 — floor for ALL callers (both pools). Matches the HTTP
# session timeout configured in set_client_keys(). A caller that
# requests less is lifted to it; a caller that requests more is
# honored as-is.
_CRITICAL_MIN_TIMEOUT = 30.0

# REV 11.3 (A1) — desc prefixes that route to the critical pool.
# Matches the caller conventions in entry.py / manage.py / exit.py.
_CRITICAL_DESC_PREFIXES = (
    "market:",     # entry.py — market entry
    "sl:",         # entry.py — initial protective stop
    "tp1:",        # entry.py — take profit 1
    "tp2:",        # entry.py — take profit 2
    "update_sl:",  # manage.py — SL update / BE / trailing
    "emg:",        # defensive — reserved for emergency-close paths
)

# Best-effort pool warns at 8 in-flight (REV 11.2 behaviour).
_ZOMBIE_WARN_THRESHOLD = 8
# Critical pool warns earlier — a stuck ORDER is more consequential
# than a stuck klines fetch.
_CRITICAL_ZOMBIE_WARN_THRESHOLD = 3

_zombie_count = {'value': 0}
_zombie_lock = threading.Lock()
_critical_zombie_count = {'value': 0}
_critical_zombie_lock = threading.Lock()

# Binance error codes that indicate our cached filters / exchangeInfo
# are stale. Hit one of these → bust cache → retry once.
_BINANCE_FILTER_ERROR_CODES = frozenset({-4005, -1013, -1111, -4164, -2010})


def get_client() -> Optional[Client]:
    return _global_client


# ═════════════════════════════════════════════════════════════
#  REV 11.3 (A1) — CRITICAL-PATH CLASSIFIER
# ═════════════════════════════════════════════════════════════
def _is_critical_desc(desc: str) -> bool:
    """True if `desc` identifies an order-placement call."""
    if not desc:
        return False
    return desc.startswith(_CRITICAL_DESC_PREFIXES)


def _run_with_timeout(fn, timeout, desc="api_call"):
    """
    REV 11.3 (A1) / REV 11.7 (A) — split pools by consequence of
    failure. Both pools share the same effective-timeout floor.

    Effective timeout for BOTH pools:
        effective = max(timeout, _CRITICAL_MIN_TIMEOUT)   # ≥30s
    The floor equals the HTTP session timeout (set_client_keys),
    so we NEVER give up on a worker before the socket layer does.
    This applies to best-effort callers too — see REV 11.7 (A) note
    in the module docstring for why the old 6s cap was removed.

    Pool routing is unchanged:
      • desc starts with a _CRITICAL_DESC_PREFIXES entry → critical
        pool (8 workers). This is the order-placement path.
      • otherwise → best-effort pool (32 workers).

    Caller must name order-placement calls with one of the critical
    desc prefixes ("market:", "sl:", "tp1:", "tp2:", "update_sl:",
    "emg:"), otherwise they land in the best-effort pool. The pool
    only affects worker-pool sizing and zombie-warning threshold;
    the timeout floor is now identical for both.

    A timeout on a critical call means the order may still be in
    flight at the exchange. The retry path MUST resolve via
    _resolve_ambiguous_market (entry.py) or the deterministic-cid
    retry (exit.py). Since we wait the full session timeout, this
    class of false-ambiguous is effectively eliminated for healthy
    exchange responses.

    Exceptions propagate to the caller (TimeoutError or the wrapped
    function's exception).
    """
    is_critical = _is_critical_desc(desc)

    if is_critical:
        executor = _CRITICAL_TIMEOUT_EXECUTOR
        zombie_counter = _critical_zombie_count
        zombie_lock = _critical_zombie_lock
        warn_threshold = _CRITICAL_ZOMBIE_WARN_THRESHOLD
        pool_label = "critical"
    else:
        executor = _TIMEOUT_EXECUTOR
        zombie_counter = _zombie_count
        zombie_lock = _zombie_lock
        warn_threshold = _ZOMBIE_WARN_THRESHOLD
        pool_label = "best-effort"

    # REV 11.7 — both pools share the same floor. See module docstring.
    effective_timeout = max(timeout, _CRITICAL_MIN_TIMEOUT)

    fut = executor.submit(fn)

    def _on_done(_f):
        with zombie_lock:
            if zombie_counter['value'] > 0:
                zombie_counter['value'] -= 1

    try:
        return fut.result(timeout=effective_timeout)
    except concurrent.futures.TimeoutError:
        # Worker is still running despite the floor being == session
        # timeout. This is unexpected — means the socket layer did not
        # honor its own timeout (e.g. TCP half-open). Decrement when
        # it eventually lands.
        try:
            fut.add_done_callback(_on_done)
        except Exception:
            pass
        with zombie_lock:
            zombie_counter['value'] += 1
            n = zombie_counter['value']
        if n >= warn_threshold:
            logger.warning(
                f"[executor:{pool_label}] {n} in-flight task(s) after "
                f"timeout on '{desc}' (effective_timeout={effective_timeout:.1f}s). "
                f"HTTP session timeout did not fire — network layer issue?"
            )
        raise


def _clean_key(s: str) -> str:
    if not s:
        return ''
    return s.strip().strip('"').strip("'")


# ─────────────────────────────────────────────────────────────
# SESSION + TIMESTAMP
# ─────────────────────────────────────────────────────────────
def _install_pooled_session(client: Client) -> None:
    """
    REV 11.1 — GET-only retry. POST/PUT/DELETE are never auto-replayed
    (prevents double-submit on 503/-1007). 429 removed from forcelist
    (rate limits handled by retry_on_rate_limit with proper backoff).
    """
    try:
        old = client.session
        sess = requests.Session()
        sess.headers.update(old.headers)
        retry = Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset(['GET']),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry, pool_connections=32,
            pool_maxsize=32, pool_block=False,
        )
        sess.mount('https://', adapter)
        sess.mount('http://', adapter)
        client.session = sess
        logger.info("🔌 Pooled HTTP session installed (GET-only retry)")
    except Exception as e:
        logger.warning(f"Could not install pooled session: {e}")


def refresh_timestamp() -> None:
    if _global_client is None:
        return
    now = time.time()
    with _TS_LOCK:
        if _TS_OFFSET['time'] and now - _TS_OFFSET['time'] < _TS_TTL:
            _global_client.timestamp_offset = _TS_OFFSET['value']
            return
    try:
        # ── REV 11.5 — read lock (server-time query is a read). ──
        with _read_lock:
            server_time = _global_client.get_server_time()['serverTime']
        offset = server_time - int(time.time() * 1000)
        with _TS_LOCK:
            _TS_OFFSET['value'] = offset
            _TS_OFFSET['time'] = time.time()
        _global_client.timestamp_offset = offset
    except Exception as e:
        logger.debug(f"Timestamp refresh skipped: {e}")


# ─────────────────────────────────────────────────────────────
# RATE-LIMIT DECORATOR
# ─────────────────────────────────────────────────────────────
def retry_on_rate_limit(func=None, *, is_write: bool = False):
    """
    REV 11.1 — jitter + -1008 heavy backoff + Retry-After respect.

    REV 11.5 — upgraded to decorator-with-optional-args:
        @retry_on_rate_limit                    # read path (default)
        @retry_on_rate_limit(is_write=True)     # write path

    The lock (read or write) is RE-ENTERED per retry attempt. It is
    NOT held across the exponential-backoff sleep, so other readers /
    writers proceed between retries. The wrapper does not touch the
    '_read_lock vs _write_lock' decision itself — the decorator
    parameter decides which lock wraps each attempt.

    Existing usages (@retry_on_rate_limit on get_binance_klines and
    fetch_position_raw) are reads and need no change.
    """
    from functools import wraps

    lock = _write_lock if is_write else _read_lock

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            max_retries = 5
            for attempt in range(max_retries):
                try:
                    with lock:
                        return fn(*args, **kwargs)
                except BinanceAPIException as e:
                    if e.code not in (-1003, -1008):
                        raise

                    retry_after = None
                    try:
                        resp = getattr(e, 'response', None)
                        if resp is not None:
                            ra = resp.headers.get('Retry-After')
                            if ra:
                                retry_after = float(ra)
                    except Exception:
                        retry_after = None

                    base = retry_after if retry_after is not None else (2 ** attempt)
                    if e.code == -1008 and retry_after is None:
                        base *= 2
                    jitter = random.uniform(0, 0.5)
                    sleep_time = min(base + jitter, 30.0)

                    logger.warning(
                        f"Rate limit {e.code}, retrying in {sleep_time:.2f}s "
                        f"({attempt + 1}/{max_retries}) on {fn.__name__}"
                    )
                    time.sleep(sleep_time)
                    try:
                        refresh_timestamp()
                    except Exception:
                        pass
                    continue
            raise RuntimeError(f"Rate limit retries exhausted for {fn.__name__}")

        return wrapper

    # Support both @retry_on_rate_limit and @retry_on_rate_limit(is_write=...)
    if func is None:
        return decorator
    return decorator(func)


# ─────────────────────────────────────────────────────────────
# ACCOUNT CACHE
# ─────────────────────────────────────────────────────────────
_ACCOUNT_CACHE = {'data': None, 'time': 0.0}
_ACCOUNT_CACHE_LOCK = threading.Lock()
_LAST_GOOD_BAL = {'value': None, 'time': 0.0}
_LAST_GOOD_BAL_LOCK = threading.Lock()
_LAST_GOOD_BAL_MAX_AGE = 300

# REV 11.2 — 20s → 60s. Demo API is ~10x production latency; 60s stale
# equity is acceptable for risk gates (per-position mark-to-market is
# computed from the position payload, not this cache).
_ACCOUNT_CACHE_TTL = 60.0


def get_account_cached() -> dict:
    """
    REV 11.2 — TTL 60s.
    REV 11.3 (A1) — non-critical; runs on best-effort pool.
    REV 11.5 — account fetch is a read → _read_lock.
    REV 11.7 — best-effort floor is now the full session timeout
    (30s), not 6s. See module docstring REV 11.7 (A).
    """
    with _ACCOUNT_CACHE_LOCK:
        if _ACCOUNT_CACHE['data'] is not None and \
                time.time() - _ACCOUNT_CACHE['time'] < _ACCOUNT_CACHE_TTL:
            return _ACCOUNT_CACHE['data']

    if _global_client is None:
        raise RuntimeError("get_account_cached: client not initialised")

    def _fetch():
        with _read_lock:
            return _global_client.futures_account()

    acc = _run_with_timeout(_fetch, 6, "futures_account")
    with _ACCOUNT_CACHE_LOCK:
        _ACCOUNT_CACHE['data'] = acc
        _ACCOUNT_CACHE['time'] = time.time()
    return acc


def invalidate_account_cache() -> None:
    with _ACCOUNT_CACHE_LOCK:
        _ACCOUNT_CACHE['data'] = None


def get_balance_or_last_good() -> tuple[float, bool]:
    try:
        acc = get_account_cached()
        bal = float(acc['totalWalletBalance']) + \
            float(acc.get('totalUnrealizedProfit', 0))
        with _LAST_GOOD_BAL_LOCK:
            _LAST_GOOD_BAL['value'] = bal
            _LAST_GOOD_BAL['time'] = time.time()
        return bal, False
    except Exception as e:
        with _LAST_GOOD_BAL_LOCK:
            v, t = _LAST_GOOD_BAL['value'], _LAST_GOOD_BAL['time']
        if v is not None and (time.time() - t) < _LAST_GOOD_BAL_MAX_AGE:
            logger.warning(
                f"Balance fetch failed ({type(e).__name__}); "
                f"last-good ${v:.2f}"
            )
            return v, True
        raise


# ─────────────────────────────────────────────────────────────
# EXCHANGE INFO + SYMBOLS
# ─────────────────────────────────────────────────────────────
_EXCHANGE_INFO_CACHE = {'data': None, 'time': 0.0}
_EXCHANGE_INFO_LOCK = threading.Lock()
_EXCHANGE_INFO_TTL = 600
VALID_SYMBOLS: set[str] = set()
_VALID_SYMBOLS_LOCK = threading.Lock()


def invalidate_exchange_info() -> None:
    with _EXCHANGE_INFO_LOCK:
        _EXCHANGE_INFO_CACHE['data'] = None
        _EXCHANGE_INFO_CACHE['time'] = 0.0
    with _VALID_SYMBOLS_LOCK:
        VALID_SYMBOLS.clear()


def _get_exchange_info() -> dict:
    now = time.time()
    with _EXCHANGE_INFO_LOCK:
        cached = _EXCHANGE_INFO_CACHE['data']
        if cached is not None and \
                now - _EXCHANGE_INFO_CACHE['time'] < _EXCHANGE_INFO_TTL:
            return cached

    if _global_client is None:
        raise RuntimeError("_get_exchange_info: client not initialised")

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            def _fetch():
                # REV 11.5 — read.
                with _read_lock:
                    return _global_client.futures_exchange_info()
            # REV 11.3 (A1) — non-critical desc → best-effort pool.
            # REV 11.7 — effective timeout now 30s (session timeout floor).
            info = _run_with_timeout(_fetch, 6, "exchange_info")
            with _EXCHANGE_INFO_LOCK:
                _EXCHANGE_INFO_CACHE['data'] = info
                _EXCHANGE_INFO_CACHE['time'] = time.time()
            return info
        except concurrent.futures.TimeoutError as e:
            last_err = e
            logger.warning(f"exchangeInfo timeout (attempt {attempt + 1}/3)")
        except Exception as e:
            last_err = e
            logger.warning(
                f"exchangeInfo error (attempt {attempt + 1}/3): "
                f"{type(e).__name__}: {e}"
            )
        if attempt < 2:
            time.sleep((1.0 + attempt * 0.5) + random.uniform(0, 0.3))

    raise last_err or RuntimeError("exchangeInfo fetch failed after 3 attempts")


def validate_symbols(coins: list[str]) -> list[str]:
    global VALID_SYMBOLS
    try:
        with _VALID_SYMBOLS_LOCK:
            snapshot = set(VALID_SYMBOLS)

        if snapshot:
            valid = [c for c in coins if c in snapshot]
            invalid = [c for c in coins if c not in snapshot]
            if invalid:
                logger.warning(f"Skipping invalid symbols (cached): {invalid}")
            return valid

        logger.info("🔍 Validating symbols (cached exchangeInfo)...")
        info = _get_exchange_info()
        fresh = {
            s['symbol'] for s in info.get('symbols', [])
            if s.get('status') == 'TRADING'
        }
        with _VALID_SYMBOLS_LOCK:
            VALID_SYMBOLS = fresh
            snapshot = set(VALID_SYMBOLS)

        valid = [c for c in coins if c in snapshot]
        invalid = [c for c in coins if c not in snapshot]
        if invalid:
            logger.warning(f"Skipping invalid symbols: {invalid}")
        logger.info(f"✅ Validated {len(valid)} symbols")
        return valid
    except concurrent.futures.TimeoutError:
        logger.warning(
            "⚠️ Validation timeout — using coins as-is except known bad"
        )
        invalid_demo = {'MATICUSDT', 'LUNAUSDT'}
        valid = [c for c in coins if c not in invalid_demo]
        with _VALID_SYMBOLS_LOCK:
            VALID_SYMBOLS = set(valid)
        return valid
    except Exception as e:
        logger.error(f"Validation error: {e}")
        invalid_demo = {'MATICUSDT', 'LUNAUSDT'}
        valid = [c for c in coins if c not in invalid_demo]
        with _VALID_SYMBOLS_LOCK:
            VALID_SYMBOLS = set(valid)
        return valid


def get_bot_coins() -> list[str]:
    return list(CONFIG.bot_coins)


# ─────────────────────────────────────────────────────────────
# FILTERS
# ─────────────────────────────────────────────────────────────
HARDCODED_FILTERS = {
    'BTCUSDT':       {'stepSize': Decimal('0.001'), 'minQty': Decimal('0.001'), 'tickSize': Decimal('0.1'),       'minNotional': 5.0},
    'ETHUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'BNBUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'LTCUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'TONUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'SOLUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'SUIUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'AVAXUSDT':      {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'FILUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'NEARUSDT':      {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'APTUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'INJUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'TIAUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'SEIUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'AAVEUSDT':      {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'UNIUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'RENDERUSDT':    {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'TAOUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'JUPUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'FETUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'LINKUSDT':      {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'ADAUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'DOTUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'ATOMUSDT':      {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    'ARBUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'POLUSDT':       {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'OPUSDT':        {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'TRXUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.00001'),   'minNotional': 5.0},
    'DOGEUSDT':      {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.00001'),   'minNotional': 5.0},
    'XRPUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    'ZECUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.01'),      'minNotional': 5.0},
    'ETCUSDT':       {'stepSize': Decimal('0.01'),  'minQty': Decimal('0.01'),  'tickSize': Decimal('0.001'),     'minNotional': 5.0},
    '1000PEPEUSDT':  {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
    '1000SHIBUSDT':  {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
    'WIFUSDT':       {'stepSize': Decimal('0.1'),   'minQty': Decimal('0.1'),   'tickSize': Decimal('0.0001'),    'minNotional': 5.0},
    '1000BONKUSDT':  {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
    '1000FLOKIUSDT': {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
    'BOMEUSDT':      {'stepSize': Decimal('1'),     'minQty': Decimal('1'),     'tickSize': Decimal('0.0000001'), 'minNotional': 5.0},
}

_DEFAULT_MAX_QTY = Decimal('1000000000')

FILTERS_CACHE: dict[str, dict] = {}
FILTERS_CACHE_LOCK = threading.Lock()
FILTERS_UNVERIFIED: dict[str, int] = {}
FILTERS_UNVERIFIED_LOCK = threading.Lock()

_FILTERS_CACHE_TTL = 600


def _mark_unverified(pair: str) -> None:
    with FILTERS_UNVERIFIED_LOCK:
        FILTERS_UNVERIFIED[pair] = FILTERS_UNVERIFIED.get(pair, 0) + 1


def _clear_unverified(pair: str) -> None:
    with FILTERS_UNVERIFIED_LOCK:
        FILTERS_UNVERIFIED.pop(pair, None)


def _filters_ok_to_trade(pair: str) -> bool:
    with FILTERS_UNVERIFIED_LOCK:
        return FILTERS_UNVERIFIED.get(pair, 0) < 2


def invalidate_filters(pair: Optional[str] = None) -> None:
    with FILTERS_CACHE_LOCK:
        if pair is None:
            FILTERS_CACHE.clear()
        else:
            FILTERS_CACHE.pop(pair, None)
    with FILTERS_UNVERIFIED_LOCK:
        if pair is None:
            FILTERS_UNVERIFIED.clear()
        else:
            FILTERS_UNVERIFIED.pop(pair, None)
    invalidate_exchange_info()


def handle_order_filter_error(pair: str, exc: BinanceAPIException) -> bool:
    code = getattr(exc, 'code', None)
    if code in _BINANCE_FILTER_ERROR_CODES:
        logger.warning(
            f"[filters] {pair} → invalidating cache on Binance error "
            f"{code}: {exc}"
        )
        invalidate_filters(pair)
        return True
    return False


def _fallback_filters(pair: str, now: float, why: str = "") -> dict:
    _mark_unverified(pair)
    if pair in HARDCODED_FILTERS:
        base = HARDCODED_FILTERS[pair]
        result = {
            'stepSize': base['stepSize'],
            'minQty': base['minQty'],
            'maxQty': base.get('maxQty', _DEFAULT_MAX_QTY),
            'tickSize': base['tickSize'],
            'minNotional': base['minNotional'],
            'timestamp': now,
            'verified': False,
        }
        logger.warning(
            f"⚠️ Filters for {pair} HARDCODED fallback ({why}) — entries blocked"
        )
    else:
        result = {
            'stepSize': Decimal('0.01'),
            'minQty': Decimal('0.01'),
            'maxQty': _DEFAULT_MAX_QTY,
            'tickSize': Decimal('0.01'),
            'minNotional': 5.0,
            'timestamp': now,
            'verified': False,
        }
        logger.warning(
            f"⚠️ Unknown {pair} - default filters ({why}) — entries blocked"
        )
    with FILTERS_CACHE_LOCK:
        FILTERS_CACHE[pair] = result
    return result


def get_filters(pair: str) -> dict:
    now = time.time()
    with FILTERS_CACHE_LOCK:
        cached = FILTERS_CACHE.get(pair)
        if cached and now - cached['timestamp'] < _FILTERS_CACHE_TTL \
                and cached.get('verified', False):
            return cached
    try:
        info = _get_exchange_info()
        symbols = info.get('symbols', []) if isinstance(info, dict) else []
        sym = next((s for s in symbols if s.get('symbol') == pair), None)
        if sym is None:
            raise ValueError(f"symbol {pair} not in exchangeInfo")

        status = sym.get('status', 'TRADING')
        if status != 'TRADING':
            raise ValueError(f"symbol {pair} not tradable (status={status})")

        lot_f = [f for f in sym['filters'] if f['filterType'] == 'LOT_SIZE']
        m_lot = [f for f in sym['filters']
                 if f['filterType'] == 'MARKET_LOT_SIZE']
        lot = m_lot[0] if m_lot else (lot_f[0] if lot_f else None)
        price_f = next(
            (f for f in sym['filters'] if f['filterType'] == 'PRICE_FILTER'),
            None,
        )
        notional = next(
            (f for f in sym['filters'] if f['filterType'] == 'NOTIONAL'),
            None,
        ) or next(
            (f for f in sym['filters'] if f['filterType'] == 'MIN_NOTIONAL'),
            None,
        )

        if not lot or not price_f:
            raise ValueError(f"missing filters for {pair}")

        max_qty_raw = lot.get('maxQty', '1000000000')
        try:
            max_qty = Decimal(str(max_qty_raw))
        except Exception:
            max_qty = _DEFAULT_MAX_QTY

        result = {
            'stepSize': Decimal(lot['stepSize']),
            'minQty': Decimal(lot['minQty']),
            'maxQty': max_qty,
            'tickSize': Decimal(price_f['tickSize']),
            'minNotional': float(notional['notional']) if notional else 5.0,
            'timestamp': now,
            'verified': True,
        }
        with FILTERS_CACHE_LOCK:
            FILTERS_CACHE[pair] = result
        _clear_unverified(pair)
        logger.info(
            f"✅ Filters for {pair} (API): step={result['stepSize']} "
            f"tick={result['tickSize']} maxQty={result['maxQty']}"
        )
        return result
    except concurrent.futures.TimeoutError:
        return _fallback_filters(pair, now, "exchangeInfo timeout")
    except Exception as e:
        logger.warning(f"Filter fetch failed for {pair}: {e}")
        return _fallback_filters(pair, now, "api error")


# ─────────────────────────────────────────────────────────────
# DECIMAL ADJUSTMENT
# ─────────────────────────────────────────────────────────────
def adjust_qty(qty, step, min_qty) -> str:
    """
    Floor `qty` to `step`, clamp to `min_qty`, and format with the
    step's decimal precision.

    REV 11.7 (B) — the fallback path (on any exception in the primary
    path) now re-formats via Decimal using step's exponent, matching
    the primary path's output contract. Previously it returned
    str(min_qty) verbatim, which could violate Binance's strict
    precision rules (e.g. min_qty=Decimal('0.0010') → "0.0010" while
    the exchange expects "0.001") and trigger -1111.
    """
    try:
        step_dec = Decimal(str(step))
        qty_d = (Decimal(str(qty)) // step_dec) * step_dec
        if qty_d < min_qty:
            qty_d = min_qty
        exp = step_dec.as_tuple().exponent
        if exp < 0:
            return format(qty_d, f'.{abs(exp)}f')
        return format(qty_d, 'f')
    except Exception as e:
        # ── REV 11.7 (B) — fallback preserves step precision ──
        logger.warning(f"adjust_qty error {e}")
        try:
            step_dec = Decimal(str(step))
            exp = step_dec.as_tuple().exponent
            mq = Decimal(str(min_qty))
            if exp < 0:
                return format(mq, f'.{abs(exp)}f')
            return format(mq, 'f')
        except Exception:
            # Both primary and fallback failed — return the raw value.
            # At this point the caller is in a state where nothing is
            # safe; the exchange will reject with -1111 and the retry
            # path (handle_order_filter_error → invalidate_filters)
            # will fetch fresh filters.
            return str(min_qty)


def adjust_price(price, tick) -> str:
    """
    Floor `price` to `tick` and format with the tick's decimal
    precision.

    REV 11.7 (B) — the fallback path (on any exception in the primary
    path) now re-formats via Decimal using tick's exponent, matching
    the primary path's output contract. Previously it returned
    str(price) verbatim, which could violate Binance's strict
    precision rules and trigger -1111.
    """
    try:
        tick_dec = Decimal(str(tick))
        price_d = (Decimal(str(price)) // tick_dec) * tick_dec
        exp = tick_dec.as_tuple().exponent
        if exp < 0:
            return format(price_d, f'.{abs(exp)}f')
        return format(price_d, 'f')
    except Exception as e:
        # ── REV 11.7 (B) — fallback preserves tick precision ──
        logger.warning(f"adjust_price error {e}")
        try:
            tick_dec = Decimal(str(tick))
            exp = tick_dec.as_tuple().exponent
            p = Decimal(str(price))
            if exp < 0:
                return format(p, f'.{abs(exp)}f')
            return format(p, 'f')
        except Exception:
            return str(price)


# ─────────────────────────────────────────────────────────────
# KLINES
# ─────────────────────────────────────────────────────────────
_KLINES_TTL: dict[str, float] = {
    '1m':  20,
    '3m':  30,
    '5m':  30,
    '15m': 60,
    '30m': 120,
    '1h':  120,
    '2h':  240,
    '4h':  300,
    '6h':  600,
    '8h':  600,
    '12h': 900,
    '1d':  900,
    '3d':  1800,
    '1w':  3600,
    '1M':  3600,
}
_KLINES_TTL_DEFAULT = 60.0

_KLINES_CACHE: dict[tuple[str, str, int], tuple[pd.DataFrame, float]] = {}
_KLINES_CACHE_LOCK = threading.Lock()
_KLINES_CACHE_MAX = 1000


def _klines_cache_get(symbol: str, interval: str,
                      limit: int) -> Optional[pd.DataFrame]:
    key = (symbol, interval, limit)
    ttl = _KLINES_TTL.get(interval, _KLINES_TTL_DEFAULT)
    now = time.time()
    with _KLINES_CACHE_LOCK:
        hit = _KLINES_CACHE.get(key)
        if hit is not None and now - hit[1] < ttl:
            return hit[0]
    return None


def _klines_cache_set(symbol: str, interval: str,
                      limit: int, df: pd.DataFrame) -> None:
    key = (symbol, interval, limit)
    with _KLINES_CACHE_LOCK:
        _KLINES_CACHE[key] = (df, time.time())
        if len(_KLINES_CACHE) > _KLINES_CACHE_MAX:
            now = time.time()
            for k in list(_KLINES_CACHE.keys()):
                _, ts = _KLINES_CACHE[k]
                if now - ts > 3600:
                    del _KLINES_CACHE[k]


def invalidate_klines_cache(symbol: Optional[str] = None) -> None:
    with _KLINES_CACHE_LOCK:
        if symbol is None:
            _KLINES_CACHE.clear()
        else:
            for k in list(_KLINES_CACHE.keys()):
                if k[0] == symbol:
                    del _KLINES_CACHE[k]


# REV 11.5 — @retry_on_rate_limit defaults to read path (_read_lock).
@retry_on_rate_limit
def get_binance_klines(symbol: str, interval: str, limit: int = 250):
    cached = _klines_cache_get(symbol, interval, limit)
    if cached is not None:
        return cached

    try:
        klines = _global_client.futures_klines(
            symbol=symbol, interval=interval, limit=limit
        )
        if not klines:
            return None
        df = pd.DataFrame(klines, columns=[
            'Open Time', 'Open', 'High', 'Low', 'Close', 'Volume',
            'Close Time', 'QAV', 'NOT', 'TBBAV', 'TBQAV', 'I'
        ])
        for c in ['Open', 'High', 'Low', 'Close', 'Volume',
                  'QAV', 'TBBAV', 'TBQAV']:
            df[c] = pd.to_numeric(df[c], errors='coerce').astype(float)
        df['Datetime'] = pd.to_datetime(df['Open Time'], unit='ms')
        df.set_index('Datetime', inplace=True)
        _klines_cache_set(symbol, interval, limit, df)
        return df
    except BinanceAPIException as e:
        if e.code in (-1003, -1008):
            raise
        logger.debug(f"Klines error {symbol} {interval}: {type(e).__name__}")
        return None
    except Exception as e:
        logger.debug(f"Klines error {symbol} {interval}: {type(e).__name__}")
        return None


# ─────────────────────────────────────────────────────────────
# LIVE PRICES
# ─────────────────────────────────────────────────────────────
def get_live_prices() -> dict[str, float]:
    """Fetch all futures tickers in ONE call → {symbol: price} map."""
    if _global_client is None:
        return {}
    try:
        # REV 11.5 — read.
        with _read_lock:
            tickers = _global_client.futures_symbol_ticker()
        if not tickers:
            return {}
        return {
            t['symbol']: float(t['price'])
            for t in tickers
            if t.get('symbol') and t.get('price') is not None
        }
    except Exception as e:
        logger.debug(f"get_live_prices failed: {type(e).__name__}: {e}")
        return {}


# ─────────────────────────────────────────────────────────────
# BOOK TICKER / SPREAD
# ─────────────────────────────────────────────────────────────
_BOOK_TICKER_CACHE: dict[str, dict] = {}
_BOOK_TICKER_LOCK = threading.Lock()
_BOOK_TICKER_TTL = 3.0


def get_book_ticker(symbol: str) -> dict:
    now = time.time()
    with _BOOK_TICKER_LOCK:
        hit = _BOOK_TICKER_CACHE.get(symbol)
        if hit and now - hit['time'] < _BOOK_TICKER_TTL:
            return {'bid': hit['bid'], 'ask': hit['ask']}

    if _global_client is None:
        return {}
    try:
        # REV 11.5 — read.
        with _read_lock:
            resp = _global_client.futures_orderbook_ticker(symbol=symbol)
        if not resp:
            return {}
        bid = float(resp.get('bidPrice', 0) or 0)
        ask = float(resp.get('askPrice', 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            return {}
        with _BOOK_TICKER_LOCK:
            _BOOK_TICKER_CACHE[symbol] = {
                'bid': bid, 'ask': ask, 'time': now
            }
        return {'bid': bid, 'ask': ask}
    except Exception as e:
        logger.debug(f"book_ticker {symbol}: {type(e).__name__}")
        return {}


def get_book_tickers() -> dict[str, dict]:
    if _global_client is None:
        return {}
    try:
        # REV 11.5 — read.
        with _read_lock:
            tickers = _global_client.futures_orderbook_ticker()
        if not tickers:
            return {}
        out: dict[str, dict] = {}
        for t in tickers:
            sym = t.get('symbol')
            if not sym:
                continue
            try:
                bid = float(t.get('bidPrice', 0) or 0)
                ask = float(t.get('askPrice', 0) or 0)
                if bid > 0 and ask > 0 and ask >= bid:
                    out[sym] = {'bid': bid, 'ask': ask}
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        logger.debug(f"get_book_tickers failed: {type(e).__name__}: {e}")
        return {}


def calc_spread_pct(bid: float, ask: float) -> float:
    try:
        bid_f = float(bid)
        ask_f = float(ask)
        if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
            return 0.0
        mid = (bid_f + ask_f) / 2.0
        if mid <= 0:
            return 0.0
        return (ask_f - bid_f) / mid * 100.0
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────────────────────────────────────
# POSITIONS
# ─────────────────────────────────────────────────────────────
# REV 11.5 — @retry_on_rate_limit defaults to read path (_read_lock).
@retry_on_rate_limit
def fetch_position_raw(symbol_short: str):
    pair = symbol_short + 'USDT'
    refresh_timestamp()
    for i in range(3):
        try:
            arr = _global_client.futures_position_information(symbol=pair)
            if arr and len(arr) > 0:
                return arr[0]
            return {
                'symbol': pair, 'positionAmt': '0', 'entryPrice': '0',
                'markPrice': '0', 'unRealizedProfit': '0',
            }
        except BinanceAPIException as e:
            if e.code in (-1003, -1008):
                raise
            if i == 2:
                logger.debug(
                    f"fetch_position_raw {symbol_short} failed: "
                    f"{type(e).__name__}"
                )
                return None
            time.sleep(0.6 + i * 0.4)
        except Exception as e:
            if i == 2:
                logger.debug(
                    f"fetch_position_raw {symbol_short} failed: "
                    f"{type(e).__name__}"
                )
                return None
            time.sleep(0.6 + i * 0.4)
    return None


def _position_amt(pair: str, retries: int = 3):
    refresh_timestamp()
    for attempt in range(retries):
        try:
            # REV 11.5 — read.
            with _read_lock:
                arr = _global_client.futures_position_information(symbol=pair)
            if arr:
                return float(arr[0].get('positionAmt', 0) or 0)
            return 0.0
        except BinanceAPIException as e:
            if e.code in (-1003, -1008):
                raise
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return None


# ─────────────────────────────────────────────────────────────
# ALGO ORDERS
# ─────────────────────────────────────────────────────────────
_REQUIRED_SDK_METHODS = (
    'futures_get_open_algo_orders',
    'futures_cancel_algo_order',
)


def assert_sdk_methods() -> None:
    if _global_client is None:
        raise RuntimeError("assert_sdk_methods: client not initialised")
    missing = [m for m in _REQUIRED_SDK_METHODS
               if getattr(_global_client, m, None) is None]
    if missing:
        raise RuntimeError(
            f"Installed python-binance is missing required method(s): "
            f"{', '.join(missing)}. Pin a supported version in "
            f"requirements.txt (see REV 11.1 note)."
        )
    logger.info("✅ SDK algo-order methods present")


def _get_open_algo_orders(pair: str) -> Optional[list]:
    """
    Fetch the list of open algo orders for `pair`.

    Return contract (REV 11.8 — H5b):

      • list of dicts — the exchange returned a (possibly empty) list.
                        An empty list means "no algo orders exist" —
                        a valid, trustworthy state.

      • None          — the fetch itself failed. The true state is
                        UNKNOWN. Callers MUST NOT treat None as
                        "no orders exist".

    REV 11.8 rationale:
      Previously this function returned [] on any exception. That made
      a failed fetch indistinguishable from an empty book, and callers
      read it as "no protective stops exist". Concrete consequences:

        • exit.py::robust_cancel_all could log "All orphaned orders
          cancelled" when it could not read the algo-order book —
          the check `if not remaining and not remaining_algo` was
          True both for "empty" and for "unreadable".
        • repair.py orphan-cancel paths could report 0 cancelled when
          they had silently skipped every check.
        • manage.py TP1-detection could conclude "no TP1 open" (i.e.
          TP1 filled) from a failed fetch and promote SL to BE
          prematurely.

      The new contract separates "empty" from "unknown". Callers must
      branch:

          _algo = _get_open_algo_orders(pair)
          if _algo is None:
              # unknown — do NOT assume empty. Leave state alone,
              # retry next cycle, or escalate.
              logger.warning(f"[{pair}] algo-order list unreadable")
              return  # or continue / fail-closed as appropriate
          for o in _algo:
              ...

      Existing callers that do `for o in _get_open_algo_orders(pair):`
      inside a try/except will get a TypeError from iterating None,
      which is caught — so the bot does not crash. Behaviour for those
      callers is effectively unchanged (silent skip) until they are
      migrated to the None-aware contract above.
    """
    getter = getattr(_global_client, 'futures_get_open_algo_orders', None)
    if getter is None:
        logger.error(
            "[algo] futures_get_open_algo_orders missing on client — "
            "call assert_sdk_methods() at startup"
        )
        # REV 11.8 — SDK method missing is UNKNOWN, not empty.
        return None
    try:
        # REV 11.5 — listing algo orders is a read.
        with _read_lock:
            resp = getter(symbol=pair)
        if isinstance(resp, dict):
            return resp.get('orders') or resp.get('data') or []
        return resp or []
    except Exception as e:
        logger.debug(f"algo orders {pair}: {e}")
        # REV 11.8 — fetch failed is UNKNOWN, not empty.
        return None


def _cancel_algo_order(pair: str, algo_id) -> bool:
    cancel = getattr(_global_client, 'futures_cancel_algo_order', None)
    if cancel is None or not algo_id:
        return False
    try:
        # REV 11.5 — cancelling an order is a WRITE.
        with _write_lock:
            cancel(symbol=pair, algoId=algo_id)
        return True
    except Exception as e:
        logger.warning(f"algo cancel {algo_id} {pair}: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# CLIENT SETUP
# ─────────────────────────────────────────────────────────────
def check_position_mode() -> Optional[bool]:
    """
    Query the account's position mode.

    Returns:
      True  → HEDGE MODE (dualSidePosition=True). Callers MUST fail.
      False → ONE-WAY mode. Safe to trade.
      None  → the check itself failed (network / API error). Not a
              HEDGE verdict — do NOT treat as a hard fail; the caller
              can log and proceed, since a transient API hiccup should
              not block startup. Binance will reject orders with a
              specific error if the mode is actually HEDGE and we
              missed it here.

    REV 11.4 — same return contract, but set_client_keys() and
    startup_checks() now RAISE on True instead of ignoring the value.
    REV 11.5 — read lock.
    """
    if _global_client is None:
        return None
    try:
        with _read_lock:
            mode = _global_client.futures_get_position_mode()
        dual = bool(mode.get('dualSidePosition', False))
        if dual:
            logger.critical(
                "❌ Account is in HEDGE MODE (dualSidePosition=True). "
                "All orders will fail. Set one-way mode before trading."
            )
        else:
            logger.info("✅ Account position mode: ONE-WAY")
        return dual
    except Exception as e:
        logger.warning(f"Position-mode check failed: {e}")
        return None


def set_client_keys(api_key: str, api_secret: str) -> Client:
    """
    Initialise the Binance client with the given credentials.

    REV 11.4 — HEDGE mode is now a HARD FAIL. Previously the mode
    check ran but its result was discarded, so the bot would boot
    into HEDGE mode and every subsequent order failed with cryptic
    codes. Now a HEDGE account raises RuntimeError immediately with
    a clear, actionable message.
    """
    global _global_client
    api_key = _clean_key(api_key)
    api_secret = _clean_key(api_secret)
    if len(api_key) < 20 or len(api_secret) < 20:
        logger.critical(
            f"❌ API key/secret too short "
            f"(key={len(api_key)}, secret={len(api_secret)})"
        )
        raise ValueError("API key/secret too short")

    with _CLIENT_INIT_LOCK:
        client_params = {'requests_params': {'timeout': 30}}
        if CONFIG.demo_mode:
            _global_client = Client(api_key, api_secret, demo=True, **client_params)
            logger.info("DEMO MODE ACTIVE - demo.binance.com")
        elif CONFIG.testnet:
            _global_client = Client(api_key, api_secret, testnet=True, **client_params)
            logger.info("TESTNET MODE ACTIVE - testnet.binance.vision")
        else:
            _global_client = Client(api_key, api_secret, **client_params)
            logger.warning("MAINNET MODE - REAL MONEY!")
        _global_client.recvWindow = 15000   # <--- YEH LINE ADD KAREIN
        _install_pooled_session(_global_client)

    refresh_timestamp()

    try:
        assert_sdk_methods()
    except Exception as e:
        logger.critical(f"SDK assertion failed: {e}")
        raise

    # ── REV 11.4 — HEDGE mode is a hard fail ──
    # check_position_mode() returns:
    #   True  → HEDGE, we MUST abort
    #   False → ONE-WAY, proceed
    #   None  → transient check failure; log & proceed (Binance will
    #           reject orders with -4061 if HEDGE is actually active,
    #           and the caller will see that error directly).
    _dual = check_position_mode()
    if _dual is True:
        raise RuntimeError(
            "Account is in HEDGE mode (dualSidePosition=True). "
            "All orders will fail with -4061. Switch the account to "
            "ONE-WAY mode in Binance UI before starting the bot."
        )

    if not CONFIG.telegram_enabled or not CONFIG.telegram_bot_token:
        logger.warning(
            "⚠️ Telegram disabled or token empty — CRITICAL alerts will "
            "NOT be delivered. Fix .env before live trading."
        )

    send_telegram(
        f"✅ Bot started (Demo: {CONFIG.demo_mode}, Testnet: {CONFIG.testnet})"
    )
    return _global_client


def _validate_api_credentials(client: Client) -> tuple[bool, float]:
    try:
        acc = client.futures_account()
        bal = float(acc.get('totalWalletBalance', 0))
        logger.info(f"✅ API credentials OK — Wallet: ${bal:.2f}")
        return True, bal
    except BinanceAPIException as e:
        if e.code == -2014:
            logger.critical(
                "❌ API-key format invalid (-2014). Check .env has no "
                "whitespace/quotes."
            )
        elif e.code == -2015:
            logger.critical(
                f"❌ API key invalid/disabled/IP-restricted: {e}"
            )
        else:
            logger.critical(f"❌ API validation failed: {e}")
        return False, 0.0
    except Exception as e:
        logger.critical(f"❌ API validation unexpected error: {e}")
        return False, 0.0


def startup_checks() -> None:
    """
    Startup sanity checks. Called by future.py during CLI bootstrap.

    REV 11.4 — HEDGE mode is a HARD FAIL here too, matching
    set_client_keys(). Same rationale: never let the bot run with
    a position mode that makes every order fail at the exchange.
    """
    if _global_client is None:
        raise RuntimeError("startup_checks: client not initialised")
    assert_sdk_methods()

    _dual = check_position_mode()
    if _dual is True:
        raise RuntimeError(
            "Account is in HEDGE mode (dualSidePosition=True). "
            "Switch to ONE-WAY mode before starting the bot."
        )

    if not CONFIG.telegram_enabled or not CONFIG.telegram_bot_token:
        logger.warning(
            "⚠️ Telegram alerts disabled — CRITICAL events will be silent."
        )


# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────
def send_telegram(msg: str) -> None:
    if not CONFIG.telegram_enabled or not CONFIG.telegram_bot_token:
        return
    try:
        import html as _html
        url = (
            f"https://api.telegram.org/bot{CONFIG.telegram_bot_token}"
            f"/sendMessage"
        )
        safe_msg = _html.escape(msg)
        data = {
            "chat_id": CONFIG.telegram_chat_id,
            "text": safe_msg,
            "parse_mode": "HTML",
        }
        requests.post(url, data=data, timeout=5)
    except requests.exceptions.Timeout:
        logger.warning("Telegram timeout")
    except Exception as e:
        logger.warning(f"Telegram error: {e}")