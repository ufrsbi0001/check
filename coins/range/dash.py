"""
dash.py — DASHUSDT coin configuration (IDENTITY ONLY).

Tuning (ST_PARAMS, CAPS, FILTERS) comes from:
  core/family_baselines.py → BASELINES["range_coins"]
"""
from __future__ import annotations

SYMBOL     = "DASHUSDT"
BASE       = "DASH"
FAMILY     = "range_coins"
PROFILE    = "RANGE"
VOL_CLASS  = "MED"
ENABLED    = True

TD_FADE = {
    "rsi_sell":     63.0,
    "rsi_buy":      37.0,
    "sl_atr":       0.8,
    "adx_max":      45.0,
    "ema_tol":      1.01,
    "min_adx":      18.0,
    "bb_pos_sell":  0.97,
    "bb_pos_buy":   0.03,
    "enabled_sell": True,
    "enabled_buy":  True,
}
