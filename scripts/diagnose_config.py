"""
scripts/diagnose_config.py — Config diagnostic entry point.

Yahan `python -m core.config_center` ki jagah `python -m scripts.diagnose_config`
use karein. Ye script core.config_center ko NORMALLY import karta hai
(dual-load issue nahi hota — ek hi GLOBAL dict).

Usage:
    python -m scripts.diagnose_config
"""
from __future__ import annotations

from core import config_center as cc


def main() -> None:
    print("=" * 70)
    print("  CONFIG CENTER DIAGNOSTIC — REV 5.0 (unified, single source)")
    print("=" * 70)

    # ── Migrated-from-config.py keys ──
    print()
    print("  REV 5.0 — Migrated trading params (was in core/config.py):")
    for k in (
        "leverage", "risk_percent", "max_open_positions",
        "max_same_side_positions", "max_total_margin_pct",
        "max_daily_loss_trades", "max_daily_drawdown_percent",
        "max_account_drawdown", "hold_minutes",
        "cooldown_after_sl_min", "cooldown_after_tp_min",
        "partial_close_usdt", "min_confidence", "min_adx",
        "require_htf_agreement", "use_5m_trend_filter",
        "fg_enabled", "max_trades_per_coin_per_day", "htf_align_relaxed",
        "use_taker_volume", "use_volume_profile", "use_anchored_vwap",
        "use_funding_z", "use_mtf_confluence", "kz_bypass",
        "use_spread_filter", "max_spread_pct",
        "max_spread_trend", "max_spread_range", "max_spread_volatility",
    ):
        print(f"    {k:<32} = {cc.GLOBAL[k]}")

    # ── TIME_EXIT state ──
    print()
    print("  TIME_EXIT state:")
    print(f"    time_exit_enabled     = {cc.GLOBAL.get('time_exit_enabled')}")
    print(f"    hold_minutes          = {cc.GLOBAL.get('hold_minutes')}")
    print(f"    is_time_exit_enabled()= {cc.is_time_exit_enabled()}")

    # ── Sample configs ──
    samples = [
        ("POLUSDT",  "SUPERTREND_RIDE",  "TREND_DOWN"),
        ("APTUSDT",  "SUPERTREND_RIDE",  "TREND_UP"),
        ("BTCUSDT",  "SUPERTREND_RIDE",  "TREND_UP"),
        ("HYPEUSDT", "SUPERTREND_RIDE",  "VOLATILE"),
        ("XRPUSDT",  "RANGE_SCALPER",    "CHOP"),
    ]
    for sym, strat, reg in samples:
        print()
        print(cc.describe(sym, strat, reg))

    # ── Flat config test ──
    print()
    print("=" * 70)
    print("  Flat get_config(include_family=False) test:")
    flat = cc.get_config(include_family=False)
    flat.pop("_meta", None)
    print(f"  Keys: {len(flat)}")
    print(f"  sl_atr={flat['sl_atr']} tp1_atr={flat['tp1_atr']} tp2_atr={flat['tp2_atr']}")
    print(f"  min_rr={flat['min_rr']} tp1_qty={flat['tp1_qty']}")
    print(f"  min_confidence={flat.get('min_confidence')}")
    print(f"  leverage={flat.get('leverage')} risk_percent={flat.get('risk_percent')}")
    print(f"  time_exit_enabled={flat.get('time_exit_enabled')}")
    print(f"  sl_mult={flat['sl_mult']} tp_mult={flat['tp_mult']} rsi_period={flat['rsi_period']}")

    # ── Family filters ──
    print()
    print("=" * 70)
    print("  FAMILY FILTERS new keys (REV 4.3):")
    for fam in ("momentum_coins", "volatility_coins", "trend_coins", "range_coins"):
        f = cc.FAMILY[fam]["FILTERS"]
        print(f"  {fam:20} "
              f"floor={f.get('st_rsi_sell_floor')} "
              f"ob={f.get('rsi_buy_overbought')} "
              f"os={f.get('rsi_sell_oversold')}")

    # ── Decision config ──
    print()
    print("=" * 70)
    print("  DECISION config test (REV 4.0):")
    dec = cc.get_decision_cfg()
    print(f"  min_approvals              = {dec['min_approvals']}")
    print(f"  total_filters              = {dec['total_filters']}")
    print(f"  btc_bias_enabled           = {dec['btc_bias_enabled']}")
    print(f"  btc_bias_mode              = {dec['btc_bias_mode']}")
    print(f"  btc_bias_high_risk_coins   = {len(dec['btc_bias_high_risk_coins'])} coins")
    print(f"    → {sorted(dec['btc_bias_high_risk_coins'])[:3]} ...")
    print(f"  btc_regime_ttl_sec         = {dec['btc_regime_ttl_sec']}")
    print(f"  adx_counter_trend_hard_max = {dec['adx_counter_trend_hard_max']}")
    print(f"  adx_counter_trend_soft_max = {dec['adx_counter_trend_soft_max']}")
    print(f"  atr_ratio_extreme          = {dec['atr_ratio_extreme']}")
    print(f"  loss_streak_trigger        = {dec['loss_streak_trigger']}")
    print(f"  loss_streak_cooldown_min   = {dec['loss_streak_cooldown_min']}")
    print(f"  wr_mult_min                = {dec['wr_mult_min']}")
    print(f"  wr_mult_max                = {dec['wr_mult_max']}")
    print(f"  wr_mult_min_trades         = {dec['wr_mult_min_trades']}")
    print("=" * 70)


if __name__ == "__main__":
    main()