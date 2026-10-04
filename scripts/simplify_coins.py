"""
simplify_coins.py — Convert each coin file to identity-only form.

Preserves:
  • SYMBOL, BASE, FAMILY, PROFILE, VOL_CLASS, ENABLED
  • TD_FADE (strategy-specific config)

Removes:
  • ST_PARAMS, CAPS, FILTERS (now come from core/family_baselines.py)

DRY-RUN by default. Backup before write.
"""
from __future__ import annotations
import argparse
import os
import re
import shutil
from datetime import datetime
from pathlib import Path


COINS_DIR = "coins"

TEMPLATE = '''"""
{module_name}.py — {symbol} coin configuration (IDENTITY ONLY).

Tuning (ST_PARAMS, CAPS, FILTERS) comes from:
  core/family_baselines.py → BASELINES["{family}"]
"""
from __future__ import annotations

SYMBOL     = "{symbol}"
BASE       = "{base}"
FAMILY     = "{family}"
PROFILE    = "{profile}"
VOL_CLASS  = "{vol_class}"
ENABLED    = {enabled}
{td_fade_block}'''


def _extract_field(content: str, field: str, default=""):
    m = re.search(rf'^{field}\s*=\s*(.+?)\s*$', content, re.MULTILINE)
    if m:
        val = m.group(1).strip()
        if val.startswith(('"', "'")) and val.endswith(('"', "'")):
            return val[1:-1]
        return val
    return default


def _extract_dict(content: str, dict_name: str) -> str | None:
    pat = re.compile(rf'\b{re.escape(dict_name)}\s*=\s*\{{.*?\n\}}', re.DOTALL)
    m = pat.search(content)
    return m.group(0) if m else None


def _process_file(path: Path, apply: bool) -> bool:
    try:
        original = path.read_text(encoding="utf-8")
    except Exception:
        return False

    symbol = _extract_field(original, "SYMBOL")
    if not symbol:
        return False

    base      = _extract_field(original, "BASE", symbol.replace("USDT", ""))
    family    = _extract_field(original, "FAMILY", "trend_coins")
    profile   = _extract_field(original, "PROFILE", "MIXED")
    vol_class = _extract_field(original, "VOL_CLASS", "MED")
    enabled   = _extract_field(original, "ENABLED", "False")

    td_fade = _extract_dict(original, "TD_FADE")
    td_fade_block = "\n" + td_fade + "\n" if td_fade else ""

    new_content = TEMPLATE.format(
        module_name=path.stem,
        symbol=symbol, base=base, family=family,
        profile=profile, vol_class=vol_class, enabled=enabled,
        td_fade_block=td_fade_block,
    )

    if new_content.strip() == original.strip():
        return False

    if apply:
        path.write_text(new_content, encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(COINS_DIR):
        print(f"❌ '{COINS_DIR}' not found"); return

    if args.apply:
        bdir = f"coins_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copytree(COINS_DIR, bdir)
        print(f"✅ Backup: {bdir}/\n")

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"━━━ Mode: {mode} ━━━\n")

    total = changed = 0
    for root, _, files in os.walk(COINS_DIR):
        for fname in sorted(files):
            if not fname.endswith(".py"): continue
            if fname in ("__init__.py", "__main__.py", "_template.py"): continue
            path = Path(root) / fname
            total += 1
            if _process_file(path, args.apply):
                changed += 1
                print(f"  {path.relative_to(COINS_DIR)}")

    print(f"\n━━━ Total: {total} | Changed: {changed}")
    if not args.apply:
        print("\n  DRY-RUN. To apply:")
        print("      python simplify_coins.py --apply")


if __name__ == "__main__":
    main()