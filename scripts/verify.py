# verify.py — Hardening + review-point verification.
#
# Usage (from anywhere):
#     python verify.py
#     python scripts/verify.py
#
# Auto-detects repo root by walking up until it finds core/ + orders/.
# Prints a per-check report and exits 0 (all OK) / 1 (something missing).
#
# REV 2.0 (2026-10-04):
#   ✅ Auto-detect repo root — works from any cwd (root, scripts/, etc.)
#   ✅ UTF-8 stdout guard — no UnicodeEncodeError on Windows cp1252
#   ✅ Relative-path reporting (readable on long Windows paths)
#   ✅ Path matching uses pathlib parts (cross-platform)
#   ✅ Added Point 4 (adopted_fallback_sl_pct) and Point 5
#     (warning-level log upgrades) checks
#   ✅ Non-zero exit code on failure (CI-friendly)
#   ✅ Per-check summary counts (files vs hits)

import ast
import io
import pathlib
import re
import sys
import tokenize


# ═════════════════════════════════════════════════════════════
#  WINDOWS UTF-8 STDOUT GUARD
# ═════════════════════════════════════════════════════════════
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, Exception):
        pass


# ═════════════════════════════════════════════════════════════
#  CHECKS — 8 hardening fixes + 2 review points
# ═════════════════════════════════════════════════════════════
CHECKS = {
    # ── 8 hardening fixes ──
    "C1 (UNKNOWN guard)":
        r"NOT retrying with fresh cid",
    "C2 (fast reconcile)":
        r"_UNVERIFIED_FAST_RECONCILE_SEC",
    "C3 (closePosition)":
        r"closePosition\s*=\s*True",
    "C4 (deterministic emg)":
        r"_emg_cid",
    "C5 (abort mult)":
        r"risk_oversize_abort_mult",
    "C6 (sizing reject)":
        r"REJECTING entry",
    "A1 (critical pool)":
        r"_CRITICAL_MIN_TIMEOUT|_CRITICAL_TIMEOUT_EXECUTOR",
    "A2 (TP1 lock)":
        r"_open_orders_tp1",

    # ── Point 4 (hardcoded SL % → config) ──
    "P4 (adopted SL config)":
        r"adopted_fallback_sl_pct",

    # ── Point 5 (log-level upgrades) ──
    "P5 (indicator warn)":
        r"indicator fetch failed",
    "P5 (btc regime warn)":
        r"regime update failed",

    # ── PATCH P1 / P2 (see PATCH_NOTES.md) ──
    "P1 (slippage cfg key)":
        r"\"max_fill_slippage_pct\"\s*:",
    "P2 (orphan watch)":
        r"def _orphan_watch_tick",
}

# Scan scope: any .py under these top-level dirs, plus these root files.
TARGET_DIRS = ("core", "orders", "signals", "market", "web")
TARGET_ROOT_FILES = ("future.py", "run.py", "app.py")


# ═════════════════════════════════════════════════════════════
#  CODE-ONLY VIEW (PATCH P3)
#  The old check was a plain grep, so a fix that existed only in a
#  comment or docstring still printed [+]. We blank out comments and
#  docstrings first, so a hit means real code (string literals such
#  as log messages still count, on purpose).
# ═════════════════════════════════════════════════════════════
def code_only_lines(text: str):
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None)
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(getattr(body[0], "value", None),
                                       ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    for ln in range(body[0].lineno, body[0].end_lineno + 1):
                        lines[ln - 1] = ""
    except SyntaxError:
        pass
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                r, c = tok.start
                if r - 1 < len(lines):
                    lines[r - 1] = lines[r - 1][:c]
    except (tokenize.TokenError, IndentationError):
        pass
    return lines


# ═════════════════════════════════════════════════════════════
#  REPO ROOT DETECTION
# ═════════════════════════════════════════════════════════════
def find_repo_root(start: pathlib.Path) -> pathlib.Path:
    """
    Walk up from `start` until a directory containing BOTH
    `core/` and `orders/` is found. Falls back to cwd.
    """
    p = start.resolve()
    for _ in range(10):  # max depth
        if (p / "core").is_dir() and (p / "orders").is_dir():
            return p
        # also accept: at least future.py at root
        if (p / "future.py").is_file() and (p / "core").is_dir():
            return p
        parent = p.parent
        if parent == p:
            break
        p = parent
    return start.resolve()


# ═════════════════════════════════════════════════════════════
#  PATH MATCHER (cross-platform via pathlib parts)
# ═════════════════════════════════════════════════════════════
def should_scan(path: pathlib.Path, root: pathlib.Path) -> bool:
    if path.suffix != ".py":
        return False
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False

    parts = rel.parts
    if not parts:
        return False

    # Root-level file we care about
    if len(parts) == 1 and parts[0] in TARGET_ROOT_FILES:
        return True

    # Under one of the target directories
    if parts[0] in TARGET_DIRS:
        return True

    return False


# ═════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════
def main() -> int:
    here = pathlib.Path(__file__).parent
    root = find_repo_root(here)

    print("=" * 72)
    print("  HARDENING + REVIEW VERIFICATION")
    print(f"  repo root: {root}")
    print("=" * 72)

    # Collect all in-scope files once (avoid repeated rglob)
    scanned_files = [p for p in root.rglob("*.py") if should_scan(p, root)]
    print(f"  scanning {len(scanned_files)} .py file(s) "
          f"in {TARGET_DIRS} + {TARGET_ROOT_FILES}")
    print()

    any_missing = False
    total_ok = 0
    total_missing = 0

    for label, pattern in CHECKS.items():
        rx = re.compile(pattern)
        hits = []
        for p in scanned_files:
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for i, line in enumerate(code_only_lines(text), 1):
                if rx.search(line):
                    try:
                        rel = p.relative_to(root)
                    except ValueError:
                        rel = p
                    hits.append(f"    {rel}:{i}: {line.strip()[:100]}")

        if hits:
            marker = "[+]"
            total_ok += 1
        else:
            marker = "[X]"
            total_missing += 1
            any_missing = True

        print(f"{marker} {label}")
        print(f"    pattern: {pattern}")
        if hits:
            for h in hits[:6]:
                print(h)
            if len(hits) > 6:
                print(f"    ... +{len(hits) - 6} more")
        else:
            print("    NO HITS — fix NOT applied")
        print()

    print("=" * 72)
    print(f"  RESULT: {total_ok} OK / {total_missing} MISSING "
          f"(total {len(CHECKS)})")
    print("=" * 72)

    return 1 if any_missing else 0


if __name__ == "__main__":
    sys.exit(main())