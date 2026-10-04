"""
trend_coins.py — TREND family signal engine.

REV 23.3 (2026-10-03) — DEAD CODE CLEANUP:
  ✅ Removed dead `from core.config import CONFIG` import.
  ✅ Removed dead `_SPEC._rs_rng_min/_rs_rng_max` assignments —
     values now passed as constructor kwargs (base.py REV 23.7).
  ✅ Zero behaviour change for every other parameter.

REV 23.2 (2026-10-02) — PHASE 3 CLEANUP.
REV 23.0 (2026-10-02) — UNIFIED CONFIG CLEANUP.
REV 21.2 (2026-09-30) — FAMILY-SPECIFIC TUNING.
"""
from __future__ import annotations

from core import config_center as CC
from core.coins_config import get_family_coins
from core.config_center import FAMILY as _CC_FAMILY

from signals.base import (
    FamilySpec, DiagnosticsTracker, build_all_strategies,
    route_signal as _route_signal_base,
    generate_signal_live as _generate_signal_live_base,
    is_family_coin as _is_family_coin_base,
)


FAMILY_NAME  = "TREND"
FAMILY_KEY   = "trend_coins"
FAMILY_COINS = get_family_coins(FAMILY_KEY)

__all__ = [
    "FAMILY_NAME", "FAMILY_COINS",
    "REGIME_STRATEGIES", "ALL_STRATEGIES", "DISABLED_STRATEGIES",
    "is_family_coin",
    "route_signal", "generate_signal_live",
    "reset_diagnostics", "get_diagnostics", "format_diagnostics",
]


REGIME_STRATEGIES = {
    "TREND_UP":   {"SUPERTREND_RIDE"},
    "TREND_DOWN": {"TREND_DOWN_FADE", "SUPERTREND_RIDE"},
    "CHOP":       {"MEAN_REVERSION", "RANGE_SCALPER"},
    "QUIET":      {"RANGE_SCALPER"},
    "VOLATILE":   {"SUPERTREND_RIDE", "RANGE_SCALPER"},
    "UNKNOWN":    {"SUPERTREND_RIDE"},
}

DISABLED_STRATEGIES: set[str] = {"CHOP_FADE"}

MIN_ADX = {
    "MEAN_REVERSION":   15.0,
    "RANGE_SCALPER":    12.0,
    "SUPERTREND_RIDE":  18.0,
    "TREND_DOWN_FADE":  20.0,
}

# ── REV 23.2 — FALLBACK_CAPS sourced from config_center ──
FALLBACK_CAPS = dict(_CC_FAMILY[FAMILY_KEY]["CAPS"])


_SPEC = FamilySpec(
    name=FAMILY_NAME,
    family_key=FAMILY_KEY,
    regime_strategies=REGIME_STRATEGIES,
    disabled_strategies=DISABLED_STRATEGIES,
    min_adx=MIN_ADX,
    fallback_caps=FALLBACK_CAPS,
    min_confidence=CC.get('min_confidence'),
    min_adx_env=18.0,
    min_sl_pct=0.0075,
    min_adx_outside_kz=30.0,
    kz_bypass=CC.get('kz_bypass'),
    block_ny_am_for_st=False,
    block_ny_am_for_td_fade=False,
    block_quiet=False,
    counter_trend_adx_cutoff=35.0,
    rsi_buy_overbought=82.0,
    rsi_sell_oversold=25.0,
    st_min_dist_atr=0.5,
    st_max_chase_atr=1.8,
    st_flips_min=4,
    st_flips_max=10,
    st_rsi_buy_min=38.0,
    st_rsi_buy_max=70.0,
    st_rsi_sell_min=30.0,
    st_rsi_sell_max=65.0,
    st_rsi_sell_floor=28.0,
    pullback_dist_atr=1.8,
    mr_rsi_buy_max=30.0,
    mr_rsi_sell_min=70.0,
    mr_stoch_buy_max=25.0,
    mr_stoch_sell_min=75.0,
    rs_rsi_buy_max=35.0,
    rs_rsi_sell_min=65.0,
    td_fade_rsi_sell=65.0,
    td_fade_rsi_buy=32.0,
    td_fade_sl_atr=0.9,
    td_fade_adx_max=55.0,
    td_fade_ema_tol=1.02,
    td_fade_min_adx=20.0,
    td_fade_bb_pos_sell=0.98,
    td_fade_bb_pos_buy=0.02,
    # REV 23.3 — per-family rng bounds now proper fields.
    rs_rng_min=0.004,
    rs_rng_max=0.20,
)

_TRACKER = DiagnosticsTracker()
_ALL_STRATEGIES = build_all_strategies(_SPEC, _TRACKER)
ALL_STRATEGIES = _ALL_STRATEGIES


def is_family_coin(symbol: str) -> bool:
    return _is_family_coin_base(FAMILY_COINS, symbol)


def route_signal(ind_1h, ind_4h, ind_1d=None,
                 profile: str = "MIXED", symbol: str = "",
                 mode: str = None):
    return _route_signal_base(
        _SPEC, _TRACKER, _ALL_STRATEGIES,
        ind_1h, ind_4h, ind_1d,
        profile=profile, symbol=symbol, mode=mode,
    )


def generate_signal_live(ind_1h, ind_4h, ind_1d=None,
                         symbol="", mode="MULTI"):
    return _generate_signal_live_base(
        _SPEC, _TRACKER, FAMILY_COINS, _ALL_STRATEGIES,
        ind_1h, ind_4h, ind_1d, symbol=symbol, mode=mode,
    )


def reset_diagnostics() -> None:
    _TRACKER.reset()


def get_diagnostics() -> dict:
    return _TRACKER.snapshot()


def format_diagnostics() -> str:
    return _TRACKER.format()