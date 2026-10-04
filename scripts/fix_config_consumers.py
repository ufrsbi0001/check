"""
scripts/fix_config_consumers.py - Auto-migrate legacy CONFIG.<trading_key>
usages to config_center.get('key').

REV 2 — Fixed nested-quote bug: uses SINGLE quotes inside CC.get() so
it works safely inside double-quoted f-strings (Python < 3.12).
"""
from __future__ import annotations

import argparse
import difflib
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

SPECIAL_RENAMES = {
    "max_hold_minutes":    "hold_minutes",
    "use_1m_trend_filter": "use_5m_trend_filter",
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


def _q(s: str) -> str:
    """Quote key with SINGLE quotes (safe inside f"..." strings)."""
    return "'" + s + "'"


def _transform_text(text: str):
    changes = 0
    key_group = "|".join(re.escape(k) for k in TRADING_KEYS)

    # getattr(CONFIG, "X", CONFIG.Y) → CC.get('X', CC.get('Y'))
    def _r1(m):
        nonlocal changes
        changes += 1
        return f"CC.get({_q(m.group(1))}, CC.get({_q(m.group(2))}))"
    text = re.sub(
        r'getattr\(\s*CONFIG\s*,\s*["\']([\w]+)["\']\s*,\s*CONFIG\.([\w]+)\s*\)',
        _r1, text,
    )

    # getattr(CONFIG, "X") → CC.get('X')
    def _r2(m):
        nonlocal changes
        changes += 1
        attr = m.group(1)
        key = SPECIAL_RENAMES.get(attr, attr)
        return f"CC.get({_q(key)})"
    text = re.sub(
        r'getattr\(\s*CONFIG\s*,\s*["\']([\w]+)["\']\s*\)',
        _r2, text,
    )

    # CONFIG.<key> → CC.get('key')
    def _r3(m):
        nonlocal changes
        changes += 1
        attr = m.group(1)
        key = SPECIAL_RENAMES.get(attr, attr)
        return f"CC.get({_q(key)})"
    text = re.sub(rf'\bCONFIG\.({key_group})\b', _r3, text)

    # Ensure CC import
    if changes > 0 and "from core import config_center as CC" not in text:
        text2, n = re.subn(
            r'^(from\s+core\.config\s+import\s+CONFIG.*)$',
            r'\1\nfrom core import config_center as CC',
            text, count=1, flags=re.MULTILINE,
        )
        if n == 0:
            text2, n = re.subn(
                r'^(from\s+core[^\n]*)$',
                r'\1\nfrom core import config_center as CC',
                text, count=1, flags=re.MULTILINE,
            )
        if n == 0:
            print("   [WARN] Could not auto-insert `from core import config_center as CC`")
        text = text2

    return text, changes


def _should_skip(path, root):
    rel = path.relative_to(root).as_posix()
    if rel in SKIP_FILES:
        return True
    return any(p in SKIP_DIRS for p in path.parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    root = pathlib.Path(__file__).resolve().parent.parent
    targets = []

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
        print("[OK] Nothing to fix - all clean.")
        return 0

    total = sum(t[3] for t in targets)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode} - {len(targets)} file(s), {total} change(s)")
    print("=" * 72)

    for py, old, new, n in targets:
        rel = py.relative_to(root).as_posix()
        print(f"FILE: {rel}  ({n} change(s))")
        # Use unified diff — shows ONLY true changes, ignores line shifts
        diff = difflib.unified_diff(
            old.splitlines(keepends=False),
            new.splitlines(keepends=False),
            lineterm="",
            n=1,  # 1 line of context
        )
        for line in diff:
            if line.startswith("---") or line.startswith("+++"):
                continue
            print(f"   {line}")
        print()

        if args.apply:
            if not args.no_backup:
                shutil.copy2(py, str(py) + ".bak")
            py.write_text(new, encoding="utf-8")

    print("=" * 72)
    if args.apply:
        print(f"[OK] Applied to {len(targets)} file(s).")
        if not args.no_backup:
            print("     Backups: *.py.bak")
        print()
        print("[VERIFY] Run syntax check:")
        print("         python -m compileall -q .")
    else:
        print("[INFO] Dry-run. To apply:")
        print("       python -m scripts.fix_config_consumers --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())