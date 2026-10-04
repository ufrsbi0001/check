"""
coins/__main__.py — CLI entry point for `python -m coins`.

REV 19.12 (2026-09-25) — Lets you run `python -m coins` to inspect
the coin registry without importing anything else.
"""
from __future__ import annotations

from coins import all_coins, load_errors


def _main() -> None:
    print("=" * 70)
    print("  COINS PACKAGE DIAGNOSTIC")
    print("=" * 70)

    errs = load_errors()
    if errs:
        print(f"\n⚠️  {len(errs)} load error(s):")
        for k, v in sorted(errs.items()):
            print(f"    {k}: {v}")
    else:
        print("\n✅ No load errors.")

    all_c = all_coins()
    print(f"\nLoaded {len(all_c)} coins:")
    for sym in sorted(all_c):
        c = all_c[sym]
        status = "✅" if c["enabled"] else "❌"
        print(f"  {status} {sym:<16} {c['family']:<18} "
              f"{c['profile']:<9} vol={c['vol_class']:<5}")

    enabled = [s for s, c in all_c.items() if c["enabled"]]
    print(f"\nEnabled: {len(enabled)}")

    by_fam: dict[str, list[str]] = {}
    for sym, c in all_c.items():
        by_fam.setdefault(c["family"], []).append(sym)
    if by_fam:
        print("\nFamily breakdown (dynamic):")
        for fam, syms in sorted(by_fam.items()):
            enabled_in_fam = [s for s in syms if all_c[s]["enabled"]]
            print(f"  {fam:<20} ({len(enabled_in_fam):>2} enabled / "
                  f"{len(syms):>2} total) → {sorted(enabled_in_fam)}")
    else:
        print("\n⚠️  No families found — loader returned zero coins.")


if __name__ == "__main__":
    _main()