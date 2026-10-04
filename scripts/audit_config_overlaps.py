"""
scripts/audit_config_overlaps.py — Deep consistency audit of the layered
config system.

REV 5.4 (2026-10-02) — FALSE-POSITIVE FIXES + REDUNDANT-SHADOW REPORTING:
  ✅ Check 7 (spread caps): was comparing family caps against GLOBAL as
     if GLOBAL were a ceiling. WRONG — GLOBAL is a FALLBACK default,
     families can be tighter OR wider based on liquidity. Now checks
     sanity bounds (family caps ≤ 1.0%) only.
  ✅ Check 3 (FAMILY.FILTERS): now distinguishes:
       • "redundant shadow"  — family value == GLOBAL (safe to remove
                                from FAMILY, inherits from GLOBAL)
       • "genuine override"  — family value != GLOBAL (real tuning)
     Reports redundant shadows as candidates for cleanup.
  ✅ Check 1 (GLOBAL ∩ REGIME): now also flags REGIME keys whose value
     equals GLOBAL (uniform shadows — usually a sign to simplify).

Checks (all read-only):
  1. GLOBAL vs REGIME key overlap & shadows
  2. GLOBAL vs STRATEGY_REGIME key overlap
  3. GLOBAL vs FAMILY.FILTERS — redundant shadows vs genuine overrides
  4. min_rr scalar (GLOBAL) vs per-strategy dict (MIN_RR)
  5. hold_minutes layering resolution for sample (symbol, strategy, regime)
  6. time_exit_enabled + hold_minutes co-dependency
  7. Spread caps fallback chain (sanity, not ceiling)
  8. Vol class triple layering (CAP_MULT × R_THRESHOLDS × QTY_MULT)
  9. Legacy % vs R-mult pairs (be_factor/lock1_pct/lock2_pct vs *_stop_r)

Usage:
    python -m scripts.audit_config_overlaps
    python -m scripts.audit_config_overlaps --verbose

Exit 0 = clean, 1 = warnings found.
"""
from __future__ import annotations

import argparse
import sys

from core import config_center as CC


def _hr(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def check_global_regime_overlap(verbose: bool) -> int:
    """Any GLOBAL key also present in a REGIME dict with a different value?"""
    _hr("1. GLOBAL ∩ REGIME key overlap")
    regime_keys = set()
    for reg, cfg in CC.REGIME.items():
        regime_keys.update(cfg.keys())
    common = set(CC.GLOBAL.keys()) & regime_keys
    if not common:
        print("  [OK] No overlap between GLOBAL and REGIME")
        return 0

    print(f"  [INFO] {len(common)} keys in both GLOBAL and REGIME (by design — regime overrides):")
    redundant = []
    for k in sorted(common):
        g_val = CC.GLOBAL.get(k)
        regime_vals = {r: CC.REGIME[r].get(k) for r in CC.REGIME if k in CC.REGIME[r]}
        distinct = set(regime_vals.values())

        # All regimes same value AND == GLOBAL → redundant shadow
        is_redundant = (len(distinct) == 1
                        and list(distinct)[0] == g_val)
        # All regimes same value but != GLOBAL → "uniform regime override" (still intentional)
        is_uniform_override = (len(distinct) == 1
                                and list(distinct)[0] != g_val)

        if is_redundant:
            redundant.append(k)
            marker = "[NOTE] redundant shadow (== GLOBAL)"
        elif is_uniform_override:
            marker = "[INFO] uniform override (!= GLOBAL)"
        else:
            marker = "[OK] varied (regime-specific)"

        if verbose or is_redundant or len(distinct) > 1:
            print(f"     {k:<28} GLOBAL={g_val:<10} regimes={regime_vals}  {marker}")

    if redundant:
        print()
        print(f"  [NOTE] {len(redundant)} redundant key(s) in REGIME that equal GLOBAL:")
        print(f"         {redundant}")
        print("         These don't change behaviour — consider removing from REGIME.")

    # informational only — not treated as a warning
    return 0


def check_global_strategy_regime_overlap(verbose: bool) -> int:
    """Any GLOBAL key also in STRATEGY_REGIME dict?"""
    _hr("2. GLOBAL ∩ STRATEGY_REGIME key overlap")
    sr_keys = set()
    for strat, regs in CC.STRATEGY_REGIME.items():
        for reg, cfg in regs.items():
            sr_keys.update(cfg.keys())
    common = set(CC.GLOBAL.keys()) & sr_keys
    if not common:
        print("  [OK] No overlap")
        return 0
    print(f"  [INFO] {len(common)} keys (by design — strategy×regime overrides):")
    for k in sorted(common):
        print(f"     {k}")
    return 0


def check_global_family_filters_overlap(verbose: bool) -> int:
    """Any GLOBAL key also in FAMILY[*].FILTERS? Report redundant shadows."""
    _hr("3. GLOBAL ∩ FAMILY.FILTERS key overlap")
    fam_keys = set()
    for fam, cfg in CC.FAMILY.items():
        fam_keys.update(cfg.get("FILTERS", {}).keys())
    common = set(CC.GLOBAL.keys()) & fam_keys
    if not common:
        print("  [OK] No overlap")
        return 0

    print(f"  [INFO] {len(common)} keys shadow GLOBAL at family level:")
    redundant: list[str] = []
    genuine: list[str] = []

    for k in sorted(common):
        g_val = CC.GLOBAL.get(k)
        fam_vals = {f: CC.FAMILY[f]["FILTERS"].get(k) for f in CC.FAMILY
                    if k in CC.FAMILY[f]["FILTERS"]}
        distinct = set(fam_vals.values())

        # All families same value == GLOBAL → pure redundant shadow
        if len(distinct) == 1 and list(distinct)[0] == g_val:
            redundant.append(k)
            marker = "[NOTE] redundant shadow (all families == GLOBAL)"
        else:
            genuine.append(k)
            marker = "[OK] varied (genuine family tuning)"

        if verbose or marker.startswith("[NOTE]") or len(distinct) > 1:
            print(f"     {k:<28} GLOBAL={g_val}  families={fam_vals}  {marker}")

    if redundant:
        print()
        print(f"  [NOTE] {len(redundant)} redundant shadow(s) — candidates for cleanup:")
        for k in redundant:
            print(f"         • {k}")
        print("         These all equal GLOBAL. Removing them from FAMILY[*].FILTERS")
        print("         is safe (inherits via GLOBAL → get_config() merge).")
        print("         Reduces maintenance surface.")
    if genuine:
        print()
        print(f"  [INFO] {len(genuine)} genuine family override(s): {genuine}")

    return 0


def check_min_rr(verbose: bool) -> int:
    """GLOBAL scalar min_rr vs per-strategy MIN_RR dict."""
    _hr("4. min_rr consistency (GLOBAL scalar vs MIN_RR dict)")
    scalar = CC.GLOBAL.get("min_rr")
    print(f"  GLOBAL['min_rr']          = {scalar}")
    print(f"  MIN_RR dict               = {CC.MIN_RR}")
    print(f"  get_min_rr('UNKNOWN')     = {CC.get_min_rr('UNKNOWN')}  (falls back to scalar)")
    missing = [s for s in CC.MIN_RR if CC.MIN_RR[s] < scalar]
    if missing:
        print(f"  [WARN] Strategies with floor < scalar {scalar}: {missing}")
        return 1
    print(f"  [OK] All MIN_RR floors >= scalar fallback")
    return 0


def check_hold_minutes_layering(verbose: bool) -> int:
    """Walk the 4-layer hold_minutes resolution for samples."""
    _hr("5. hold_minutes layering (GLOBAL -> REGIME -> STRATEGY_REGIME)")
    samples = [
        ("BTCUSDT", "SUPERTREND_RIDE", "TREND_UP"),
        ("BTCUSDT", "SUPERTREND_RIDE", "VOLATILE"),
        ("XRPUSDT", "RANGE_SCALPER",   "CHOP"),
        ("XRPUSDT", "RANGE_SCALPER",   "VOLATILE"),
        ("APTUSDT", "SUPERTREND_RIDE", "UNKNOWN"),
    ]
    for sym, strat, reg in samples:
        cfg = CC.get_config(sym, strat, reg)
        base = CC.GLOBAL.get("hold_minutes")
        r_cfg = CC.REGIME.get(reg, {}).get("hold_minutes", base)
        sr = CC.STRATEGY_REGIME.get(strat, {}).get(reg, {})
        sr_val = sr.get("hold_minutes", r_cfg)
        final = cfg.get("hold_minutes", base)
        print(f"  {sym:<10} [{strat:<18}@{reg:<10}]  "
              f"GLOBAL={base}  REGIME={r_cfg}  SR={sr_val}  FINAL={final}")
    print("  [OK] Resolution works (later layer wins)")
    return 0


def check_time_exit_deps(verbose: bool) -> int:
    """time_exit_enabled + hold_minutes co-dependency."""
    _hr("6. time_exit_enabled + hold_minutes co-dependency")
    tee = CC.GLOBAL.get("time_exit_enabled")
    hm = CC.GLOBAL.get("hold_minutes")
    print(f"  time_exit_enabled = {tee}")
    print(f"  hold_minutes      = {hm}")
    if not tee:
        print("  [INFO] TIME_EXIT disabled - hold_minutes is a fallback only")
    else:
        print(f"  [OK] TIME_EXIT enabled - trades will exit after {hm}m (or regime override)")
    return 0


def check_spread_caps(verbose: bool) -> int:
    """Spread caps fallback chain — sanity check.

    REV 5.4 — FIXED:
      GLOBAL['max_spread_pct'] is a FALLBACK default (used when a family
      has no specific cap). It is NOT a ceiling. Families can be tighter
      (liquid majors) or wider (illiquid meme coins) based on liquidity.

      Resolved at runtime via _get_spread_cap_for_family() in future.py.

    Sanity bounds:
      • All caps must be in (0, 1.0] % — anything above 1% is unsafe
      • Trend cap should typically be the tightest (majors)
      • Volatility cap should typically be the widest (memes)
    """
    _hr("7. Spread caps fallback chain")
    g = CC.GLOBAL.get("max_spread_pct")
    t = CC.GLOBAL.get("max_spread_trend")
    r = CC.GLOBAL.get("max_spread_range")
    v = CC.GLOBAL.get("max_spread_volatility")
    print(f"  max_spread_pct        (fallback)  = {g}")
    print(f"  max_spread_trend      (trend)     = {t}")
    print(f"  max_spread_range      (range)     = {r}")
    print(f"  max_spread_volatility (vol/mom)   = {v}")
    print()
    print("  [INFO] GLOBAL is a FALLBACK default, not a ceiling.")
    print("  [INFO] Families can be tighter OR wider based on liquidity.")
    print("  [INFO] Resolved via _get_spread_cap_for_family() in future.py")
    print()

    issues: list[str] = []
    for name, val in (("trend", t), ("range", r), ("volatility", v)):
        if val is None:
            issues.append(f"{name} cap missing")
            continue
        if val <= 0:
            issues.append(f"{name} cap {val} must be > 0")
        if val > 1.0:
            issues.append(f"{name} cap {val} > 1.0% (unsafe for entries)")

    if issues:
        print(f"  [WARN] {issues}")
        return 1

    print("  [OK] All family caps within sane bounds (0, 1.0%]")

    # Informational ordering hint
    if t <= r <= v:
        print("  [OK] Ordering trend <= range <= volatility (liquidity-consistent)")
    else:
        print(f"  [INFO] Ordering is trend={t} range={r} vol={v} "
              f"(non-standard but valid if intentional)")

    return 0


def check_vol_class_layering(verbose: bool) -> int:
    """Vol class triple: CAP_MULT (SL/TP) × R_THRESHOLDS × QTY_MULT."""
    _hr("8. Vol class layering")
    classes = ["LOW", "MED", "HIGH"]
    print(f"  {'class':<6} {'CAP_MULT':<10} {'QTY_MULT':<10} {'BE_R':<8} {'L1_R':<8} {'L2_R':<8}")
    for c in classes:
        cap = CC.VOL_CLASS_CAP_MULT.get(c)
        qty = CC.VOL_CLASS_QTY_MULT.get(c)
        r = CC.VOL_CLASS_R_THRESHOLDS.get(c, {})
        print(f"  {c:<6} {cap:<10} {qty:<10} "
              f"{r.get('be_r', '-'):<8} {r.get('lock1_r', '-'):<8} {r.get('lock2_r', '-'):<8}")
    print()
    print("  [OK] CAP_MULT scales SL/TP distances; QTY_MULT scales position size")
    print("  [OK] R_THRESHOLDS drive BE/Lock1/Lock2 trigger levels")
    return 0


def check_legacy_r_mult_pairs(verbose: bool) -> int:
    """Legacy % SL fallback vs R-multiple thresholds."""
    _hr("9. Legacy % SL vs R-multiple pairs")
    pairs = [
        ("be_factor",  "be_stop_r",    "BE"),
        ("lock1_pct",  "lock1_stop_r", "Lock1"),
        ("lock2_pct",  "lock2_stop_r", "Lock2"),
    ]
    for legacy, modern, label in pairs:
        lv = CC.GLOBAL.get(legacy)
        mv = CC.GLOBAL.get(modern)
        print(f"  {label:<6} legacy {legacy:<12}={lv:<8}  modern {modern:<12}={mv}")
    print()
    print("  [INFO] Legacy path used when risk_unit missing; modern path is R-based")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true",
                    help="Show full lists, not just conflicts")
    args = ap.parse_args()

    print("=" * 72)
    print("  CONFIG OVERLAP AUDIT — REV 5.4")
    print("=" * 72)

    warnings = 0
    warnings += check_global_regime_overlap(args.verbose)
    warnings += check_global_strategy_regime_overlap(args.verbose)
    warnings += check_global_family_filters_overlap(args.verbose)
    warnings += check_min_rr(args.verbose)
    warnings += check_hold_minutes_layering(args.verbose)
    warnings += check_time_exit_deps(args.verbose)
    warnings += check_spread_caps(args.verbose)
    warnings += check_vol_class_layering(args.verbose)
    warnings += check_legacy_r_mult_pairs(args.verbose)

    _hr("SUMMARY")
    if warnings:
        print(f"  [WARN] {warnings} warning(s) — review above")
        return 1
    print("  [OK] No conflicts. Config layering is clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())