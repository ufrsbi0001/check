"""
TRADING DESK · Flask Backend — Production Refactor
─────────────────────────────────────────────────────────────
REV 1.5.7 (2026-10-05) — READ/WRITE LOCK MIGRATION (Phase 2, Step 7):
  ✅ Migrated from the legacy `_requests_lock` (which aliases
     _write_lock) to the explicit read/write split introduced in
     core/client.py REV 11.5.

     Classification applied:
       • _fetch_positions_cached()       → _read_lock
         (futures_position_information is a read)
       • api_close_position() first close → _write_lock
       • api_close_position() retry close → _write_lock

     The dashboard's position-fetch probe no longer serialises against
     order placement from the trade manager. Its occasional MARKET
     closes (manual user-initiated) correctly contend on _write_lock
     with the rest of the order path.

     Zero behaviour change. Same lock semantics at each call site.

REV 1.5.6 (2026-10-05) — BINANCE UI PARITY VIA BREAK-EVEN PRICE (retained).
REV 1.5.5 (2026-10-05) — LEVERAGE FALLBACK TO CONFIG (retained).
REV 1.5.4 (2026-10-05) — ROI % IN POSITION PAYLOAD (retained).
REV 1.5.3 (2026-10-04) — TRADE-MANAGER LIFECYCLE FIX (retained).
REV 1.5.2 (2026-10-03) — RATE-LIMIT RELAXATION FOR READS (retained).
REV 1.5.1 (2026-10-03) — ERROR VISIBILITY + CACHING (retained).
REV 1.5.0 (2026-10-03) — DEFENSIVE HARDENING (retained).
REV 1.4.2 (2026-10-02) — DAILY LOSS DASHBOARD WIRING (retained).
REV 1.4.1 (2026-10-02) — TIME_EXIT TOGGLE API (retained).
REV 1.4.0 (2026-10-02) — PHASE 4 WEB MIGRATION (retained).
REV 1.3.16 (2026-09-28) — DEAD-PROXY FALLOUT CLEANUP (retained).
REV 1.3.15 (2026-09-28) — MANUAL CLOSE FROM UI (retained).
REV 1.3.14 (2026-09-28) — DECISION ENGINE STATS EXPOSED (retained).
"""

from __future__ import annotations

import hmac
import json
import logging
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Optional

from flask import Flask, Response, jsonify, render_template, request
from flask_cors import CORS

from core.config import CONFIG, print_config_banner

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────
PKT = timezone(timedelta(hours=5))
LOG_PREFIX_IDLE = "⏳ System idle. Waiting for API keys."

_MAX_HOLD_MINUTES = 1440
_POS_CACHE_TTL = 3.0


# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
def _build_logger() -> logging.Logger:
    logger = logging.getLogger("trading_desk")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    try:
        from pathlib import Path as _P
        log_dir = _P(CONFIG.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(log_dir / "trading_desk.log"), encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger

log = _build_logger()

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

logging.getLogger("werkzeug").setLevel(logging.ERROR)


# ─────────────────────────────────────────────────────────────
# TRADING ENGINE
# ─────────────────────────────────────────────────────────────
try:
    import future as tt
    TT_AVAILABLE = True
except ImportError:
    tt = None
    TT_AVAILABLE = False
    log.warning("Trading engine module 'future' not found — running in UI-only mode")


# ─────────────────────────────────────────────────────────────
# ENGINE ADAPTERS
# ─────────────────────────────────────────────────────────────
def _engine_attr(name: str, *fallback_modules: str) -> Optional[Any]:
    if TT_AVAILABLE and tt is not None:
        attr = getattr(tt, name, None)
        if attr is not None:
            return attr
    for mod_name in fallback_modules:
        try:
            mod = __import__(mod_name, fromlist=[name])
            attr = getattr(mod, name, None)
            if attr is not None:
                return attr
        except ImportError:
            continue
    return None


def get_client() -> Optional[Any]:
    """Resolve the active Binance client."""
    getter = _engine_attr("get_client", "core.client")
    if callable(getter):
        try:
            return getter()
        except Exception:
            pass
    return None


def _refresh_timestamp() -> None:
    fn = _engine_attr("refresh_timestamp", "core.client")
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _safe_err(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


# ─────────────────────────────────────────────────────────────
# APP FACTORY + SECURITY HEADERS
# ─────────────────────────────────────────────────────────────
def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = CONFIG.flask_secret_key
    app.config["JSON_AS_ASCII"] = False

    CORS(app, resources={
        r"/api/*": {
            "origins": list(CONFIG.allowed_origins),
            "methods": ["GET", "POST"],
            "allow_headers": ["Authorization", "Content-Type", "X-Requested-With"],
        }
    })

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.tailwindcss.com 'unsafe-inline'; "
            "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
            "font-src https://fonts.gstatic.com; "
            "connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; frame-ancestors 'none'"
        )
        is_https = request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        if is_https:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if CONFIG.allowed_origins != ("*",):
            origin = request.headers.get("Origin", "")
            if origin in CONFIG.allowed_origins:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
        return response

    return app


app = create_app()

# ─────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────
state_lock = threading.Lock()
log_lock = threading.Lock()

bot_state: dict[str, Any] = {
    "running": False,
    "keys_set": False,
    "balance": 0.0,
    "daily_loss": 0,
    "active_trades": [],
    "last_update": "--",
    "position_count": 0,
    "uptime": "00:00:00",
}

logs_deque: deque[str] = deque(maxlen=200)
logs_deque.append(f"[{datetime.now().strftime('%H:%M:%S')}] {LOG_PREFIX_IDLE}")

bot_thread: Optional[threading.Thread] = None
tm_thread: Optional[threading.Thread] = None
start_time: Optional[float] = None


def add_log(msg: str) -> None:
    entry = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    with log_lock:
        logs_deque.append(entry)
    log.info(msg)


def _read_daily_loss_count(fallback: int = 0) -> int:
    try:
        from core.state import daily_tracker
        return int(daily_tracker.get_loss_count())
    except Exception as e:
        log.debug(f"[daily_loss] read failed (using fallback={fallback}): {e}")
        return fallback


# ─────────────────────────────────────────────────────────────
# POSITION CACHE
# ─────────────────────────────────────────────────────────────
_pos_cache: dict[str, Any] = {"data": None, "time": 0.0}
_pos_cache_lock = threading.Lock()


def _fetch_positions_cached(client: Any) -> list:
    now = time.time()
    with _pos_cache_lock:
        if _pos_cache["data"] is not None and \
                now - _pos_cache["time"] < _POS_CACHE_TTL:
            return _pos_cache["data"]

    # ── REV 1.5.7 — read lock (position fetch is a read). ──
    try:
        from core.client import _read_lock as _rl
    except Exception:
        _rl = None

    if _rl is not None:
        with _rl:
            data = client.futures_position_information()
    else:
        data = client.futures_position_information()

    with _pos_cache_lock:
        _pos_cache["data"] = data
        _pos_cache["time"] = time.time()
    return data


# ─────────────────────────────────────────────────────────────
# AUTH + RATE LIMITING
# ─────────────────────────────────────────────────────────────
def token_required(f: Callable) -> Callable:
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        if not CONFIG.token_present:
            return jsonify({"success": False,
                            "error": "Server not configured (WEB_TOKEN missing)"}), 500

        auth = request.headers.get("Authorization", "")
        token = auth.strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()

        if not token or not hmac.compare_digest(token, CONFIG.web_token):
            return jsonify({"success": False, "error": "Unauthorized"}), 401

        return f(*args, **kwargs)
    return decorated


_last_req: dict[str, float] = {}
_last_req_lock = threading.Lock()


def rate_limit(seconds: int = 2) -> Callable:
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            fwd = request.headers.get("X-Forwarded-For", "")
            ip = (fwd.split(",")[0].strip() if fwd else "") or request.remote_addr or "unknown"
            now = time.time()
            with _last_req_lock:
                for k in list(_last_req):
                    if now - _last_req[k] > 60:
                        del _last_req[k]
                if ip in _last_req and now - _last_req[ip] < seconds:
                    return jsonify({"success": False,
                                    "error": "Too many requests"}), 429
                _last_req[ip] = now
            return f(*args, **kwargs)
        return wrapper
    return decorator


def api_error(message: str, status: int = 500) -> tuple[Response, int]:
    return jsonify({"success": False, "error": message}), status


# ─────────────────────────────────────────────────────────────
# BOT RUNNER
# ─────────────────────────────────────────────────────────────
def bot_runner() -> None:
    global start_time, tm_thread
    start_time = time.time()

    if not TT_AVAILABLE:
        add_log("⚠️ Bot engine unavailable (future.py not loaded) — UI-only mode")
        return

    with state_lock:
        bot_state["running"] = True
    add_log("🚀 Bot engine started successfully")

    tm_loop = _engine_attr("trade_manager_loop", "orders.manage", "orders")
    if callable(tm_loop):
        tm_thread = threading.Thread(
            target=tm_loop, daemon=True, name="tm_engine"
        )
        tm_thread.start()
        add_log("🔄 Trade manager thread started")

    main_loop = _engine_attr("main_loop")
    try:
        if callable(main_loop):
            main_loop()
        else:
            add_log("❌ future.main_loop not found")
    except Exception as exc:
        add_log(f"❌ Bot error: {_safe_err(exc)}")
        log.exception("Engine main_loop crashed")
    finally:
        with state_lock:
            bot_state["running"] = False
        add_log("⏹️ Bot stopped.")

        try:
            _stop_fn = _engine_attr("request_stop", "core.state")
            if callable(_stop_fn):
                _stop_fn()
                log.debug("[bot_runner] request_stop() signalled")
        except Exception as _se:
            log.warning(f"[bot_runner] request_stop failed: {_safe_err(_se)}")

        if tm_thread is not None and tm_thread.is_alive():
            tm_thread.join(timeout=15)
            if tm_thread.is_alive():
                add_log("⚠️ Trade manager still winding down after 15s "
                        "(restart blocked until it exits)")
            else:
                add_log("✅ Trade manager exited cleanly")


# ─────────────────────────────────────────────────────────────
# STATUS FETCHER
# ─────────────────────────────────────────────────────────────
def _fetch_balance(client: Any) -> float:
    acc = client.futures_account()
    return float(acc["totalWalletBalance"]) + float(acc.get("totalUnrealizedProfit", 0))


def _resolve_sl_price(client: Any, symbol_pair: str) -> float:
    """Best-effort stop-loss price from open protective stop orders."""
    try:
        orders = client.futures_get_open_orders(symbol=symbol_pair)
    except Exception as e:
        log.debug(f"_resolve_sl_price {symbol_pair}: {type(e).__name__}: {e}")
        return 0.0
    for o in orders:
        if o.get("type") not in ("STOP_MARKET", "STOP"):
            continue
        if not (o.get("reduceOnly") or o.get("closePosition")):
            continue
        try:
            v = float(o.get("stopPrice") or o.get("triggerPrice") or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            continue
    return 0.0


def _normalize_position(client: Any, pos: dict) -> Optional[dict]:
    """
    Convert raw Binance position dict into the UI's trade shape.

    REV 1.5.4 — adds `roi_pct` and `leverage` in the FALLBACK path.
    REV 1.5.5 — leverage falls back to config_center.leverage.
    REV 1.5.6 — PnL computed from breakEvenPrice (Binance UI parity).
    """
    try:
        amt = float(pos.get("positionAmt", 0))
        if abs(amt) <= 1e-6:
            return None

        symbol_pair = pos.get("symbol", "")
        symbol_short = symbol_pair.replace("USDT", "")

        st: Optional[dict] = None
        get_status = _engine_attr("get_trade_status", "orders", "orders.utils")
        if callable(get_status):
            try:
                st = get_status(symbol_short, pos=pos)
            except TypeError:
                try:
                    st = get_status(symbol_short)
                except Exception:
                    st = None
            except Exception:
                st = None

        if not st:
            entry = float(pos.get("entryPrice", 0))
            mark = float(pos.get("markPrice", 0))

            # ── REV 1.5.6 — Binance UI parity via break-even price ──
            # Mirrors orders/utils.py REV 23.3. breakEvenPrice includes
            # entry commission; using entryPrice inflated our PnL by
            # ~$0.60 on a 5× LTC position vs Binance's display.
            try:
                be_price = float(pos.get("breakEvenPrice", 0) or 0)
            except (TypeError, ValueError):
                be_price = 0.0
            if be_price <= 0:
                be_price = entry

            if amt > 0:
                pnl = (mark - be_price) * abs(amt)
                pnl_pct = ((mark - be_price) / entry * 100) if entry else 0.0
            else:
                pnl = (be_price - mark) * abs(amt)
                pnl_pct = ((be_price - mark) / entry * 100) if entry else 0.0

            # ── REV 1.5.5 — leveraged ROI on margin (Binance UI parity) ──
            leverage = None
            try:
                _lev_raw = pos.get("leverage", None)
                if _lev_raw not in (None, ""):
                    _lv = int(float(_lev_raw))
                    if _lv >= 1:
                        leverage = _lv
            except (TypeError, ValueError):
                leverage = None

            if leverage is None:
                try:
                    from core import config_center as _CC
                    leverage = int(_CC.get("leverage", 1) or 1)
                    if leverage < 1:
                        leverage = 1
                except Exception:
                    leverage = 1

            roi_pct = pnl_pct * leverage

            st = {
                "symbol": symbol_short,
                "side": "LONG" if amt > 0 else "SHORT",
                "entry": entry,
                "mark": mark,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "roi_pct": roi_pct,
                "leverage": leverage,
                "break_even": be_price,   # REV 1.5.6 — exposed for UI/debug
                "qty": abs(amt),
                "sl": _resolve_sl_price(client, symbol_pair),
                "is_manual": True,
            }
        return st
    except Exception as exc:
        add_log(f"⚠️ Position parse error {pos.get('symbol')}: {_safe_err(exc)}")
        return None


def get_bot_status() -> dict[str, Any]:
    """
    No network call inside state_lock — prevents UI freeze / DoS.
    """
    with state_lock:
        is_running = bot_state["running"]
        prev_daily_loss = int(bot_state.get("daily_loss", 0) or 0)

    if is_running and start_time:
        elapsed = int(time.time() - start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        uptime_str = f"{h:02d}:{m:02d}:{s:02d}"
    else:
        uptime_str = "00:00:00"

    daily_loss_now = _read_daily_loss_count(fallback=prev_daily_loss)

    if not is_running:
        with state_lock:
            bot_state.update(
                balance=0.0,
                position_count=0,
                active_trades=[],
                daily_loss=daily_loss_now,
                last_update=datetime.now().strftime("%H:%M:%S"),
                uptime=uptime_str,
            )
            return dict(bot_state)

    balance = 0.0
    trades: list[dict[str, Any]] = []
    client = get_client()
    if client is not None:
        try:
            bal_fn = _engine_attr("get_balance_or_last_good", "core.client")
            if callable(bal_fn):
                balance, degraded = bal_fn()
                if degraded:
                    add_log("⚠️ Balance from last-good (fresh fetch failed)")
            else:
                cached_fn = _engine_attr("get_account_cached", "core.client")
                if callable(cached_fn):
                    acc = cached_fn()
                    balance = float(acc["totalWalletBalance"]) + float(
                        acc.get("totalUnrealizedProfit", 0)
                    )
                else:
                    balance = _fetch_balance(client)
        except Exception as exc:
            add_log(f"⚠️ Balance fetch error: {_safe_err(exc)}")

        try:
            for pos in _fetch_positions_cached(client):
                st = _normalize_position(client, pos)
                if st:
                    trades.append(st)
        except Exception as exc:
            add_log(f"⚠️ Active trades fetch error: {_safe_err(exc)}")

    with state_lock:
        bot_state.update(
            balance=balance,
            position_count=len(trades),
            active_trades=trades,
            daily_loss=daily_loss_now,
            last_update=datetime.now().strftime("%H:%M:%S"),
            uptime=uptime_str,
        )
        return dict(bot_state)


# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────
@app.route("/")
def dashboard() -> str:
    return render_template("index.html", web_token=CONFIG.web_token)


@app.route("/api/status")
def api_status() -> Response:
    return jsonify(get_bot_status())


@app.route("/api/config")
@token_required
def api_config() -> Response:
    return jsonify(CONFIG.public_dict())


@app.route("/api/scan")
def api_scan() -> Response:
    if not TT_AVAILABLE:
        return jsonify({"results": [], "timestamp": "--"})

    with state_lock:
        if not bot_state["running"]:
            return jsonify({"results": [], "timestamp": "Bot Stopped"})

    try:
        getter = _engine_attr("get_scan_results", "core.state")
        if callable(getter):
            results, ts = getter()
        else:
            results, ts = [], None
            try:
                from core.state import scan_results as _sr, SCAN_RESULTS_LOCK as _sl, scan_timestamp as _st
                with _sl:
                    results = list(_sr)
                    ts = _st
            except ImportError:
                pass
        return jsonify({"results": results or [], "timestamp": ts})
    except Exception as exc:
        add_log(f"⚠️ Scan API error: {_safe_err(exc)}")
        return jsonify({"results": [], "timestamp": "Error"})


@app.route("/api/start", methods=["POST"])
@token_required
@rate_limit(3)
def api_start() -> Any:
    global bot_thread, tm_thread

    with state_lock:
        if bot_state["running"]:
            return api_error("Already running", 400)
        if bot_thread is not None and bot_thread.is_alive():
            return api_error("Previous instance still shutting down", 400)

    if tm_thread is not None and tm_thread.is_alive():
        tm_thread.join(timeout=5)
        if tm_thread.is_alive():
            return api_error(
                "Trade manager still shutting down — try again in a few seconds",
                429,
            )

    if not TT_AVAILABLE:
        return api_error(
            "Trading engine (future.py) not available — UI-only mode",
            503,
        )

    if not CONFIG.keys_present:
        return api_error("API keys not found in .env", 400)

    try:
        set_keys = _engine_attr("set_client_keys", "core.client")
        if not callable(set_keys):
            return api_error(
                "Trading engine missing set_client_keys "
                "(check that client.py and future.py are up to date)",
                500,
            )
        set_keys(CONFIG.api_key, CONFIG.api_secret)

        clear = _engine_attr("clear_stop", "core.state")
        if callable(clear):
            clear()

        validate = _engine_attr("_validate_api_credentials", "core.client")
        if callable(validate):
            client_obj = get_client()
            if client_obj is None:
                return api_error("Client not initialized after set_client_keys", 500)
            ok, _bal = validate(client_obj)
            if not ok:
                return api_error("API credentials rejected by exchange "
                                 "(check .env / demo vs mainnet key)", 400)

        with state_lock:
            bot_state["keys_set"] = True

        add_log("✅ API keys validated. Initializing engine...")
        bot_thread = threading.Thread(
            target=bot_runner, daemon=True, name="bot_runner"
        )
        bot_thread.start()

        try:
            from web import analytics
            analytics.schedule_refresh()
        except Exception as e:
            log.debug(f"[analytics] schedule on start failed: {e}")

        return jsonify({"success": True, "message": "Bot started!"})
    except Exception as exc:
        add_log(f"❌ Start error: {_safe_err(exc)}")
        log.exception("Failed to start bot")
        return api_error(_safe_err(exc))


@app.route("/api/stop", methods=["POST"])
@token_required
@rate_limit(2)
def api_stop() -> Any:
    with state_lock:
        if not bot_state["running"]:
            return api_error("Not running", 400)
        bot_state["running"] = False

    stop_fn = _engine_attr("stop_bot")
    if callable(stop_fn):
        try:
            stop_fn()
        except Exception as exc:
            log.warning("Engine stop_bot() raised: %s", _safe_err(exc))
    else:
        request_stop = _engine_attr("request_stop", "core.state")
        if callable(request_stop):
            try:
                request_stop()
            except Exception as exc:
                log.warning("request_stop() raised: %s", _safe_err(exc))

    add_log("⏹️ Stopping bot...")

    try:
        from web import analytics
        analytics.schedule_refresh()
    except Exception:
        pass

    return jsonify({"success": True, "message": "Stopping..."})


@app.route("/api/logs")
def api_logs() -> Response:
    with log_lock:
        payload = {"logs": list(logs_deque)[-100:]}
    return Response(
        json.dumps(payload, ensure_ascii=False),
        mimetype="application/json; charset=utf-8",
    )


# ─────────────────────────────────────────────────────────────
# MANUAL POSITION CLOSE
# ─────────────────────────────────────────────────────────────
@app.route("/api/close_position/<symbol>", methods=["POST"])
@token_required
@rate_limit(2)
def api_close_position(symbol: str) -> Any:
    """Manually close a single position at market."""
    client = get_client()
    if client is None:
        return api_error("Client not ready — bot may not be running", 503)

    raw = (symbol or "").strip().upper()
    if not raw:
        return api_error("Missing symbol", 400)
    if raw.endswith("USDT"):
        pair = raw
        sym_short = raw[:-4]
    else:
        sym_short = raw
        pair = raw + "USDT"

    if not sym_short or len(sym_short) > 20:
        return api_error(f"Invalid symbol: {symbol!r}", 400)

    try:
        _refresh_timestamp()
        pos_arr = client.futures_position_information(symbol=pair)
    except Exception as e:
        log.exception(f"[close_position] position fetch failed for {pair}")
        return api_error(f"Position fetch failed: {type(e).__name__}: {_safe_err(e)}", 500)

    if not pos_arr:
        return api_error(f"No position data returned for {sym_short}", 404)

    pos = pos_arr[0]
    try:
        amt = float(pos.get("positionAmt", 0) or 0)
    except (TypeError, ValueError):
        return api_error(f"Could not parse positionAmt for {sym_short}", 500)

    if abs(amt) <= 1e-9:
        return api_error(f"{sym_short} has no open position", 400)

    # ── REV 1.5.7 — import write lock for the MARKET close path. ──
    try:
        from core.client import get_filters, adjust_qty, _write_lock
        f = get_filters(pair)
        qty_str = adjust_qty(abs(amt), f["stepSize"], f["minQty"])
    except Exception as e:
        log.exception(f"[close_position] filter lookup failed for {pair}")
        return api_error(f"Filter/qty calc failed: {type(e).__name__}: {_safe_err(e)}", 500)

    close_side = "SELL" if amt > 0 else "BUY"

    order_id = None
    try:
        # ── REV 1.5.7 — write lock. ──
        with _write_lock:
            resp = client.futures_create_order(
                symbol=pair,
                side=close_side,
                type="MARKET",
                quantity=qty_str,
                reduceOnly=True,
            )
        if isinstance(resp, dict):
            order_id = resp.get("orderId") or resp.get("clientOrderId")
        add_log(
            f"🎯 MANUAL CLOSE {sym_short}: {close_side} {qty_str} "
            f"@ MARKET (orderId={order_id})"
        )
    except Exception as e:
        log.exception(f"[close_position] market order failed for {pair}")
        return api_error(f"Market close failed: {type(e).__name__}: {_safe_err(e)}", 500)

    settled = False
    try:
        time.sleep(1.5)
        chk = client.futures_position_information(symbol=pair)
        still = float(chk[0].get("positionAmt", 0) or 0) if chk else 0.0
        if abs(still) <= 1e-9:
            settled = True
        else:
            add_log(f"⚠️ {sym_short} still {still} after first attempt — retrying")
            # ── REV 1.5.7 — write lock. ──
            with _write_lock:
                client.futures_create_order(
                    symbol=pair,
                    side=close_side,
                    type="MARKET",
                    quantity=adjust_qty(abs(still), f["stepSize"], f["minQty"]),
                    reduceOnly=True,
                )
            time.sleep(1.5)
            chk2 = client.futures_position_information(symbol=pair)
            still2 = float(chk2[0].get("positionAmt", 0) or 0) if chk2 else 0.0
            settled = abs(still2) <= 1e-9
    except Exception as e:
        log.warning(f"[close_position] settle check failed for {pair}: {_safe_err(e)}")

    if not settled:
        add_log(f"⚠️ {sym_short} manual close did not settle — state unchanged")

    if settled:
        handle = _engine_attr("handle_trade_close", "orders.exit", "orders")
        if callable(handle):
            try:
                handle(sym_short, pair, reason="MANUAL_UI")
            except Exception as e:
                log.warning(f"[close_position] handle_trade_close failed: {_safe_err(e)}")
        else:
            log.warning("[close_position] handle_trade_close not available")

    try:
        inv = _engine_attr("invalidate_account_cache", "core.client")
        if callable(inv):
            inv()
    except Exception:
        pass

    with _pos_cache_lock:
        _pos_cache["data"] = None
        _pos_cache["time"] = 0.0

    return jsonify({
        "success": True,
        "symbol": sym_short,
        "side": close_side,
        "qty": qty_str,
        "order_id": order_id,
        "settled": settled,
        "message": f"{sym_short} closed at market"
                   + ("" if settled else " (unconfirmed)"),
    })


# ─────────────────────────────────────────────────────────────
# HISTORY — income aggregation
# ─────────────────────────────────────────────────────────────
def _paginate_income(client: Any) -> list[dict]:
    """Fetch income history bounded to the HISTORY_LOOKBACK_DAYS window."""
    _refresh_timestamp()

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - CONFIG.history_lookback_days * 24 * 60 * 60 * 1000

    all_income: list[dict] = []
    seen_ids: set = set()
    cursor_ms = start_ms
    stall_count = 0

    for _ in range(CONFIG.max_income_pages):
        try:
            batch = client.futures_income_history(
                limit=CONFIG.income_page_size,
                startTime=cursor_ms,
                endTime=end_ms,
            )
        except Exception as exc:
            add_log(f"⚠️ Income batch error: {_safe_err(exc)}")
            break
        if not batch:
            break

        new_rows = 0
        for row in batch:
            key = row.get("tranId")
            if key is None:
                key = (
                    row.get("time"),
                    row.get("symbol"),
                    row.get("incomeType"),
                    row.get("income"),
                )
            if key in seen_ids:
                continue
            seen_ids.add(key)
            all_income.append(row)
            new_rows += 1

        if len(batch) < CONFIG.income_page_size:
            break

        last_time = int(batch[-1]["time"])
        if new_rows == 0 or last_time <= cursor_ms:
            cursor_ms = cursor_ms + 1
            stall_count += 1
        else:
            cursor_ms = last_time
            stall_count = 0

        if stall_count > 10:
            add_log("⚠️ Income pagination stalled (10× no progress) — stopping")
            break

        if cursor_ms >= end_ms:
            break

        _refresh_timestamp()
        time.sleep(0.15)

    return all_income


def _fmt_ts(ms: int) -> str:
    return (datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .astimezone(PKT).strftime("%Y-%m-%d %H:%M:%S"))


def _build_income_history(rows: list[dict],
                          time_key: str = "time"
                          ) -> tuple[list[dict], float, float, float, int, int]:
    """Group income rows by (symbol, second) → per-event PnL."""
    grouped: dict[Any, dict] = {}
    commission_by_sec: dict[Any, float] = defaultdict(float)
    total_pnl = 0.0
    total_commission = 0.0
    total_funding = 0.0
    wins = 0
    losses = 0

    for inc in rows:
        inc_type = inc.get("incomeType")
        try:
            val = float(inc.get("income") or 0)
            t_ms = int(inc.get(time_key) or 0)
        except (TypeError, ValueError):
            continue
        sec_key = (inc.get("symbol"), t_ms // 1000)

        if inc_type == "COMMISSION":
            total_commission += val
            commission_by_sec[sec_key] += val
            continue
        if inc_type == "FUNDING_FEE":
            total_funding += val
            continue
        if inc_type != "REALIZED_PNL" or abs(val) < 1e-5:
            continue

        if sec_key not in grouped:
            grouped[sec_key] = {
                "pnl": 0.0,
                "symbol": inc.get("symbol", ""),
                "time": _fmt_ts(t_ms),
                "time_ms": t_ms,
                "commission": 0.0,
            }
        grouped[sec_key]["pnl"] += val
        if t_ms < grouped[sec_key]["time_ms"]:
            grouped[sec_key]["time_ms"] = t_ms
            grouped[sec_key]["time"] = _fmt_ts(t_ms)

    matched = 0.0
    for sec_key, comm in commission_by_sec.items():
        if sec_key in grouped:
            grouped[sec_key]["commission"] += comm
            matched += comm

    if grouped:
        orphan = total_commission - matched
        if abs(orphan) > 0.001:
            share = orphan / len(grouped)
            for g in grouped.values():
                g["commission"] += share

    income_history: list[dict] = []
    for g in grouped.values():
        pnl = g["pnl"]
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        income_history.append({
            "time": g["time"],
            "time_ms": g["time_ms"],
            "symbol": g["symbol"],
            "pnl": pnl,
            "commission": g["commission"],
            "net": pnl + g["commission"],
        })

    income_history.sort(key=lambda x: x["time_ms"], reverse=True)
    return (income_history, total_pnl, total_commission,
            total_funding, wins, losses)


@app.route("/api/history")
def api_history() -> Response:
    """Income history + paired trades."""
    income_history: list[dict] = []
    total_pnl = 0.0
    total_commission = 0.0
    total_funding = 0.0
    wins = 0
    losses = 0
    all_income: list[dict] = []
    source = "legacy"

    analytics_snap: Optional[dict] = None
    try:
        from web import analytics
        analytics_snap = analytics.snapshot()
    except Exception as e:
        log.debug(f"[analytics] snapshot unavailable: {e}")

    if analytics_snap is not None:
        raw_income = analytics_snap.get("income") or []
        if raw_income:
            source = "analytics"
            all_income = raw_income
            try:
                (income_history,
                 total_pnl,
                 total_commission,
                 total_funding,
                 wins,
                 losses) = _build_income_history(raw_income, time_key="time_ms")
                income_history = income_history[: CONFIG.history_limit]
            except Exception as e:
                log.debug(f"[analytics] income build failed, using legacy: {e}")
                income_history = []
                total_pnl = 0.0
                total_commission = 0.0
                total_funding = 0.0
                wins = 0
                losses = 0
                all_income = []
                source = "legacy"

    if source == "legacy":
        client = get_client()
        try:
            if client is not None:
                all_income = _paginate_income(client)
                (income_history,
                 total_pnl,
                 total_commission,
                 total_funding,
                 wins,
                 losses) = _build_income_history(all_income, time_key="time")
                income_history = income_history[: CONFIG.history_limit]
        except Exception as exc:
            add_log(f"⚠️ History error: {_safe_err(exc)}")
            log.exception("Income history aggregation failed")

    total_trades = wins + losses

    trades_summary: list[dict] = []
    analytics_summary: dict = {}
    if analytics_snap is not None:
        trades_summary = analytics_snap.get("trades") or []
        analytics_summary = analytics_snap.get("summary") or {}

    return jsonify({
        "income_history": income_history,
        "total_pnl": total_pnl,
        "total_commission": total_commission,
        "total_funding": total_funding,
        "net_pnl": total_pnl + total_commission + total_funding,
        "wins": wins,
        "losses": losses,
        "total": total_trades,
        "winrate": (wins / total_trades * 100) if total_trades else 0,
        "total_records_fetched": len(all_income),
        "lookback_days": CONFIG.history_lookback_days,
        "trades_summary": trades_summary,
        "analytics_summary": analytics_summary,
        "source": source,
    })


# ─────────────────────────────────────────────────────────────
# DETAILED FILLS
# ─────────────────────────────────────────────────────────────
def _paginate_fills(client: Any, symbol: str) -> list[dict]:
    """Legacy fallback — used only if analytics module unavailable."""
    _refresh_timestamp()

    trades: list[dict] = []
    from_id: Optional[int] = None
    for _ in range(CONFIG.max_fills_pages):
        params: dict[str, Any] = {"symbol": symbol, "limit": CONFIG.fills_page_size}
        if from_id:
            params["fromId"] = from_id
        try:
            batch = client.futures_account_trades(**params)
        except Exception as exc:
            add_log(f"⚠️ Fills fetch error {symbol}: {_safe_err(exc)}")
            break
        if not batch:
            break
        trades.extend(batch)
        if len(batch) < CONFIG.fills_page_size:
            break
        from_id = batch[-1]["id"] + 1
        _refresh_timestamp()
        time.sleep(0.1)
    return trades


def _is_buyer(t: dict) -> bool:
    return bool(t.get("isBuyer", t.get("buyer", False)))


@app.route("/api/history/detailed")
def api_history_detailed() -> Response:
    """Raw fills + orders (served from analytics; legacy fallback)."""
    try:
        from web import analytics
        snap = analytics.snapshot() or {}
        snap_fills = snap.get("fills") or []
        snap_orders = snap.get("orders") or []
        if snap_fills:
            return jsonify({
                "trades": snap_fills,
                "orders": snap_orders,
                "total_pnl": sum(
                    float(f.get("realizedPnl", 0) or 0) for f in snap_fills
                ),
                "total_commission": sum(
                    float(f.get("commission", 0) or 0) for f in snap_fills
                ),
                "total_count": len(snap_fills),
                "orders_count": len(snap_orders),
                "last_refresh_ms": snap.get("last_refresh_ms"),
                "source": "analytics",
            })
    except Exception as e:
        log.debug(f"[analytics] fills fallback: {e}")

    client = get_client()
    if client is None:
        return jsonify({"trades": [], "error": "Client not ready"})
    if not CONFIG.bot_coins:
        return jsonify({"trades": [], "error": "BOT_COINS not set in .env"})

    try:
        all_trades: list[dict] = []
        for sym in CONFIG.bot_coins:
            all_trades.extend(_paginate_fills(client, sym))

        all_trades.sort(key=lambda x: x["time"], reverse=True)

        grouped = {}
        for t in all_trades:
            sec_key = (t.get("symbol",""), t["time"]//1000, _is_buyer(t))
            if sec_key not in grouped:
                grouped[sec_key] = {
                    "time": t["time"],
                    "symbol": t.get("symbol",""),
                    "side": "BUY" if _is_buyer(t) else "SELL",
                    "price": float(t.get("price",0)),
                    "qty": 0.0,
                    "realizedPnl": 0.0,
                    "commission": 0.0,
                    "count": 0,
                    "id": t.get("id",""),
                }
            grouped[sec_key]["qty"] += float(t.get("qty",0))
            grouped[sec_key]["realizedPnl"] += float(t.get("realizedPnl",0))
            grouped[sec_key]["commission"] += float(t.get("commission",0))
            grouped[sec_key]["count"] += 1
            if grouped[sec_key]["count"] > 1:
                prev_qty = grouped[sec_key]["qty"] - float(t.get("qty",0))
                prev_price = grouped[sec_key].get("_vwap_prev", grouped[sec_key]["price"])
                cur_qty = float(t.get("qty",0))
                cur_price = float(t.get("price",0))
                total = prev_qty + cur_qty
                vwap = (prev_qty*prev_price + cur_qty*cur_price)/total if total>0 else cur_price
                grouped[sec_key]["price"] = vwap
                grouped[sec_key]["_vwap_prev"] = vwap
            else:
                grouped[sec_key]["_vwap_prev"] = float(t.get("price",0))

        aggregated = sorted(grouped.values(), key=lambda x: x["time"], reverse=True)

        formatted = [{
            "time": _fmt_ts(t["time"]),
            "symbol": t["symbol"],
            "side": t["side"],
            "price": round(t["price"], 6),
            "qty": round(t["qty"], 2),
            "realizedPnl": round(t["realizedPnl"], 4),
            "commission": round(t["commission"], 4),
            "count": t["count"],
            "id": t["id"],
        } for t in aggregated[: CONFIG.history_limit]]

        return jsonify({
            "trades": formatted,
            "total_pnl": sum(float(t.get("realizedPnl", 0)) for t in all_trades),
            "total_commission": sum(float(t.get("commission", 0)) for t in all_trades),
            "total_count": len(all_trades),
            "source": "legacy",
        })
    except Exception as exc:
        log.exception("Detailed history failed")
        return jsonify({"trades": [], "error": _safe_err(exc)})


# ─────────────────────────────────────────────────────────────
# ANALYTICS ROUTES
# ─────────────────────────────────────────────────────────────
def _analytics_payload() -> dict[str, Any]:
    """Common shape for all analytics routes — includes cache status."""
    try:
        from web import analytics
    except ImportError:
        return {
            "success": False,
            "error": "analytics module not available",
            "items": [],
            "cache": {},
        }
    snap = analytics.snapshot() or {}
    return {
        "success": True,
        "cache": {
            "last_refresh_ms": snap.get("last_refresh_ms"),
            "in_progress": snap.get("refresh_in_progress", False),
            "last_error": snap.get("last_error"),
        },
        "summary": snap.get("summary", {}),
        "snapshot": snap,
    }


@app.route("/api/analytics/trades")
def api_analytics_trades() -> Response:
    payload = _analytics_payload()
    if not payload["success"]:
        return jsonify(payload), 503
    payload["items"] = payload["snapshot"].get("trades", [])
    payload.pop("snapshot", None)
    return jsonify(payload)


@app.route("/api/analytics/fills")
def api_analytics_fills() -> Response:
    payload = _analytics_payload()
    if not payload["success"]:
        return jsonify(payload), 503
    payload["items"] = payload["snapshot"].get("fills", [])
    payload.pop("snapshot", None)
    return jsonify(payload)


@app.route("/api/analytics/orders")
def api_analytics_orders() -> Response:
    payload = _analytics_payload()
    if not payload["success"]:
        return jsonify(payload), 503
    payload["items"] = payload["snapshot"].get("orders", [])
    payload.pop("snapshot", None)
    return jsonify(payload)


@app.route("/api/analytics/income")
def api_analytics_income() -> Response:
    payload = _analytics_payload()
    if not payload["success"]:
        return jsonify(payload), 503
    payload["items"] = payload["snapshot"].get("income", [])
    payload.pop("snapshot", None)
    return jsonify(payload)


@app.route("/api/analytics/summary")
def api_analytics_summary() -> Response:
    payload = _analytics_payload()
    if not payload["success"]:
        return jsonify(payload), 503
    payload.pop("snapshot", None)
    return jsonify(payload)


@app.route("/api/analytics/refresh", methods=["POST"])
@token_required
@rate_limit(10)
def api_analytics_refresh() -> Any:
    try:
        from web import analytics
        analytics.schedule_refresh()
        return jsonify({"success": True, "message": "Refresh scheduled"})
    except ImportError:
        return api_error("analytics module not available", 500)
    except Exception as e:
        return api_error(_safe_err(e))


# ─────────────────────────────────────────────────────────────
# DECISION ENGINE STATS
# ─────────────────────────────────────────────────────────────
@app.route("/api/decision/stats")
def api_decision_stats() -> Response:
    """Paused strategies + recent win/loss memory."""
    try:
        import signals.decision_engine as decision_engine
    except ImportError:
        return jsonify({
            "success": False,
            "error": "decision_engine module not available",
            "recent_count": 0,
            "by_strategy": {},
            "paused_strategies": {},
        }), 503

    try:
        stats = decision_engine.get_stats()
        stats["success"] = True
        return jsonify(stats)
    except Exception as e:
        log.exception("decision_engine.get_stats() failed")
        return jsonify({
            "success": False,
            "error": _safe_err(e),
            "recent_count": 0,
            "by_strategy": {},
            "paused_strategies": {},
        }), 500


# ─────────────────────────────────────────────────────────────
# TIME EXIT TOGGLE
# ─────────────────────────────────────────────────────────────
@app.route("/api/time_exit", methods=["GET"])
def api_time_exit_get() -> Response:
    """Current TIME_EXIT config (public read — no secrets exposed)."""
    try:
        from core import config_center as cc
        return jsonify({
            "success": True,
            "time_exit_enabled": bool(cc.GLOBAL.get("time_exit_enabled", False)),
            "hold_minutes": int(cc.GLOBAL.get("hold_minutes", 180)),
        })
    except Exception as e:
        log.exception("api_time_exit_get failed")
        return api_error(f"config_center unavailable: {_safe_err(e)}", 500)


@app.route("/api/time_exit", methods=["POST"])
@token_required
@rate_limit(2)
def api_time_exit_set() -> Any:
    """
    Toggle TIME_EXIT on/off at runtime.

    Body:
        {
          "enabled": true|false,
          "hold_minutes": 180
        }
    """
    try:
        from core import config_center as cc
    except Exception as e:
        return api_error(f"config_center unavailable: {_safe_err(e)}", 500)

    data = request.get_json(silent=True) or {}
    updates: dict = {}

    if "enabled" in data:
        updates["time_exit_enabled"] = bool(data["enabled"])

    if "hold_minutes" in data:
        try:
            hm = int(data["hold_minutes"])
        except (ValueError, TypeError):
            return api_error("invalid hold_minutes", 400)
        if hm < 0:
            return api_error("hold_minutes must be >= 0", 400)
        if hm > _MAX_HOLD_MINUTES:
            return api_error(
                f"hold_minutes must be <= {_MAX_HOLD_MINUTES} (24 h)",
                400,
            )
        updates["hold_minutes"] = hm

    if not updates:
        return api_error("no valid fields (enabled / hold_minutes)", 400)

    try:
        cc.update_runtime(**updates)
    except Exception as e:
        log.exception("update_runtime failed")
        return api_error(f"update failed: {_safe_err(e)}", 500)

    enabled_now = bool(cc.GLOBAL.get("time_exit_enabled", False))
    hold_now = int(cc.GLOBAL.get("hold_minutes", 180))
    add_log(f"⚙️ TIME_EXIT updated → enabled={enabled_now} hold={hold_now}m")

    return jsonify({
        "success": True,
        "time_exit_enabled": enabled_now,
        "hold_minutes": hold_now,
    })


# ─────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────
@app.route("/api/health")
def api_health() -> Response:
    with state_lock:
        running = bot_state["running"]
    analytics_ok = False
    try:
        from web import analytics  # noqa: F401
        analytics_ok = True
    except ImportError:
        pass
    decision_ok = False
    try:
        import signals.decision_engine as decision_engine  # noqa: F401
        decision_ok = True
    except ImportError:
        pass
    return jsonify({
        "status": "ok",
        "tt_available": TT_AVAILABLE,
        "coins_configured": len(CONFIG.bot_coins),
        "keys_set": CONFIG.keys_present,
        "running": running,
        "analytics": analytics_ok,
        "decision_engine": decision_ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ─────────────────────────────────────────────────────────────
# ERROR HANDLERS
# ─────────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(_: Any) -> Any:
    return api_error("Not found", 404)


@app.errorhandler(405)
def method_not_allowed(_: Any) -> Any:
    return api_error("Method not allowed", 405)


@app.errorhandler(500)
def internal_error(_: Any) -> Any:
    log.exception("Unhandled 500")
    return api_error("Internal server error", 500)


# ─────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────
def main() -> None:
    print_config_banner()

    log.info("=" * 60)
    log.info("  TRADING DESK TERMINAL")
    log.info("=" * 60)
    log.info("  Trading Engine: %s", "✅ Loaded" if TT_AVAILABLE else "❌ Not available")
    log.info("  WEB_TOKEN:      %s", "✅ Set" if CONFIG.token_present else "❌ NOT SET")
    log.info("  API Keys:       %s", "✅ Set" if CONFIG.keys_present else "❌ NOT SET")

    try:
        from web import analytics
        analytics.start_background_worker()
        log.info("  Analytics:      ✅ background worker started")
    except Exception as e:
        log.warning(f"  Analytics:      ❌ not started ({_safe_err(e)})")

    log.info("-" * 60)
    log.info("  URL: http://localhost:%s", CONFIG.port)
    log.info("=" * 60)

    if CONFIG.host not in ("127.0.0.1", "localhost"):
        log.warning(
            "Binding to %s exposes this dashboard beyond localhost. "
            "The built-in Flask server is fine for personal/local use, but "
            "for anything reachable over a network, put it behind a "
            "production WSGI server (gunicorn/waitress) and a reverse proxy "
            "with TLS + access control.",
            CONFIG.host,
        )

    app.run(host=CONFIG.host, port=CONFIG.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()