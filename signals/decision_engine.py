"""
decision_engine.py — Multi-filter trade approval layer.

REV 4.5 (2026-10-04) — RSI DIVERGENCE + FVG RETEST FILTERS:
  ✅ Added _filter_rsi_divergence() — reads ind_1h["divergence"]
     (already computed by market/indicators.py). Soft-pass vote:
     bullish div on BUY or bearish div on SELL → pass with boost.
     Counter-divergence → pass with warn (log-only for now).
  ✅ Added _filter_fvg_retest() — reads the nearest unfilled FVG
     zones (fvg_zone_bull_*/fvg_zone_bear_*) from ind_1h. Passes
     when price is inside the matching-direction FVG. Otherwise
     soft-pass with "no_fvg_retest".
  ✅ evaluate_trade() now registers BOTH as filters 8 and 9.
  ✅ DECISION["total_filters"] bumped 7 → 9 in config_center REV 6.1.
  ✅ _DEFAULT_DECISION fallback dict bumped 7 → 9 for consistency.
  ✅ get_stats() total_filters fallback bumped 7 → 9.
  ✅ Zero behaviour change for the other seven filters.

REV 4.4 (2026-10-04) — LIQUIDITY SWEEP FILTER (retained):
  ✅ Added _filter_liquidity_sweep() — soft-pass confirmation vote.
  ✅ evaluate_trade() registered "liquidity_sweep" as 7th filter.
  ✅ DECISION["total_filters"] bumped 6 → 7 in config_center REV 6.0.

REV 4.3 (2026-10-03) — CORRECTNESS HARDENING (retained):
  ✅ _check_and_pause_strategy() now bounds loss-streak lookback to a
     24 h window. Prevents ancient losses from triggering a pause.
  ✅ _dec_get() now uses an explicit _UNSET sentinel.
  ✅ get_btc_regime() preserves explicit TTL=0 as "never expire".
  ✅ evaluate_trade() captures the BTC regime snapshot ONCE.
  ✅ DecisionScore.total_filters class default removed.
  ✅ _UNSET sentinel + _LOSS_STREAK_WINDOW_SEC documented.

REV 4.2 (2026-10-02) — LIVE CONFIG READS (retained).
REV 4.1 (2026-10-02) — PHASE 1 CLEANUP (retained).
REV 4.0 (2026-10-02) — CENTRALIZED CONFIG.
REV 1.3.0 — 1.0.0 — various.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from core.client import logger
from core import config_center as CC

# ── REV 4.1 — COUNTER_TREND_STRATEGIES from config_center ──
try:
    from core.config_center import COUNTER_TREND_STRATEGIES
except Exception as _ct_err:
    logger.warning(
        f"[decision_engine] COUNTER_TREND_STRATEGIES import failed "
        f"({_ct_err}); using built-in fallback"
    )
    COUNTER_TREND_STRATEGIES = frozenset({
        "TREND_DOWN_FADE",
        "MEAN_REVERSION",
        "RANGE_SCALPER",
        "CHOP_FADE",
    })


# ═════════════════════════════════════════════════════════════
#  MODULE CONSTANTS
# ═════════════════════════════════════════════════════════════
# Sentinel to distinguish "caller didn't pass a default" from
# "caller explicitly passed None".
_UNSET = object()

# Only count losses within this window when deciding whether to pause
# a strategy. Prevents ancient losses (from a different regime) from
# triggering a spurious pause.
_LOSS_STREAK_WINDOW_SEC = 24 * 3600  # 24 h


# ═════════════════════════════════════════════════════════════
#  LIVE CONFIG HELPERS
#  Read fresh on every call so update_runtime() takes effect
#  immediately. Falls back to safe defaults only in the
#  pathological case where config_center is unavailable.
# ═════════════════════════════════════════════════════════════
_DEFAULT_DECISION = {
    "min_approvals": 5,
    # REV 4.5 — bumped 7 → 9 to match config_center REV 6.1
    # (added rsi_divergence + fvg_retest filters).
    "total_filters": 9,
    "btc_bias_enabled": True,
    "btc_bias_mode": "full",
    "btc_bias_high_risk_coins": frozenset(),
    "btc_regime_ttl_sec": 600,
    "adx_counter_trend_hard_max": 45.0,
    "adx_counter_trend_soft_max": 35.0,
    "atr_ratio_extreme": 2.5,
    "loss_streak_trigger": 4,
    "loss_streak_cooldown_min": 60,
    "wr_mult_min": 0.85,
    "wr_mult_max": 1.10,
    "wr_mult_min_trades": 15,
}


def _dec_get(key: str, default=_UNSET):
    """
    Live decision config read (fresh each call).

    REV 4.3 — sentinel-based default. Semantics:
      • key present in DECISION (non-None)      → return it
      • key absent + explicit default passed    → return default
        (default=None means "return None", not "use _DEFAULT_DECISION")
      • key absent + no default passed          → use _DEFAULT_DECISION
    """
    try:
        val = CC.get_decision_cfg().get(key)
        if val is not None:
            return val
    except Exception:
        pass
    if default is not _UNSET:
        return default
    return _DEFAULT_DECISION.get(key)


def _global_get(key: str, default=None):
    """Live GLOBAL config read."""
    try:
        return CC.get(key, default)
    except Exception:
        return default


# ═════════════════════════════════════════════════════════════
#  GLOBAL BTC BIAS (module-level shared state)
# ═════════════════════════════════════════════════════════════
_btc_regime: str = "UNKNOWN"
_btc_regime_ts: float = 0.0
_btc_lock = threading.Lock()


def set_btc_regime(regime: str) -> None:
    """
    Called by the scanner once per scan cycle AFTER computing the
    BTC 1h indicators dict.

    Expected values (case-insensitive):
      "TREND_UP", "TREND_DOWN", "CHOP", "VOLATILE", "QUIET", "UNKNOWN"

    Until this is called at least once, the gate is a no-op.
    """
    global _btc_regime, _btc_regime_ts
    new = (regime or "UNKNOWN").upper().strip()
    with _btc_lock:
        if new != _btc_regime:
            logger.info(f"[decision] BTC regime → {new}")
        _btc_regime = new
        _btc_regime_ts = time.time()


def get_btc_regime() -> str:
    """
    Returns cached BTC regime, or UNKNOWN if stale.

    REV 4.2 — TTL read LIVE from config_center each call.
    REV 4.3 — TTL=0 preserved as "never expire" (was coerced to 600).
    """
    _ttl = float(_dec_get("btc_regime_ttl_sec", 600))
    with _btc_lock:
        if _btc_regime_ts <= 0:
            return "UNKNOWN"
        # TTL <= 0 disables staleness check (regime stays until re-set).
        if _ttl > 0 and time.time() - _btc_regime_ts > _ttl:
            return "UNKNOWN"
        return _btc_regime


# ═════════════════════════════════════════════════════════════
#  RECENT PERFORMANCE MEMORY
# ═════════════════════════════════════════════════════════════
_recent_results: list = []
_recent_lock = threading.Lock()
_MAX_RECENT = 200
_strategy_pauses: dict = {}
_pauses_lock = threading.Lock()


def record_trade_result(strategy: str, pnl: float) -> None:
    """
    Record a closed-trade outcome for the loss-streak guard.

    BE (pnl == 0) is classified as `won=True`:
      • A breakeven trade is NOT a loss.
      • It breaks any in-progress loss streak.
      • Only actual pnl < 0 trades extend the streak.
    """
    with _recent_lock:
        _recent_results.append({
            "ts": time.time(),
            "strategy": strategy or "UNKNOWN",
            "won": pnl >= 0,
        })
        if len(_recent_results) > _MAX_RECENT:
            del _recent_results[: len(_recent_results) - _MAX_RECENT]
    if pnl < 0:
        _check_and_pause_strategy(strategy)


def _check_and_pause_strategy(strategy: str) -> None:
    """
    Pause a strategy after N consecutive losses.

    REV 4.3 — losses older than _LOSS_STREAK_WINDOW_SEC are ignored.
    Without this window, 4 losses spread over days/weeks triggered a
    pause even though the losing regime was long gone.
    """
    if not strategy:
        return
    _trigger = int(_dec_get("loss_streak_trigger", 4))
    _cooldown = int(_dec_get("loss_streak_cooldown_min", 60))
    cutoff = time.time() - _LOSS_STREAK_WINDOW_SEC

    with _recent_lock:
        recent_same = [
            r for r in _recent_results
            if r["strategy"] == strategy and r["ts"] >= cutoff
        ]

    if len(recent_same) < _trigger:
        return
    last_n = recent_same[-_trigger:]
    if all(not r["won"] for r in last_n):
        pause_until = time.time() + _cooldown * 60
        with _pauses_lock:
            _strategy_pauses[strategy] = pause_until
        logger.warning(
            f"[decision] {strategy} paused for {_cooldown}m "
            f"after {_trigger} consecutive losses "
            f"(within {_LOSS_STREAK_WINDOW_SEC // 3600}h)"
        )


def _is_strategy_paused(strategy: str):
    with _pauses_lock:
        until = _strategy_pauses.get(strategy, 0)
    if until > time.time():
        return True, int((until - time.time()) / 60)
    return False, 0


# ═════════════════════════════════════════════════════════════
#  WIN-RATE MULTIPLIER (public)
# ═════════════════════════════════════════════════════════════
def get_strategy_multiplier(strategy: str) -> float:
    """
    Return a 0.85–1.10 confidence multiplier based on the strategy's
    recent win rate. Live-reads bounds from config_center each call.

    Contract:
      • Returns 1.0 (neutral) when:
          - strategy is empty/None
          - fewer than `wr_mult_min_trades` recorded for that strategy
      • Otherwise: multiplier = min + (max - min) * win_rate

    Soft confidence nudge, not a gate.
    """
    if not strategy:
        return 1.0
    _min = float(_dec_get("wr_mult_min", 0.85))
    _max = float(_dec_get("wr_mult_max", 1.10))
    _min_trades = int(_dec_get("wr_mult_min_trades", 15))
    try:
        with _recent_lock:
            rows = [r for r in _recent_results if r["strategy"] == strategy]
        if len(rows) < _min_trades:
            return 1.0
        wins = sum(1 for r in rows if r["won"])
        wr = wins / len(rows)
        return _min + (_max - _min) * wr
    except Exception:
        return 1.0


# ═════════════════════════════════════════════════════════════
#  INDIVIDUAL FILTERS
# ═════════════════════════════════════════════════════════════
def _filter_trend_strength(side: str, strategy: str, ind_1h: dict):
    is_counter = strategy in COUNTER_TREND_STRATEGIES
    adx = float(ind_1h.get("adx", 0) or 0)
    if not is_counter:
        return True, f"trend-following adx={adx:.0f}"
    _hard = float(_dec_get("adx_counter_trend_hard_max", 45.0))
    _soft = float(_dec_get("adx_counter_trend_soft_max", 35.0))
    if adx > _hard:
        return False, f"counter-trend adx={adx:.0f}>{_hard:.0f}"
    if adx > _soft:
        return True, f"counter-trend adx={adx:.0f} (soft-warn)"
    return True, f"counter-trend adx={adx:.0f} (weak, ideal)"


def _filter_mtf_consensus(side: str, ind_1h: dict, ind_4h: dict,
                          ind_1d: Optional[dict]):
    """
    SOLE MTF FILTER in the system.

    TF set: 1h, 4h, 1d — EXACTLY the TFs the scan loop caches.
    Behaviour: 2 of 3 TFs must agree with the trade direction.
      • BUY  agrees if e20 > e50 AND price > e50
      • SELL agrees if e20 < e50 AND price < e50
    When `use_mtf_confluence` is False, short-circuits to PASS with
    reason "mtf_disabled" and still counts toward the total (free vote).

    REV 4.2 — reads flag LIVE from config_center each call.
    """
    if not _global_get("use_mtf_confluence", False):
        return True, "mtf_disabled"

    def agrees(ind, s):
        if not ind:
            return False
        e20 = float(ind.get("ema20", 0) or 0)
        e50 = float(ind.get("ema50", 0) or 0)
        price = float(ind.get("price", 0) or 0)
        if e20 <= 0 or e50 <= 0 or price <= 0:
            return False
        if s == "BUY":
            return e20 > e50 and price > e50
        return e20 < e50 and price < e50

    agree_count = 0
    agree_list = []
    for tf, ind in (("1h", ind_1h), ("4h", ind_4h), ("1d", ind_1d)):
        if agrees(ind, side):
            agree_count += 1
            agree_list.append(tf)
    if agree_count >= 2:
        return True, f"mtf {agree_count}/3 ({','.join(agree_list)})"
    return False, f"mtf only {agree_count}/3"


def _filter_volume_confirmation(side: str, ind_1h: dict,
                                strict: bool = False):
    """CVD slope is a HARD FAIL when strict=True."""
    try:
        cvd_div = str(ind_1h.get("cvd_div", "NONE"))
        cvd_slope = float(ind_1h.get("cvd_slope", 0) or 0)

        if side == "BUY" and cvd_div == "BEARISH_CVD":
            return False, "cvd_bearish_div"
        if side == "SELL" and cvd_div == "BULLISH_CVD":
            return False, "cvd_bullish_div"

        if side == "BUY" and cvd_slope < 0:
            if strict:
                return False, "cvd_slope_neg (strict)"
            return True, "cvd_slope_neg (weak)"
        if side == "SELL" and cvd_slope > 0:
            if strict:
                return False, "cvd_slope_pos (strict)"
            return True, "cvd_slope_pos (weak)"
        return True, "cvd_ok"
    except Exception as e:
        return True, f"cvd_skip:{type(e).__name__}"


# ═════════════════════════════════════════════════════════════
#  REV 4.4 — LIQUIDITY SWEEP FILTER (retained)
# ═════════════════════════════════════════════════════════════
def _filter_liquidity_sweep(side: str, ind_1h: dict):
    """
    Confirmation vote when the current bar shows a liquidity sweep
    in our trade direction.

    Soft-pass semantics:
      • If a matching sweep is present → pass with strength detail.
      • Otherwise                      → pass with "no_sweep" reason.
    Never returns False — this filter ADDS a positive vote when the
    sweep aligns, but does not BLOCK trades on its absence.
    """
    try:
        if side == "BUY" and bool(ind_1h.get("sweep_bull")):
            _strength = float(ind_1h.get("sweep_strength", 0) or 0)
            return True, f"sweep_bull_{_strength:.2f}"
        if side == "SELL" and bool(ind_1h.get("sweep_bear")):
            _strength = float(ind_1h.get("sweep_strength", 0) or 0)
            return True, f"sweep_bear_{_strength:.2f}"
        return True, "no_sweep"
    except Exception:
        return True, "sweep_skip"


# ═════════════════════════════════════════════════════════════
#  REV 4.5 — RSI DIVERGENCE FILTER (NEW)
# ═════════════════════════════════════════════════════════════
def _filter_rsi_divergence(side: str, ind_1h: dict):
    """
    Confirmation vote when RSI divergence aligns with the trade.

    Reads ind_1h["divergence"] which is computed by
    market/indicators._compute_divergence(). Values:
      • "BULLISH_DIV" — price lower low, RSI higher low → BUY signal
      • "BEARISH_DIV" — price higher high, RSI lower high → SELL signal
      • "NONE"        — no divergence

    Soft-pass semantics:
      • Aligned divergence → pass with boost detail
      • Counter divergence → pass with warn (log-only; could gate in future)
      • No divergence      → pass with "no_divergence"

    Never returns False — preserves N-filter vote semantics.
    """
    try:
        div = str(ind_1h.get("divergence", "NONE")).upper()
        if side == "BUY" and div == "BULLISH_DIV":
            return True, "bullish_div"
        if side == "SELL" and div == "BEARISH_DIV":
            return True, "bearish_div"
        if side == "BUY" and div == "BEARISH_DIV":
            return True, "counter_bearish_div (warn)"
        if side == "SELL" and div == "BULLISH_DIV":
            return True, "counter_bullish_div (warn)"
        return True, "no_divergence"
    except Exception:
        return True, "div_skip"


# ═════════════════════════════════════════════════════════════
#  REV 4.5 — FVG RETEST FILTER (NEW)
# ═════════════════════════════════════════════════════════════
def _filter_fvg_retest(side: str, ind_1h: dict):
    """
    Confirmation vote when price is inside the nearest unfilled FVG.

    Reads ind_1h keys (all computed by market/indicators REV 3.7):
      • fvg_zone_bull_top / fvg_zone_bull_bottom — nearest unfilled
        bullish FVG zone (top > bottom when valid)
      • fvg_zone_bear_top / fvg_zone_bear_bottom — nearest unfilled
        bearish FVG zone
      • price

    Pass conditions:
      • BUY : price within [fvg_zone_bull_bottom, fvg_zone_bull_top]
      • SELL: price within [fvg_zone_bear_bottom, fvg_zone_bear_top]

    Soft-pass otherwise ("no_fvg_retest"). Never returns False.
    """
    try:
        price = float(ind_1h.get("price", 0) or 0)
        if price <= 0:
            return True, "fvg_no_price"

        if side == "BUY":
            top = float(ind_1h.get("fvg_zone_bull_top", 0) or 0)
            bot = float(ind_1h.get("fvg_zone_bull_bottom", 0) or 0)
            if top > bot > 0 and bot <= price <= top:
                gap_pct = (top - bot) / price * 100.0
                return True, f"fvg_bull_retest_{gap_pct:.2f}%"

        if side == "SELL":
            top = float(ind_1h.get("fvg_zone_bear_top", 0) or 0)
            bot = float(ind_1h.get("fvg_zone_bear_bottom", 0) or 0)
            if top > bot > 0 and bot <= price <= top:
                gap_pct = (top - bot) / price * 100.0
                return True, f"fvg_bear_retest_{gap_pct:.2f}%"

        return True, "no_fvg_retest"
    except Exception:
        return True, "fvg_skip"


def _filter_recent_performance(strategy: str):
    if not strategy:
        return True, "no_strategy_key"
    paused, mins_left = _is_strategy_paused(strategy)
    if paused:
        return False, f"{strategy}_paused_{mins_left}m"
    return True, f"{strategy}_not_paused"


def _filter_correlation(side: str, active_trades: list):
    """
    Correlation counter for same-side positions.

    NOTE: the HARD GATE in evaluate_trade() already short-circuits when
    same-side >= max_same_side_positions, so this filter will always
    pass when reached. Kept for defence-in-depth and detail[] trail.

    REV 4.2 — cap read LIVE from config_center each call.
    """
    if not active_trades:
        return True, "no_positions"
    _max_ss = int(_global_get("max_same_side_positions", 2))
    same_side = sum(
        1 for t in active_trades
        if (side == "BUY" and t.get("side") == "LONG")
        or (side == "SELL" and t.get("side") == "SHORT")
    )
    if same_side >= _max_ss:
        return False, f"{same_side}_same_side>={_max_ss}"
    return True, f"{same_side}_same_side"


def _count_same_side(side: str, active_trades: list) -> int:
    """Count of currently-open trades on the same side."""
    if not active_trades:
        return 0
    return sum(
        1 for t in active_trades
        if (side == "BUY" and t.get("side") == "LONG")
        or (side == "SELL" and t.get("side") == "SHORT")
    )


def _filter_volatility(ind_1h: dict):
    try:
        atr_ratio = float(ind_1h.get("atr_ratio", 1.0) or 1.0)
    except (TypeError, ValueError):
        return True, "atr_parse_fail"
    _extreme = float(_dec_get("atr_ratio_extreme", 2.5))
    if atr_ratio > _extreme:
        return False, f"atr_{atr_ratio:.2f}x>={_extreme}"
    return True, f"atr_{atr_ratio:.2f}x"


# ═════════════════════════════════════════════════════════════
#  BTC BIAS GATE (helper)
#  REV 4.0 — 3 modes support (full | high_risk_only | disabled)
#  REV 4.2 — all reads LIVE from config_center each call
# ═════════════════════════════════════════════════════════════
def _btc_bias_blocks(side: str, symbol: str = "",
                     regime: Optional[str] = None) -> Optional[str]:
    """
    BTC bias gate with 3 modes.

    Modes (config_center.DECISION.btc_bias_mode):
      • "disabled"       → gate OFF, no block
      • "high_risk_only" → sirf high-risk meme coins block
      • "full"           → har TREND_UP mein SHORT block,
                            TREND_DOWN mein LONG block

    `regime` param (REV 4.3): optional pre-fetched BTC regime so the
    caller can avoid a second get_btc_regime() call for logging.

    Returns rejection reason string, or None if allowed.
    """
    if not _dec_get("btc_bias_enabled", True):
        return None

    _mode = _dec_get("btc_bias_mode", "full")
    if _mode == "disabled":
        return None

    _high_risk = _dec_get("btc_bias_high_risk_coins", frozenset())
    if not isinstance(_high_risk, (set, frozenset, list, tuple)):
        _high_risk = frozenset()

    # Normalize symbol for high-risk check
    _sym = (symbol or "").upper()
    if _sym and not _sym.endswith("USDT"):
        _sym += "USDT"

    # High-risk-only mode: allow all other coins freely
    if _mode == "high_risk_only":
        if not _sym or _sym not in _high_risk:
            return None

    # Full mode or high-risk coin → check regime
    regime_now = regime if regime is not None else get_btc_regime()
    if regime_now == "TREND_UP" and side == "SELL":
        return "BTC_TREND_UP_blocks_SHORT"
    if regime_now == "TREND_DOWN" and side == "BUY":
        return "BTC_TREND_DOWN_blocks_LONG"
    return None


# ═════════════════════════════════════════════════════════════
#  DECISION SCORE
# ═════════════════════════════════════════════════════════════
@dataclass
class DecisionScore:
    approved: bool = False
    approvals: int = 0
    # REV 4.3 — default 0; instance always sets from live config.
    total_filters: int = 0
    top_reason: str = ""
    reasons: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    def add(self, name: str, passed: bool, reason: str):
        self.detail[name] = {"pass": passed, "reason": reason}
        if passed:
            self.approvals += 1
        else:
            self.reasons.append(f"{name}:{reason}")


# ═════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════
def evaluate_trade(symbol: str, side: str, ind_1h: dict, ind_4h: dict,
                   ind_1d: Optional[dict], strategy: str = "",
                   conf: float = 0.0, rr: float = 0.0,
                   active_trades_list: Optional[list] = None) -> DecisionScore:
    """
    Two HARD GATES before the vote:

      GATE 1: BTC global bias. Depends on btc_bias_mode:
              • "full"           → TREND_UP blocks all SELLs
              • "high_risk_only" → sirf high-risk meme coins block
              • "disabled"       → gate OFF

      GATE 2: Same-side correlation limit.

    If both gates pass, the N-filter vote runs.
    Approve iff approvals >= min_approvals.

    REV 4.2 — All config values read LIVE from config_center, so
    update_runtime() takes effect on the very next call.
    REV 4.3 — BTC regime captured ONCE per call.
    REV 4.4 — N is now 7 (added liquidity_sweep confirmation filter).
    REV 4.5 — N is now 9 (added rsi_divergence + fvg_retest filters).
    """
    # Live reads (fresh per call)
    _min_approvals = int(_dec_get("min_approvals", 5))
    # REV 4.5 — default bumped 7 → 9 to match config_center REV 6.1.
    _total_filters = int(_dec_get("total_filters", 9))
    _max_ss = int(_global_get("max_same_side_positions", 2))
    _btc_mode = _dec_get("btc_bias_mode", "full")

    # REV 4.3 — single snapshot for both gate check and reject log
    _btc_regime_snapshot = get_btc_regime()

    result = DecisionScore()
    result.total_filters = _total_filters
    active = active_trades_list or []

    # ═══════════════════════════════════════════════════════════
    #  HARD GATE #1 — BTC global bias
    # ═══════════════════════════════════════════════════════════
    btc_reason = _btc_bias_blocks(side, symbol, regime=_btc_regime_snapshot)
    if btc_reason:
        result.approved = False
        result.approvals = 0
        result.top_reason = f"HARD_REJECT: {btc_reason}"
        result.reasons = [f"btc_bias:{btc_reason}"]
        result.detail["btc_bias"] = {"pass": False, "reason": btc_reason}
        logger.warning(
            f"[decision] 🚫 HARD REJECT {symbol} {side} [{strategy}] — "
            f"BTC regime={_btc_regime_snapshot} mode={_btc_mode} → "
            f"{btc_reason} | conf={conf:.0f}% rr={rr:.2f}"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    #  HARD GATE #2 — same-side correlation limit
    # ═══════════════════════════════════════════════════════════
    same_side_count = _count_same_side(side, active)
    if same_side_count >= _max_ss:
        result.approved = False
        result.approvals = 0
        result.top_reason = (
            f"HARD_REJECT: {same_side_count} same-side already open "
            f"(max {_max_ss})"
        )
        result.reasons = [
            f"correlation:{same_side_count}_same_side>={_max_ss}"
        ]
        result.detail["correlation"] = {
            "pass": False,
            "reason": f"{same_side_count}_same_side>={_max_ss}",
        }
        logger.warning(
            f"[decision] 🚫 HARD REJECT {symbol} {side} [{strategy}] — "
            f"{same_side_count} same-side already open "
            f"(max {_max_ss}) | conf={conf:.0f}% rr={rr:.2f}"
        )
        return result

    # ═══════════════════════════════════════════════════════════
    #  NORMAL N-FILTER VOTE  (now 9 filters — REV 4.5)
    # ═══════════════════════════════════════════════════════════
    _is_trend_following = strategy not in COUNTER_TREND_STRATEGIES

    result.add("trend_strength",
               *_filter_trend_strength(side, strategy, ind_1h))
    result.add("mtf_consensus",
               *_filter_mtf_consensus(side, ind_1h, ind_4h, ind_1d))
    result.add("volume_confirm",
               *_filter_volume_confirmation(side, ind_1h,
                                            strict=_is_trend_following))
    result.add("recent_perf",
               *_filter_recent_performance(strategy))
    result.add("correlation",
               *_filter_correlation(side, active))
    result.add("volatility",
               *_filter_volatility(ind_1h))
    # REV 4.4 — 7th filter: liquidity sweep
    result.add("liquidity_sweep",
               *_filter_liquidity_sweep(side, ind_1h))
    # REV 4.5 — 8th filter: RSI divergence (NEW)
    result.add("rsi_divergence",
               *_filter_rsi_divergence(side, ind_1h))
    # REV 4.5 — 9th filter: FVG retest (NEW)
    result.add("fvg_retest",
               *_filter_fvg_retest(side, ind_1h))

    result.approved = result.approvals >= _min_approvals
    result.top_reason = (
        "APPROVED" if result.approved
        else (result.reasons[0] if result.reasons else "unknown_reject")
    )

    tag = "✅" if result.approved else "🚫"
    logger.info(
        f"[decision] {tag} {symbol} {side} [{strategy}] "
        f"{result.approvals}/{result.total_filters} "
        f"conf={conf:.0f}% rr={rr:.2f} → {result.top_reason}"
    )
    if not result.approved:
        logger.debug(f"[decision] {symbol} reject reasons: {result.reasons}")
    return result


def get_stats() -> dict:
    with _recent_lock:
        recent = list(_recent_results)
    with _pauses_lock:
        pauses = {
            k: int((v - time.time()) / 60)
            for k, v in _strategy_pauses.items()
            if v > time.time()
        }
    by_strat: dict = {}
    for r in recent:
        s = r["strategy"]
        if s not in by_strat:
            by_strat[s] = {"wins": 0, "losses": 0}
        if r["won"]:
            by_strat[s]["wins"] += 1
        else:
            by_strat[s]["losses"] += 1
    return {
        "recent_count": len(recent),
        "by_strategy": by_strat,
        "paused_strategies": pauses,
        "btc_regime": get_btc_regime(),
        "btc_bias_mode": _dec_get("btc_bias_mode", "full"),
        "min_approvals": int(_dec_get("min_approvals", 5)),
        # REV 4.5 — fallback bumped 7 → 9 for consistency.
        "total_filters": int(_dec_get("total_filters", 9)),
    }