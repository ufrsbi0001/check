"""
scripts/fix_config_consumers.py — Auto-migrate legacy CONFIG.<trading_key>
usages to config_center.get("<key>").

SAFE:
  • Dry-run by default (--dry-run implied)
  • --apply needed to write changes
  • .bak backups created automatically (unless --no-backup)

HANDLES:
  • CONFIG.<trading_key>          → CC.get("<trading_key>")
  • getattr(CONFIG, "X")          → CC.get("X")
  • getattr(CONFIG, "X", CONFIG.Y) → CC.get("X", CC.get("Y"))
  • CONFIG.max_hold_minutes       → CC.get("hold_minutes")   [rename]
  • CONFIG.use_1m_trend_filter    → CC.get("use_5m_trend_filter")  [rename]
  • Auto-adds `from core import config_center as CC` if missing

Usage:
    python -m scripts.fix_config_consumers               # dry-run
    python -m scripts.fix_config_consumers --apply       # write changes
    python -m scripts.fix_config_consumers --apply --no-backup
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import sys


TRADING_KEYS = [
    "leverage", "risk_percent", "max_open_positions",
    "max_same_side_positions", "max_total_margin_pct",
    "max_daily_loss_trades", "max_daily_drawdown_percent",
    "max_account_drawdown", "min_confidence", "min_adx",
    "require_htf_agreement", "use_5m_trend_filter",
    "use_1m_trend_filter", "fg_enabled",
    "max_trades_per_coin_per_day", "htf_align_relaxed",
    "use_taker_volume", "use_volume_profile",
    "use_anchored_vwap", "use_funding_z", "use_mtf_confluence",
    "kz_bypass", "use_spread_filter", "max_spread_pct",
    "max_spread_trend", "max_spread_range", "max_spread_volatility",
    "hold_minutes", "max_hold_minutes", "partial_close_usdt",
    "min_rr", "cooldown_after_sl_min", "cooldown_after_tp_min",
]

# Legacy attr name → correct config_center key
SPECIAL_RENAMES = {
    "max_hold_minutes":     "hold_minutes",
    "use_1m_trend_filter":  "use_5m_trend_filter",
}

SKIP_FILES = {
    "core/config.py",
    "core/config_center.py",
    "scripts/audit_config_consumers.py",
    "scripts/diagnose_config.py",
    "scripts/fix_config_consumers.py",
}

SKIP_DIRS = {
    ".venv", "venv", "__pycache__", ".git", "node_modules",
    ".pytest_cache", ".mypy_cache", "build", "dist",
}


def _transform_text(text: str) -> tuple[str, int]:
    """Apply transformations. Return (new_text, change_count)."""
    changes = 0
    key_group = "|".join(re.escape(k) for k in TRADING_KEYS)

    # 1. getattr(CONFIG, "X", CONFIG.Y) → CC.get("X", CC.get("Y"))
    def _r1(m):
        nonlocal changes
        changes += 1
        return f'CC.get("{m.group(1)}", CC.get("{m.group(2)}"))'
    text = re.sub(
        r'getattr\(\s*CONFIG\s*,\s*["\']([\w]+)["\']\s*,\s*CONFIG\.([\w]+)\s*\)',
        _r1, text,
    )

    # 2. getattr(CONFIG, "X") → CC.get("X")
    def _r2(m):
        nonlocal changes
        changes += 1
        attr = m.group(1)
        key = SPECIAL_RENAMES.get(attr, attr)
        return f'CC.get("{key}")'
    text = re.sub(
        r'getattr\(\s*CONFIG\s*,\s*["\']([\w]+)["\']\s*\)',
        _r2, text,
    )

    # 3. CONFIG.<key> → CC.get("<key>")
    def _r3(m):
        nonlocal changes
        changes += 1
        attr = m.group(1)
        key = SPECIAL_RENAMES.get(attr, attr)
        return f'CC.get("{key}")'
    text = re.sub(rf'\bCONFIG\.({key_group})\b', _r3, text)

    # 4. Ensure CC import
    if changes > 0 and "from core import config_center as CC" not in text:
        # Insert after `from core.config import CONFIG` if present
        text2, n = re.subn(
            r'^(from\s+core\.config\s+import\s+CONFIG.*)$',
            r'\1\nfrom core import config_center as CC',
            text, count=1, flags=re.MULTILINE,
        )
        if n == 0:
            # Else after last `from core...` import
            text2, n = re.subn(
                r'^(from\s+core[^\n]*)$',
                r'\1\nfrom core import config_center as CC',
                text, count=1, flags=re.MULTILINE,
            )
        if n == 0:
            print(f"   ⚠️  Could not auto-insert `from core import config_center as CC` — add manually.")
        text = text2

    return text, changes


def _should_skip(path: pathlib.Path, root: pathlib.Path) -> bool:
    rel = path.relative_to(root).as_posix()
    if rel in SKIP_FILES:
        return True
    return any(p in SKIP_DIRS for p in path.parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="Actually write changes (default = dry-run).")
    ap.add_argument("--no-backup", action="store_true",
                    help="Skip .bak backups when applying.")
    args = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parent.parent
    targets: list[tuple[pathlib.Path, str, str, int]] = []

    for py in sorted(root.rglob("*.py")):
        if _should_skip(py, root):
            continue
        try:
            old = py.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        new, n = _transform_text(old)
        if n == 0:
            continue
        targets.append((py, old, new, n))

    if not targets:
        print("✅ Nothing to fix — all clean.")
        return 0

    total = sum(t[3] for t in targets)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode} — {len(targets)} file(s), {total} change(s)")
    print("=" * 72)

    for py, old, new, n in targets:
        rel = py.relative_to(root).as_posix()
        print(f"📄 {rel}  ({n} change(s))")
        old_lines = old.splitlines()
        new_lines = new.splitlines()
        for i, (o, nw) in enumerate(zip(old_lines, new_lines), 1):
            if o != nw:
                print(f"   L{i}:")
                print(f"     - {o.strip()}")
                print(f"     + {nw.strip()}")
        print()

        if args.apply:
            if not args.no_backup:
                shutil.copy2(py, str(py) + ".bak")
            py.write_text(new, encoding="utf-8")

    print("=" * 72)
    if args.apply:
        print(f"✅ Applied to {len(targets)} file(s).")
        if not args.no_backup:
            print("   Backups: *.py.bak (delete after verifying)")
    else:
        print("ℹ️  Dry-run. To apply:")
        print("     python -m scripts.fix_config_consumers --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())