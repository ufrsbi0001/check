# scripts/audit_dead_keys.py
"""
Audit GLOBAL keys: report usage count using word boundaries.
Catches true dead keys (only in config_center.py).

Usage:
    python -m scripts.audit_dead_keys
"""
from __future__ import annotations

import pathlib
import re
import sys

from core import config_center as CC


SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules"}
SELF = "core/config_center.py"


def main() -> int:
    root = pathlib.Path(__file__).resolve().parent.parent
    files = []
    for py in root.rglob("*.py"):
        rel = py.relative_to(root).as_posix()
        if rel == SELF or any(p in SKIP_DIRS for p in py.parts):
            continue
        try:
            files.append((rel, py.read_text(encoding="utf-8")))
        except (UnicodeDecodeError, OSError):
            continue

    print("=" * 72)
    print("  GLOBAL KEY USAGE (word-boundary match, excludes config_center.py)")
    print("=" * 72)

    dead = []
    borderline = []
    alive = []

    for key in sorted(CC.GLOBAL.keys()):
        pat = re.compile(rf"\b{re.escape(key)}\b")
        hits = [(rel, len(pat.findall(txt))) for rel, txt in files
                if pat.search(txt)]
        total = sum(h[1] for h in hits)
        if total == 0:
            dead.append(key)
        elif total <= 2:
            borderline.append((key, hits))
        else:
            alive.append((key, total))

    print()
    print(f"❌ DEAD ({len(dead)} keys — safe to delete):")
    for k in dead:
        print(f"   {k}")

    print()
    print(f"⚠️  BORDERLINE ({len(borderline)} keys — verify before deleting):")
    for k, hits in borderline:
        files_str = ", ".join(f"{rel}({n})" for rel, n in hits[:3])
        print(f"   {k:<32} → {files_str}")

    print()
    print(f"✅ ALIVE ({len(alive)} keys — in use)")

    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())