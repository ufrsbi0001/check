"""
signals/base.py — Shared signal-engine core for all 4 families.

REV 23.8 (2026-10-04) — VWAP MEAN REVERSION OVERLAY:
  ✅ strategy_mean_reversion() now includes a VWAP-distance counter-
     trend overlay in CHOP regimes. Fires when:
       • ADX < 20 (choppy confirmed)
       • |avwap_dist_pct| >= vwap_rev_min_dist_pct (default 1.5%)
       • RSI at extreme (>= vwap_rev_rsi_sell_min OR <= vwap_rev_rsi_buy_max)
       • ATR ratio < 1.8 (not expanding volatility)
     TP2 target is set to the Anchored VWAP (mean reversion target)
     capped by 3× risk, so the trade always has an RR >= 1.8.
  ✅ All thresholds read LIVE from config_center.GLOBAL via
     _cc_get_num() — tunable without code changes.
  ✅ The classic BB+RSI+Stoch path is preserved as an OR branch, so
     behaviour is unchanged when VWAP is unavailable (avwap <= 0).
  ✅ Zero behaviour change for SUPERTREND_RIDE, TREND_DOWN_FADE,
     and RANGE_SCALPER.

REV 23.7 (2026-10-03) — RANGE-BOUNDS FIELD FIX (retained):
  ✅ Added rs_rng_min / rs_rng_max as PROPER FamilySpec fields.
     My REV 23.6 tried to read these from config_center.FAMILY[*]
     FILTERS, but those keys never existed there — so all four
     family modules silently fell back to the _RS_RNG_* module
     defaults, losing per-family tuning.
  ✅ strategy_range_scalper now reads from spec.rs_rng_min /
     spec.rs_rng_max — the family-level values that every family
     module passes as constructor kwargs.
  ✅ Family modules (trend/range/momentum/volatility REV 23.3) now
     accept these kwargs without TypeError.

REV 23.6 (2026-10-03) — RANGE-BOUNDS + ZERO-COERCION FIX:
  ✅ FIXED: spec_range_min / spec_range_max were dead shims.
  ✅ REMOVED: spec_range_min / spec_range_max helper functions.
  ✅ FIXED: _mk() min_confidence read no longer uses `or` idiom.
  ✅ ADDED: _cc_get_num() helper.

REV 23.5 (2026-10-02) — DOCSTRING PATH FIX.
REV 23.4 (2026-10-02) — LIVE CONFIG READS + DEAD IMPORT CLEANUP.
REV 23.3 (2026-10-02) — COSMETIC CLEANUP.
REV 23.2 (2026-10-02) — PHASE 3 CLEANUP.
REV 23.1 (2026-10-02) — PHASE 2 CLEANUP.
REV 23.0 (2026-10-02) — UNIFIED CONFIG CLEANUP.
REV 22.2 (2026-10-02) — VOL CLASS CAP ADJUSTMENT.
REV 22.1 (2026-10-02) — CONFIG CENTER INTEGRATION.
REV 22.0 (2026-10-01) — PHASE 1 (3 CHANGES).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from core import config_center as CC
from core.coins_config import (
    get_caps, get_profile, is_enabled,
    get_coin_filters, get_coin_st_params, get_coin_td_fade,
    get_coin_vol_class, get_vol_class_mult,
)
from market.indicators import htf_aligns as _htf_aligns_shared

# ─── REV 22.1 — Centralized config (single source of truth) ───
# ─── REV 23.4 — GLOBAL import removed; use CC.get() for live reads ───
from core.config_center import (
    get_config as _get_central_config,
    get_min_rr as _get_min_rr,
    COUNTER_TREND_STRATEGIES as _COUNTER_TREND_STRATS,
)

# ─── Optional regime-multiplier helper (graceful fallback) ──
try:
    from market.indicators import get_regime_multipliers as _get_regime_mult
except Exception:
    def _get_regime_mult(regime):  # pragma: no cover — fallback
        return {"sl_mult": 1.0, "tp_mult": 1.0}


# ─── Debug flag (per-process, from env) ───────────────────
_DEBUG_STRAT = os.getenv("DEBUG_STRATEGY", "false").strip().lower() == "true"


# ═══════════════════════════════════════════════════════════
#  REV 23.6 — 0-PRESERVING CC NUMERIC READ
#  Mirrors orders/exit.py REV 1.6.1 / entry.py REV 1.8.1 /
#  manage.py REV 1.9.1 / decision_engine.py REV 4.3 /
#  future.py REV 1.4.29.
# ═══════════════════════════════════════════════════════════
def _cc_get_num(key: str, default):
    v = CC.get(key, None)
    return default if v is None else v


# ═══════════════════════════════════════════════════════════
#  TUNABLES
# ═══════════════════════════════════════════════════════════
# NOTE (REV 23.3): DEFAULT_TREND_FOLLOW_RSI_FLOOR removed — inlined
# as `15.0` in FamilySpec dataclass default. All 4 family modules
# pass explicit st_rsi_sell_floor so the default is unused in
# practice; inlining keeps the dataclass self-contained.

HTF_ESCAPE_MIN_ADX   = 20.0
HTF_ESCAPE_MIN_FLIPS = 4

# REV 21.7 — Reversal target multiples (used by MEAN_REVERSION, RANGE_SCALPER)
REVERSAL_TP1_MULT = 1.8
REVERSAL_TP2_MULT = 2.7

# REV 21.5 — Float tolerance for boundary RR comparisons.
_RR_FLOAT_EPS = 1e-6

# REV 23.7 — These are FamilySpec dataclass DEFAULT values.
# Individual families override via the rs_rng_min / rs_rng_max
# constructor kwargs (see trend/volatility/momentum/range modules).
_RS_RNG_MIN_DEFAULT = 0.005
_RS_RNG_MAX_DEFAULT = 0.15
_RS_RNG_MAX_VOLATILE = 0.30


# ═══════════════════════════════════════════════════════════
#  EXTENDED-MOVE GUARD — REV 23.0
#  All thresholds read from config_center at runtime.
# ═══════════════════════════════════════════════════════════
def _extended_entry_guard(ind_1h, ind_4h, side, symbol=""):
    """
    Reject if price is already extended at signal time.

    REV 23.0 — Thresholds come from core/config_center.py GLOBAL:
      - max_move_20bar_pct   (default 0.10)
      - near_120high_tol     (default 0.997)
      - near_120low_tol      (default 1.003)
      - rsi_1h_extreme_buy   (default 72.0)
      - rsi_1h_extreme_sell  (default 28.0)
      - rsi_4h_extreme_buy   (default 72.0)
      - rsi_4h_extreme_sell  (default 28.0)

    Returns (ok, reason). ok=False → caller must reject.
    """
    if not ind_1h:
        return True, ""

    try:
        cfg = _get_central_config(symbol, "", "UNKNOWN")
    except Exception:
        cfg = {}

    # Pull thresholds from config_center (with safe fallbacks)
    _max_move = float(cfg.get("max_move_20bar_pct", 0.10))
    _rsi_1h_buy = float(cfg.get("rsi_1h_extreme_buy", 72.0))
    _rsi_1h_sell = float(cfg.get("rsi_1h_extreme_sell", 28.0))
    _rsi_4h_buy = float(cfg.get("rsi_4h_extreme_buy", 72.0))
    _rsi_4h_sell = float(cfg.get("rsi_4h_extreme_sell", 28.0))
    _tol_hi = float(cfg.get("near_120high_tol", 0.997))
    _tol_lo = float(cfg.get("near_120low_tol", 1.003))

    try:
        price = float(ind_1h.get("price", 0) or 0)
        if price <= 0:
            return True, ""

        # 1. 20-bar momentum
        mom = float(ind_1h.get("momentum_pct", 0.0) or 0.0)
        if side == "BUY" and mom > _max_move:
            return False, f"move20_{mom*100:.1f}%"
        if side == "SELL" and mom < -_max_move:
            return False, f"move20_{mom*100:.1f}%"

        # 2. 120-bar high/low proximity
        dc_hi = float(ind_1h.get("dc_high_slow", 0) or 0)
        dc_lo = float(ind_1h.get("dc_low_slow",  0) or 0)
        if side == "BUY" and dc_hi > 0 and price >= dc_hi * _tol_hi:
            return False, "at_120bar_high"
        if side == "SELL" and dc_lo > 0 and price <= dc_lo * _tol_lo:
            return False, "at_120bar_low"

        # 3. 1h RSI extreme
        rsi_1h = float(ind_1h.get("rsi", 50.0) or 50.0)
        if side == "BUY" and rsi_1h > _rsi_1h_buy:
            return False, f"rsi1h_{rsi_1h:.0f}"
        if side == "SELL" and rsi_1h < _rsi_1h_sell:
            return False, f"rsi1h_{rsi_1h:.0f}"

        # 4. 4h RSI extreme
        if ind_4h:
            rsi_4h = float(ind_4h.get("rsi", 50.0) or 50.0)
            if side == "BUY" and rsi_4h > _rsi_4h_buy:
                return False, f"rsi4h_{rsi_4h:.0f}"
            if side == "SELL" and rsi_4h < _rsi_4h_sell:
                return False, f"rsi4h_{rsi_4h:.0f}"
    except Exception:
        pass
    return True, ""


# ═══════════════════════════════════════════════════════════
#  DIAGNOSTICS TRACKER
# ═══════════════════════════════════════════════════════════
class DiagnosticsTracker:
    """Per-family rejection log."""

    __slots__ = ("log",)

    def __init__(self):
        self.log: dict[str, dict[str, int]] = {}

    def rej(self, name: str, reason: str) -> None:
        if not name:
            name = "UNKNOWN"
        if not reason:
            reason = "unspecified"
        self.log.setdefault(name, {})
        self.log[name][reason] = self.log[name].get(reason, 0) + 1

    def reset(self) -> None:
        self.log.clear()

    def snapshot(self) -> dict:
        return {k: dict(v) for k, v in self.log.items()}

    def format(self) -> str:
        if not self.log:
            return "  (no rejections logged)"
        lines = []
        for strat, reasons in sorted(self.log.items()):
            total = sum(reasons.values())
            detail = ", ".join(
                f"{k}={v}"
                for k, v in sorted(reasons.items(), key=lambda x: -x[1])
            )
            lines.append(f"    {strat:<20} {total:>5}  ({detail})")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════
#  FAMILY SPEC
#  REV 23.0 — Removed dead late_entry_guard_* / top_chase_* fields.
#  config_center always provides these values via _cfg.
#  REV 23.7 — rs_rng_min / rs_rng_max added as proper fields
#             (previously loose attrs set post-construction).
# ═══════════════════════════════════════════════════════════
@dataclass
class FamilySpec:
    name: str
    family_key: str

    regime_strategies: dict
    disabled_strategies: set
    min_adx: dict

    fallback_caps: dict

    min_confidence: float
    min_adx_env: float
    min_sl_pct: float
    min_adx_outside_kz: float

    kz_bypass: bool = False

    block_ny_am_for_st: bool = False
    block_ny_am_for_td_fade: bool = False
    block_quiet: bool = False

    counter_trend_adx_cutoff: float = 35.0

    rsi_buy_overbought: float = 86.0
    rsi_sell_oversold: float = 20.0

    # NOTE: late_entry_guard_* / top_chase_* were REMOVED in REV 23.0.
    #       They are always provided by config_center.get_config().
    #       FamilySpec no longer duplicates them.

    st_min_dist_atr: float = 0.25
    st_max_chase_atr: float = 1.4
    st_flips_min: int = 2
    st_flips_max: int = 12
    st_rsi_buy_min: float = 40.0
    st_rsi_buy_max: float = 70.0
    st_rsi_sell_min: float = 30.0
    st_rsi_sell_max: float = 62.0

    # REV 23.3 — inlined former DEFAULT_TREND_FOLLOW_RSI_FLOOR constant.
    st_rsi_sell_floor: float = 15.0

    pullback_dist_atr: float = 0.8

    mr_rsi_buy_max: float = 30.0
    mr_rsi_sell_min: float = 70.0
    mr_stoch_buy_max: float = 25.0
    mr_stoch_sell_min: float = 75.0

    rs_rsi_buy_max: float = 35.0
    rs_rsi_sell_min: float = 65.0
    rs_sl_atr: float = 1.2

    td_fade_rsi_sell: float = 65.0
    td_fade_rsi_buy: float = 32.0
    td_fade_sl_atr: float = 0.9
    td_fade_adx_max: float = 55.0
    td_fade_ema_tol: float = 1.02
    td_fade_min_adx: float = 20.0
    td_fade_bb_pos_sell: float = 0.98
    td_fade_bb_pos_buy: float = 0.02

    # ═══════════════════════════════════════════════════════════
    #  REV 23.7 — RANGE_SCALPER range-bounds (per-family tunable)
    #  Previously stored as loose attrs (_SPEC._rs_rng_min/max) set
    #  AFTER construction — fragile, easy to lose, and NOT read by
    #  strategy_range_scalper after REV 23.6. Now first-class fields.
    # ═══════════════════════════════════════════════════════════
    rs_rng_min: float = 0.005
    rs_rng_max: float = 0.15


# ═══════════════════════════════════════════════════════════
#  FILTER HELPERS
# ═══════════════════════════════════════════════════════════
def _st_allows(ind_1h, side: str) -> bool:
    st = ind_1h.get("st_trend", "N/A")
    if st == "N/A":
        return True
    if side == "BUY" and st == "DOWN":
        return False
    if side == "SELL" and st == "UP":
        return False
    return True


def _st_policy(spec: FamilySpec, tracker: DiagnosticsTracker,
               name: str, ind_1h, side: str) -> bool:
    if name == "SUPERTREND_RIDE":
        if _st_allows(ind_1h, side):
            return True
        tracker.rej(name, "st_align_block")
        return False

    if name in ("MEAN_REVERSION", "TREND_DOWN_FADE", "RANGE_SCALPER"):
        adx = ind_1h.get("adx", 0)
        if adx < spec.counter_trend_adx_cutoff:
            return True
        if _st_allows(ind_1h, side):
            return True
        tracker.rej(name, "st_align_block_high_adx")
        return False

    return True


def _is_ny_pm(ind_1h) -> bool:
    return ind_1h.get("killzone", "NONE") == "NY_PM"


def _is_ny_am(ind_1h) -> bool:
    return ind_1h.get("killzone", "NONE") == "NY_AM"


def _kz_required(spec: FamilySpec, tracker: DiagnosticsTracker,
                 ind_1h, strat_name: str) -> bool:
    if spec.kz_bypass:
        return True
    if ind_1h.get("killzone", "NONE") == "NONE":
        tracker.rej(strat_name, "no_killzone")
        return False
    return True


def _kz_or_high_adx(spec: FamilySpec, tracker: DiagnosticsTracker,
                    ind_1h, strat_name: str) -> bool:
    if spec.kz_bypass:
        return True
    kz = ind_1h.get("killzone", "NONE")
    if kz != "NONE":
        return True
    if ind_1h.get("adx", 0) >= spec.min_adx_outside_kz:
        return True
    tracker.rej(strat_name, "no_killzone_lowadx")
    return False


# ═══════════════════════════════════════════════════════════
#  MODERN INDICATOR CONFIDENCE MODIFIERS
# ═══════════════════════════════════════════════════════════
def _apply_modern_modifiers(conf: float, side: str,
                            ind_1h, extra: list[str]) -> float:
    if ind_1h is None:
        return conf
    try:
        if CC.get('use_taker_volume'):
            z = float(ind_1h.get("taker_ratio_z", 0.0) or 0.0)
            if side == "BUY" and z > 1.5:
                conf = min(95.0, conf + 4.0)
                extra.append(f"taker+{z:.1f}")
            elif side == "SELL" and z < -1.5:
                conf = min(95.0, conf + 4.0)
                extra.append(f"taker{z:.1f}")

        if CC.get('use_volume_profile'):
            pd_pct = float(ind_1h.get("poc_dist_pct", 0.0) or 0.0)
            if side == "BUY" and -1.5 < pd_pct < 0.5:
                conf = min(95.0, conf + 2.0)
                extra.append("atPOC")
            elif side == "SELL" and -0.5 < pd_pct < 1.5:
                conf = min(95.0, conf + 2.0)
                extra.append("atPOC")

        if CC.get('use_anchored_vwap'):
            ad = float(ind_1h.get("avwap_dist_pct", 0.0) or 0.0)
            if side == "BUY" and 0.0 < ad < 3.0:
                conf = min(95.0, conf + 2.0)
                extra.append(">aVWAP")
            elif side == "SELL" and -3.0 < ad < 0.0:
                conf = min(95.0, conf + 2.0)
                extra.append("<aVWAP")

        if CC.get('use_funding_z'):
            fz = float(ind_1h.get("funding_z", 0.0) or 0.0)
            if side == "BUY" and fz > 2.0:
                conf = max(0.0, conf - 5.0)
                extra.append(f"crowdedL{fz:.1f}")
            elif side == "SELL" and fz < -2.0:
                conf = max(0.0, conf - 5.0)
                extra.append(f"crowdedS{fz:.1f}")

        # Orderbook imbalance modifier (±3, soft)
        try:
            ob_bias = str(ind_1h.get("ob_bias", "BALANCED"))
            ob_imb  = float(ind_1h.get("ob_imbalance", 0.0) or 0.0)
            if side == "BUY":
                if ob_bias == "BUY_PRESSURE" and ob_imb > 0.25:
                    conf = min(95.0, conf + 3.0)
                    extra.append(f"obBuy{ob_imb:.2f}")
                elif ob_bias == "SELL_PRESSURE" and ob_imb < -0.25:
                    conf = max(0.0, conf - 3.0)
                    extra.append(f"obSell{ob_imb:.2f}")
            elif side == "SELL":
                if ob_bias == "SELL_PRESSURE" and ob_imb < -0.25:
                    conf = min(95.0, conf + 3.0)
                    extra.append(f"obSell{ob_imb:.2f}")
                elif ob_bias == "BUY_PRESSURE" and ob_imb > 0.25:
                    conf = max(0.0, conf - 3.0)
                    extra.append(f"obBuy{ob_imb:.2f}")
        except Exception:
            pass
    except Exception:
        pass
    return conf


# ═══════════════════════════════════════════════════════════
#  HTF ALIGNMENT WRAPPER
# ═══════════════════════════════════════════════════════════
def _htf_aligns(ind_4h, side, ind_1h=None):
    if _htf_aligns_shared(ind_4h, side, relaxed=CC.get('htf_align_relaxed')):
        return True

    if not CC.get('htf_align_relaxed') or ind_1h is None:
        return False

    try:
        regime = ind_1h.get("regime", "")
        adx    = float(ind_1h.get("adx", 0) or 0)
        flips  = int(ind_1h.get("st_flips", 0) or 0)

        if side == "SELL" and regime == "TREND_DOWN" \
                and adx >= HTF_ESCAPE_MIN_ADX and flips >= HTF_ESCAPE_MIN_FLIPS:
            return True
        if side == "BUY" and regime == "TREND_UP" \
                and adx >= HTF_ESCAPE_MIN_ADX and flips >= HTF_ESCAPE_MIN_FLIPS:
            return True
    except Exception:
        pass
    return False


# ═══════════════════════════════════════════════════════════
#  SIGNAL BUILDER (with caps + RR floor)
# ═══════════════════════════════════════════════════════════
def _get_caps_safe(spec: FamilySpec, symbol: str) -> dict:
    if not symbol:
        return spec.fallback_caps
    try:
        return get_caps(symbol)
    except Exception:
        return spec.fallback_caps


def _mk(spec: FamilySpec, side, conf, entry, sl, tp1, tp2,
        reasons, symbol="", ind_1h=None, strategy_name=""):
    """Signal builder with caps + RR floor.

    REV 23.1 — 
      • Removed duplicate BTC bias check (decision_engine handles it).
      • min_confidence now from config_center.GLOBAL (fallback: spec).
      • RR floor from config_center.get_min_rr().
    REV 23.4 —
      • min_confidence read LIVE (was: snapshot via _CC_GLOBAL).
    REV 23.6 —
      • min_confidence read 0-preserving via _cc_get_num().
    """
    # NOTE (REV 23.1): The BTC bias gate was REMOVED from here.
    # decision_engine.evaluate_trade() runs the gate as HARD GATE #1
    # — single source of truth. Removing it here eliminates the
    # duplicate log line and the risk of drift between two checks.

    caps = _get_caps_safe(spec, symbol)

    # ── Vol class cap adjustment (REV 22.2) ──
    max_sl_pct  = caps["sl"]
    max_tp1_pct = caps["tp1"]
    max_tp2_pct = caps["tp2"]
    try:
        vcls = get_coin_vol_class(symbol) or "MED"
        vmult = get_vol_class_mult(vcls)
        max_sl_pct  *= vmult
        max_tp1_pct *= vmult
        max_tp2_pct *= vmult
        if _DEBUG_STRAT:
            print(f"[_mk] {symbol} vol_class={vcls} mult={vmult:.2f} "
                  f"→ caps sl={max_sl_pct:.4f} tp1={max_tp1_pct:.4f} tp2={max_tp2_pct:.4f}")
    except Exception as _vce:
        if _DEBUG_STRAT:
            print(f"[_mk] {symbol} vol_class mult failed: {_vce}")

    if entry <= 0:
        if _DEBUG_STRAT:
            print(f"[_mk] {symbol} REJECT: entry<=0 (entry={entry})")
        return None

    if side == "BUY":
        sl = min(sl, entry * (1.0 - spec.min_sl_pct))
        sl = max(sl, entry * (1.0 - max_sl_pct))
    else:
        sl = max(sl, entry * (1.0 + spec.min_sl_pct))
        sl = min(sl, entry * (1.0 + max_sl_pct))

    risk = abs(entry - sl)
    if risk <= 0:
        if _DEBUG_STRAT:
            print(f"[_mk] {symbol} REJECT: risk<=0 (entry={entry} sl={sl})")
        return None

    if risk / entry > max_sl_pct + 1e-9:
        if _DEBUG_STRAT:
            print(f"[_mk] {symbol} REJECT: risk_pct {risk/entry*100:.3f}% "
                  f"> cap {max_sl_pct*100:.3f}%")
        return None

    if side == "BUY":
        if tp1 <= entry or tp2 <= entry:
            if _DEBUG_STRAT:
                print(f"[_mk] {symbol} REJECT: TP wrong side BUY "
                      f"(entry={entry} tp1={tp1} tp2={tp2})")
            return None
    else:
        if tp1 >= entry or tp2 >= entry:
            if _DEBUG_STRAT:
                print(f"[_mk] {symbol} REJECT: TP wrong side SELL "
                      f"(entry={entry} tp1={tp1} tp2={tp2})")
            return None

    extra_reasons: list[str] = []
    conf = _apply_modern_modifiers(conf, side, ind_1h, extra_reasons)
    if extra_reasons:
        reasons = f"{reasons} | {' '.join(extra_reasons)}"

    if side == "BUY":
        tp1 = entry + min(tp1 - entry, entry * max_tp1_pct)
        tp2 = entry + min(tp2 - entry, entry * max_tp2_pct)
    else:
        tp1 = entry - min(entry - tp1, entry * max_tp1_pct)
        tp2 = entry - min(entry - tp2, entry * max_tp2_pct)

    rr = abs(tp1 - entry) / risk

    # ── REV 23.0 — RR floor from config_center ──
    _effective_floor = _get_min_rr(strategy_name)
    if rr < _effective_floor - _RR_FLOAT_EPS:
        if _DEBUG_STRAT:
            print(f"[_mk] {symbol} REJECT: rr {rr:.3f} < {_effective_floor} "
                  f"[{strategy_name or 'UNKNOWN'}] "
                  f"(entry={entry} sl={sl} tp1={tp1} risk={risk})")
        return None

    # ── REV 23.4 / 23.6 — min_confidence LIVE read (fallback: spec) ──
    # 0-preserving: a CC_MIN_CONFIDENCE=0 config is honoured as-is.
    _min_conf = float(_cc_get_num("min_confidence", spec.min_confidence))
    if conf < _min_conf:
        if _DEBUG_STRAT:
            print(f"[_mk] {symbol} REJECT: conf {conf:.1f} < "
                  f"{_min_conf} (rr={rr:.2f})")
        return None

    sig = ("STRONG_BUY" if conf >= 72 else "BUY") if side == "BUY" \
          else ("STRONG_SELL" if conf >= 72 else "SELL")

    return {
        "sig": sig, "side": side, "conf": round(min(conf, 95), 1),
        "entry": float(entry),
        "sl": float(sl), "tp1": float(tp1), "tp2": float(tp2),
        "rr": round(rr, 2),
        "reasons": reasons,
    }


# ═══════════════════════════════════════════════════════════
#  STRATEGY: SUPERTREND_RIDE
# ═══════════════════════════════════════════════════════════
def strategy_supertrend_ride(spec, tracker, ind_1h, ind_4h,
                             ind_1d=None, symbol=""):
    filters = get_coin_filters(symbol)
    st_params = get_coin_st_params(symbol)

    regime = ind_1h.get("regime", "UNKNOWN") if ind_1h else "UNKNOWN"
    _cfg = _get_central_config(symbol, "SUPERTREND_RIDE", regime)

    rsi_buy_min   = filters.get("rsi_buy_min",   spec.st_rsi_buy_min)
    rsi_buy_max   = filters.get("rsi_buy_max",   spec.st_rsi_buy_max)
    rsi_sell_max  = filters.get("rsi_sell_max",  spec.st_rsi_sell_max)
    trend_follow_floor = filters.get("st_rsi_sell_floor",
                                     spec.st_rsi_sell_floor)
    # ── REV 23.2 — pull from filters (config_center primary) ──
    rsi_buy_overbought = filters.get("rsi_buy_overbought",
                                     spec.rsi_buy_overbought)
    rsi_sell_oversold  = filters.get("rsi_sell_oversold",
                                     spec.rsi_sell_oversold)

    min_flips     = filters.get("min_flips",     spec.st_flips_min)
    max_flips     = filters.get("max_flips",     spec.st_flips_max)
    min_dist_atr  = filters.get("min_dist_atr",  spec.st_min_dist_atr)
    max_dist_atr  = filters.get("max_dist_atr",  spec.st_max_chase_atr)
    pullback_atr  = filters.get("pullback_dist_atr", spec.pullback_dist_atr)
    min_adx_st    = filters.get("min_adx_st",
                                max(spec.min_adx["SUPERTREND_RIDE"],
                                    spec.min_adx_env))
    guard_dist    = filters.get("late_guard_dist", 1.0)
    top_rsi       = filters.get("top_chase_rsi",   68.0)
    top_flips     = filters.get("top_chase_flips", 4)
    block_ny_am   = filters.get("block_ny_am",  spec.block_ny_am_for_st)
    block_ny_pm   = filters.get("block_ny_pm",  False)

    # Late guard from config_center (single source)
    late_adx = float(_cfg.get("late_guard_adx", 35.0))
    late_rsi = float(_cfg.get("late_guard_rsi", 58.0))

    if _is_ny_pm(ind_1h) and block_ny_pm:
        tracker.rej("SUPERTREND_RIDE", "NY_PM"); return None
    if _is_ny_am(ind_1h) and block_ny_am:
        tracker.rej("SUPERTREND_RIDE", "NY_AM"); return None

    if not _kz_required(spec, tracker, ind_1h, "SUPERTREND_RIDE"):
        return None

    st_trend = ind_1h.get("st_trend", "N/A")
    st_flips = ind_1h.get("st_flips", 0)
    adx      = ind_1h.get("adx", 0)
    rsi      = ind_1h.get("rsi", 50)

    if st_trend == "N/A":
        tracker.rej("SUPERTREND_RIDE", "st_na"); return None

    if regime == "CHOP":
        tracker.rej("SUPERTREND_RIDE", "regime_chop"); return None
    if regime == "QUIET":
        tracker.rej("SUPERTREND_RIDE", "regime_quiet"); return None

    if st_flips < min_flips:
        tracker.rej("SUPERTREND_RIDE", "st_flips_low"); return None
    if st_flips > max_flips:
        tracker.rej("SUPERTREND_RIDE", "st_flips_high"); return None
    if adx < min_adx_st:
        tracker.rej("SUPERTREND_RIDE", "adx_low"); return None

    price  = ind_1h.get("price", 0)
    atr    = ind_1h.get("atr", 0)
    st_val = ind_1h.get("st_value", 0)
    ema20  = ind_1h.get("ema20", 0)
    if price <= 0 or atr <= 0:
        tracker.rej("SUPERTREND_RIDE", "bad_data"); return None

    _coin_sl  = st_params.get("sl_atr",  2.5)
    _coin_tp1 = st_params.get("tp1_atr", 2.5)
    _coin_tp2 = st_params.get("tp2_atr", 5.0)

    sl_atr  = float(_cfg.get("sl_atr",  _coin_sl))  * float(_cfg.get("sl_mult", 1.0))
    tp1_atr = float(_cfg.get("tp1_atr", _coin_tp1)) * float(_cfg.get("tp_mult", 1.0))
    tp2_atr = float(_cfg.get("tp2_atr", _coin_tp2)) * float(_cfg.get("tp_mult", 1.0))

    # Exhausted thresholds from config_center
    _exh_rsi_buy  = float(_cfg.get("exhausted_rsi_buy", 78.0))
    _exh_rsi_sell = float(_cfg.get("exhausted_rsi_sell", 22.0))
    _exh_dist_mult = float(_cfg.get("exhausted_dist_mult", 1.5))

    # ── BUY ──
    if st_trend == "UP" and price > st_val:
        if regime in ("CHOP", "QUIET"):
            tracker.rej("SUPERTREND_RIDE", f"regime_{regime.lower()}_no_buy")
            return None
        if not _htf_aligns(ind_4h, "BUY", ind_1h=ind_1h):
            tracker.rej("SUPERTREND_RIDE", "htf_buy"); return None
        if rsi < rsi_buy_min:
            tracker.rej("SUPERTREND_RIDE", f"rsi_buy_low_{rsi:.0f}"); return None
        if rsi > rsi_buy_max:
            tracker.rej("SUPERTREND_RIDE", f"rsi_buy_high_{rsi:.0f}"); return None
        if rsi > rsi_buy_overbought:
            tracker.rej("SUPERTREND_RIDE", "rsi_overbought"); return None
        if rsi > top_rsi and st_flips > top_flips:
            tracker.rej("SUPERTREND_RIDE", "top_chase_buy"); return None

        if ema20 > 0:
            ext_atr = (price - ema20) / atr
            if ext_atr > pullback_atr:
                tracker.rej("SUPERTREND_RIDE",
                            f"extended_{ext_atr:.2f}atr")
                return None

        dist = price - st_val
        dist_atr = dist / atr

        if rsi >= _exh_rsi_buy and dist_atr >= guard_dist * _exh_dist_mult:
            tracker.rej("SUPERTREND_RIDE",
                        f"exhausted_{adx:.0f}_{rsi:.0f}"); return None

        if dist > atr * max_dist_atr:
            tracker.rej("SUPERTREND_RIDE", "chasing"); return None
        if dist_atr < min_dist_atr:
            tracker.rej("SUPERTREND_RIDE",
                        f"st_too_close_{min_dist_atr:.2f}"); return None

        sl  = price - atr * sl_atr
        tp1 = price + atr * tp1_atr
        tp2 = price + atr * tp2_atr

        _risk = abs(price - sl)
        if _risk > 0:
            _min_tp1 = _risk * 1.5
            if abs(tp1 - price) < _min_tp1:
                tp1 = price + _min_tp1
            if abs(tp2 - price) < _min_tp1 * 1.5:
                tp2 = price + _min_tp1 * 1.5

        if adx > late_adx and rsi > late_rsi:
            tracker.rej("SUPERTREND_RIDE", f"late_{adx:.0f}_{rsi:.0f}")
            return None

        conf = 63 + min(12, st_flips)
        return _mk(spec, "BUY", conf, price, sl, tp1, tp2,
                   f"ST_BUY flips={st_flips} adx={adx:.0f} "
                   f"dist={dist_atr:.2f}atr rsi={rsi:.0f}",
                   symbol=symbol, ind_1h=ind_1h,
                   strategy_name="SUPERTREND_RIDE")

    # ── SELL ──
    if st_trend == "DOWN" and price < st_val:
        if regime in ("CHOP", "QUIET"):
            tracker.rej("SUPERTREND_RIDE", f"regime_{regime.lower()}_no_sell")
            return None
        if not _htf_aligns(ind_4h, "SELL", ind_1h=ind_1h):
            tracker.rej("SUPERTREND_RIDE", "htf_sell"); return None
        if rsi < trend_follow_floor:
            tracker.rej("SUPERTREND_RIDE", f"rsi_sell_low_{rsi:.0f}"); return None
        if rsi > rsi_sell_max:
            tracker.rej("SUPERTREND_RIDE", f"rsi_sell_high_{rsi:.0f}"); return None
        if rsi < rsi_sell_oversold:
            tracker.rej("SUPERTREND_RIDE", "rsi_oversold"); return None

        if ema20 > 0:
            ext_atr = (ema20 - price) / atr
            if ext_atr > pullback_atr:
                tracker.rej("SUPERTREND_RIDE",
                            f"extended_sell_{ext_atr:.2f}atr")
                return None

        dist = st_val - price
        dist_atr = dist / atr

        if rsi <= _exh_rsi_sell and dist_atr >= guard_dist * _exh_dist_mult:
            tracker.rej("SUPERTREND_RIDE",
                        f"exhausted_sell_{adx:.0f}_{rsi:.0f}"); return None

        if dist > atr * max_dist_atr:
            tracker.rej("SUPERTREND_RIDE", "chasing"); return None
        if dist_atr < min_dist_atr:
            tracker.rej("SUPERTREND_RIDE",
                        f"st_too_close_{min_dist_atr:.2f}"); return None

        sl  = price + atr * sl_atr
        tp1 = price - atr * tp1_atr
        tp2 = price - atr * tp2_atr

        _risk = abs(sl - price)
        if _risk > 0:
            _min_tp1 = _risk * 1.5
            if abs(price - tp1) < _min_tp1:
                tp1 = price - _min_tp1
            if abs(price - tp2) < _min_tp1 * 1.5:
                tp2 = price - _min_tp1 * 1.5

        if adx > late_adx and rsi < (100.0 - late_rsi):
            tracker.rej("SUPERTREND_RIDE", f"late_{adx:.0f}_{rsi:.0f}")
            return None

        conf = 63 + min(12, st_flips)
        return _mk(spec, "SELL", conf, price, sl, tp1, tp2,
                   f"ST_SELL flips={st_flips} adx={adx:.0f} "
                   f"dist={dist_atr:.2f}atr rsi={rsi:.0f}",
                   symbol=symbol, ind_1h=ind_1h,
                   strategy_name="SUPERTREND_RIDE")

    tracker.rej("SUPERTREND_RIDE", "no_pattern")
    return None


# ═══════════════════════════════════════════════════════════
#  STRATEGY: TREND_DOWN_FADE
# ═══════════════════════════════════════════════════════════
def strategy_trend_down_fade(spec, tracker, ind_1h, ind_4h,
                             ind_1d=None, symbol=""):
    tdf = get_coin_td_fade(symbol)
    rsi_sell = tdf.get("rsi_sell", spec.td_fade_rsi_sell)
    rsi_buy  = tdf.get("rsi_buy",  spec.td_fade_rsi_buy)
    sl_atr   = tdf.get("sl_atr",   spec.td_fade_sl_atr)
    adx_max  = tdf.get("adx_max",  spec.td_fade_adx_max)
    ema_tol  = tdf.get("ema_tol",  spec.td_fade_ema_tol)
    min_adx  = tdf.get("min_adx",  spec.td_fade_min_adx)
    bb_sell  = tdf.get("bb_pos_sell", spec.td_fade_bb_pos_sell)
    bb_buy   = tdf.get("bb_pos_buy",  spec.td_fade_bb_pos_buy)
    en_sell  = tdf.get("enabled_sell", True)
    en_buy   = tdf.get("enabled_buy",  False)

    regime = ind_1h.get("regime", "UNKNOWN")
    if regime != "TREND_DOWN":
        tracker.rej("TREND_DOWN_FADE", "regime"); return None
    if not _kz_required(spec, tracker, ind_1h, "TREND_DOWN_FADE"):
        return None

    if not spec.kz_bypass and _is_ny_pm(ind_1h):
        tracker.rej("TREND_DOWN_FADE", "NY_PM"); return None
    if spec.block_ny_am_for_td_fade and _is_ny_am(ind_1h):
        tracker.rej("TREND_DOWN_FADE", "NY_AM"); return None

    adx = ind_1h.get("adx", 0)
    if adx < min_adx:
        tracker.rej("TREND_DOWN_FADE", "adx_low"); return None
    if adx > adx_max:
        tracker.rej("TREND_DOWN_FADE", "adx_too_high"); return None

    price = ind_1h.get("price", 0); atr = ind_1h.get("atr", 0)
    rsi   = ind_1h.get("rsi", 50)
    bb_lo = ind_1h.get("bb_lower", 0); bb_up = ind_1h.get("bb_upper", 0)
    ema20 = ind_1h.get("ema20", 0)

    if price <= 0 or atr <= 0 or bb_lo <= 0 or bb_up <= 0 or ema20 <= 0:
        tracker.rej("TREND_DOWN_FADE", "bad_data"); return None

    _reg = _get_regime_mult(regime)
    sl_atr = sl_atr * _reg["sl_mult"]

    mid = (bb_lo + bb_up) / 2
    bb_range = max(bb_up - bb_lo, 1e-9)
    bb_pos = (price - bb_lo) / bb_range

    if en_sell and (bb_pos >= bb_sell and rsi > rsi_sell
                    and price < ema20 * ema_tol):
        sl  = price + atr * sl_atr
        tp1 = mid
        tp2 = bb_lo * 1.005
        conf = 68 + min(10, (rsi - rsi_sell))
        return _mk(spec, "SELL", conf, price, sl, tp1, tp2,
                   f"TD_FADE_SELL rsi={rsi:.0f} adx={adx:.0f}",
                   symbol=symbol, ind_1h=ind_1h,
                   strategy_name="TREND_DOWN_FADE")

    if bb_pos <= bb_buy and rsi < rsi_buy:
        if not en_buy:
            tracker.rej("TREND_DOWN_FADE", "buy_disabled")
            return None
        sl  = price - atr * sl_atr
        tp1 = mid
        tp2 = bb_up * 0.995
        conf = 68 + min(10, (rsi_buy - rsi))
        return _mk(spec, "BUY", conf, price, sl, tp1, tp2,
                   f"TD_FADE_BUY rsi={rsi:.0f} adx={adx:.0f}",
                   symbol=symbol, ind_1h=ind_1h,
                   strategy_name="TREND_DOWN_FADE")

    tracker.rej("TREND_DOWN_FADE", "no_pattern")
    return None


# ═══════════════════════════════════════════════════════════
#  STRATEGY: MEAN_REVERSION (CHOP only)
#  REV 23.8 — VWAP MEAN REVERSION OVERLAY ADDED
# ═══════════════════════════════════════════════════════════
def strategy_mean_reversion(spec, tracker, ind_1h, ind_4h,
                            ind_1d=None, symbol=""):
    """
    CHOP-regime mean-reversion with an OPTIONAL VWAP-distance overlay.

    Fires in two distinct modes (OR-combined):

    MODE A — Classic band reversion (unchanged since REV 21.7):
      • RSI < mr_rsi_buy_max AND Stoch RSI < mr_stoch_buy_max
        AND price <= BB lower        → BUY
      • RSI > mr_rsi_sell_min AND Stoch RSI > mr_stoch_sell_min
        AND price >= BB upper        → SELL

    MODE B — VWAP-distance reversion (NEW in REV 23.8):
      • |avwap_dist_pct| >= vwap_rev_min_dist_pct
      • RSI <= vwap_rev_rsi_buy_max (extreme oversold) for BUY
        or RSI >= vwap_rev_rsi_sell_min (extreme overbought) for SELL
      • ATR ratio < 1.8 (reject expanding volatility — reversion
        trades die in expansion)
      Target TP2 = Anchored VWAP (mean), capped by 3× risk so the
      RR floor (MIN_RR["MEAN_REVERSION"] = 1.8) is always preserved.

    All MODE B thresholds are read LIVE from config_center.GLOBAL so
    they can be tuned via .env without editing code.
    """
    filters = get_coin_filters(symbol)

    rsi_buy_max    = filters.get("mr_rsi_buy_max",    spec.mr_rsi_buy_max)
    rsi_sell_min   = filters.get("mr_rsi_sell_min",   spec.mr_rsi_sell_min)
    stoch_buy_max  = filters.get("mr_stoch_buy_max",  spec.mr_stoch_buy_max)
    stoch_sell_min = filters.get("mr_stoch_sell_min", spec.mr_stoch_sell_min)

    regime = ind_1h.get("regime", "UNKNOWN")
    if regime != "CHOP":
        tracker.rej("MEAN_REVERSION", "regime"); return None
    if not _kz_required(spec, tracker, ind_1h, "MEAN_REVERSION"):
        return None
    if ind_1h.get("adx", 0) < spec.min_adx["MEAN_REVERSION"]:
        tracker.rej("MEAN_REVERSION", "adx_low"); return None

    price = ind_1h.get("price", 0); atr = ind_1h.get("atr", 0)
    rsi = ind_1h.get("rsi", 50)
    bb_lo = ind_1h.get("bb_lower", 0); bb_up = ind_1h.get("bb_upper", 0)
    stoch = ind_1h.get("stoch_rsi", 50)
    atr_ratio = ind_1h.get("atr_ratio", 1.0)
    st_trend = ind_1h.get("st_trend", "N/A")

    if price <= 0 or atr <= 0 or bb_lo <= 0 or bb_up <= 0:
        tracker.rej("MEAN_REVERSION", "bad_data"); return None

    if atr_ratio > 2.0:
        tracker.rej("MEAN_REVERSION", f"atr_expanding_{atr_ratio:.2f}")
        return None

    _reg = _get_regime_mult(regime)
    sl_atr = 1.5 * _reg["sl_mult"]

    mid = (bb_lo + bb_up) / 2

    # ═══════════════════════════════════════════════════════════
    #  REV 23.8 — VWAP MEAN REVERSION OVERLAY
    #  Read thresholds LIVE from config_center. Zero-preserving read.
    # ═══════════════════════════════════════════════════════════
    avwap = float(ind_1h.get("anchored_vwap", 0) or 0)
    avwap_dist = float(ind_1h.get("avwap_dist_pct", 0) or 0)
    _vwap_min_dist  = float(_cc_get_num("vwap_rev_min_dist_pct", 1.5))
    _vwap_rsi_buy   = float(_cc_get_num("vwap_rev_rsi_buy_max", 32.0))
    _vwap_rsi_sell  = float(_cc_get_num("vwap_rev_rsi_sell_min", 68.0))
    vwap_target = avwap if avwap > 0 else mid

    # ── BUY: classic OR VWAP reversion ──
    classic_buy = (rsi < rsi_buy_max and stoch < stoch_buy_max
                   and price <= bb_lo)
    vwap_buy = (avwap > 0 and avwap_dist <= -_vwap_min_dist
                and rsi < _vwap_rsi_buy and atr_ratio < 1.8)

    if classic_buy or vwap_buy:
        if st_trend == "DOWN":
            tracker.rej("MEAN_REVERSION", "st_down_no_buy")
            return None
        sl = price - atr * sl_atr
        risk = abs(price - sl)
        tp1 = price + risk * REVERSAL_TP1_MULT
        tp2_candidate = max(mid, price + risk * REVERSAL_TP2_MULT)
        if vwap_buy and vwap_target > price:
            tp2_candidate = min(vwap_target, price + risk * 3.0)
        tp2 = tp2_candidate
        tag = "VWAP_REV_BUY" if vwap_buy else "CHOP_BUY"
        conf = 72 + min(15, (rsi_buy_max - rsi))
        if vwap_buy:
            conf = min(95, conf + 4)
        return _mk(spec, "BUY", conf, price, sl, tp1, tp2,
                   f"{tag} rsi={rsi:.0f} avwap_dist={avwap_dist:.2f}%",
                   symbol=symbol, ind_1h=ind_1h,
                   strategy_name="MEAN_REVERSION")

    # ── SELL: mirror ──
    classic_sell = (rsi > rsi_sell_min and stoch > stoch_sell_min
                    and price >= bb_up)
    vwap_sell = (avwap > 0 and avwap_dist >= _vwap_min_dist
                 and rsi > _vwap_rsi_sell and atr_ratio < 1.8)

    if classic_sell or vwap_sell:
        if st_trend == "UP":
            tracker.rej("MEAN_REVERSION", "st_up_no_sell")
            return None
        sl = price + atr * sl_atr
        risk = abs(sl - price)
        tp1 = price - risk * REVERSAL_TP1_MULT
        tp2_candidate = min(mid, price - risk * REVERSAL_TP2_MULT)
        if vwap_sell and 0 < vwap_target < price:
            tp2_candidate = max(vwap_target, price - risk * 3.0)
        tp2 = tp2_candidate
        tag = "VWAP_REV_SELL" if vwap_sell else "CHOP_SELL"
        conf = 72 + min(15, (rsi - rsi_sell_min))
        if vwap_sell:
            conf = min(95, conf + 4)
        return _mk(spec, "SELL", conf, price, sl, tp1, tp2,
                   f"{tag} rsi={rsi:.0f} avwap_dist={avwap_dist:.2f}%",
                   symbol=symbol, ind_1h=ind_1h,
                   strategy_name="MEAN_REVERSION")

    tracker.rej("MEAN_REVERSION", "no_pattern")
    return None


# ═══════════════════════════════════════════════════════════
#  STRATEGY: RANGE_SCALPER
#  REV 23.7 — range bounds now read from spec.rs_rng_min/max
#             (proper FamilySpec fields, per-family tunable).
#             _RS_RNG_MAX_VOLATILE still overrides in VOLATILE regime.
# ═══════════════════════════════════════════════════════════
def strategy_range_scalper(spec, tracker, ind_1h, ind_4h,
                           ind_1d=None, symbol=""):
    filters = get_coin_filters(symbol)
    rsi_buy_max  = filters.get("rs_rsi_buy_max",  spec.rs_rsi_buy_max)
    rsi_sell_min = filters.get("rs_rsi_sell_min", spec.rs_rsi_sell_min)

    # REV 23.7 — read from spec fields (was: dead filters.get on
    # keys that never existed). Family modules now pass
    # rs_rng_min / rs_rng_max as constructor kwargs.
    rng_min = float(spec.rs_rng_min)
    rng_max_base = float(spec.rs_rng_max)

    regime = ind_1h.get("regime", "UNKNOWN")
    if regime not in ("QUIET", "CHOP", "VOLATILE"):
        tracker.rej("RANGE_SCALPER", "regime"); return None
    if not _kz_or_high_adx(spec, tracker, ind_1h, "RANGE_SCALPER"):
        return None
    if ind_1h.get("adx", 0) < spec.min_adx["RANGE_SCALPER"]:
        tracker.rej("RANGE_SCALPER", "adx_low"); return None

    price = ind_1h.get("price", 0); atr = ind_1h.get("atr", 0)
    rsi = ind_1h.get("rsi", 50)
    st_trend = ind_1h.get("st_trend", "N/A")
    atr_ratio = ind_1h.get("atr_ratio", 1.0)

    dc_hi_fast = ind_1h.get("dc_high", 0)
    dc_lo_fast = ind_1h.get("dc_low", 0)
    dc_hi_slow = ind_1h.get("dc_high_slow", 0)
    dc_lo_slow = ind_1h.get("dc_low_slow", 0)

    dc_hi = dc_hi_fast
    dc_lo = dc_lo_fast

    if price <= 0 or atr <= 0 or dc_hi <= 0 or dc_lo <= 0:
        tracker.rej("RANGE_SCALPER", "bad_data"); return None

    atr_limit = 2.5 if regime == "VOLATILE" else 1.5
    if atr_ratio > atr_limit:
        tracker.rej("RANGE_SCALPER", f"atr_expanding_{atr_ratio:.2f}")
        return None

    rng = dc_hi - dc_lo
    rng_pct = rng / price
    rng_max = _RS_RNG_MAX_VOLATILE if regime == "VOLATILE" else rng_max_base
    if rng_pct < rng_min or rng_pct > rng_max:
        tracker.rej("RANGE_SCALPER", f"range_bad_{rng_pct*100:.2f}%")
        return None
    mid = (dc_hi + dc_lo) / 2

    _tp_hi = dc_hi_slow if dc_hi_slow > 0 else dc_hi
    _tp_lo = dc_lo_slow if dc_lo_slow > 0 else dc_lo
    _tp_rng = _tp_hi - _tp_lo

    _reg = _get_regime_mult(regime)
    rs_sl_atr = spec.rs_sl_atr * _reg["sl_mult"]

    if price <= dc_lo + rng * 0.2 and rsi < rsi_buy_max:
        if st_trend == "DOWN":
            tracker.rej("RANGE_SCALPER", "st_down_no_buy")
            return None
        sl = dc_lo - atr * rs_sl_atr
        risk = abs(price - sl)
        tp1 = price + risk * REVERSAL_TP1_MULT
        tp2 = max(_tp_hi - _tp_rng * 0.1, price + risk * REVERSAL_TP2_MULT)
        conf = 70 + min(10, (rsi_buy_max - rsi))
        return _mk(spec, "BUY", conf, price, sl, tp1, tp2,
                   f"RNG_BUY rng={rng_pct*100:.2f}%",
                   symbol=symbol, ind_1h=ind_1h,
                   strategy_name="RANGE_SCALPER")

    if price >= dc_hi - rng * 0.2 and rsi > rsi_sell_min:
        if st_trend == "UP":
            tracker.rej("RANGE_SCALPER", "st_up_no_sell")
            return None
        sl = dc_hi + atr * rs_sl_atr
        risk = abs(sl - price)
        tp1 = price - risk * REVERSAL_TP1_MULT
        tp2 = min(_tp_lo + _tp_rng * 0.1, price - risk * REVERSAL_TP2_MULT)
        conf = 70 + min(10, (rsi - rsi_sell_min))
        return _mk(spec, "SELL", conf, price, sl, tp1, tp2,
                   f"RNG_SELL rng={rng_pct*100:.2f}%",
                   symbol=symbol, ind_1h=ind_1h,
                   strategy_name="RANGE_SCALPER")

    tracker.rej("RANGE_SCALPER", "no_pattern")
    return None


# ═══════════════════════════════════════════════════════════
#  REGISTRY FACTORY
# ═══════════════════════════════════════════════════════════
def build_all_strategies(spec: FamilySpec, tracker: DiagnosticsTracker) -> dict:
    return {
        "MEAN_REVERSION":  lambda i1, i4, i1d, s: strategy_mean_reversion(
            spec, tracker, i1, i4, i1d, s),
        "RANGE_SCALPER":   lambda i1, i4, i1d, s: strategy_range_scalper(
            spec, tracker, i1, i4, i1d, s),
        "SUPERTREND_RIDE": lambda i1, i4, i1d, s: strategy_supertrend_ride(
            spec, tracker, i1, i4, i1d, s),
        "TREND_DOWN_FADE": lambda i1, i4, i1d, s: strategy_trend_down_fade(
            spec, tracker, i1, i4, i1d, s),
    }


# ═══════════════════════════════════════════════════════════
#  ROUTER
# ═══════════════════════════════════════════════════════════
def route_signal(spec: FamilySpec, tracker: DiagnosticsTracker,
                 strategies: dict,
                 ind_1h, ind_4h, ind_1d=None,
                 profile: str = "MIXED", symbol: str = "",
                 mode: str = None):
    _ = mode

    if not ind_1h:
        tracker.rej("ROUTER", "no_ind_1h")
        return None

    regime = ind_1h.get("regime", "UNKNOWN")

    if spec.block_quiet and regime == "QUIET":
        return None

    strategies_to_try = (
        spec.regime_strategies.get(regime, set(strategies.keys()))
        - spec.disabled_strategies
    )

    if not strategies_to_try:
        tracker.rej("ROUTER", f"no_strategy_for_{regime.lower()}")
        return None

    coin_profile = get_profile(symbol) if symbol else profile
    bonus_map = {
        "RANGE": "MEAN_REVERSION", "TREND": "SUPERTREND_RIDE",
        "MOMENTUM": "SUPERTREND_RIDE", "VOLATILE": "SUPERTREND_RIDE",
    }
    bonus_strategy = bonus_map.get(coin_profile)

    results = []
    for name, fn in strategies.items():
        if name not in strategies_to_try:
            continue
        try:
            r = fn(ind_1h, ind_4h, ind_1d, symbol)
            if r is None:
                continue
            if not _st_policy(spec, tracker, name, ind_1h, r["side"]):
                continue

            if bonus_strategy and name == bonus_strategy:
                r["conf"] = min(95.0, r["conf"] + 5.0)

            try:
                from signals.decision_engine import get_strategy_multiplier
                _mult = get_strategy_multiplier(name)
                if _mult and _mult != 1.0:
                    r["conf"] = min(95.0, r["conf"] * _mult)
            except Exception:
                pass

            r["strategy"] = name
            results.append(r)
        except Exception as e:
            tracker.rej(name, f"exception_{type(e).__name__}")

    if not results:
        return None

    if len(results) >= 2:
        buy_votes  = sum(1 for r in results if r["side"] == "BUY")
        sell_votes = sum(1 for r in results if r["side"] == "SELL")
        if buy_votes >= 2:
            results = [r for r in results if r["side"] == "BUY"]
        elif sell_votes >= 2:
            results = [r for r in results if r["side"] == "SELL"]
        else:
            tracker.rej("ROUTER", "conflicting_signals")
            return None

    return max(results, key=lambda x: (x["conf"], x["rr"]))


# ═══════════════════════════════════════════════════════════
#  LIVE SIGNAL GENERATOR
# ═══════════════════════════════════════════════════════════
def generate_signal_live(spec: FamilySpec, tracker: DiagnosticsTracker,
                         family_coins: set, strategies: dict,
                         ind_1h, ind_4h, ind_1d=None,
                         symbol="", mode="MULTI"):
    empty_lvl = {"SL": 0.0, "TP1": 0.0, "TP2": 0.0, "Qty": 0.0, "RR": 0.0}

    if symbol and not is_family_coin(family_coins, symbol):
        return "NEUTRAL", 0.0, [f"not_{spec.name.lower()}_coin"], empty_lvl, "NONE"

    tracker.reset()

    profile = get_profile(symbol)
    result = route_signal(spec, tracker, strategies,
                          ind_1h, ind_4h, ind_1d,
                          profile=profile, symbol=symbol, mode=mode)

    if result is None:
        rej_summary: list[str] = []
        try:
            snapshot = tracker.snapshot()
            all_rejs: list[tuple[int, str]] = []
            for strat_name, reason_counts in snapshot.items():
                if not reason_counts:
                    continue
                top_reason, top_count = max(
                    reason_counts.items(), key=lambda kv: kv[1]
                )
                all_rejs.append((top_count, f"{strat_name}:{top_reason}"))
            if all_rejs:
                all_rejs.sort(reverse=True)
                rej_summary = [all_rejs[0][1]]
        except Exception:
            pass

        if _DEBUG_STRAT:
            try:
                dbg_str = tracker.format().replace("\n", " | ")
            except Exception:
                dbg_str = "(no rejections)"
            if ind_1h:
                print(f"[DEBUG {symbol}] NEUTRAL | "
                      f"regime={ind_1h.get('regime')} "
                      f"st_trend={ind_1h.get('st_trend')} "
                      f"st_flips={ind_1h.get('st_flips')} "
                      f"adx={ind_1h.get('adx', 0):.1f} "
                      f"rsi={ind_1h.get('rsi', 0):.1f}")
            else:
                print(f"[DEBUG {symbol}] NEUTRAL | ind_1h=None")
            print(f"[DEBUG {symbol}] REJECTIONS: {dbg_str}")

        return "NEUTRAL", 0.0, rej_summary, empty_lvl, "NONE"

    _ext_ok, _ext_reason = _extended_entry_guard(
        ind_1h, ind_4h, result["side"], symbol
    )
    if not _ext_ok:
        tracker.rej(result.get("strategy", "UNKNOWN"), f"ext_{_ext_reason}")
        if _DEBUG_STRAT:
            print(f"[{symbol}] EXT-MOVE REJECT: {_ext_reason}")
        return "NEUTRAL", 0.0, [f"ext_{_ext_reason}"], empty_lvl, "NONE"

    lvl = {
        "SL": result["sl"], "TP1": result["tp1"], "TP2": result["tp2"],
        "Qty": 0.0, "RR": result["rr"],
    }
    reasons = []
    if result.get("strategy"):
        reasons.append(f"strat={result['strategy']}")
    if result.get("reasons"):
        reasons.append(str(result["reasons"]))

    return result["sig"], result["conf"], reasons, lvl, "NONE"


# ═══════════════════════════════════════════════════════════
#  FAMILY-COIN CHECK
# ═══════════════════════════════════════════════════════════
def is_family_coin(family_coins: set, symbol: str) -> bool:
    if not symbol:
        return False
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    return is_enabled(sym) and sym in family_coins