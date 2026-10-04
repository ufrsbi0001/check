"""
coins/__init__.py — Recursive coin loader.

REV 19.12 (2026-09-25) — RECURSIVE LOADING:
  ✅ Walks coins/ recursively — picks up coins/volatility/*.py,
     coins/trend/*.py, coins/momentum/*.py, coins/range/*.py, and
     any future subfolder.
  ✅ Skips __pycache__, __init__.py, _private.py, and non-coin
     modules (those without SYMBOL attribute).
  ✅ Detects duplicate SYMBOLs and reports them (does NOT silently
     overwrite).
  ✅ Exposes LOAD_ERRORS for diagnostics.

API:
    get_coin(symbol)  → dict | None
    all_coins()       → dict[str, dict]  (keyed by SYMBOL, uppercase)
"""
from __future__ import annotations

import importlib
from pathlib import Path

_COINS: dict[str, dict] = {}
_LOAD_ERRORS: dict[str, str] = {}
_COINS_ROOT = Path(__file__).parent
_PROJECT_ROOT = _COINS_ROOT.parent


def _discover_modules():
    """Yield (module_name, file_path) for every .py under coins/."""
    for py_file in sorted(_COINS_ROOT.rglob("*.py")):
        parts = py_file.parts
        if "__pycache__" in parts:
            continue
        if py_file.name.startswith("_"):
            continue

        # Derive dotted module name: coins.volatility.avax
        try:
            rel = py_file.relative_to(_PROJECT_ROOT)
        except ValueError:
            continue
        mod_name = ".".join(rel.with_suffix("").parts)
        yield mod_name, py_file


def _load_all():
    for mod_name, py_file in _discover_modules():
        try:
            mod = importlib.import_module(mod_name)
        except Exception as e:
            _LOAD_ERRORS[mod_name] = f"{type(e).__name__}: {e}"
            continue

        sym = getattr(mod, "SYMBOL", None)
        if not sym:
            # Not a coin config file — skip silently
            continue

        sym = str(sym).upper()
        if sym in _COINS:
            prev = _COINS[sym].get("_module", "?")
            _LOAD_ERRORS[mod_name] = (
                f"duplicate SYMBOL {sym} (already loaded from {prev})"
            )
            continue

        _COINS[sym] = {
            "_module":   mod_name,
            "symbol":    sym,
            "base":      str(getattr(mod, "BASE", sym)),
            "family":    str(getattr(mod, "FAMILY", "trend_coins")),
            "profile":   str(getattr(mod, "PROFILE", "MIXED")),
            "enabled":   bool(getattr(mod, "ENABLED", False)),
            "vol_class": str(getattr(mod, "VOL_CLASS", "MED")),
            "caps":      dict(getattr(mod, "CAPS", {}) or {}),
            "st_params": dict(getattr(mod, "ST_PARAMS", {}) or {}),
            "filters":   dict(getattr(mod, "FILTERS", {}) or {}),
            "td_fade":   dict(getattr(mod, "TD_FADE", {}) or {}),
        }


_load_all()


# ─── Public API ──────────────────────────────────────────
def all_coins() -> dict[str, dict]:
    """Return {SYMBOL: config-dict}. Read-only by convention."""
    return _COINS


def get_coin(symbol: str) -> dict | None:
    """Lookup coin config by symbol. Normalizes case + USDT suffix."""
    if not symbol:
        return None
    sym = str(symbol).upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    return _COINS.get(sym)


def load_errors() -> dict[str, str]:
    """Return {module_name: error_message} for failed loads."""
    return dict(_LOAD_ERRORS)


if __name__ == "__main__":
    print(f"Loaded {len(_COINS)} coins from {_COINS_ROOT}")
    if _LOAD_ERRORS:
        print(f"\n⚠️  {len(_LOAD_ERRORS)} load error(s):")
        for k, v in sorted(_LOAD_ERRORS.items()):
            print(f"    {k}: {v}")
    else:
        print("✅ No load errors.")
    print(f"\nBy family:")
    by_fam: dict[str, list[str]] = {}
    for sym, c in _COINS.items():
        by_fam.setdefault(c["family"], []).append(sym)
    for fam, syms in sorted(by_fam.items()):
        print(f"  {fam:<20} ({len(syms):>2}) → {sorted(syms)}")