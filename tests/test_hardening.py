"""
tests/test_hardening.py — Regression tests for the 8 hardening fixes
+ review Points 4 and 5.

Run:
    python -m pytest tests/test_hardening.py -v

Requires:
    pip install pytest

All tests are OFFLINE. No Binance API calls, no network, no orders.
They call pure functions and check outputs against the hardening spec.

If a test fails after a refactor, the refactor likely broke one of the
hardening fixes — re-examine BEFORE live deploy.
"""
import pathlib
import sys
from decimal import Decimal

# Ensure repo root is on sys.path so `import core.*` / `import orders.*`
# work when pytest is invoked from any cwd.
_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ═════════════════════════════════════════════════════════════
#  C4 — Deterministic emergency-close clientOrderId
#  Protects: orders/exit.py::_emg_cid
#  Why: if this ever returns a random value on retry, a lost-ack
#       emergency close can double-submit MARKET reduceOnly.
# ═════════════════════════════════════════════════════════════
def test_emg_cid_is_deterministic():
    from orders.exit import _emg_cid

    cid1 = _emg_cid("BTCUSDT", "SELL", "0.001")
    cid2 = _emg_cid("BTCUSDT", "SELL", "0.001")
    assert cid1 == cid2, (
        "same (pair, side, qty) must produce the same cid — "
        "Binance's idempotency guarantee depends on this"
    )


def test_emg_cid_changes_with_qty():
    from orders.exit import _emg_cid

    cid_a = _emg_cid("BTCUSDT", "SELL", "0.001")
    cid_b = _emg_cid("BTCUSDT", "SELL", "0.002")
    assert cid_a != cid_b, (
        "different qty is a different order — cid must differ"
    )


def test_emg_cid_changes_with_side():
    from orders.exit import _emg_cid

    cid_sell = _emg_cid("BTCUSDT", "SELL", "0.001")
    cid_buy = _emg_cid("BTCUSDT", "BUY", "0.001")
    assert cid_sell != cid_buy, "side must be part of the cid seed"


def test_emg_cid_within_binance_36_char_limit():
    from orders.exit import _emg_cid

    # Worst realistic case — long pair, long qty string
    cid = _emg_cid("1000FLOKIUSDT", "BUY", "12345678.9012345")
    assert len(cid) <= 36, (
        f"Binance hard-caps clientOrderId at 36 chars; got {len(cid)}"
    )


# ═════════════════════════════════════════════════════════════
#  C5 / C6 — Risk sizing floor guard
#  Protects: orders/entry.py::_risk_size_with_floor_guard
#  Why: if the guard ever returns an oversized qty, the abort
#       threshold is bypassed and a single trade can blow the
#       daily risk budget.
# ═════════════════════════════════════════════════════════════
def test_risk_size_aborts_oversize_floor():
    from orders.entry import _risk_size_with_floor_guard

    # Tiny wallet ($100), wide SL ($10), min_qty forces 1 unit.
    # Forced risk = 1 * $10 = $10 = 10% of wallet.
    # Budget = 0.5% * 1.20 tolerance = 0.6%.
    # 10% >> 0.6% → MUST return None.
    result = _risk_size_with_floor_guard(
        wallet_balance=100.0,
        risk_percent=0.5,
        ref_price=100.0,
        sl_price=90.0,
        step=Decimal("0.001"),
        min_qty=Decimal("1"),
        min_notional=10.0,
        max_qty=None,
        max_oversize_mult=1.20,
    )
    assert result is None, (
        "floor-forced oversize (10% vs 0.6% budget) must abort — "
        "this is the C5 guard's entire purpose"
    )


def test_risk_size_accepts_reasonable_floor():
    from orders.entry import _risk_size_with_floor_guard

    # $10k wallet, tight $1 SL, $50 risk budget → ideal qty = 50.
    # Floor is small (min_qty=0.01, minNotional=5/100=0.05).
    # Forced risk stays inside budget → MUST return a Decimal.
    result = _risk_size_with_floor_guard(
        wallet_balance=10000.0,
        risk_percent=0.5,
        ref_price=100.0,
        sl_price=99.0,
        step=Decimal("0.001"),
        min_qty=Decimal("0.01"),
        min_notional=5.0,
        max_qty=None,
        max_oversize_mult=1.20,
    )
    assert result is not None
    assert isinstance(result, Decimal)
    assert result >= Decimal("0.01"), (
        "returned qty must respect exchange minQty"
    )


def test_risk_size_returns_none_for_zero_wallet():
    from orders.entry import _risk_size_with_floor_guard

    result = _risk_size_with_floor_guard(
        wallet_balance=0.0,
        risk_percent=0.5,
        ref_price=100.0,
        sl_price=99.0,
        step=Decimal("0.001"),
        min_qty=Decimal("0.01"),
        min_notional=5.0,
        max_qty=None,
        max_oversize_mult=1.20,
    )
    assert result is None, "zero wallet cannot produce a valid qty"


def test_risk_size_returns_none_for_zero_sl_distance():
    from orders.entry import _risk_size_with_floor_guard

    # SL == entry → zero distance → division by zero risk → must abort
    result = _risk_size_with_floor_guard(
        wallet_balance=1000.0,
        risk_percent=0.5,
        ref_price=100.0,
        sl_price=100.0,
        step=Decimal("0.001"),
        min_qty=Decimal("0.01"),
        min_notional=5.0,
        max_qty=None,
        max_oversize_mult=1.20,
    )
    assert result is None, "zero SL distance must abort (no div-by-zero)"


# ═════════════════════════════════════════════════════════════
#  state.py — DailyTracker fail-CLOSED on bad equity
#  Protects: core/state.py::DailyTracker.is_limit_reached
#  Why: if bad equity silently returned (False, "") the DD gate
#       would be skipped and trading continues on a blind book.
# ═════════════════════════════════════════════════════════════
def test_daily_tracker_fail_closed_on_zero_equity():
    from core.state import daily_tracker

    is_limited, reason = daily_tracker.is_limit_reached(0.0)
    assert is_limited is True, "0 equity must halt trading (fail-closed)"
    assert "EQUITY" in reason.upper()


def test_daily_tracker_fail_closed_on_negative_equity():
    from core.state import daily_tracker

    is_limited, _ = daily_tracker.is_limit_reached(-500.0)
    assert is_limited is True


def test_daily_tracker_fail_closed_on_nan():
    from core.state import daily_tracker

    is_limited, _ = daily_tracker.is_limit_reached(float("nan"))
    assert is_limited is True


# ═════════════════════════════════════════════════════════════
#  Point 4 — Adopted fallback SL is config-driven
#  Protects: core/config_center.py + future.py + orders/repair.py
#  Why: if the key goes missing, future.py falls back to 0.02 silently
#       (safe), but a rename would leave the code without a bound check.
# ═════════════════════════════════════════════════════════════
def test_adopted_sl_config_key_resolves():
    from core.config_center import get

    v = get("adopted_fallback_sl_pct", None)
    assert v is not None, (
        "key 'adopted_fallback_sl_pct' must exist in GLOBAL"
    )


def test_adopted_sl_is_within_documented_bounds():
    from core.config_center import get

    v = float(get("adopted_fallback_sl_pct", 0.02))
    assert 0.001 <= v <= 0.10, (
        f"adopted_fallback_sl_pct={v} outside documented bounds [0.001, 0.10]"
    )


def test_adopted_sl_modules_resolve_consistently():
    """future.py and orders/repair.py must resolve the SAME value."""
    import future
    import orders.repair as repair

    assert future._ADOPTED_FALLBACK_SL_PCT == repair._ADOPTED_FALLBACK_SL_PCT, (
        "future.py and orders/repair.py must see the same adopted SL %"
    )


# ═════════════════════════════════════════════════════════════
#  Utility sanity — decimal flooring
#  Protects: core/client.py::adjust_qty / adjust_price
#  Why: rounding UP instead of flooring produces -1013/-1111 rejects
#       at Binance. These are the primitives every order relies on.
# ═════════════════════════════════════════════════════════════
def test_adjust_qty_floors_to_step():
    from core.client import adjust_qty

    # 0.0012345 floored to 0.001 step → "0.001"
    result = adjust_qty(Decimal("0.0012345"), Decimal("0.001"),
                        Decimal("0.001"))
    assert result == "0.001"


def test_adjust_price_floors_to_tick():
    from core.client import adjust_price

    # 100.99999 floored to 0.01 tick → "100.99"
    result = adjust_price(Decimal("100.99999"), Decimal("0.01"))
    assert result == "100.99"


def test_adjust_qty_respects_min_qty():
    from core.client import adjust_qty

    # Sub-minimum input → bumped to min (this is the CORRECT use of min_qty
    # at the filter level, distinct from the C6 outer-except bump that
    # bypasses risk budget).
    result = adjust_qty(Decimal("0.0001"), Decimal("0.001"),
                        Decimal("0.005"))
    assert Decimal(result) >= Decimal("0.005")


# ═════════════════════════════════════════════════════════════
#  C2 — Fast-reconcile constant
#  Protects: orders/repair.py
#  Why: if the unverified window ever returns to 90s, unverified
#       (naked) trades stay unprotected for 90 seconds again.
# ═════════════════════════════════════════════════════════════
def test_fast_reconcile_window_is_short():
    import orders.repair as repair

    assert repair._UNVERIFIED_FAST_RECONCILE_SEC <= 15.0, (
        f"fast-reconcile window must be short; "
        f"got {repair._UNVERIFIED_FAST_RECONCILE_SEC}s"
    )
    assert repair._UNVERIFIED_FAST_RECONCILE_SEC > 0.0