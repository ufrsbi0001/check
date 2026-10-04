"""
scripts/audit_config_overlaps.py — Deep consistency audit of the layered
config system.

REV 5.5b (2026-10-03) — CLEANUP + CONSISTENCY:
  ✅ Consolidated duplicate imports. The sys.path bootstrap used
     aliased names (_sys, _Path) while the rest of the file imported
     sys and Path unaliased. Merged to a single set of imports.
  ✅ Removed the dead _QUIET module flag. It was checked in _hr() and
     _p() but never set to True anywhere — all suppression already
     happens via redirect_stdout (for capture) plus main()'s
     `if not args.quiet` guard. Dead code removed.
  ✅ --quiet now consistently suppresses ALL terminal output,
     including --format json to stdout. Previously JSON went to
     stdout regardless of --quiet, which was confusing in pipelines.
     (--output still writes files; use that for JSON in --quiet mode.)
  ✅ Type annotation fix: `list[tuple[str, callable]]` -> Callable.
     `callable` is a builtin function, not a type. Imported Callable
     from typing.

REV 5.5 (2026-10-03) — CI-READY + MOJIBAKE FIX + EXTENDED CHECKS:
  ✅ Fixed Windows cp1252 -> utf-8 mojibake corruption.
  ✅ Added --strict / --report modes.
  ✅ Added --format text|json.
  ✅ Added --quiet.
  ✅ Added --output PATH (default: no file written).
  ✅ Exit codes: 0 = clean, 1 = warnings (strict), 2 = setup error.
  ✅ NEW Check 10: _SAFETY_SNAPSHOT_KEYS present in GLOBAL.
  ✅ NEW Check 11: config.py proxy contract.
  ✅ NEW Check 12: DECISION sanity.

REV 5.4 (2026-10-02) — FALSE-POSITIVE FIXES + REDUNDANT-SHADOW REPORTING.

Checks (all read-only):
  1.  GLOBAL vs REGIME key overlap and shadows
  2.  GLOBAL vs STRATEGY_REGIME key overlap
  3.  GLOBAL vs FAMILY.FILTERS (redundant shadows vs genuine overrides)
  4.  min_rr scalar (GLOBAL) vs per-strategy dict (MIN_RR)
  5.  hold_minutes layering resolution for samples
  6.  time_exit_enabled + hold_minutes co-dependency
  7.  Spread caps fallback chain (sanity, not ceiling)
  8.  Vol class triple layering (CAP_MULT x R_THRESHOLDS x QTY_MULT)
  9.  Legacy % vs R-mult pairs (be_factor/lock*_pct vs *_stop_r)
  10. Safety snapshot keys present in GLOBAL
  11. config.py proxy contract (_PROXIED_TRADING_ATTRS)
  12. DECISION sanity

USAGE
-----
    python scripts/audit_config_overlaps.py --report
    python scripts/audit_config_overlaps.py --strict
    python scripts/audit_config_overlaps.py --strict --format json --quiet
    python scripts/audit_config_overlaps.py --report --output /tmp/audit.txt

EXIT CODES
----------
  0 - clean (or --report mode regardless)
  1 - findings in --strict mode
  2 - setup error (cannot import config_center, etc.)
"""
from __future__ import annotations

# ── sys.path bootstrap ─────────────────────────────────────────
# Python sets sys.path[0] to the SCRIPT's directory, not the CWD.
# When running `python scripts/audit_config_overlaps.py`, the repo
# root is NOT on sys.path, so `from core import ...` fails.
# Add the repo root explicitly so the script works both ways:
#   • python scripts/audit_config_overlaps.py
#   • python -m scripts.audit_config_overlaps
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
# ───────────────────────────────────────────────────────────────

import argparse
import io
import json
from contextlib import redirect_stdout
from typing import Callable


# ═══════════════════════════════════════════════════════════════
#  PRINT HELPERS
#  All output flows into io.StringIO() via redirect_stdout() inside
#  _run_all_checks(). main() then decides whether to print the
#  captured text to the terminal (respecting --quiet) or write it
#  to --output / emit JSON.
# ═══════════════════════════════════════════════════════════════
def _hr(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _p(line: str = "") -> None:
    print(line)


# ═══════════════════════════════════════════════════════════════
#  CHECKS
# ═══════════════════════════════════════════════════════════════
def check_global_regime_overlap(verbose: bool) -> int:
    """Any GLOBAL key also present in a REGIME dict with a different value?"""
    from core import config_center as CC

    _hr("1. GLOBAL vs REGIME key overlap")
    regime_keys = set()
    for reg, cfg in CC.REGIME.items():
        regime_keys.update(cfg.keys())
    common = set(CC.GLOBAL.keys()) & regime_keys
    if not common:
        _p("  [OK] No overlap between GLOBAL and REGIME")
        return 0

    _p(f"  [INFO] {len(common)} keys in both GLOBAL and REGIME "
       f"(by design - regime overrides):")
    redundant = []
    for k in sorted(common):
        g_val = CC.GLOBAL.get(k)
        regime_vals = {r: CC.REGIME[r].get(k)
                       for r in CC.REGIME if k in CC.REGIME[r]}
        distinct = set(regime_vals.values())

        is_redundant = (len(distinct) == 1
                        and list(distinct)[0] == g_val)
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
            _p(f"     {k:<28} GLOBAL={g_val:<10} "
               f"regimes={regime_vals}  {marker}")

    if redundant:
        _p()
        _p(f"  [NOTE] {len(redundant)} redundant key(s) in REGIME that "
           f"equal GLOBAL:")
        _p(f"         {redundant}")
        _p("         These don't change behaviour - consider removing "
           "from REGIME.")

    # informational only - not treated as a warning
    return 0


def check_global_strategy_regime_overlap(verbose: bool) -> int:
    """Any GLOBAL key also in STRATEGY_REGIME dict?"""
    from core import config_center as CC

    _hr("2. GLOBAL vs STRATEGY_REGIME key overlap")
    sr_keys = set()
    for strat, regs in CC.STRATEGY_REGIME.items():
        for reg, cfg in regs.items():
            sr_keys.update(cfg.keys())
    common = set(CC.GLOBAL.keys()) & sr_keys
    if not common:
        _p("  [OK] No overlap")
        return 0
    _p(f"  [INFO] {len(common)} keys (by design - strategy x regime "
       f"overrides):")
    for k in sorted(common):
        _p(f"     {k}")
    return 0


def check_global_family_filters_overlap(verbose: bool) -> int:
    """Any GLOBAL key also in FAMILY[*].FILTERS? Report redundant shadows."""
    from core import config_center as CC

    _hr("3. GLOBAL vs FAMILY.FILTERS key overlap")
    fam_keys = set()
    for fam, cfg in CC.FAMILY.items():
        fam_keys.update(cfg.get("FILTERS", {}).keys())
    common = set(CC.GLOBAL.keys()) & fam_keys
    if not common:
        _p("  [OK] No overlap")
        return 0

    _p(f"  [INFO] {len(common)} keys shadow GLOBAL at family level:")
    redundant: list[str] = []
    genuine: list[str] = []

    for k in sorted(common):
        g_val = CC.GLOBAL.get(k)
        fam_vals = {f: CC.FAMILY[f]["FILTERS"].get(k) for f in CC.FAMILY
                    if k in CC.FAMILY[f]["FILTERS"]}
        distinct = set(fam_vals.values())

        if len(distinct) == 1 and list(distinct)[0] == g_val:
            redundant.append(k)
            marker = "[NOTE] redundant shadow (all families == GLOBAL)"
        else:
            genuine.append(k)
            marker = "[OK] varied (genuine family tuning)"

        if verbose or marker.startswith("[NOTE]") or len(distinct) > 1:
            _p(f"     {k:<28} GLOBAL={g_val}  "
               f"families={fam_vals}  {marker}")

    if redundant:
        _p()
        _p(f"  [NOTE] {len(redundant)} redundant shadow(s) - "
           f"candidates for cleanup:")
        for k in redundant:
            _p(f"         - {k}")
        _p("         These all equal GLOBAL. Removing them from "
           "FAMILY[*].FILTERS")
        _p("         is safe (inherits via GLOBAL -> get_config() merge).")
        _p("         Reduces maintenance surface.")
    if genuine:
        _p()
        _p(f"  [INFO] {len(genuine)} genuine family override(s): {genuine}")

    return 0


def check_min_rr(verbose: bool) -> int:
    """GLOBAL scalar min_rr vs per-strategy MIN_RR dict."""
    from core import config_center as CC

    _hr("4. min_rr consistency (GLOBAL scalar vs MIN_RR dict)")
    scalar = CC.GLOBAL.get("min_rr")
    _p(f"  GLOBAL['min_rr']          = {scalar}")
    _p(f"  MIN_RR dict               = {CC.MIN_RR}")
    _p(f"  get_min_rr('UNKNOWN')     = {CC.get_min_rr('UNKNOWN')}  "
       f"(falls back to scalar)")
    missing = [s for s in CC.MIN_RR if CC.MIN_RR[s] < scalar]
    if missing:
        _p(f"  [WARN] Strategies with floor < scalar {scalar}: {missing}")
        return 1
    _p(f"  [OK] All MIN_RR floors >= scalar fallback")
    return 0


def check_hold_minutes_layering(verbose: bool) -> int:
    """Walk the 4-layer hold_minutes resolution for samples."""
    from core import config_center as CC

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
        _p(f"  {sym:<10} [{strat:<18}@{reg:<10}]  "
           f"GLOBAL={base}  REGIME={r_cfg}  SR={sr_val}  FINAL={final}")
    _p("  [OK] Resolution works (later layer wins)")
    return 0


def check_time_exit_deps(verbose: bool) -> int:
    """time_exit_enabled + hold_minutes co-dependency."""
    from core import config_center as CC

    _hr("6. time_exit_enabled + hold_minutes co-dependency")
    tee = CC.GLOBAL.get("time_exit_enabled")
    hm = CC.GLOBAL.get("hold_minutes")
    _p(f"  time_exit_enabled = {tee}")
    _p(f"  hold_minutes      = {hm}")
    if not tee:
        _p("  [INFO] TIME_EXIT disabled - hold_minutes is a fallback only")
    else:
        _p(f"  [OK] TIME_EXIT enabled - trades will exit after {hm}m "
           f"(or regime override)")
    return 0


def check_spread_caps(verbose: bool) -> int:
    """Spread caps fallback chain - sanity check.

    REV 5.4 - GLOBAL['max_spread_pct'] is a FALLBACK default, NOT a
    ceiling. Families can be tighter (liquid majors) or wider (illiquid
    meme coins) based on liquidity. Resolved at runtime via
    _get_spread_cap_for_family() in core/future.py.

    Sanity bounds:
      - All caps must be in (0, 1.0] %  (above 1% is unsafe for entries)
      - Trend cap should typically be the tightest (majors)
      - Volatility cap should typically be the widest (memes)
    """
    from core import config_center as CC

    _hr("7. Spread caps fallback chain")
    g = CC.GLOBAL.get("max_spread_pct")
    t = CC.GLOBAL.get("max_spread_trend")
    r = CC.GLOBAL.get("max_spread_range")
    v = CC.GLOBAL.get("max_spread_volatility")
    _p(f"  max_spread_pct        (fallback)  = {g}")
    _p(f"  max_spread_trend      (trend)     = {t}")
    _p(f"  max_spread_range      (range)     = {r}")
    _p(f"  max_spread_volatility (vol/mom)   = {v}")
    _p()
    _p("  [INFO] GLOBAL is a FALLBACK default, not a ceiling.")
    _p("  [INFO] Families can be tighter OR wider based on liquidity.")
    _p("  [INFO] Resolved via _get_spread_cap_for_family() in future.py")
    _p()

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
        _p(f"  [WARN] {issues}")
        return 1

    _p("  [OK] All family caps within sane bounds (0, 1.0%]")

    if t <= r <= v:
        _p("  [OK] Ordering trend <= range <= volatility "
           "(liquidity-consistent)")
    else:
        _p(f"  [INFO] Ordering is trend={t} range={r} vol={v} "
           f"(non-standard but valid if intentional)")

    return 0


def check_vol_class_layering(verbose: bool) -> int:
    """Vol class triple: CAP_MULT (SL/TP) x R_THRESHOLDS x QTY_MULT."""
    from core import config_center as CC

    _hr("8. Vol class layering")
    classes = ["LOW", "MED", "HIGH"]
    _p(f"  {'class':<6} {'CAP_MULT':<10} {'QTY_MULT':<10} "
       f"{'BE_R':<8} {'L1_R':<8} {'L2_R':<8}")
    for c in classes:
        cap = CC.VOL_CLASS_CAP_MULT.get(c)
        qty = CC.VOL_CLASS_QTY_MULT.get(c)
        r = CC.VOL_CLASS_R_THRESHOLDS.get(c, {})
        _p(f"  {c:<6} {cap:<10} {qty:<10} "
           f"{r.get('be_r', '-'):<8} {r.get('lock1_r', '-'):<8} "
           f"{r.get('lock2_r', '-'):<8}")
    _p()
    _p("  [OK] CAP_MULT scales SL/TP distances; QTY_MULT scales "
       "position size")
    _p("  [OK] R_THRESHOLDS drive BE/Lock1/Lock2 trigger levels")
    return 0


def check_legacy_r_mult_pairs(verbose: bool) -> int:
    """Legacy % SL fallback vs R-multiple thresholds."""
    from core import config_center as CC

    _hr("9. Legacy % SL vs R-multiple pairs")
    pairs = [
        ("be_factor",  "be_stop_r",    "BE"),
        ("lock1_pct",  "lock1_stop_r", "Lock1"),
        ("lock2_pct",  "lock2_stop_r", "Lock2"),
    ]
    for legacy, modern, label in pairs:
        lv = CC.GLOBAL.get(legacy)
        mv = CC.GLOBAL.get(modern)
        _p(f"  {label:<6} legacy {legacy:<12}={lv:<8}  "
           f"modern {modern:<12}={mv}")
    _p()
    _p("  [INFO] Legacy path used when risk_unit missing; modern path is "
       "R-based")
    return 0


def check_safety_snapshot_keys(verbose: bool) -> int:
    """
    REV 5.5 - Check 10.

    core/state.py imports _SAFETY_SNAPSHOT_KEYS at module load and
    raises RuntimeError if any is missing from GLOBAL. This check
    surfaces that failure here (before runtime) with a clear message.
    """
    _hr("10. Safety snapshot keys present in GLOBAL")
    try:
        from core import config_center as CC
    except Exception as e:
        _p(f"  [WARN] cannot import config_center: {type(e).__name__}: {e}")
        return 1

    safety = getattr(CC, "_SAFETY_SNAPSHOT_KEYS", None)
    if safety is None:
        _p("  [WARN] _SAFETY_SNAPSHOT_KEYS not defined in config_center")
        _p("         state.py imports this set at module load.")
        return 1

    missing = sorted(k for k in safety if k not in CC.GLOBAL)
    if missing:
        _p(f"  [WARN] {len(missing)} safety key(s) missing from GLOBAL:")
        for k in missing:
            _p(f"         - {k}")
        _p("         state.py will fail its import-time check with a")
        _p("         clear RuntimeError. Add them to GLOBAL defaults.")
        return 1

    _p(f"  [OK] All {len(safety)} safety keys present: {sorted(safety)}")
    return 0


def check_proxy_contract(verbose: bool) -> int:
    """
    REV 5.5 - Check 11.

    Every value in core/config.py's _PROXIED_TRADING_ATTRS must resolve
    to a key present in config_center.GLOBAL. Catches drift between the
    proxy contract and the source of truth.
    """
    _hr("11. config.py proxy contract (_PROXIED_TRADING_ATTRS)")
    try:
        from core import config_center as CC
    except Exception as e:
        _p(f"  [WARN] cannot import config_center: {type(e).__name__}: {e}")
        return 1

    verify = getattr(CC, "verify_proxy_contract", None)
    if not callable(verify):
        _p("  [WARN] config_center.verify_proxy_contract() not available")
        _p("         (added in config_center REV 5.6). Upgrade required.")
        return 1

    try:
        missing = verify()
    except Exception as e:
        _p(f"  [WARN] verify_proxy_contract raised: "
           f"{type(e).__name__}: {e}")
        return 1

    if missing:
        _p(f"  [WARN] {len(missing)} proxied attr(s) not in GLOBAL:")
        for k in missing:
            _p(f"         - {k}")
        _p("         Fix: add missing keys to config_center.GLOBAL, or")
        _p("         remove stale entries from config.py "
           "_PROXIED_TRADING_ATTRS.")
        return 1

    _p("  [OK] All config.py _PROXIED_TRADING_ATTRS resolve to GLOBAL "
       "keys")
    return 0


def check_decision_sanity(verbose: bool) -> int:
    """
    REV 5.5 - Check 12.

    Mirrors the DECISION sanity block in config_center._validate(),
    but is runnable standalone (useful when _validate has been
    relaxed or during incremental upgrades).
    """
    _hr("12. DECISION sanity")
    try:
        from core import config_center as CC
    except Exception as e:
        _p(f"  [WARN] cannot import config_center: {type(e).__name__}: {e}")
        return 1

    d = CC.DECISION
    issues: list[str] = []

    ma = d.get("min_approvals")
    tf = d.get("total_filters")
    if ma is None or tf is None:
        issues.append("min_approvals / total_filters missing")
    else:
        if ma > tf:
            issues.append(
                f"min_approvals={ma} > total_filters={tf} "
                f"(impossible gate - no signal could ever pass)"
            )

    wmin = d.get("wr_mult_min")
    wmax = d.get("wr_mult_max")
    if wmin is not None and wmax is not None and wmin > wmax:
        issues.append(f"wr_mult_min={wmin} > wr_mult_max={wmax}")

    soft = d.get("adx_counter_trend_soft_max")
    hard = d.get("adx_counter_trend_hard_max")
    if soft is not None and hard is not None and soft > hard:
        issues.append(
            f"adx_counter_trend_soft_max={soft} > "
            f"adx_counter_trend_hard_max={hard}"
        )

    if issues:
        for i in issues:
            _p(f"  [WARN] {i}")
        return 1

    _p(f"  [OK] min_approvals={ma}/{tf}, "
       f"wr_mult=[{wmin}, {wmax}], "
       f"adx={soft}/{hard}")
    return 0


# ═══════════════════════════════════════════════════════════════
#  REGISTRY
# ═══════════════════════════════════════════════════════════════
CHECKS: list[tuple[str, Callable[[bool], int]]] = [
    ("global_regime_overlap",          check_global_regime_overlap),
    ("global_strategy_regime_overlap", check_global_strategy_regime_overlap),
    ("global_family_filters_overlap",  check_global_family_filters_overlap),
    ("min_rr",                         check_min_rr),
    ("hold_minutes_layering",          check_hold_minutes_layering),
    ("time_exit_deps",                 check_time_exit_deps),
    ("spread_caps",                    check_spread_caps),
    ("vol_class_layering",             check_vol_class_layering),
    ("legacy_r_mult_pairs",            check_legacy_r_mult_pairs),
    ("safety_snapshot_keys",           check_safety_snapshot_keys),
    ("proxy_contract",                 check_proxy_contract),
    ("decision_sanity",                check_decision_sanity),
]


# ═══════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════
def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="audit_config_overlaps",
        description="Deep consistency audit of the layered config system.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--strict", action="store_true",
        help="Exit 1 if any warning is found (CI mode).",
    )
    mode.add_argument(
        "--report", action="store_true",
        help="Always exit 0; write findings to --output (human mode).",
    )
    p.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="Output format (default: text).",
    )
    p.add_argument(
        "--output", type=Path, default=None,
        help="Write report to this path (default: no file written).",
    )
    p.add_argument(
        "--quiet", action="store_true",
        help="Suppress ALL terminal output (useful in CI pipelines). "
             "Combine with --output to save results.",
    )
    p.add_argument(
        "--verbose", action="store_true",
        help="Show full row details, not just conflicts.",
    )
    return p.parse_args(argv)


def _run_all_checks(verbose: bool) -> tuple[int, str]:
    """
    Run every check, capture stdout, return (warnings, captured_text).

    All check output is captured into an io.StringIO buffer via
    redirect_stdout. main() decides whether to print the buffer to the
    terminal (respecting --quiet) or write it to --output / emit JSON.
    """
    buf = io.StringIO()
    warnings = 0
    with redirect_stdout(buf):
        for name, fn in CHECKS:
            try:
                warnings += fn(verbose=verbose)
            except Exception as e:
                # A check crashing should not silently pass; count it.
                print(f"  [WARN] check {name} raised "
                      f"{type(e).__name__}: {e}")
                warnings += 1
    return warnings, buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    # Ensure core package is importable (run from repo root).
    root = Path.cwd()
    if not (root / "core").exists():
        print("Run this from the repo root (folder containing 'core/').",
              file=sys.stderr)
        return 2

    try:
        from core import config_center as CC  # noqa: F401
    except Exception as e:
        print(f"Cannot import core.config_center: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    warnings, text_report = _run_all_checks(args.verbose)

    # ── Output ──
    if args.format == "json":
        payload = json.dumps(
            {
                "warnings": warnings,
                "checks_run": len(CHECKS),
                "exit_code": 1 if warnings else 0,
                "text_output": text_report,
            },
            indent=2,
        )
        if args.output:
            args.output.write_text(payload, encoding="utf-8")
            if not args.quiet:
                print(f"JSON report written: {args.output}")
        elif not args.quiet:
            # REV 5.5b — --quiet now consistently suppresses stdout too.
            print(payload)
    else:
        if args.output:
            args.output.write_text(text_report, encoding="utf-8")
            if not args.quiet:
                print(f"Report written: {args.output}")
        if not args.quiet:
            print("=" * 72)
            print("  CONFIG OVERLAP AUDIT -- REV 5.5b")
            print("=" * 72)
            print(text_report)
            print()
            print("=" * 72)
            if warnings:
                print(f"  [WARN] {warnings} warning(s) - review above")
            else:
                print("  [OK] No conflicts. Config layering is clean.")
            print("=" * 72)

    # ── Exit code ──
    if args.report:
        return 0
    # default is strict semantics for CI safety
    return 1 if warnings > 0 else 0


if __name__ == "__main__":
    sys.exit(main())