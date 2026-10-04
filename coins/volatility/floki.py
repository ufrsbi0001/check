"""
floki.py — 1000FLOKIUSDT coin configuration (IDENTITY ONLY).

Tuning (ST_PARAMS, CAPS, FILTERS) comes from:
  core/family_baselines.py → BASELINES["volatility_coins"]
"""
from __future__ import annotations

SYMBOL     = "1000FLOKIUSDT"
BASE       = "1000FLOKI"
FAMILY     = "volatility_coins"
PROFILE    = "VOLATILE"
VOL_CLASS  = "MED"
ENABLED    = True

TD_FADE = {
    "rsi_sell":     65.0,
    "rsi_buy":      32.0,
    "sl_atr":       0.9,
    "adx_max":      65.0,
    "ema_tol":      1.02,
    "min_adx":      20.0,
    "bb_pos_sell":  0.98,
    "bb_pos_buy":   0.02,
    "enabled_sell": True,
    "enabled_buy":  True,
}
