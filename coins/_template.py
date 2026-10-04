"""
coins/_template.py — Copy this file to add a new coin.

STEPS:
  1. Copy this file to coins/YOURCOIN.py (e.g. coins/avax.py)
  2. Fill in all values below
  3. Done — loader picks it up automatically.
"""
SYMBOL     = "XXXUSDT"        # e.g. "AVAXUSDT"
BASE       = "XXX"            # e.g. "AVAX"
FAMILY     = "trend_coins"    # trend_coins | momentum_coins | range_coins | volatility_coins
PROFILE    = "TREND"          # TREND | MOMENTUM | RANGE | VOLATILE
ENABLED    = False            # start disabled — enable after backtest

VOL_CLASS  = "MED"            # LOW | MED | HIGH

CAPS = {
    "sl":  0.045,   # SL cap (% of price)
    "tp1": 0.045,   # TP1 cap = 1R
    "tp2": 0.112,   # TP2 cap = 2.5R
}

ST_PARAMS = {
    "sl_atr":  2.5,
    "tp1_atr": 3.75,   # AUTO-FIX RR 1.000 → 1.5
    "tp2_atr": 6.25,
}

FILTERS = {
    "rsi_buy_min":  40.0,
    "rsi_buy_max":  65.0,
    "rsi_sell_min": 35.0,
    "rsi_sell_max": 60.0,

    "min_flips":    4,
    "max_flips":    10,

    "min_dist_atr": 0.25,
    "max_dist_atr": 1.4,

    "min_adx_st":   22.0,

    "late_guard_adx":  35.0,
    "late_guard_dist": 1.0,
    "late_guard_rsi":  58.0,

    "top_chase_rsi":   68.0,
    "top_chase_flips": 4,

    "block_ny_am":  True,
    "block_ny_pm":  False,
    "block_london": False,
}

TD_FADE = {
    "rsi_sell":     65.0,
    "rsi_buy":      32.0,
    "sl_atr":       0.9,
    "adx_max":      55.0,
    "ema_tol":      1.02,
    "min_adx":      20.0,
    "bb_pos_sell":  0.98,
    "bb_pos_buy":   0.02,
    "enabled_sell": True,
    "enabled_buy":  False,
}