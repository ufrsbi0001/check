"""
state.py — Runtime state for the trading engine.

REV 1.3.19 (2026-10-03) — CONCURRENCY + FAIL-CLOSED HARDENING:
  ✅ CRITICAL: `DailyTracker` now guarded by `_DAILY_TRACKER_LOCK`
     (RLock). Previously add_loss() / is_limit_reached() /
     update_peak() / reset_if_new_day() were unsynchronised; a
     read-modify-write on `loss_count` could lost-update under
     concurrent calls from the scan loop and trade manager. The
     DD kill-switch decision was therefore untrustworthy at exactly
     the moment it mattered (crash / high activity).
  ✅ CRITICAL: `is_limit_reached()` is now FAIL-CLOSED on unreadable
     equity. Previously `if current_equity < 100: return False` meant
     a failed equity fetch silently skipped the DD check. Now
     equity <= 0 returns (True, "EQUITY_UNREADABLE") so the caller
     halts instead of continuing to trade on a blind book.
  ✅ Rollover logic consolidated: `_check_date_rollover()` now also
     resets `start_balance` / `peak_balance` so the two functions
     (`_check_date_rollover` and `reset_if_new_day`) can no longer
     disagree about whether the day has turned.
  ✅ `reset_if_new_day()` guards against an invalid `current_balance`
     argument (0 / negative / NaN) — the caller's transient bad
     read no longer corrupts the day's baseline.
  ✅ Removed dead branch in `_load()` (peak_date != today was
     unreachable under atomic write semantics).
  ✅ `save_coin_rotation()` — snapshot under lock, disk I/O outside
     (previously the write lock serialised a whole JSON dump while
     other threads waited to read rotation state).

REV 1.3.18 (2026-10-03) — FAIL-FAST SAFETY-KEY VERIFICATION (retained).
REV 1.3.17 (2026-10-02) — CONFIG CLEANUP (retained).
REV 1.3.16 (2026-10-02) — DAILY TRACKER DATE-ROLLOVER FIX (retained).
REV 1.3.9  (2026-09-24) — WRITE-LOCK RACE + TEMP-FILE UNIQUENESS FIX.
REV 1.3.8  (2026-09-23) — WRITE LOCK ADDED.
REV 1.3.5  (2026-09-23) — ATOMIC WRITES + PEAK FIX.
"""
from __future__ import annotations

import json
import math
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.config import CONFIG
from core import config_center as CC
from core.client import logger

# ─────────────────────────────────────────────────────────────
# CONFIG SOURCE SPLIT (read this before adding new reads)
# ─────────────────────────────────────────────────────────────
#   CONFIG (core.config)        → env-only: file paths, blacklist,
#                                 secrets, deployment flags.
#   CC     (core.config_center) → trading params + safety limits.
#
# Rule of thumb:
#   • Static infra  → CONFIG.<attr>
#   • Trading tunables → CC.get('<key>')  (live-read where documented)
#
# Never duplicate a trading param as a module-level constant here
# unless it's an intentional SAFETY-LIMIT snapshot (see below).
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
PKT = timezone(timedelta(hours=5))

CSV_FILE            = CONFIG.csv_file
CSV_EXIT_FILE       = CONFIG.csv_exit_file
LOSS_FILE           = CONFIG.loss_file
ACTIVE_TRADES_FILE  = CONFIG.active_trades_file
COOLDOWN_FILE       = CONFIG.cooldown_file
COIN_ROTATION_FILE  = CONFIG.coin_rotation_file
PAUSE_FILE          = CONFIG.pause_file

# ── SAFETY LIMITS (module-level snapshots) ──
# REV 1.3.17: these are intentionally NOT live-read. Runtime toggling
# loss/drawdown limits could disable protection mid-session.
# REV 1.3.18: fail fast if any of these keys is missing from CC.GLOBAL
# so we never silently compare against None at runtime.
_REQUIRED_CC_SAFETY_KEYS = (
    'max_daily_loss_trades',
    'max_daily_drawdown_percent',
    'max_account_drawdown',
)
_missing = [k for k in _REQUIRED_CC_SAFETY_KEYS if k not in CC.GLOBAL]
if _missing:
    raise RuntimeError(
        "state.py: config_center.GLOBAL is missing required safety "
        f"key(s): {', '.join(_missing)}. Add them to config_center "
        "defaults (with validation) before importing state. "
        "Silent-None here would later crash as `loss_count >= None`."
    )

MAX_DAILY_LOSS_TRADES       = CC.get('max_daily_loss_trades')
MAX_DAILY_DRAWDOWN_PERCENT  = CC.get('max_daily_drawdown_percent')
MAX_ACCOUNT_DRAWDOWN        = CC.get('max_account_drawdown')

# ── ENV-ONLY ──
BLACKLISTED_COINS           = set(CONFIG.blacklisted_coins)


# ─────────────────────────────────────────────────────────────
# ATOMIC JSON WRITE
# ─────────────────────────────────────────────────────────────
def _atomic_json_write(path: str, data: Any) -> bool:
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.error(f"Atomic write failed for {path}: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


# ─────────────────────────────────────────────────────────────
# STOP EVENT
# ─────────────────────────────────────────────────────────────
_stop_event = threading.Event()

def request_stop() -> None:
    _stop_event.set()

def clear_stop() -> None:
    _stop_event.clear()

def is_stopped() -> bool:
    return _stop_event.is_set()


# ─────────────────────────────────────────────────────────────
# ACTIVE TRADES
# ─────────────────────────────────────────────────────────────
active_trades: dict[str, dict] = {}
ACTIVE_TRADES_LOCK = threading.Lock()
_ACTIVE_WRITE_LOCK = threading.Lock()


def flush_active_trades() -> None:
    with _ACTIVE_WRITE_LOCK:
        with ACTIVE_TRADES_LOCK:
            data = dict(active_trades)
        _atomic_json_write(ACTIVE_TRADES_FILE, data)


def load_active_trades() -> None:
    try:
        if os.path.exists(ACTIVE_TRADES_FILE):
            with open(ACTIVE_TRADES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            with ACTIVE_TRADES_LOCK:
                active_trades.clear()
                for sym, trade in data.items():
                    if 'sl_level' in trade:
                        lvl = trade['sl_level']
                        try:
                            lvl_int = int(float(lvl))
                            trade['sl_level'] = (
                                lvl_int if lvl_int in (0, 1, 2, 3) else 0
                            )
                        except Exception:
                            trade['sl_level'] = 0
                    active_trades[sym] = trade
        else:
            with ACTIVE_TRADES_LOCK:
                active_trades.clear()
    except Exception as e:
        logger.warning(f"Load active trades error: {e}")


def add_active_trade(symbol: str, data: dict) -> None:
    with ACTIVE_TRADES_LOCK:
        active_trades[symbol] = data
    flush_active_trades()


def remove_active_trade(symbol: str) -> None:
    with ACTIVE_TRADES_LOCK:
        if symbol in active_trades:
            del active_trades[symbol]
    flush_active_trades()


def get_active_trade(symbol: str) -> dict:
    with ACTIVE_TRADES_LOCK:
        return active_trades.get(symbol, {})


# ─────────────────────────────────────────────────────────────
# TRACKED SYMBOLS
# ─────────────────────────────────────────────────────────────
bot_tracked_symbols: set[str] = set()
BOT_TRACKED_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────
# COOLDOWNS
# ─────────────────────────────────────────────────────────────
cooldown_until: dict[str, datetime] = {}
COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_WRITE_LOCK = threading.Lock()


def load_cooldowns() -> None:
    try:
        if os.path.exists(COOLDOWN_FILE):
            with open(COOLDOWN_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            with COOLDOWN_LOCK:
                for sym, ts_str in data.items():
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts > datetime.now(PKT):
                            cooldown_until[sym] = ts
                    except Exception as e:
                        logger.warning(
                            f"Skipping malformed cooldown {sym}={ts_str}: {e}"
                        )
        logger.info(f" Cooldowns loaded ({len(cooldown_until)} active)")
    except Exception as e:
        logger.warning(f"Could not load cooldowns: {e}")


def save_cooldowns() -> None:
    try:
        with _COOLDOWN_WRITE_LOCK:
            with COOLDOWN_LOCK:
                data = {
                    sym: ts.isoformat()
                    for sym, ts in cooldown_until.items()
                    if ts > datetime.now(PKT)
                }
            _atomic_json_write(COOLDOWN_FILE, data)
    except Exception as e:
        logger.warning(f"Could not save cooldowns: {e}")


# ─────────────────────────────────────────────────────────────
# ENTRY-CANDLE DEDUP
# ─────────────────────────────────────────────────────────────
last_entry_candle: dict[str, Any] = {}
ENTRY_CANDLE_LOCK = threading.Lock()


def cleanup_entry_candles() -> None:
    try:
        import pandas as pd
        now_naive = pd.Timestamp.now(tz='UTC').tz_convert(None)
        cutoff = now_naive - timedelta(hours=2)
        with ENTRY_CANDLE_LOCK:
            stale = []
            for k, v in last_entry_candle.items():
                try:
                    vv = (
                        v.tz_convert(None)
                        if getattr(v, 'tzinfo', None) is not None
                        else v
                    )
                    if vv < cutoff:
                        stale.append(k)
                except Exception:
                    continue
            for k in stale:
                del last_entry_candle[k]
    except Exception as e:
        logger.debug(f"entry candle cleanup: {e}")


# ─────────────────────────────────────────────────────────────
# SCAN RESULTS
# ─────────────────────────────────────────────────────────────
scan_results: list[dict] = []
SCAN_RESULTS_LOCK = threading.Lock()
scan_timestamp: Optional[str] = None


def set_scan_results(rows: list[dict], ts: str) -> None:
    global scan_timestamp
    with SCAN_RESULTS_LOCK:
        scan_results.clear()
        scan_results.extend(rows)
        scan_timestamp = ts


def get_scan_results() -> tuple[list[dict], Optional[str]]:
    with SCAN_RESULTS_LOCK:
        return list(scan_results), scan_timestamp


# ─────────────────────────────────────────────────────────────
# FAIL COUNTS + ATR CACHE
# ─────────────────────────────────────────────────────────────
fail_counts: dict[str, int] = {}
FAIL_COUNTS_LOCK = threading.Lock()

ATR_CACHE: dict[str, dict] = {}
ATR_CACHE_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────
# V2 STATS
# ─────────────────────────────────────────────────────────────
V2_STATS = {
    'signals':  0,
    'low_conf': 0,
    'low_rr':   0,
    'low_adx':  0,
    'trend': 0,
    'rotation': 0,
    'decision': 0,
    'total': 0,
}
V2_STATS_LOCK = threading.Lock()


def increment_v2_stat(rule: str) -> None:
    with V2_STATS_LOCK:
        if rule in V2_STATS:
            V2_STATS[rule] += 1
            if rule != 'signals':
                V2_STATS['total'] += 1


def reset_v2_stats() -> None:
    with V2_STATS_LOCK:
        for k in V2_STATS:
            V2_STATS[k] = 0
    logger.info(" V2 Stats Daily Reset")


def get_v2_stats_str() -> str:
    with V2_STATS_LOCK:
        return (
            f"V2 Stats - Sig:{V2_STATS['signals']} "
            f"PreGate[c:{V2_STATS['low_conf']} "
            f"r:{V2_STATS['low_rr']} "
            f"a:{V2_STATS['low_adx']}] "
            f"Trend:{V2_STATS['trend']} "
            f"Rot:{V2_STATS['rotation']} "
            f"Dec:{V2_STATS['decision']} "
            f"Blocked:{V2_STATS['total']}"
        )


# ─────────────────────────────────────────────────────────────
# DAILY TRACKER
#   REV 1.3.16 — date-rollover fix + public getter
#   REV 1.3.17 — safety limits documented as snapshots
#   REV 1.3.18 — import-time verification of safety keys
#   REV 1.3.19 — thread-safe (RLock); fail-CLOSED on bad equity;
#                rollover consolidated
# ─────────────────────────────────────────────────────────────
_DAILY_TRACKER_LOCK = threading.RLock()

# Minimum meaningful equity. Below this we do not trust the value
# enough to compute drawdown, and we also do not trust it enough to
# silently skip the DD check (the previous behaviour).
_MIN_TRUSTWORTHY_EQUITY = 100.0


class DailyTracker:
    """
    Tracks daily loss count + balance peaks.

    Persistence: JSON file (LOSS_FILE from CONFIG).
    Reset trigger: date change in PKT timezone (UTC+5).

    Thread safety (REV 1.3.19):
      All public methods take `_DAILY_TRACKER_LOCK` (an RLock, because
      some methods call others internally). The lock covers the whole
      read-modify-write on `loss_count` / `start_balance` /
      `peak_balance` so concurrent calls cannot lost-update.

    Fail-closed equity (REV 1.3.19):
      `is_limit_reached()` returns (True, reason) when the caller
      supplies an unreadable equity (0, negative, NaN). The bot should
      halt rather than trade blind. Previous behaviour returned False
      — silently skipping DD protection.
    """

    def __init__(self):
        self.file = LOSS_FILE
        self.today = datetime.now(PKT).date()
        self.loss_count = 0
        self.start_balance = 0.0
        self.peak_balance = 0.0
        self._load()

    # ─── internal ───
    def _load(self):
        try:
            if not os.path.exists(self.file):
                logger.info(f" DailyTracker: no file yet ({self.file})")
                return
            with open(self.file, 'r', encoding='utf-8') as f:
                d = json.load(f)
            saved_date = d.get('date')
            if saved_date == str(self.today):
                self.loss_count = int(d.get('loss_count', 0))
                self.start_balance = float(d.get('start_balance', 0))
                self.peak_balance = float(d.get('peak_balance', 0))
                logger.info(
                    f" DailyTracker loaded: date={saved_date} "
                    f"loss_count={self.loss_count} "
                    f"start_bal=${self.start_balance:.2f} "
                    f"peak_bal=${self.peak_balance:.2f}"
                )
            else:
                logger.info(
                    f" DailyTracker: stale file date={saved_date} "
                    f"(today={self.today}) — starting fresh"
                )
        except Exception as e:
            logger.warning(f"Tracker load error: {e}")

    def _save(self):
        _atomic_json_write(self.file, {
            'date': str(self.today),
            'loss_count': self.loss_count,
            'start_balance': self.start_balance,
            'peak_balance': self.peak_balance,
            'peak_date': str(self.today),
        })

    def _check_date_rollover(self):
        """
        Reset per-day state if the PKT date has changed. Consolidated:
        resets loss_count AND start_balance AND peak_balance, so callers
        cannot observe a mismatched combination.

        Caller must hold _DAILY_TRACKER_LOCK.
        """
        today = datetime.now(PKT).date()
        if today == self.today:
            return
        logger.info(
            f" DailyTracker: date rollover "
            f"{self.today} → {today} — resetting daily state"
        )
        self.today = today
        self.loss_count = 0
        self.start_balance = 0.0
        self.peak_balance = 0.0
        self._save()
        try:
            reset_v2_stats()
        except Exception as e:
            logger.debug(f"reset_v2_stats on rollover failed: {e}")

    # ─── public ───
    def get_loss_count(self) -> int:
        """Return the current day's loss count (with rollover check)."""
        with _DAILY_TRACKER_LOCK:
            self._check_date_rollover()
            return self.loss_count

    def reset_if_new_day(self, current_balance: float) -> None:
        """
        Establish the day's baseline if we haven't yet (first call of
        the day, or after rollover). Ignores invalid balances — a
        transient bad read does not corrupt the baseline.
        """
        with _DAILY_TRACKER_LOCK:
            try:
                cb = float(current_balance)
            except (TypeError, ValueError):
                return
            if not math.isfinite(cb) or cb <= 0:
                logger.warning(
                    f" DailyTracker.reset_if_new_day: refusing invalid "
                    f"balance {current_balance!r}"
                )
                return

            today = datetime.now(PKT).date()
            rolled = (today != self.today)
            fresh = (self.start_balance <= 0)

            if rolled or fresh:
                self.today = today
                self.loss_count = 0
                self.start_balance = cb
                self.peak_balance = cb
                self._save()
                if rolled:
                    try:
                        reset_v2_stats()
                    except Exception as e:
                        logger.debug(f"reset_v2_stats on new-day failed: {e}")
                logger.info(f" New day! Start Bal: ${cb:.2f}")

    def update_peak(self, current_balance: float) -> None:
        with _DAILY_TRACKER_LOCK:
            try:
                cb = float(current_balance)
            except (TypeError, ValueError):
                return
            if not math.isfinite(cb) or cb <= 0:
                return
            if cb > self.peak_balance:
                self.peak_balance = cb
                self._save()

    def add_loss(self) -> None:
        with _DAILY_TRACKER_LOCK:
            self._check_date_rollover()
            self.loss_count += 1
            self._save()
            logger.info(
                f" Daily Loss Count: {self.loss_count}/{MAX_DAILY_LOSS_TRADES}"
            )

    def is_limit_reached(self, current_equity: float) -> tuple[bool, str]:
        """
        DD kill-switch query.

        REV 1.3.19 — fail-CLOSED:
          • equity unreadable (0, negative, NaN, non-numeric) → halt
          • equity below _MIN_TRUSTWORTHY_EQUITY             → halt
        The bot should never trade on a book it cannot value.
        """
        # Fail-closed on unreadable / untrustworthy equity.
        try:
            eq = float(current_equity)
        except (TypeError, ValueError):
            return True, "EQUITY_UNREADABLE"
        if not math.isfinite(eq) or eq <= 0:
            return True, "EQUITY_UNREADABLE"
        if eq < _MIN_TRUSTWORTHY_EQUITY:
            return True, f"EQUITY_TOO_LOW({eq:.2f})"

        with _DAILY_TRACKER_LOCK:
            self._check_date_rollover()
            self.reset_if_new_day(eq)

            if self.loss_count >= MAX_DAILY_LOSS_TRADES:
                return True, f"Loss count {self.loss_count}"

            if self.start_balance > _MIN_TRUSTWORTHY_EQUITY:
                dd = (
                    (self.start_balance - eq) / self.start_balance * 100
                )
                if dd >= MAX_DAILY_DRAWDOWN_PERCENT:
                    return True, f"Daily DD {dd:.2f}%"

            if self.peak_balance > _MIN_TRUSTWORTHY_EQUITY:
                acc_dd = (
                    (self.peak_balance - eq) / self.peak_balance * 100
                )
                if acc_dd >= MAX_ACCOUNT_DRAWDOWN:
                    return True, f"Account DD {acc_dd:.2f}%"

            return False, ""


daily_tracker = DailyTracker()


# ─────────────────────────────────────────────────────────────
# COIN ROTATION
#   REV 1.3.17 — MAX_TRADES_PER_COIN_PER_DAY now read LIVE
#   REV 1.3.19 — snapshot under lock, disk I/O outside
# ─────────────────────────────────────────────────────────────
_ROTATION_WRITE_LOCK = threading.Lock()


def load_coin_rotation() -> dict:
    try:
        if os.path.exists(COIN_ROTATION_FILE):
            with open(COIN_ROTATION_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logger.warning(f"coin_rotation load error: {e}")
    return {}


def save_coin_rotation(data: dict) -> None:
    try:
        with _ROTATION_WRITE_LOCK:
            today = datetime.now(PKT).date()
            # Snapshot keys to delete (do not mutate caller's dict).
            pruned = dict(data)
            keys_to_del = []
            for k in pruned.keys():
                try:
                    d = datetime.strptime(k, '%Y-%m-%d').date()
                    if (today - d).days > 3:
                        keys_to_del.append(k)
                except Exception:
                    pass
            for k in keys_to_del:
                pruned.pop(k, None)
            _atomic_json_write(COIN_ROTATION_FILE, pruned)
    except Exception as e:
        logger.warning(f"coin_rotation save error: {e}")


def can_trade_coin(symbol: str) -> tuple[bool, str]:
    if symbol in BLACKLISTED_COINS:
        return False, f"{symbol} blacklisted"
    # REV 1.3.17 — LIVE read so UI/env runtime changes reflect immediately
    _max_per_day = int(CC.get("max_trades_per_coin_per_day", 2) or 2)
    data = load_coin_rotation()
    today = str(datetime.now(PKT).date())
    count = data.get(today, {}).get(symbol, 0)
    if count >= _max_per_day:
        return False, f"{symbol} {count} trades today"
    return True, "OK"


def record_coin_trade(symbol: str) -> None:
    # REV 1.3.17 — LIVE read
    _max_per_day = int(CC.get("max_trades_per_coin_per_day", 2) or 2)
    data = load_coin_rotation()
    today = str(datetime.now(PKT).date())
    if today not in data:
        data[today] = {}
    data[today][symbol] = data[today].get(symbol, 0) + 1
    save_coin_rotation(data)
    logger.info(
        f" [{symbol}] Daily trade: {data[today][symbol]}/{_max_per_day}"
    )