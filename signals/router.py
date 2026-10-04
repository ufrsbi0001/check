"""
signals/router.py — Routes a symbol to its correct family module.

REV 19.19 (2026-10-03) — RESOLUTION + LOGGING HARDENING:
  ✅ FIXED: _resolve() fallback family-name mismatch. When get_family()
     returned an unregistered string AND DEFAULT_FAMILY was also
     unavailable (e.g. trend module import failed), the function
     returned (unregistered_name, fallback_module) — a pair where the
     name and module did not correspond. Now the registered name of
     the actual fallback module is looked up, so the returned pair is
     always self-consistent.
  ✅ _resolve() now LOGS when it falls back (family_name != requested)
     so silent routing degradation is visible in the log file.
  ✅ print() → logger (warning/debug) in _safe_import, _resolve,
     generate_signal_live, route_signal. Import-time and runtime
     routing issues now land in the rotating bot.log alongside the
     rest of the engine's logs.
  ✅ generate_signal_live() now logs a CRITICAL when FAMILIES is
     empty (all family modules failed to import) — previously it
     silently returned "no_family_module_available" with no context.
  ✅ route_signal() mode default aligned with generate_signal_live:
     Optional[str] = None, documented as "family uses its default".
     No caller is affected (neither signature is positional on mode).

REV 19.18 (2026-10-02) — DOCSTRING PATH + LOG PREFIX FIX.
REV 19.17 (2026-09-28) — DYNAMIC ROUTING REVERTED (CRITICAL FIX).
REV 19.16 (2026-09-28) — DYNAMIC FAMILY ROUTING. [superseded]
REV 19.15 (2026-09-26) — EAGER-FALLBACK FIX + CONSOLIDATION.
REV 19.14 (2026-09-25) — IDIOMATIC IMPORT.
REV 19.12 (2026-09-25) — HARDENING PASS.

ARCHITECTURE:
  router only maps family-STRING → family-MODULE.
  Coin → family mapping lives in coins/*.py and is read at runtime
  via coins_config.get_family(symbol).

  Four families are registered by default:
    trend_coins       — default fallback for unregistered symbols
    volatility_coins  — high-beta / high-ATR instruments
    momentum_coins    — breakout / continuation instruments
    range_coins       — mean-reverting / range-bound instruments

  To add a coin:
    1. Create coins/<name>.py
    2. Set FAMILY = "<one of the registered family strings>"
    3. Set ENABLED = True
    No changes needed in this file.

  To add a new family:
    1. Create <family>.py with the same interface
    2. Add one _safe_import() call below
    3. Add it to the FAMILIES registry loop

⚠️  INTERFACE CONTRACT — every family module MUST expose:
      FAMILY_COINS                                   → set[str]
      generate_signal_live(ind_1h, ind_4h, ind_1d,
                           symbol, mode)             → (sig, conf, reasons, lvl, div)
      route_signal(ind_1h, ind_4h, ind_1d,
                   profile, symbol, mode)            → dict | None
      is_family_coin(symbol)                         → bool

⚠️  REGIME COVERAGE — every family module must explicitly handle ALL
    regime strings emitted by the regime classifier (including
    VOLATILE). A missing regime branch silently produces zero signals
    with no rejection logged.
"""
from __future__ import annotations

import importlib
from typing import Optional

from core.coins_config import get_family, is_known

# ── REV 19.19 — shared logger with safe fallback ──
# router.py is imported early by future.py; core.client is normally
# available by then, but guard against import-order surprises so
# routing messages are never lost silently.
try:
    from core.client import logger as _logger
    logger = _logger
except Exception:
    import logging
    logger = logging.getLogger("router")

__all__ = [
    "FAMILIES", "DEFAULT_FAMILY",
    "get_family_module", "get_family_name", "is_registered",
    "family_summary", "format_family_summary",
    "generate_signal_live", "route_signal", "is_family_coin",
]


# ═════════════════════════════════════════════════════════════
#  IMPORT ALL FAMILY MODULES — each import is defensive
# ═════════════════════════════════════════════════════════════
_IMPORT_ERRORS: dict[str, str] = {}


def _safe_import(modname: str):
    """Import a family module. Returns module or None on failure."""
    try:
        return importlib.import_module(modname)
    except Exception as e:
        _IMPORT_ERRORS[modname] = f"{type(e).__name__}: {e}"
        logger.warning(f"[router] failed to import {modname}: {e}")
        return None


_tc = _safe_import("signals.trend")
_vc = _safe_import("signals.volatility")
_mc = _safe_import("signals.momentum")
_rc = _safe_import("signals.range")


# ═════════════════════════════════════════════════════════════
#  FAMILY REGISTRY — maps family name → module
#  Keys must match the `family` string used in coins/*.py.
# ═════════════════════════════════════════════════════════════
FAMILIES: dict[str, object] = {}
for _key, _mod in (
    ("trend_coins",      _tc),
    ("volatility_coins", _vc),
    ("momentum_coins",   _mc),
    ("range_coins",      _rc),
):
    if _mod is not None:
        FAMILIES[_key] = _mod

DEFAULT_FAMILY = "trend_coins"


def _fallback_module():
    """
    Return the fallback family module.

    Prefers trend_coins; degrades to any registered family if
    trend_coins failed to import; returns None if ALL failed.
    """
    if DEFAULT_FAMILY in FAMILIES:
        return FAMILIES[DEFAULT_FAMILY]
    if FAMILIES:
        return next(iter(FAMILIES.values()))
    return None


def _registered_name_for(module) -> Optional[str]:
    """
    Reverse-lookup: given a module object, return its registered
    family name. Returns None if the module is not in FAMILIES.

    Used by _resolve() to keep the returned (name, module) pair
    self-consistent after a fallback.
    """
    if module is None:
        return None
    for name, m in FAMILIES.items():
        if m is module:
            return name
    return None


# ═════════════════════════════════════════════════════════════
#  RESOLUTION HELPER — single source of truth
# ═════════════════════════════════════════════════════════════
def _resolve(symbol: str) -> tuple[str, object | None]:
    """
    Resolve a symbol to (family_name, module_or_None).

    REV 19.17 — STATIC routing (dynamic routing reverted).
    REV 19.19 — fallback pair is now ALWAYS self-consistent.
      When the requested family is unregistered AND DEFAULT_FAMILY
      is also unavailable, we previously returned the requested
      (unregistered) name paired with a fallback module — a mismatch.
      Now we reverse-lookup the actual registered name of the
      fallback module.

    Steps:
      1. coins_config.get_family(symbol) → family string
         (exceptions → fall back to DEFAULT_FAMILY)
      2. If family string is registered → use its module.
      3. Otherwise → use _fallback_module(); derive its registered
         name so the return pair stays consistent.
      4. Log a warning whenever we fall back from the requested name,
         so silent routing degradation is visible in bot.log.

    Returns:
      (family_name_string, module_or_None)
      family_name is always a non-empty string.
    """
    # ── Step 1: family string (STATIC) ──
    if symbol:
        try:
            family_name = get_family(symbol)
        except Exception as e:
            logger.debug(
                f"[router] get_family({symbol!r}) raised: "
                f"{type(e).__name__}: {e} — using default"
            )
            family_name = DEFAULT_FAMILY
    else:
        family_name = DEFAULT_FAMILY

    requested_name = family_name

    # ── Step 2 + 3: module lookup with explicit fallback ──
    module = FAMILIES.get(family_name)
    if module is None:
        module = _fallback_module()
        if module is not None:
            # REV 19.19 — reverse-lookup so name/module are consistent.
            resolved_name = _registered_name_for(module)
            if resolved_name is not None:
                family_name = resolved_name
            # Only log a fallback when the request differed from what
            # we ended up with — avoids spam for normal DEFAULT hits.
            if family_name != requested_name:
                logger.warning(
                    f"[router] {symbol or '(empty)'}: requested "
                    f"family={requested_name!r} not registered — "
                    f"fell back to {family_name!r}"
                )
        else:
            # FAMILIES is empty — no fallback available.
            logger.critical(
                f"[router] {symbol or '(empty)'}: requested "
                f"family={requested_name!r} not registered AND no "
                f"family modules loaded. _IMPORT_ERRORS="
                f"{_IMPORT_ERRORS or '{}'}"
            )

    return family_name, module


# ═════════════════════════════════════════════════════════════
#  PUBLIC HELPERS
# ═════════════════════════════════════════════════════════════
def get_family_module(symbol: str):
    """
    Return the family module OBJECT for a symbol.

    Never raises. Returns None only when NO family module could be
    imported at all (in which case FAMILIES is empty).
    """
    _, module = _resolve(symbol)
    return module


def get_family_name(symbol: str) -> str:
    """
    Return the family NAME (string) for a symbol.

    Always returns a registered family name, or DEFAULT_FAMILY if
    even trend_coins failed to import.
    """
    family_name, _ = _resolve(symbol)
    return family_name


def is_registered(symbol: str) -> bool:
    """True if symbol is in coins_config (known universe)."""
    if not symbol:
        return False
    try:
        return bool(is_known(symbol))
    except Exception:
        return False


def family_summary() -> dict:
    """
    Return summary of all families + coin counts.

    FAMILY_COINS only contains ENABLED coins. A family with count 0 may
    still have disabled coins registered in coins/.
    """
    out = {}
    for name, module in FAMILIES.items():
        try:
            coins = getattr(module, "FAMILY_COINS", set()) or set()
        except Exception:
            coins = set()
        try:
            coins_sorted = sorted(coins)
        except Exception:
            coins_sorted = list(coins)
        out[name] = {
            "coins":  coins_sorted,
            "count":  len(coins),
            "active": len(coins) > 0,
        }
    return out


def format_family_summary() -> str:
    """Pretty-print family breakdown for logs."""
    summary = family_summary()
    lines = []
    total = 0
    for fam, info in summary.items():
        total += info["count"]
        marker = "" if info["active"] else "  (unused)"
        lines.append(f"  {fam:<20} {info['count']:>2} coins{marker}")
    lines.append(f"  {'TOTAL':<20} {total:>2} coins")
    if _IMPORT_ERRORS:
        lines.append("")
        lines.append("  ⚠️  Import errors:")
        for k, v in _IMPORT_ERRORS.items():
            lines.append(f"     {k}: {v}")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════
#  MAIN ROUTER — same interface as any family module
# ═════════════════════════════════════════════════════════════
def generate_signal_live(ind_1h, ind_4h, ind_1d=None,
                         symbol="", mode="MULTI"):
    """
    Route to correct family module and return signal.

    Args:
      mode: strategy mode hint. "MULTI" (default) or None — both
            accepted; family modules treat None as default.

    Returns (sig, conf, reasons, lvl, div).

    REV 19.19 — logs CRITICAL when FAMILIES is empty (all family
    modules failed at import time). Previously a silent
    "no_family_module_available" reason was the only signal.
    """
    empty_lvl = {"SL": 0.0, "TP1": 0.0, "TP2": 0.0, "Qty": 0.0, "RR": 0.0}

    module = get_family_module(symbol)

    if module is None:
        logger.critical(
            f"[router] generate_signal_live({symbol or '(empty)'}): "
            f"no family module available — all imports failed. "
            f"_IMPORT_ERRORS={_IMPORT_ERRORS or '{}'}"
        )
        return "NEUTRAL", 0.0, ["no_family_module_available"], empty_lvl, "NONE"

    mod_name = getattr(module, "__name__", "unknown")
    try:
        return module.generate_signal_live(
            ind_1h, ind_4h, ind_1d,
            symbol=symbol,
            mode=mode,
        )
    except AttributeError as e:
        logger.error(f"[router] {mod_name} missing generate_signal_live: {e}")
        return "NEUTRAL", 0.0, [f"router_error_{mod_name}"], empty_lvl, "NONE"
    except Exception as e:
        logger.error(f"[router] {mod_name} raised {type(e).__name__}: {e}")
        return "NEUTRAL", 0.0, [f"router_exception_{type(e).__name__}"], empty_lvl, "NONE"


def route_signal(ind_1h, ind_4h, ind_1d=None,
                 profile: str = "MIXED", symbol: str = "",
                 mode: Optional[str] = None):
    """
    Same shape as family modules' route_signal() — returns dict or None.

    Args:
      mode: strategy mode hint. None (default) is passed through to
            the family module, which treats it as "use defaults".
            REV 19.19 — annotation aligned with generate_signal_live.
    """
    module = get_family_module(symbol)
    if module is None:
        return None
    try:
        return module.route_signal(
            ind_1h, ind_4h, ind_1d,
            profile=profile, symbol=symbol, mode=mode,
        )
    except Exception as e:
        logger.error(
            f"[router] route_signal error for {symbol or '(empty)'}: "
            f"{type(e).__name__}: {e}"
        )
        return None


def is_family_coin(symbol: str) -> bool:
    """True if symbol is registered AND its family has it as an active coin."""
    if not symbol:
        return False
    module = get_family_module(symbol)
    if module is None:
        return False
    try:
        return bool(module.is_family_coin(symbol))
    except Exception:
        return False


# ═════════════════════════════════════════════════════════════
#  DIAGNOSTIC — fully dynamic, no hardcoded coins
# ═════════════════════════════════════════════════════════════
if __name__ == "__main__":
    from core.coins_config import enabled_coins, all_coins

    # NOTE: prints below are intentional — this is a user-facing
    # diagnostic run via `python -m signals.router`.
    print("=" * 70)
    print("  ROUTER DIAGNOSTIC  (static — no hardcoded coins)")
    print("=" * 70)

    # ── 0. Import errors ──
    if _IMPORT_ERRORS:
        print("\n⚠️  Family import errors:")
        for k, v in _IMPORT_ERRORS.items():
            print(f"     {k}: {v}")
    else:
        print("\n✅ All family modules imported cleanly.")

    # ── 1. Family breakdown ──
    print("\nFamily summary:")
    print(format_family_summary())

    # ── 2. Every ENABLED coin ──
    print("\nEnabled coins  (source: coins_config.enabled_coins()):")
    try:
        registered = list(enabled_coins())
    except Exception as e:
        print(f"  ⚠️  enabled_coins() failed: {e}")
        registered = []
    if not registered:
        print("  ⚠️  No enabled coins found in coins/")
    else:
        for sym in registered:
            fam = get_family_name(sym)
            print(f"  ✅ {sym:<16} → {fam}")

    # ── 3. Edge cases ──
    print("\nEdge cases (derived from coins_config):")

    try:
        all_syms = set(all_coins().keys())
    except Exception as e:
        print(f"  ⚠️  all_coins() failed: {e}")
        all_syms = set()

    enabled_set = set(registered)
    disabled_syms = sorted(all_syms - enabled_set)

    if disabled_syms:
        for sym in disabled_syms:
            fam = get_family_name(sym)
            print(f"  ⚠️  {sym:<16} → {fam:<20} registered=True, DISABLED")
    else:
        print("  (no disabled coins registered)")

    # 3b. Unregistered coin (guaranteed non-existent)
    fake = "ZZZFAKEUSDT"
    while fake in all_syms:
        fake = "Z" + fake
    fake_fam = get_family_name(fake)
    print(f"  ❌ {fake:<16} → (fallback: {fake_fam})  "
          f"registered={is_registered(fake)}")

    # 3c. Empty symbol — actually CALL the router
    empty_fam = get_family_name("")
    empty_mod = get_family_module("")
    empty_mod_name = getattr(empty_mod, "__name__", None)
    print(f"  ❓ {'(empty)':<16} → family={empty_fam}, module={empty_mod_name}")

    # 3d. Normalization sanity check (coins_config REV 19.13)
    print("\nNormalization sanity check (coins_config REV 19.13):")
    if all_syms:
        sample = sorted(all_syms)[0]
        bare = sample[:-4] if sample.endswith("USDT") else sample
        lower = sample.lower()
        for probe, label in ((sample, "canonical"),
                             (bare,  "bare"),
                             (lower, "lowercase")):
            fam = get_family_name(probe)
            reg = is_registered(probe)
            print(f"  {label:<10} {probe:<12} → family={fam:<20} "
                  f"registered={reg}")
    else:
        print("  (no coins loaded — skipped)")

    # ── 4. Family module interface check ──
    print("\nFamily module interface check:")
    for fam_name, module in FAMILIES.items():
        has_gen   = hasattr(module, "generate_signal_live")
        has_route = hasattr(module, "route_signal")
        has_is_fc = hasattr(module, "is_family_coin")
        has_fc    = hasattr(module, "FAMILY_COINS")
        ok = has_gen and has_route and has_is_fc and has_fc
        marker = "✅" if ok else "❌"
        print(f"  {marker} {fam_name:<20} "
              f"gen={has_gen}  route={has_route}  "
              f"is_fc={has_is_fc}  FAMILY_COINS={has_fc}")

    print("\n" + "=" * 70)