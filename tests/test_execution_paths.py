"""
tests/test_execution_paths.py — behaviour tests for the money paths.

Unlike test_hardening.py (pure functions), these drive the REAL
orders.entry._place_market_idempotent and orders.manage._orphan_watch_tick
against a FAKE exchange client. No network, no orders, no sleeping
(time is faked).

Run:  python -m pytest tests/test_execution_paths.py -v
"""
import pathlib
import sys
from unittest import mock

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from binance.exceptions import BinanceAPIException  # noqa: E402


class _APIErr(BinanceAPIException):
    """BinanceAPIException with a settable .code (real class parses a
    HTTP response in __init__; we only need .code / message)."""
    def __init__(self, code, msg="x"):
        Exception.__init__(self, f"APIError(code={code}): {msg}")
        self.code = code
        self.message = msg
        self.status_code = 400


class _FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += float(s)


class FakeExchange:
    """Minimal futures client. `script` is a list of behaviours, one
    per futures_create_order call: 'fill' | 'timeout_late_fill' |
    'timeout_lost' | ('api', code) ."""

    def __init__(self, script, order_status_after_exc=None):
        self.script = list(script)
        self.creates = []          # kwargs of every create call
        self.orders = {}           # cid -> order dict (what the exchange has)
        self.order_status_after_exc = order_status_after_exc

    def futures_create_order(self, **kw):
        self.creates.append(kw)
        step = self.script.pop(0)
        cid = kw["newClientOrderId"]
        qty = kw["quantity"]
        filled = {"status": "FILLED", "executedQty": str(qty),
                  "avgPrice": "10.5", "orderId": len(self.creates)}
        if step == "fill":
            self.orders[cid] = filled
            return dict(filled)
        if step == "timeout_late_fill":
            self.orders[cid] = filled          # exchange DID fill it
            raise TimeoutError("read timeout")
        if step == "timeout_lost":
            raise TimeoutError("read timeout")  # exchange never saw it
        if isinstance(step, tuple) and step[0] == "api":
            raise _APIErr(step[1])
        raise AssertionError(step)

    def futures_get_order(self, symbol, origClientOrderId):
        if origClientOrderId in self.orders:
            return self.orders[origClientOrderId]
        if self.order_status_after_exc:
            return {"status": self.order_status_after_exc,
                    "executedQty": "0", "avgPrice": "0"}
        raise _APIErr(-2013, "Order does not exist")


def _run(ex, **kw):
    import orders.entry as entry
    clock = _FakeClock()
    with mock.patch.object(entry, "time", clock), \
            mock.patch.object(entry, "refresh_timestamp", lambda: None), \
            mock.patch.object(entry, "handle_order_filter_error",
                              lambda pair, e: False):
        return entry._place_market_idempotent(
            ex, "ABCUSDT", "BUY", "5", "tb_entry_ABCUSDT_first", **kw)


def test_normal_fill_single_order():
    ex = FakeExchange(["fill"])
    status, qty, avg, cid = _run(ex)
    assert status == "FILLED" and qty == 5.0 and avg == 10.5
    assert len(ex.creates) == 1


def test_timeout_but_late_fill_is_found_and_never_resent():
    ex = FakeExchange(["timeout_late_fill", "fill"])
    status, qty, avg, cid = _run(ex)
    assert status == "FILLED" and qty == 5.0
    assert cid == "tb_entry_ABCUSDT_first"
    assert len(ex.creates) == 1, "a lost ack must NOT trigger a 2nd MARKET order"


def test_timeout_and_nothing_found_returns_unknown_without_resend():
    ex = FakeExchange(["timeout_lost", "fill"])
    status, qty, avg, cid = _run(ex)
    assert status == "UNKNOWN" and qty == 0.0
    assert len(ex.creates) == 1, "UNKNOWN must never resend (double-entry guard)"


def test_terminal_reject_after_exception_retries_with_fresh_cid():
    ex = FakeExchange(["timeout_lost", "fill"], order_status_after_exc="EXPIRED")
    status, qty, avg, cid = _run(ex)
    assert status == "FILLED"
    assert len(ex.creates) == 2
    assert ex.creates[0]["newClientOrderId"] != ex.creates[1]["newClientOrderId"]


def test_definitive_api_reject_is_rejected_single_call():
    ex = FakeExchange([("api", -4005)])
    status, qty, avg, cid = _run(ex)
    assert status == "REJECTED" and qty == 0.0
    assert len(ex.creates) == 1


def test_rate_limit_retries_with_same_cid():
    ex = FakeExchange([("api", -1003), "fill"])
    status, qty, avg, cid = _run(ex)
    assert status == "FILLED"
    assert len(ex.creates) == 2
    assert ex.creates[0]["newClientOrderId"] == ex.creates[1]["newClientOrderId"]


# ── orphan watch ─────────────────────────────────────────────
class _PosClient:
    def __init__(self, positions):
        self.positions = positions

    def futures_position_information(self, **kw):
        return self.positions


def _pos(sym, amt):
    return {"symbol": sym + "USDT", "positionAmt": str(amt)}


def _with_tracked(tracked):
    import orders.manage as manage
    manage._ORPHAN_SEEN.clear()
    manage.bot_tracked_symbols.clear()
    manage.bot_tracked_symbols.update(tracked)
    return manage


def test_orphan_confirmed_on_second_check_and_alerts_once():
    manage = _with_tracked({"BTC"})
    alerts = []
    cl = _PosClient([_pos("BTC", 1), _pos("XYZ", -50), _pos("ETH", 0)])
    with mock.patch.object(manage, "send_telegram", alerts.append):
        assert manage._orphan_watch_tick(cl) == []           # first sighting
        assert manage._orphan_watch_tick(cl) == ["XYZ"]      # confirmed
        assert manage._orphan_watch_tick(cl) == []           # no repeat alert
    assert len(alerts) == 1 and "XYZ" in alerts[0]


def test_tracked_or_flat_positions_never_flagged():
    manage = _with_tracked({"BTC"})
    cl = _PosClient([_pos("BTC", 1), _pos("ETH", 0)])
    for _ in range(4):
        assert manage._orphan_watch_tick(cl) == []


def test_orphan_counter_resets_when_position_closes():
    manage = _with_tracked(set())
    with mock.patch.object(manage, "send_telegram", lambda m: None):
        manage._orphan_watch_tick(_PosClient([_pos("XYZ", 5)]))
        manage._orphan_watch_tick(_PosClient([]))             # closed
        assert manage._orphan_watch_tick(_PosClient([_pos("XYZ", 5)])) == []


def test_orphan_watch_disabled_flag():
    manage = _with_tracked(set())
    with mock.patch.object(manage, "_cc_get_num",
                           lambda k, d: False if k == "orphan_watch_enabled" else d):
        for _ in range(3):
            assert manage._orphan_watch_tick(_PosClient([_pos("XYZ", 5)])) == []


def test_auto_adopt_only_when_flag_on():
    manage = _with_tracked(set())
    calls = []
    import orders.repair as repair
    cl = _PosClient([_pos("XYZ", 5)])
    with mock.patch.object(manage, "send_telegram", lambda m: None), \
            mock.patch.object(repair, "sync_existing_positions",
                              lambda: calls.append(1)):
        manage._orphan_watch_tick(cl)
        manage._orphan_watch_tick(cl)
        assert calls == [], "auto-adopt must be OFF by default"
        manage._ORPHAN_SEEN.clear()
        with mock.patch.object(manage, "_cc_get_num",
                               lambda k, d: True if k == "orphan_watch_auto_adopt" else d):
            manage._orphan_watch_tick(cl)
            manage._orphan_watch_tick(cl)
        assert calls == [1]


# ── config ───────────────────────────────────────────────────
def test_new_config_keys_registered_and_bounded():
    import core.config_center as cc
    g = cc.GLOBAL
    assert g["max_fill_slippage_pct"] == 0.50
    assert g["orphan_watch_enabled"] is True
    assert g["orphan_watch_auto_adopt"] is False
    assert g["risk_oversize_abort_mult"] >= g["risk_oversize_warn_mult"]
