"""
core/coins_config.py — Wrapper around coins/ directory.

REV 23.4 (2026-10-04) — LOGGER COMPLETENESS + DOCSTRING CLARITY:
  ✅ Added critical() to _LazyLogger. Some downstream diagnostics
     use logger.critical(); the lazy proxy previously would have
     raised AttributeError on first call. Same lazy-deferral
     semantics as the existing methods.
  ✅ get_vol_class_mult() docstring now explicitly warns that this
     is the CAPS multiplier (used by signals/base.py::_mk() to widen
     or tighten SL/TP distances), NOT the position-size multiplier.
     The qty multiplier lives in config_center.VOL_CLASS_QTY_MULT
     and is consumed by orders/entry.py::_apply_vol_class_sizing().
     Two different maps, similar names — the confusion cost a
     diagnostic detour; documenting it in-place.

REV 23.3 (2026-10-03) — CIRCULAR IMPORT FIX (retained):
  ✅ Lazy logger proxy — defers the core.client import to first use
     to break the config → coins_config → client → config cycle.

REV 23.2 (2026-10-03) — FALLBACK CONSOLIDATION (retained).
REV 23.1 (2026-10-02) — FALLBACK VALUE ALIGNMENT (retained).
REV 23.0 (2026-10-02) — UNIFIED CONFIG SOURCE (retained).
REV 22.0 (2026-10-02) — FAMILY-LEVEL BASELINES (retained).
REV 21.7 (2026-09-30) — FALLBACK RR FIX (retained).
REV 19.16 (2026-09-29) — DEAD DYNAMIC ROUTING REMOVED.
REV 19.14 (2026-09-26) — GET_CAPS() FALLBACK RATIO FIX.
REV 19.13 (2026-09-26) — NORMALIZE WIRING + PUBLIC SURFACE.
REV 19.0  (2026-09-22) — FILE-PER-COIN ARCHITECTURE.
"""
from __future__ import annotations

import logging

from coins import get_coin, all_coins, load_errors as _coin_load_errors


# ═══════════════════════════════════════════════════════════
#  LAZY LOGGER PROXY  (REV 23.3, extended REV 23.4)
#  Directly importing core.client at module level is unsafe here:
#  coins_config is loaded DURING core.config's own module import
#  (via _resolve_coins), and core.client imports core.config at
#  module level. That chain produces a partial-module ImportError.
#  Deferring the import to first use breaks the cycle cleanly.
# ═══════════════════════════════════════════════════════════
class _LazyLogger:
    __slots__ = ("_impl",)

    def __init__(self) -> None:
        self._impl = None

    def _get(self):
        if self._impl is None:
            try:
                from core.client import logger as _cl
                self._impl = _cl
            except Exception:
                # Ephemeral fallback — retried on next call.
                return logging.getLogger(__name__)
        return self._impl

    # REV 23.4 — full level surface so downstream diagnostics never
    # AttributeError on the proxy.
    def debug(self, *a, **kw):     self._get().debug(*a, **kw)
    def info(self, *a, **kw):      self._get().info(*a, **kw)
    def warning(self, *a, **kw):   self._get().warning(*a, **kw)
    def error(self, *a, **kw):     self._get().error(*a, **kw)
    def critical(self, *a, **kw):  self._get().critical(*a, **kw)
    def exception(self, *a, **kw): self._get().exception(*a, **kw)


_logger = _LazyLogger()


# ═══════════════════════════════════════════════════════════
#  FALLBACK CONSTANTS  (REV 23.2 — single source)
#  Values mirror core/config_center.GLOBAL / FAMILY[DEFAULT_FAMILY].
#  Used only when config_center import or call fails — practically
#  never in practice. Single definition used by BOTH the emergency
#  ImportError block and the per-function except handlers.
# ═══════════════════════════════════════════════════════════
_DEFAULT_FAMILY_FALLBACK = "trend_coins"
_FALLBACK_ST_PARAMS = {"sl_atr": 1.8, "tp1_atr": 2.5, "tp2_atr": 5.0}
_FALLBACK_CAPS      = {"sl": 0.045, "tp1": 0.0675, "tp2": 0.125}


# ── REV 23.0 / 23.2 — Unified config source (single source of truth) ──
try:
    from core.config_center import (
        get_baseline as _get_family_baseline,
        get_vol_class_mult as _get_vol_class_mult,
        DEFAULT_FAMILY as _DEFAULT_FAMILY,
    )
    _HAS_CONFIG_CENTER = True
except ImportError:
    # ── EMERGENCY FALLBACK ONLY ──
    # This block should NEVER execute in practice — config_center is
    # a core dependency. Kept for defensive import safety (isolated
    # tests, tooling without full env).
    _DEFAULT_FAMILY = _DEFAULT_FAMILY_FALLBACK
    _HAS_CONFIG_CENTER = False

    def _get_family_baseline(family):  # pragma: no cover
        return {
            "ST_PARAMS": dict(_FALLBACK_ST_PARAMS),
            "CAPS":      dict(_FALLBACK_CAPS),
            "FILTERS":   {},
        }

    def _get_vol_class_mult(vcls):  # pragma: no cover
        return 1.0


__all__ = [
    # ── Symbol-taking accessors ──
    "get_profile", "get_family",
    "is_enabled", "is_known",
    "get_caps",
    "get_coin_filters", "get_coin_st_params", "get_coin_vol_class",
    "get_coin_td_fade",
    # ── Registry-level accessors ──
    "get_family_coins", "enabled_coins",
    # ── Diagnostics ──
    "load_errors", "known_families",
    # ── Re-exports (used by family_router.py) ──
    "get_coin", "all_coins",
    # ── REV 22.0 — Direct baseline access ──
    "get_vol_class_mult",
]


# ═══════════════════════════════════════════════════════════
#  SYMBOL NORMALIZATION
# ═══════════════════════════════════════════════════════════
def _normalize(symbol: str) -> str:
    """
    Canonical form for coin lookups: uppercase + USDT suffix.

    Handles:
      "btc"       → "BTCUSDT"
      "btcusdt"   → "BTCUSDT"
      "BTCUSDT"   → "BTCUSDT"
      ""          → ""    (get_coin returns None → defaults)
    """
    if not symbol:
        return ""
    sym = symbol.upper()
    if not sym.endswith("USDT"):
        sym += "USDT"
    return sym


# ═══════════════════════════════════════════════════════════
#  CORE ACCESSORS — all wrapped defensively
# ═══════════════════════════════════════════════════════════
def get_profile(symbol: str) -> str:
    try:
        c = get_coin(_normalize(symbol))
        return c["profile"] if c else "MIXED"
    except Exception as e:
        _logger.debug(f"[coins_config] get_profile({symbol!r}) fallback: "
                      f"{type(e).__name__}: {e}")
        return "MIXED"


def get_family(symbol: str) -> str:
    """
    Static family assignment from the coins/*.py registry.

    REV 23.2 — fallback uses `_DEFAULT_FAMILY` (sourced from
    config_center when available). Previously hardcoded "trend_coins"
    which would silently drift if config_center.DEFAULT_FAMILY ever
    changed.
    """
    try:
        c = get_coin(_normalize(symbol))
        return c["family"] if c else _DEFAULT_FAMILY
    except Exception as e:
        _logger.debug(f"[coins_config] get_family({symbol!r}) fallback: "
                      f"{type(e).__name__}: {e}")
        return _DEFAULT_FAMILY


def is_enabled(symbol: str) -> bool:
    try:
        c = get_coin(_normalize(symbol))
        return bool(c["enabled"]) if c else False
    except Exception as e:
        _logger.debug(f"[coins_config] is_enabled({symbol!r}) fallback: "
                      f"{type(e).__name__}: {e}")
        return False


def is_known(symbol: str) -> bool:
    try:
        return get_coin(_normalize(symbol)) is not None
    except Exception as e:
        _logger.debug(f"[coins_config] is_known({symbol!r}) fallback: "
                      f"{type(e).__name__}: {e}")
        return False


# ═══════════════════════════════════════════════════════════
#  FAMILY BASELINE ACCESSORS (REV 23.0 — from config_center)
# ═══════════════════════════════════════════════════════════
def get_caps(symbol: str) -> dict:
    """
    Return family baseline caps dict: {"sl": pct, "tp1": pct, "tp2": pct}.

    REV 23.0 — Reads from core/config_center.py.
    REV 23.1 — Emergency fallback aligned to trend_coins CAPS.
    REV 23.2 — Uses module-level `_FALLBACK_CAPS` (single source);
               logs a WARNING when the fallback fires (was silent).

    Note: caps are the MAX allowed distances. Actual SL/TP are
    computed by strategy functions (using ATR × st_params), then
    clamped to these caps.
    """
    try:
        family = get_family(symbol)
        baseline = _get_family_baseline(family)
        return dict(baseline["CAPS"])
    except Exception as e:
        _logger.warning(
            f"[coins_config] get_caps({symbol!r}) fell back to "
            f"hardcoded defaults: {type(e).__name__}: {e}"
        )
        return dict(_FALLBACK_CAPS)


def get_coin_filters(symbol: str) -> dict:
    """
    Return family baseline FILTERS dict.

    REV 23.0 — Reads from core/config_center.py.
    REV 23.2 — logs WARNING on fallback (was silent).

    This includes: late_guard_*, top_chase_*, max_flips, min_flips,
    min_adx_st, min_dist_atr, max_dist_atr, pullback_dist_atr,
    rsi_buy_min/max, rsi_sell_min/max.
    """
    try:
        family = get_family(symbol)
        baseline = _get_family_baseline(family)
        return dict(baseline["FILTERS"])
    except Exception as e:
        _logger.warning(
            f"[coins_config] get_coin_filters({symbol!r}) fell back to "
            f"empty dict: {type(e).__name__}: {e}"
        )
        return {}


def get_coin_st_params(symbol: str) -> dict:
    """
    Return family baseline ST_PARAMS dict.

    REV 23.0 — Reads from core/config_center.py.
    REV 23.1 — Emergency fallback aligned to config_center.GLOBAL
               defaults (sl_atr=1.8, tp1_atr=2.5, tp2_atr=5.0).
    REV 23.2 — Uses module-level `_FALLBACK_ST_PARAMS` (single source);
               logs WARNING when the fallback fires (was silent).

    Returns {sl_atr, tp1_atr, tp2_atr} — the ATR multipliers for
    SL and TP distances. Actual distances computed as ATR × multiplier.
    """
    try:
        family = get_family(symbol)
        baseline = _get_family_baseline(family)
        return dict(baseline["ST_PARAMS"])
    except Exception as e:
        _logger.warning(
            f"[coins_config] get_coin_st_params({symbol!r}) fell back "
            f"to hardcoded defaults: {type(e).__name__}: {e}"
        )
        return dict(_FALLBACK_ST_PARAMS)


def get_vol_class_mult(vol_class: str) -> float:
    """
    Return the SL/TP CAPS multiplier for a vol class.

    ⚠️  IMPORTANT — TWO DIFFERENT "VOL CLASS MULTIPLIERS" EXIST:

      1. THIS FUNCTION — the CAPS multiplier:
         LOW  → 0.80  (majors: tighter stops/targets)
         MED  → 1.00  (baseline)
         HIGH → 1.40  (wild alts: wider stops/targets)
         Consumed by: signals/base.py::_mk() to scale SL/TP caps.

      2. config_center.VOL_CLASS_QTY_MULT — the QTY multiplier:
         LOW  → 1.00  (no down-scale; majors are well-behaved)
         MED  → 0.70
         HIGH → 0.40  (wild alts: reduce size aggressively)
         Consumed by: orders/entry.py::_apply_vol_class_sizing().

    Confusing the two produces either oversized trades or wrong
    stop distances. If you're looking for the position-size map,
    import VOL_CLASS_QTY_MULT from config_center directly.

    Signature: this function takes a VOL CLASS LABEL ("LOW"/"MED"/
    "HIGH"), NOT a symbol. Passing a symbol like "BTCUSDT" returns
    the default (1.0) — silently, because the lookup misses.
    """
    return _get_vol_class_mult(vol_class)


# ═══════════════════════════════════════════════════════════
#  REGISTRY-LEVEL ACCESSORS
# ═══════════════════════════════════════════════════════════
def get_family_coins(family: str) -> set[str]:
    """Return {symbol, ...} of ENABLED coins belonging to `family`."""
    try:
        return {
            sym for sym, c in all_coins().items()
            if c["family"] == family and c["enabled"]
        }
    except Exception as e:
        _logger.warning(
            f"[coins_config] get_family_coins({family!r}) failed: "
            f"{type(e).__name__}: {e}"
        )
        return set()


def enabled_coins() -> tuple[str, ...]:
    """Return tuple of all ENABLED symbols, in registry order."""
    try:
        return tuple(sym for sym, c in all_coins().items() if c["enabled"])
    except Exception as e:
        _logger.warning(
            f"[coins_config] enabled_coins() failed: "
            f"{type(e).__name__}: {e}"
        )
        return tuple()


# ═══════════════════════════════════════════════════════════
#  PER-COIN EXTRAS
# ═══════════════════════════════════════════════════════════
def get_coin_vol_class(symbol: str) -> str:
    """Return volatility class label, or 'MED' if unknown."""
    try:
        c = get_coin(_normalize(symbol))
        return c["vol_class"] if c else "MED"
    except Exception as e:
        _logger.debug(f"[coins_config] get_coin_vol_class({symbol!r}) "
                      f"fallback: {type(e).__name__}: {e}")
        return "MED"


def get_coin_td_fade(symbol: str) -> dict:
    """
    Return per-coin TD_FADE params, or {} if unknown.

    NOTE: TD_FADE is strategy-specific (falling-knife BUY logic),
    so it stays per-coin. All OTHER tuning comes from config_center.
    """
    try:
        c = get_coin(_normalize(symbol))
        return dict(c["td_fade"]) if c else {}
    except Exception as e:
        _logger.debug(f"[coins_config] get_coin_td_fade({symbol!r}) "
                      f"fallback: {type(e).__name__}: {e}")
        return {}


# ═══════════════════════════════════════════════════════════
#  DIAGNOSTIC HELPERS
# ═══════════════════════════════════════════════════════════
def load_errors() -> dict[str, str]:
    """Return {coin_module_name: error_message} for files that failed
    to load during registry initialization."""
    try:
        return dict(_coin_load_errors())
    except Exception:
        return {}


def known_families() -> list[str]:
    """Return sorted list of distinct family NAMES present in the
    loaded coin registry."""
    try:
        return sorted({c["family"] for c in all_coins().values()})
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════
#  DIAGNOSTIC — REV 23.4
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 70)
    print("  COINS_CONFIG DIAGNOSTIC — REV 23.4 (unified config)")
    print("=" * 70)
    print(f"\n  config_center available: {_HAS_CONFIG_CENTER}")
    print(f"  DEFAULT_FAMILY:          {_DEFAULT_FAMILY}")

    errs = load_errors()
    if errs:
        print(f"\n⚠️  {len(errs)} load error(s):")
        for k, v in sorted(errs.items()):
            print(f"    {k}: {v}")
    else:
        print("\n✅ No coin-file load errors.")

    all_c = all_coins()
    print(f"\nLoaded {len(all_c)} coins:")
    for sym in sorted(all_c):
        c = all_c[sym]
        status = "✅" if c["enabled"] else "❌"
        print(f"  {status} {sym:<16} {c['family']:<18} "
              f"{c['profile']:<9} vol={c['vol_class']:<5}")

    print(f"\nEnabled: {len(enabled_coins())}")

    fams = known_families()
    if not fams:
        print("\n⚠️  No families found — check coins/__init__.py loader.")
    else:
        print("\nFamily breakdown (dynamic):")
        for fam in fams:
            coins = get_family_coins(fam)
            print(f"  {fam:<20} ({len(coins):>2}) → {sorted(coins)}")

    print("\n" + "=" * 70)
    print("  FAMILY BASELINE PREVIEW (from config_center)")
    print("=" * 70)
    for fam in fams:
        baseline = _get_family_baseline(fam)
        print(f"\n  {fam}:")
        print(f"    ST_PARAMS: {baseline['ST_PARAMS']}")
        print(f"    CAPS:      {baseline['CAPS']}")
        print(f"    FILTERS:   {len(baseline['FILTERS'])} keys")

    print("\n" + "=" * 70)
    print("  SAMPLE LOOKUP (should serve from config_center)")
    print("=" * 70)
    for sym in ("APTUSDT", "BTCUSDT", "HYPEUSDT"):
        if sym in all_c:
            print(f"\n  {sym}:")
            print(f"    family:     {get_family(sym)}")
            print(f"    vol_class:  {get_coin_vol_class(sym)}")
            print(f"    ST_PARAMS:  {get_coin_st_params(sym)}")
            print(f"    CAPS:       {get_caps(sym)}")
            print(f"    FILTERS:    {len(get_coin_filters(sym))} keys")

    print("\n" + "=" * 70)
    print("  NORMALIZATION SANITY CHECK")
    print("=" * 70)
    if all_c:
        sample = sorted(all_c)[0]
        bare = sample[:-4] if sample.endswith("USDT") else sample
        lower = sample.lower()
        for probe, label in ((sample, "canonical"),
                             (bare,  "bare"),
                             (lower, "lowercase")):
            fam = get_family(probe)
            prof = get_profile(probe)
            print(f"  {label:<10} {probe:<12} → family={fam:<18} "
                  f"profile={prof}")
    else:
        print("  (no coins loaded — skipped)")

    print("\n" + "=" * 70)
    print("  FALLBACK CONSTANTS")
    print("=" * 70)
    print(f"  _DEFAULT_FAMILY_FALLBACK = {_DEFAULT_FAMILY_FALLBACK!r}")
    print(f"  _FALLBACK_ST_PARAMS      = {_FALLBACK_ST_PARAMS}")
    print(f"  _FALLBACK_CAPS           = {_FALLBACK_CAPS}")