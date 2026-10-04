"""
audit_coin_tuning.py — Check every coin file for tuning completeness.

Reports:
  1. Coins with missing FILTERS fields (fallback-dependent)
  2. Coins with missing ST_PARAMS fields
  3. Coins with missing CAPS fields
  4. Inconsistent values within a family
"""
from __future__ import annotations
import json
from collections import defaultdict
from coins import all_coins


REQUIRED_FILTERS = [
    # Chase + distance
    "max_dist_atr", "pullback_dist_atr", "min_dist_atr",
    # ADX + flips
    "min_adx_st", "min_flips", "max_flips",
    # Late guard
    "late_guard_adx", "late_guard_rsi", "late_guard_dist",
    # Top chase
    "top_chase_rsi", "top_chase_flips",
    # RSI bands
    "rsi_buy_min", "rsi_buy_max", "rsi_sell_min", "rsi_sell_max",
]

REQUIRED_ST_PARAMS = ["sl_atr", "tp1_atr", "tp2_atr"]

REQUIRED_CAPS = ["sl", "tp1", "tp2"]


def main():
    coins = all_coins()
    print(f"Loaded {len(coins)} coins\n")

    by_family = defaultdict(list)

    missing_filters = defaultdict(list)   # field -> [symbols]
    missing_st = defaultdict(list)
    missing_caps = defaultdict(list)

    for sym, c in sorted(coins.items()):
        if not c.get("enabled"):
            continue
        fam = c.get("family", "?")
        by_family[fam].append(sym)

        filters = c.get("filters") or {}
        st_params = c.get("st_params") or {}
        caps = c.get("caps") or {}

        for f in REQUIRED_FILTERS:
            if f not in filters:
                missing_filters[f].append(sym)

        for f in REQUIRED_ST_PARAMS:
            if f not in st_params:
                missing_st[f].append(sym)

        for f in REQUIRED_CAPS:
            if f not in caps:
                missing_caps[f].append(sym)

    # ── Report ──
    print("=" * 70)
    print("  FAMILY DISTRIBUTION")
    print("=" * 70)
    for fam, syms in sorted(by_family.items()):
        print(f"  {fam:<22} {len(syms):>3} coins")

    print()
    print("=" * 70)
    print("  MISSING FIELDS (fallback-dependent)")
    print("=" * 70)

    total_missing = 0
    for f in REQUIRED_FILTERS:
        syms = missing_filters.get(f, [])
        if syms:
            total_missing += len(syms)
            print(f"  FILTERS.{f:<20} MISSING in {len(syms)} coins: {syms[:5]}...")

    for f in REQUIRED_ST_PARAMS:
        syms = missing_st.get(f, [])
        if syms:
            total_missing += len(syms)
            print(f"  ST_PARAMS.{f:<18} MISSING in {len(syms)} coins: {syms[:5]}...")

    for f in REQUIRED_CAPS:
        syms = missing_caps.get(f, [])
        if syms:
            total_missing += len(syms)
            print(f"  CAPS.{f:<23} MISSING in {len(syms)} coins: {syms[:5]}...")

    if total_missing == 0:
        print("  ✅ No missing fields — all coins complete")

    # ── Per-family consistency check ──
    print()
    print("=" * 70)
    print("  FAMILY BASELINE CONSISTENCY")
    print("=" * 70)

    for fam, syms in sorted(by_family.items()):
        print(f"\n  {fam}:")
        for key_path in [
            ("filters", "max_dist_atr"),
            ("filters", "min_adx_st"),
            ("filters", "late_guard_adx"),
            ("filters", "top_chase_rsi"),
            ("filters", "max_flips"),
            ("st_params", "sl_atr"),
            ("st_params", "tp1_atr"),
            ("caps", "sl"),
        ]:
            section, key = key_path
            values = set()
            for sym in syms:
                v = coins[sym].get(section, {}).get(key)
                if v is not None:
                    values.add(round(float(v), 4))
            if len(values) == 1:
                marker = "✅"
            elif len(values) == 0:
                marker = "❌ MISSING"
            else:
                marker = "⚠️  INCONSISTENT"
            vals_str = ", ".join(str(v) for v in sorted(values))[:60]
            print(f"    {section}.{key:<20} {marker:<20} {vals_str}")

    # ── Save full report ──
    with open("audit_coin_tuning.json", "w") as f:
        json.dump({
            "missing_filters": {k: v for k, v in missing_filters.items()},
            "missing_st_params": {k: v for k, v in missing_st.items()},
            "missing_caps": {k: v for k, v in missing_caps.items()},
            "by_family": dict(by_family),
        }, f, indent=2)
    print(f"\n  Full report saved: audit_coin_tuning.json")


if __name__ == "__main__":
    main()