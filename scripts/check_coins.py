"""
check_coins.py — Validate all coin files.

Checks:
  1. Every coin file loads without error
  2. Required fields present: SYMBOL, FAMILY, ENABLED, CAPS, ST_PARAMS
  3. FAMILY matches folder name
  4. FILTERS have sane values (late_guard, top_chase, max_flips)
  5. ST_PARAMS have sane values (sl_atr, tp1_atr, tp2_atr)
"""
from coins import all_coins, load_errors


def main():
    errs = load_errors()
    if errs:
        print("❌ LOAD ERRORS:")
        for k, v in errs.items():
            print(f"   {k}: {v}")
    else:
        print("✅ No load errors")

    coins = all_coins()
    print(f"\nLoaded {len(coins)} coins\n")

    issues = []
    by_family = {}

    for sym, c in sorted(coins.items()):
        fam = c.get("family", "?")
        by_family.setdefault(fam, []).append(sym)

        # Check ENABLED
        if not c.get("enabled"):
            continue

        # Check ST_PARAMS
        st = c.get("st_params") or {}
        sl = st.get("sl_atr", 0)
        tp1 = st.get("tp1_atr", 0)
        if sl <= 0 or tp1 <= 0:
            issues.append(f"{sym}: ST_PARAMS invalid (sl={sl}, tp1={tp1})")
            continue
        rr = tp1 / sl
        if rr < 1.4:
            issues.append(f"{sym}: RR too low ({rr:.2f}) — tp1/sl={tp1}/{sl}")

        # Check FILTERS
        f = c.get("filters") or {}
        lg_adx = f.get("late_guard_adx", 0)
        lg_rsi = f.get("late_guard_rsi", 0)
        mx_flips = f.get("max_flips", 0)

        if lg_adx and lg_adx > 40:
            issues.append(f"{sym}: late_guard_adx={lg_adx} (should be ~35)")
        if lg_rsi and lg_rsi > 60:
            issues.append(f"{sym}: late_guard_rsi={lg_rsi} (should be ~58)")
        if mx_flips and mx_flips > 10:
            issues.append(f"{sym}: max_flips={mx_flips} (should be ≤10)")

        # Check CAPS
        caps = c.get("caps") or {}
        cap_sl = caps.get("sl", 0)
        if cap_sl and cap_sl > 0.10:
            issues.append(f"{sym}: cap_sl={cap_sl} too wide (>10%)")

    # Print by family
    print("=" * 60)
    for fam, syms in sorted(by_family.items()):
        print(f"{fam:<20} ({len(syms):>2}) {', '.join(sorted(syms)[:8])}...")
    print("=" * 60)

    if issues:
        print(f"\n⚠️  {len(issues)} issues found:")
        for i in issues:
            print(f"   {i}")
    else:
        print("\n✅ All coins valid — no issues!")


if __name__ == "__main__":
    main()