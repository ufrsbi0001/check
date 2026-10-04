"""
fix_coin_filters.py — Safely tighten chase filters in 60 coin files.

REV 2.0 (2026-10-02) — TIGHTENED TARGETS (PHASE 1):
  Aligned with config_center.py values:
    - late_guard_adx:  45.0 → 35.0 (dead → fires)
    - late_guard_rsi:  62.0 → 58.0
    - late_guard_dist: 1.5  → 1.0
    - top_chase_rsi:   (new) 68.0  (was 68-76 mixed)
    - top_chase_flips: (new) 4     (was 5-8 mixed)
    - max_flips cap:   15   → 10

Run from bot root:
    python fix_coin_filters.py              # dry-run (preview only)
    python fix_coin_filters.py --apply      # actually write

Features:
  • Backup to coins_backup_<timestamp>/ before any write
  • Dry-run by default
  • Prints before → after for every change
  • Handles: max_dist_atr, pullback_dist_atr,
             late_guard_adx, late_guard_rsi, late_guard_dist,
             top_chase_rsi, top_chase_flips
  • max_flips: ONLY caps values > MAX_FLIPS_CAP down to cap
    (never raises values below cap, because those are already
    tighter).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
from datetime import datetime

COINS_DIR = "coins"

# ── Target values (tightened, REV 2.0) ──
# NOTE: max_flips is NOT here — it's handled separately below
#       because it must ONLY cap, never raise.
TARGETS = {
    "max_dist_atr":       "1.4",
    "pullback_dist_atr":  "0.8",
    "late_guard_adx":     "35.0",   # was 45.0
    "late_guard_rsi":     "58.0",   # was 62.0
    "late_guard_dist":    "1.0",    # was 1.5
    "top_chase_rsi":      "68.0",   # NEW
    "top_chase_flips":    "4",      # NEW
}

# ── max_flips: cap-only (values > MAX_FLIPS_CAP → cap) ──
MAX_FLIPS_CAP = 10                  # was 15


def _pattern(key: str):
    """Regex that matches '<key>': <number> inside quotes."""
    return re.compile(
        r'(["\']' + re.escape(key) + r'\s*["\']\s*:\s*)([\d.]+)'
    )


def _backup_dir() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"coins_backup_{ts}"


def _fix_max_flips(content: str) -> tuple[str, list[str]]:
    """
    Cap-only fix for max_flips.
    Returns (new_content, list_of_change_descriptions).
    """
    changes: list[str] = []
    pat = _pattern("max_flips")

    # Process matches right-to-left so offsets stay valid
    matches = list(pat.finditer(content))
    for m in reversed(matches):
        try:
            old_val = int(float(m.group(2)))
        except (TypeError, ValueError):
            continue
        if old_val > MAX_FLIPS_CAP:
            changes.append(
                f"    {'max_flips':<22} {old_val:>8}  →  {MAX_FLIPS_CAP}"
            )
            content = (
                content[:m.start(2)]
                + str(MAX_FLIPS_CAP)
                + content[m.end(2):]
            )
    return content, changes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes (default = dry-run)")
    ap.add_argument("--no-backup", action="store_true",
                    help="Skip backup (NOT recommended)")
    args = ap.parse_args()

    if not os.path.isdir(COINS_DIR):
        print(f"❌ '{COINS_DIR}' folder not found. Run from bot root.")
        return

    # ── Backup (only if writing) ──
    if args.apply and not args.no_backup:
        bdir = _backup_dir()
        shutil.copytree(COINS_DIR, bdir)
        print(f"✅ Backup created: {bdir}/")
        print()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"━━━ Mode: {mode} ━━━")
    print()

    total_files_changed = 0
    total_subs = 0

    for root, _, files in os.walk(COINS_DIR):
        for fname in sorted(files):
            if not fname.endswith(".py"):
                continue
            if fname == "__init__.py":
                continue

            path = os.path.join(root, fname)
            with open(path, "r", encoding="utf-8") as f:
                original = f.read()

            new_content = original
            file_changes: list[str] = []

            # ── Fixed-target keys (set to value) ──
            for key, target in TARGETS.items():
                pat = _pattern(key)
                for m in pat.finditer(new_content):
                    old_val = m.group(2)
                    if old_val != target:
                        file_changes.append(
                            f"    {key:<22} {old_val:>8}  →  {target}"
                        )
                        total_subs += 1
                new_content = pat.sub(r'\g<1>' + target, new_content)

            # ── max_flips (cap-only) ──
            new_content, flips_changes = _fix_max_flips(new_content)
            if flips_changes:
                file_changes.extend(flips_changes)
                total_subs += len(flips_changes)

            # ── Report + write ──
            if file_changes and new_content != original:
                total_files_changed += 1
                rel = os.path.relpath(path, COINS_DIR)
                print(f"  {rel}")
                for c in file_changes:
                    print(c)
                print()

                if args.apply:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)

    print("━" * 60)
    print(f"  Files changed : {total_files_changed}")
    print(f"  Total edits   : {total_subs}")
    if not args.apply:
        print()
        print("  This was a DRY-RUN. To apply:")
        print("      python fix_coin_filters.py --apply")
    print("━" * 60)


if __name__ == "__main__":
    main()