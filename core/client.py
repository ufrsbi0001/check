"""
client.py — Binance Futures client, exchange metadata, filters, klines,
positions, account cache, Telegram. Lowest layer of the trading engine.

Does NOT know about strategies or trade lifecycle.

REV 11.3 (2026-10-04) — CRITICAL-PATH TIMEOUT ISOLATION (A1):
  ✅ CRITICAL (A1): _run_with_timeout() no longer caps ORDER-PLACEMENT
     calls at 6s. Order placement (market:, sl:, tp1:, tp2:,
     update_sl:, emg:) now runs on a SEPARATE executor with NO upper
     cap and a MINIMUM effective timeout of 10s (matching the HTTP
     session timeout). Previously a 6s caller cap on futures_create_order
     could time out at 6s while the underlying HTTP request was still
     in flight — the caller then treated the order as ambiguous, and
     the retry path (in entry.py / exit.py) risked placing a duplicate
     MARKET order. Raising the effective minimum to the HTTP session
     timeout eliminates that class of false-ambiguous.
  ✅ Two pools:
       _TIMEOUT_EXECUTOR          — best-effort (analytics, klines,
                                    exchangeInfo). 6s cap. 32 workers.
       _CRITICAL_TIMEOUT_EXECUTOR — order placement + SL/TP. No cap
                                    (but floor of 10s). 8 workers.
     This prevents a slow exchangeInfo / analytics call from starving
     order placement slots under the same pool.
  ✅ Separate zombie counters per pool. Order-path zombie growth is
     logged at a lower threshold (3) than best-effort (8) because a
     stuck order placement is far more consequential than a stuck
     klines fetch.
  ✅ _is_critical_desc() helper classifies callers by desc prefix.
  ✅ Callers that pass <10s (entry.py's 6s, manage.py's 5s, etc.)
     are automatically lifted to the 10s floor. Callers that pass
     >10s are honored as-is. Best-effort callers are unchanged.

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
_requests_lock = threading.RLock()
_TS_LOCK = threading.Lock()
_TS_OFFSET = {'value': 0, 'time': 0.0}
_TS_TTL = 60.0

# REV 11.3 (A1) — two pools, split by consequence of failure.
#
#   _TIMEOUT_EXECUTOR           — best-effort pool.
#     Used for: futures_account, exchangeInfo, and any other
#     non-order call. Caller timeout capped at 6s so workers free
#     earlier than the 10s HTTP session timeout. A timeout here is
#     a latency nuisance, not an ambiguity risk.
#
#   _CRITICAL_TIMEOUT_EXECUTOR  — order-placement pool.
#     Used for: futures_create_order (market entries, STOP_MARKET
#     SL, TAKE_PROFIT_MARKET TP1/TP2, SL updates, emergency closes).
#     NO upper cap, MINIMUM 10s effective timeout (matches the HTTP
#     session timeout). A timeout here WOULD create an ambiguity
#     window — if we give up at 6s while the order is still in
#     flight, the retry path could double-submit. Waiting the full
#     session timeout eliminates that class of false-ambiguous.
#
#   pool sizes: best-effort 32 (parallel scan + analytics + dashboard),
#   critical 8 (max simultaneous in-flight orders; a bot with
#   max_open_positions=3 + emergency closes will not exceed this).
_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="apicl"
)
_CRITICAL_TIMEOUT_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="apicrit"
)

# Best-effort caller cap (unchanged from REV 11.2).
_TIMEOUT_EXECUTOR_CAP = 6.0

# REV 11.3 (A1) — critical-path floor. This matches the HTTP session
# timeout configured in set_client_keys(). A caller requesting less
# than this (e.g. 5s) is lifted up to it. A caller requesting more
# is honored as-is. This is the value that guarantees we never
# abandon an in-flight order placement before the socket layer does.
_CRITICAL_MIN_TIMEOUT = 10.0

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
    REV 11.3 (A1) — split pools by consequence of failure.

    Best-effort callers (analytics, klines, exchangeInfo):
      • effective_timeout = min(timeout, _TIMEOUT_EXECUTOR_CAP)   # 6s
      • runs on _TIMEOUT_EXECUTOR (32 workers)
      • timeout → zombie counter, warn at 8

    Critical callers (order placement, SL/TP updates):
      • effective_timeout = max(timeout, _CRITICAL_MIN_TIMEOUT)   # ≥10s
      • runs on _CRITICAL_TIMEOUT_EXECUTOR (8 workers)
      • timeout → critical zombie counter, warn at 3
      • NO upper cap — caller controls if it needs >10s

    A timeout on a CRITICAL call means the order may still be in flight
    at the exchange. The retry path MUST resolve via
    _resolve_ambiguous_market (entry.py) or the deterministic-cid
    retry (exit.py). Because we now wait the full 10s HTTP timeout,
    this class of false-ambiguous is effectively eliminated for
    healthy exchange responses.

    Exceptions propagate to the caller (TimeoutError or the wrapped
    function's exception).
    """
    is_critical = _is_critical_desc(desc)

    if is_critical:
        # Floor at the HTTP session timeout so we never give up on an
        # in-flight order before the socket layer does.
        effective_timeout = max(timeout, _CRITICAL_MIN_TIMEOUT)
        executor = _CRITICAL_TIMEOUT_EXECUTOR
        zombie_counter = _critical_zombie_count
        zombie_lock = _critical_zombie_lock
        warn_threshold = _CRITICAL_ZOMBIE_WARN_THRESHOLD
        pool_label = "critical"
    else:
        # Best-effort: cap so workers free earlier than the HTTP timeout.
        effective_timeout = min(timeout, _TIMEOUT_EXECUTOR_CAP)
        executor = _TIMEOUT_EXECUTOR
        zombie_counter = _zombie_count
        zombie_lock = _zombie_lock
        warn_threshold = _ZOMBIE_WARN_THRESHOLD
        pool_label = "best-effort"

    fut = executor.submit(fn)

    def _on_done(_f):
        with zombie_lock:
            if zombie_counter['value'] > 0:
                zombie_counter['value'] -= 1

    try:
        return fut.result(timeout=effective_timeout)
    except concurrent.futures.TimeoutError:
        # Worker is still running — decrement when it lands.
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
                f"API latency high — consider raising timeouts or "
                f"throttling callers."
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
        with _requests_lock:
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
def retry_on_rate_limit(func):
    """
    REV 11.1 — jitter + -1008 heavy backoff + Retry-After respect.
    Wrapper serialises under _requests_lock (RLock, reentrant-safe).
    """
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        max_retries = 5
        for attempt in range(max_retries):
            try:
                with _requests_lock:
                    return func(*args, **kwargs)
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
                    f"({attempt + 1}/{max_retries}) on {func.__name__}"
                )
                time.sleep(sleep_time)
                try:
                    refresh_timestamp()
                except Exception:
                    pass
                continue
        raise RuntimeError(f"Rate limit retries exhausted for {func.__name__}")

    return wrapper


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
    REV 11.2 — TTL 60s, caller timeout 6s (capped by executor).
    REV 11.3 (A1) — still non-critical; runs on best-effort pool.
    """
    with _ACCOUNT_CACHE_LOCK:
        if _ACCOUNT_CACHE['data'] is not None and \
                time.time() - _ACCOUNT_CACHE['time'] < _ACCOUNT_CACHE_TTL:
            return _ACCOUNT_CACHE['data']

    if _global_client is None:
        raise RuntimeError("get_account_cached: client not initialised")

    def _fetch():
        with _requests_lock:
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
                with _requests_lock:
                    return _global_client.futures_exchange_info()
            # REV 11.3 (A1) — non-critical desc → best-effort pool, 6s cap.
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
        logger.warning(f"adjust_qty error {e}")
        return str(min_qty)


def adjust_price(price, tick) -> str:
    try:
        tick_dec = Decimal(str(tick))
        price_d = (Decimal(str(price)) // tick_dec) * tick_dec
        exp = tick_dec.as_tuple().exponent
        if exp < 0:
            return format(price_d, f'.{abs(exp)}f')
        return format(price_d, 'f')
    except Exception as e:
        logger.warning(f"adjust_price error {e}")
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
        with _requests_lock:
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
        with _requests_lock:
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
        with _requests_lock:
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
            with _requests_lock:
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


def _get_open_algo_orders(pair: str) -> list:
    getter = getattr(_global_client, 'futures_get_open_algo_orders', None)
    if getter is None:
        logger.error(
            "[algo] futures_get_open_algo_orders missing on client — "
            "call assert_sdk_methods() at startup"
        )
        return []
    try:
        with _requests_lock:
            resp = getter(symbol=pair)
        if isinstance(resp, dict):
            return resp.get('orders') or resp.get('data') or []
        return resp or []
    except Exception as e:
        logger.debug(f"algo orders {pair}: {e}")
        return []


def _cancel_algo_order(pair: str, algo_id) -> bool:
    cancel = getattr(_global_client, 'futures_cancel_algo_order', None)
    if cancel is None or not algo_id:
        return False
    try:
        with _requests_lock:
            cancel(symbol=pair, algoId=algo_id)
        return True
    except Exception as e:
        logger.warning(f"algo cancel {algo_id} {pair}: {e}")
        return False


# ─────────────────────────────────────────────────────────────
# CLIENT SETUP
# ─────────────────────────────────────────────────────────────
def check_position_mode() -> Optional[bool]:
    if _global_client is None:
        return None
    try:
        with _requests_lock:
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
        client_params = {'requests_params': {'timeout': 10}}
        if CONFIG.demo_mode:
            _global_client = Client(api_key, api_secret, demo=True, **client_params)
            logger.info("DEMO MODE ACTIVE - demo.binance.com")
        elif CONFIG.testnet:
            _global_client = Client(api_key, api_secret, testnet=True, **client_params)
            logger.info("TESTNET MODE ACTIVE - testnet.binance.vision")
        else:
            _global_client = Client(api_key, api_secret, **client_params)
            logger.warning("MAINNET MODE - REAL MONEY!")
        _install_pooled_session(_global_client)

    refresh_timestamp()

    try:
        assert_sdk_methods()
    except Exception as e:
        logger.critical(f"SDK assertion failed: {e}")
        raise
    check_position_mode()
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
    if _global_client is None:
        raise RuntimeError("startup_checks: client not initialised")
    assert_sdk_methods()
    check_position_mode()
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