"""
test_multi.py — Multi-coin signal test (temporary file).

Chalao:
    python -m scripts.test_multi

Ya (agar upar wala fail ho):
    set PYTHONPATH=%CD%
    python scripts/test_multi.py

REV 1.1 (2026-10-04) — TWO FIXES:
  ✅ Path bootstrap added at top — `python scripts/test_multi.py`
     previously raised ModuleNotFoundError: 'core' because Python
     puts the SCRIPT's dir on sys.path, not the repo root. Now works
     from both invocation styles.
  ✅ Now passes CLOSED-ONLY data to calculate_pro_indicators(), matching
     future.py::_fetch_coin_indicators. Previously the in-progress
     (unclosed) bar was included, so sweep/regime detection was one
     bar ahead of production behaviour.

Baad mein delete kar dena.
"""
from __future__ import annotations

# ── Path bootstrap: make `core`, `signals`, `market` importable ──
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
# ─────────────────────────────────────────────────────────────────

from core.client import set_client_keys, get_binance_klines
from market.indicators import calculate_pro_indicators
from signals.router import generate_signal_live
from signals.decision_engine import evaluate_trade, set_btc_regime
from core.config import CONFIG

# ── Connect ──
set_client_keys(CONFIG.api_key, CONFIG.api_secret)
set_btc_regime('CHOP')   # BTC regime set karo taake gate pass ho

coins = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ADAUSDT',
    'DOGEUSDT', '1000PEPEUSDT', '1000SHIBUSDT', 'WIFUSDT', 'APTUSDT'
]

print()
print('=' * 110)
print(f"{'COIN':<15} {'REGIME':<10} {'ST':<6} {'ADX':<7} {'RSI':<7} "
      f"{'SIG':<12} {'CONF':<6} REASONS")
print('=' * 110)

signals_fired = 0
vwap_revs = 0
sweeps = 0

for sym in coins:
    try:
        df_raw = get_binance_klines(sym, '1h', 250)
        if df_raw is None or len(df_raw) < 60:
            print(f'{sym:<15} NO DATA')
            continue

        # ── FIX: drop the in-progress bar, match production behaviour ──
        df_closed = df_raw.iloc[:-1]

        ind = calculate_pro_indicators(df_closed, '1h', symbol=sym)
        if ind is None:
            print(f'{sym:<15} INDICATORS FAILED')
            continue

        sig, conf, reasons, lvl, div = generate_signal_live(
            ind, ind, ind, symbol=sym
        )

        reason_txt = reasons[0] if reasons else '-'
        # Truncate long reason for table
        if len(reason_txt) > 40:
            reason_txt = reason_txt[:37] + '...'

        print(f"{sym:<15} {ind.get('regime', '?'):<10} "
              f"{ind.get('st_trend', '?'):<6} "
              f"{ind.get('adx', 0):<7.1f} {ind.get('rsi', 0):<7.1f} "
              f"{sig:<12} {conf:<6.1f} {reason_txt}")

        # Track
        if sig in ('BUY', 'STRONG_BUY', 'SELL', 'STRONG_SELL'):
            signals_fired += 1

        # Check VWAP overlay
        full_reason = ' '.join(str(r) for r in reasons)
        if 'VWAP_REV' in full_reason:
            vwap_revs += 1
            print(f'    └─ ✅ VWAP OVERLAY FIRED!')

        # Check sweep
        if ind.get('sweep_bull') or ind.get('sweep_bear'):
            sweeps += 1
            sweep_dir = 'BULL' if ind.get('sweep_bull') else 'BEAR'
            sweep_str = ind.get('sweep_strength', 0)
            print(f'    └─ 🔥 SWEEP {sweep_dir} strength={sweep_str}')

        # FVG info
        fvg_count = ind.get('fvg_zone_count_unfilled', 0)
        if fvg_count > 0:
            print(f'    └─ 📊 {fvg_count} unfilled FVG zones')

        # Decision engine test (only if signal fired)
        if sig in ('BUY', 'STRONG_BUY', 'SELL', 'STRONG_SELL'):
            side = 'BUY' if 'BUY' in sig else 'SELL'
            strategy = 'UNKNOWN'
            for r in reasons:
                if 'strat=' in str(r):
                    strategy = str(r).split('strat=')[1].split()[0]
                    break

            r = evaluate_trade(
                sym, side, ind, ind, ind,
                strategy=strategy, conf=conf, rr=lvl.get('RR', 0)
            )
            print(f'    └─ Decision: {"✅ APPROVED" if r.approved else "🚫 REJECTED"} '
                  f'{r.approvals}/{r.total_filters}')

    except Exception as e:
        print(f'{sym:<15} ERROR: {type(e).__name__}: {e}')

print('=' * 110)
print(f"Summary: {signals_fired} signals fired | "
      f"{vwap_revs} VWAP overlay | {sweeps} sweeps detected")
print('=' * 110)