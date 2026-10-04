"""
indicators.py — V2.9.9 (2026-09-24) for BEST_SCALP_V2.

REV 3.7 (2026-10-04) — LIQUIDITY SWEEP + FVG ZONES (NEW):
  ✅ Added _liquidity_sweep() — detects stop-hunt reversals on the
     most recent closed bar. Gated by GLOBAL["use_liquidity_sweep"].
     Output keys: sweep_bull, sweep_bear, sweep_bull_low,
     sweep_bear_high, sweep_strength.
  ✅ Added _fvg_zones() — returns Fair Value Gap zones as list of
     dicts (type/top/bottom/mid/filled/gap_pct). Gated by
     GLOBAL["use_fvg_zones"]. Output keys expose the NEAREST
     unfilled bull/bear zone plus a count.
  ✅ Both functions default to no-op dicts when their flag is off,
     so output shape stays constant for downstream .get() reads.
  ✅ Zero behaviour change for existing consumers — all previous
     output keys unchanged.

REV 3.6 (2026-10-03) — FLAG GATING + DIV-BY-ZERO + DEAD STUB CLEANUP:
  ✅ Modern indicator flags now HONOURED (were declared in GLOBAL but
     ignored here):
       • use_taker_volume   → gates _taker_features()
       • use_volume_profile → gates _volume_profile()
       • use_anchored_vwap  → gates _anchored_vwap()
     Matches the existing use_funding_z gate pattern (REV 3.5).
     Output keys ALWAYS present (empty defaults when disabled) so
     downstream .get() reads don't change shape.
     ► NOTE for users: previous behaviour was "compute regardless of
       flag". If you relied on real values while flag=False, set the
       corresponding CC_USE_* env or .env flag to True.
  ✅ FIXED: `dx` divide-by-zero — was `.fillna(0)` which does NOT
     catch inf. Now `.replace(0, 1e-9).fillna(0)` matching the
     pattern in _detect_regime().
  ✅ Dead `liq` stub dict REMOVED (REV 3.4 removed the populating
     functions). Three legacy output keys (`long_liq_usd`,
     `short_liq_usd`, `liq_bias`) retained as 0.0/0.0/"BALANCED"
     literals for backward compat with any downstream .get() readers.
  ✅ Zero behaviour change for the current .env (all three flags True).

REV 3.5 (2026-10-02) — DEAD IMPORT + LIVE FUNDING_Z READ.
REV 3.4 (2026-10-02) — DEAD FUNCTION CLEANUP.
REV 3.3 (2026-10-02) — FULLY DELEGATED TO config_center.
REV 3.2 (2026-10-02) — UNIFIED CONFIG (Option B).
REV 1.4.14 (2026-10-01) — 3-LAYER 5m TREND FILTER.
REV 1.4.13 (2026-10-01) — PHASE 1 ORDERBOOK ENABLE.
REV 1.4.12 (2026-09-29) — 5M TREND CACHE TTL EXTENSION.
REV 1.4.11 (2026-09-29) — DEAD HELPER REMOVAL.
REV 1.4.10 (2026-09-29) — DEAD CODE CLEANUP.
REV 1.4.9  (2026-09-29) — REGIME MOMENTUM OVERRIDE.
REV 1.4.8  (2026-09-28) — REGIME MULTIPLIERS + SLOW DONCHIAN.
REV 1.4.7  (2026-09-28) — GATED UNUSED API CALLS.
REV 1.4.6  (2026-09-28) — LEGACY SIGNAL BLOCK DELETED.
REV 1.4.5  (2026-09-28) — SHARED HTF ALIGNMENT FUNCTION.
REV 1.4.1  (2026-09-26) — DEAD CONSTANT CLEANUP.
REV 1.4.0  (2026-09-24) — MODERN INDICATOR PACK (Phase 1).
REV 1.3.4  (2026-09-23) — MIN_RR FIX.
REV 1.3.3  (2026-09-22) — SENTIMENT + DEAD CODE FIX.
REV 1.3.2  (2026-09-22) — BE STOP FIXED.
REV 1.3.1  (2026-09-22) — R-SCALED KEYS NOW FALLBACK-ONLY.
V2.9.3 (2026-09-21) — REV 15.0 (superseded).
V2.8 FIXES (kept): frozen kline guard, cache TTL 300s.
V2.7 FIXES (kept): MIN_SL_PCT floor, top-chase gate.
V2.6 FIXES (kept): _LIQ_404_CACHED + lock, 401/403/404/410 break.
V2.4 FIX (kept): st_flips ROLLING (last 500 bars).
V2.3 FIX (kept): REGIME CLASSIFIER — Hurst 0.52 + ADX/EMA fallback.
"""
from __future__ import annotations

import atexit
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────
# HTTP CLIENT
# ─────────────────────────────────────────────────────────────
try:
    import httpx
    _HTTP_CLIENT: Optional[Any] = httpx.Client(
        timeout=6.0, follow_redirects=True
    )
    _USE_HTTPX = True
    atexit.register(_HTTP_CLIENT.close)
except ImportError:
    import requests
    _HTTP_CLIENT = None
    _USE_HTTPX = False

try:
    from core.client import get_binance_klines as _get_klines
except Exception:
    _get_klines = None

try:
    from core.client import logger  # type: ignore
except Exception:
    import logging
    logger = logging.getLogger("indicators")
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())

# ── REV 3.5 — config_center delegation (single source of truth) ──
from core.config_center import (
    get_config as _cc_get_config,
    get_regime_cfg as _cc_get_regime_cfg,
    get as _cc_get,
)

UTC = timezone.utc


# ─────────────────────────────────────────────────────────────
# HTTP HELPER
# ─────────────────────────────────────────────────────────────
def _http_get_json(url: str, params: dict | None = None,
                   retries: int = 2) -> Any:
    last_err = None
    last_status = None
    for attempt in range(retries + 1):
        try:
            if _USE_HTTPX and _HTTP_CLIENT is not None:
                r = _HTTP_CLIENT.get(url, params=params or {})
                last_status = r.status_code
                if r.status_code == 200:
                    return r.json()
                last_err = f"HTTP {r.status_code}"
                if r.status_code in (401, 403, 404, 410):
                    break
            else:
                import requests
                r = requests.get(url, params=params or {}, timeout=6)
                last_status = r.status_code
                if r.status_code == 200:
                    return r.json()
                last_err = f"HTTP {r.status_code}"
                if r.status_code in (401, 403, 404, 410):
                    break
        except Exception as e:
            last_err = str(e)
        if attempt < retries:
            time.sleep(0.4 * (attempt + 1))

    if last_status in (401, 403, 404, 410):
        logger.debug(f"http_get_json {url}: {last_err} (endpoint unavailable)")
    else:
        logger.debug(f"http_get_json failed {url}: {last_err}")
    return None


# ─────────────────────────────────────────────────────────────
# REGIME-ADAPTIVE CONFIG — REV 3.3
#   Fully delegated to core.config_center (single source of truth).
# ─────────────────────────────────────────────────────────────
def get_trading_config() -> dict:
    """Flat GLOBAL + REGIME[UNKNOWN] — legacy _BASE_CFG shape."""
    out = _cc_get_config(include_family=False)
    out.pop("_meta", None)
    return out


def get_regime_multipliers(regime: str) -> dict:
    cfg = _regime_config(regime)
    return {"sl_mult": float(cfg.get("sl_mult", 1.0)),
            "tp_mult": float(cfg.get("tp_mult", 1.0))}


def _regime_config(regime: str) -> dict:
    """Return REGIME[regime] — served by config_center."""
    return _cc_get_regime_cfg(regime)


# ─────────────────────────────────────────────────────────────
# CACHE
# ─────────────────────────────────────────────────────────────
_CACHE_TTL = {"1m": 30, "5m": 60, "15m": 120, "1h": 300, "4h": 600, "1d": 1800}
_indicator_cache: dict[tuple[str, str], tuple[dict, float]] = {}
_cache_lock = threading.Lock()


def get_cached_indicator(coin: str, tf: str) -> Optional[dict]:
    key, ttl, now = (coin, tf), _CACHE_TTL.get(tf, 60), time.time()
    with _cache_lock:
        e = _indicator_cache.get(key)
        if e and now - e[1] < ttl:
            return e[0]
    return None


def set_cached_indicator(coin: str, tf: str, ind: dict) -> None:
    with _cache_lock:
        _indicator_cache[(coin, tf)] = (ind, time.time())


def cleanup_indicator_cache() -> None:
    now = time.time()
    with _cache_lock:
        for key in list(_indicator_cache.keys()):
            coin, tf = key
            _, ts = _indicator_cache[key]
            ttl = _CACHE_TTL.get(tf, 60)
            if now - ts > ttl * 3:
                _indicator_cache.pop(key, None)


# ─────────────────────────────────────────────────────────────
# KILLZONES
# ─────────────────────────────────────────────────────────────
def _is_london_bst(dt_utc: datetime) -> bool:
    year = dt_utc.year
    mar = datetime(year, 3, 31, tzinfo=UTC)
    while mar.weekday() != 6:
        mar -= timedelta(days=1)
    oct_ = datetime(year, 10, 31, tzinfo=UTC)
    while oct_.weekday() != 6:
        oct_ -= timedelta(days=1)
    return mar <= dt_utc < oct_


def _current_killzone() -> str:
    now_utc = datetime.now(UTC)
    h = now_utc.hour
    london_offset = 1 if _is_london_bst(now_utc) else 0
    london_start = 7 + london_offset
    london_end = 10 + london_offset
    if london_start <= h < london_end:
        return "LONDON"
    if 12 <= h < 15:
        return "NY_AM"
    if 17 <= h < 20:
        return "NY_PM"
    return "NONE"


# ─────────────────────────────────────────────────────────────
# FEAR & GREED
# ─────────────────────────────────────────────────────────────
_fg_cache: dict[str, Any] = {"value": 50, "class": "Neutral", "time": 0.0}
_fg_lock = threading.Lock()
_FG_TTL = 300


def get_fear_greed_index() -> tuple[int, str]:
    now = time.time()
    with _fg_lock:
        if now - _fg_cache["time"] < _FG_TTL:
            return int(_fg_cache["value"]), str(_fg_cache["class"])
    data = _http_get_json("https://api.alternative.me/fng/?limit=1")
    try:
        d = data["data"][0]
        val, cls = int(d["value"]), str(d["value_classification"])
    except Exception:
        val, cls = 50, "Neutral"
    with _fg_lock:
        _fg_cache.update(value=val, **{"class": cls}, time=now)
    return val, cls


# ─────────────────────────────────────────────────────────────
# FUNDING RATE Z-SCORE
# ─────────────────────────────────────────────────────────────
_fr_cache: dict[str, dict] = {}
_fr_lock = threading.Lock()
_FR_TTL = 1800


def get_funding_rate_z(symbol: str) -> dict:
    now = time.time()
    with _fr_lock:
        hit = _fr_cache.get(symbol)
        if hit and now - hit["time"] < _FR_TTL:
            return hit["data"]

    out = {"funding_now": 0.0, "funding_mean": 0.0, "funding_z": 0.0}
    pair = symbol if symbol.endswith("USDT") else symbol + "USDT"

    try:
        from core.client import get_client
        cl = get_client()
        if cl is not None:
            hist = cl.futures_funding_rate(symbol=pair, limit=100)
            if hist and len(hist) >= 20:
                rates = [float(h.get("fundingRate", 0) or 0) for h in hist]
                arr = np.asarray(rates, dtype=float)
                mean = float(arr.mean())
                std = float(arr.std())
                latest = float(arr[-1])
                z = (latest - mean) / std if std > 1e-12 else 0.0
                out["funding_now"] = latest
                out["funding_mean"] = mean
                out["funding_z"] = z
    except Exception as e:
        logger.debug(f"funding_z {pair}: {type(e).__name__}")

    with _fr_lock:
        _fr_cache[symbol] = {"data": out, "time": now}
    return out


# ─────────────────────────────────────────────────────────────
# ORDER BOOK IMBALANCE
# ─────────────────────────────────────────────────────────────
_ob_cache: dict[str, dict] = {}
_ob_lock = threading.Lock()
_OB_TTL = 30


def get_order_book_imbalance(symbol: str) -> dict:
    now = time.time()
    with _ob_lock:
        hit = _ob_cache.get(symbol)
        if hit and now - hit["time"] < _OB_TTL:
            return hit["data"]

    out = {"bid_vol": 0.0, "ask_vol": 0.0, "imbalance": 0.0, "bias": "BALANCED"}
    pair = symbol if symbol.endswith("USDT") else symbol + "USDT"
    try:
        depth = _http_get_json("https://fapi.binance.com/fapi/v1/depth",
                               {"symbol": pair, "limit": 20})
        if depth:
            bid_vol = sum(float(b[1]) for b in depth.get("bids", [])[:20])
            ask_vol = sum(float(a[1]) for a in depth.get("asks", [])[:20])
            total = bid_vol + ask_vol
            if total > 0:
                imb = (bid_vol - ask_vol) / total
                out["bid_vol"] = round(bid_vol, 4)
                out["ask_vol"] = round(ask_vol, 4)
                out["imbalance"] = round(imb, 4)
                if imb > 0.25:
                    out["bias"] = "BUY_PRESSURE"
                elif imb < -0.25:
                    out["bias"] = "SELL_PRESSURE"
    except Exception as e:
        logger.debug(f"OB imbalance {pair}: {e}")

    with _ob_lock:
        _ob_cache[symbol] = {"data": out, "time": now}
    return out


# ─────────────────────────────────────────────────────────────
# CANDLE PATTERNS
# ─────────────────────────────────────────────────────────────
def detect_candle_patterns(df: Optional[pd.DataFrame],
                           i: Optional[int] = None) -> list[str]:
    if df is None or len(df) < 4:
        return []
    if i is None:
        i = len(df) - 1
    if i < 3 or i >= len(df):
        return []

    c3, c2, c1 = df.iloc[i - 2], df.iloc[i - 1], df.iloc[i]
    o3, h3, l3, cc3 = (float(c3[k]) for k in ("Open", "High", "Low", "Close"))
    o2, h2, l2, cc2 = (float(c2[k]) for k in ("Open", "High", "Low", "Close"))
    o1, h1, l1, cc1 = (float(c1[k]) for k in ("Open", "High", "Low", "Close"))

    body1 = abs(cc1 - o1); range1 = max(h1 - l1, 1e-12)
    body2 = abs(cc2 - o2); body3 = abs(cc3 - o3)
    sma20_slice = df["Close"].iloc[max(0, i - 20):i]
    sma20 = float(sma20_slice.mean()) if len(sma20_slice) else cc1
    pats: list[str] = []

    if body1 / range1 < 0.1:
        pats.append("Doji")
    if body1 > 0:
        lw = min(o1, cc1) - l1; uw = h1 - max(o1, cc1)
        if lw > body1 * 2.5 and uw < body1 * 0.3:
            pats.append("Hammer" if cc1 < sma20 else "Hanging Man")
        if uw > body1 * 2.5 and lw < body1 * 0.3:
            pats.append("Inverted Hammer" if cc1 < sma20 else "Shooting Star")
    if cc1 > o1 and cc2 < o2 and cc1 > o2 and o1 < cc2:
        pats.append("Bullish Engulfing")
    if cc1 < o1 and cc2 > o2 and o1 > cc2 and cc1 < o2:
        pats.append("Bearish Engulfing")
    mid2 = (o2 + cc2) / 2
    if cc2 < o2 and cc1 > o1 and o1 < cc2 and cc1 > mid2 and cc1 < o2:
        pats.append("Piercing Line")
    if cc2 > o2 and cc1 < o1 and o1 > cc2 and cc1 < mid2 and cc1 > o2:
        pats.append("Dark Cloud")
    if body2 > 0:
        if cc3 < o3 and body3 > body2 * 2 and cc1 > o1 and cc1 > (o3 + cc3) / 2 and body2 < body1 * 0.5:
            pats.append("Morning Star")
        if cc3 > o3 and body3 > body2 * 2 and cc1 < o1 and cc1 < (o3 + cc3) / 2 and body2 < body1 * 0.5:
            pats.append("Evening Star")
    return pats


# ─────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────
def _safe_last(series: pd.Series, default: float = 0.0) -> float:
    try:
        v = series.iloc[-1]
        if pd.isna(v) or not math.isfinite(float(v)):
            return default
        return float(v)
    except Exception:
        return default


def _compute_divergence(close: pd.Series, rsi: pd.Series,
                        lookback: int = 20) -> str:
    if len(close) < lookback + 5 or len(rsi) < lookback + 5:
        return "NONE"
    try:
        p_now, p_prev = float(close.iloc[-1]), float(close.iloc[-lookback])
        r_now, r_prev = float(rsi.iloc[-1]), float(rsi.iloc[-lookback])
        if not all(math.isfinite(x) for x in (p_now, p_prev, r_now, r_prev)):
            return "NONE"
        if p_now > p_prev and (r_prev - r_now) > 5:
            return "BEARISH_DIV"
        if p_now < p_prev and (r_now - r_prev) > 5:
            return "BULLISH_DIV"
    except Exception:
        pass
    return "NONE"


# ─────────────────────────────────────────────────────────────
# CVD
# ─────────────────────────────────────────────────────────────
def _cvd_features(df: pd.DataFrame) -> dict:
    close = df["Close"].astype(float); open_ = df["Open"].astype(float)
    high = df["High"].astype(float); low = df["Low"].astype(float)
    vol = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series(1.0, index=df.index)

    rng = (high - low).replace(0, 1e-9)
    delta = ((close - open_) / rng) * vol
    cvd = delta.cumsum()
    if len(cvd) < 50:
        return {"cvd_slope": 0.0, "cvd_z": 0.0, "delta_z": 0.0,
                "cvd_div": "NONE", "cvd_rising": False}

    cvd_slope = float(cvd.diff(10).iloc[-1])
    d_mean = delta.rolling(50).mean()
    d_std = delta.rolling(50).std().replace(0, 1e-9)
    delta_z = float(((delta - d_mean) / d_std).iloc[-1])
    if not math.isfinite(delta_z):
        delta_z = 0.0

    cvd_mean = cvd.rolling(50).mean()
    cvd_std = cvd.rolling(50).std().replace(0, 1e-9)
    cz = (cvd - cvd_mean) / cvd_std
    cvd_z = float(cz.iloc[-1]) if len(cz) and math.isfinite(float(cz.iloc[-1])) else 0.0

    cvd_div = "NONE"
    if len(close) >= 25:
        p_now, p_prev = float(close.iloc[-1]), float(close.iloc[-20])
        c_now, c_prev = float(cvd.iloc[-1]), float(cvd.iloc[-20])
        if p_now > p_prev and c_now < c_prev:
            cvd_div = "BEARISH_CVD"
        elif p_now < p_prev and c_now > c_prev:
            cvd_div = "BULLISH_CVD"

    return {"cvd_slope": cvd_slope, "cvd_z": cvd_z, "delta_z": delta_z,
            "cvd_div": cvd_div, "cvd_rising": cvd_slope > 0}


# ─────────────────────────────────────────────────────────────
# TAKER BUY/SELL FEATURES
# ─────────────────────────────────────────────────────────────
def _taker_features(df: pd.DataFrame) -> dict:
    empty = {"taker_buy_ratio": 0.5, "taker_ratio_z": 0.0}
    if "TBBAV" not in df.columns or "Volume" not in df.columns:
        return empty
    try:
        vol = df["Volume"].astype(float)
        taker_buy = df["TBBAV"].astype(float)
    except Exception:
        return empty
    if len(vol) < 50 or vol.iloc[-1] <= 0:
        return empty

    ratio = (taker_buy / vol.replace(0, 1e-9)).clip(0, 1)
    mean = ratio.rolling(50).mean()
    std = ratio.rolling(50).std().replace(0, 1e-9)
    z = (ratio - mean) / std

    return {
        "taker_buy_ratio": _safe_last(ratio, 0.5),
        "taker_ratio_z": _safe_last(z, 0.0),
    }


# ─────────────────────────────────────────────────────────────
# VOLUME PROFILE (POC / VAH / VAL)
# ─────────────────────────────────────────────────────────────
def _volume_profile(df: pd.DataFrame,
                    bins: int = 50,
                    lookback: int = 200) -> dict:
    empty = {"poc": 0.0, "vah": 0.0, "val": 0.0, "poc_dist_pct": 0.0}
    if len(df) < 50:
        return empty

    recent = df.iloc[-lookback:] if len(df) >= lookback else df
    try:
        price_min = float(recent["Low"].min())
        price_max = float(recent["High"].max())
    except Exception:
        return empty
    if not math.isfinite(price_max) or price_max <= price_min:
        return empty

    bin_size = (price_max - price_min) / bins
    if bin_size <= 0:
        return empty

    vp = np.zeros(bins)
    try:
        highs = recent["High"].astype(float).values
        lows = recent["Low"].astype(float).values
        vols = recent["Volume"].astype(float).values
    except Exception:
        return empty

    for i in range(len(recent)):
        low = lows[i]; high = highs[i]; vol = vols[i]
        if not (math.isfinite(low) and math.isfinite(high) and math.isfinite(vol)):
            continue
        low_bin = max(0, min(bins - 1, int((low - price_min) / bin_size)))
        high_bin = max(0, min(bins - 1, int((high - price_min) / bin_size)))
        touched = high_bin - low_bin + 1
        if touched <= 0:
            continue
        share = vol / touched
        for b in range(low_bin, high_bin + 1):
            vp[b] += share

    total = vp.sum()
    if total <= 0:
        return empty

    poc_bin = int(np.argmax(vp))
    poc_price = price_min + (poc_bin + 0.5) * bin_size

    target = total * 0.70
    order = np.argsort(vp)[::-1]
    accumulated = 0.0
    va_bins: set[int] = set()
    for idx in order:
        va_bins.add(int(idx))
        accumulated += vp[idx]
        if accumulated >= target:
            break
    if not va_bins:
        va_bins = {poc_bin}
    vah_bin = max(va_bins)
    val_bin = min(va_bins)
    vah_price = price_min + (vah_bin + 1) * bin_size
    val_price = price_min + val_bin * bin_size

    price = _safe_last(df["Close"].astype(float), 0.0)
    poc_dist_pct = ((price - poc_price) / poc_price * 100.0) if poc_price > 0 else 0.0

    return {
        "poc": round(poc_price, 8),
        "vah": round(vah_price, 8),
        "val": round(val_price, 8),
        "poc_dist_pct": round(poc_dist_pct, 4),
    }


# ─────────────────────────────────────────────────────────────
# ANCHORED VWAP
# ─────────────────────────────────────────────────────────────
def _anchored_vwap(df: pd.DataFrame, lookback: int = 100) -> dict:
    empty = {"anchored_vwap": 0.0, "avwap_dist_pct": 0.0}
    if len(df) < 30:
        return empty

    recent = df.iloc[-lookback:] if len(df) >= lookback else df
    try:
        highs = recent["High"].astype(float).values
        lows = recent["Low"].astype(float).values
        closes = recent["Close"].astype(float).values
        vols = recent["Volume"].astype(float).values
    except Exception:
        return empty

    if len(closes) < 5:
        return empty

    ll_idx = int(np.argmin(lows))
    hh_idx = int(np.argmax(highs))
    anchor_idx = max(ll_idx, hh_idx)
    if anchor_idx >= len(recent) - 3:
        anchor_idx = max(0, len(recent) - 30)

    a_highs = highs[anchor_idx:]
    a_lows = lows[anchor_idx:]
    a_closes = closes[anchor_idx:]
    a_vols = vols[anchor_idx:]

    if len(a_closes) == 0:
        return empty

    typical = (a_highs + a_lows + a_closes) / 3.0
    vol_sum = float(a_vols.sum())
    if vol_sum <= 0:
        return empty

    avwap = float((typical * a_vols).sum() / vol_sum)
    price = float(closes[-1])
    dist_pct = ((price - avwap) / avwap * 100.0) if avwap > 0 else 0.0

    return {
        "anchored_vwap": round(avwap, 8),
        "avwap_dist_pct": round(dist_pct, 4),
    }


# ─────────────────────────────────────────────────────────────
# SHARED HTF ALIGNMENT
# ─────────────────────────────────────────────────────────────
def htf_aligns(ind_4h, side: str, relaxed: bool = False) -> bool:
    if not ind_4h:
        return True
    try:
        e20 = float(ind_4h.get("ema20", 0) or 0)
        e50 = float(ind_4h.get("ema50", 0) or 0)
        p   = float(ind_4h.get("price", 0) or 0)
    except (TypeError, ValueError):
        return True
    if e20 <= 0 or e50 <= 0 or p <= 0:
        return True

    if side == "BUY":
        return (e20 > e50 or p > e50) if relaxed else (e20 > e50 and p > e50)
    return (e20 < e50 or p < e50) if relaxed else (e20 < e50 and p < e50)


# ─────────────────────────────────────────────────────────────
# REGIME
# ─────────────────────────────────────────────────────────────
def _hurst(series: np.ndarray, max_lag: int = 20) -> float:
    if len(series) < max_lag * 2:
        return 0.5
    try:
        lags = list(range(2, max_lag))
        tau = []
        for lag in lags:
            diff = series[lag:] - series[:-lag]
            std = float(np.std(diff))
            tau.append(std if std > 0 else 1e-9)
        if len(tau) < 3:
            return 0.5
        poly = np.polyfit(np.log(lags[:len(tau)]), np.log(tau), 1)
        return float(poly[0])
    except Exception:
        return 0.5


def _detect_regime(df: pd.DataFrame) -> dict:
    close = df["Close"].astype(float).values
    if len(close) < 100:
        return {"regime": "UNKNOWN", "hurst": 0.5, "vol_pct": 0.5,
                "momentum_pct": 0.0}

    window = close[-200:] if len(close) >= 200 else close
    hurst = _hurst(window)

    high = df["High"].astype(float); low = df["Low"].astype(float)
    close_s = df["Close"].astype(float).shift()
    tr = pd.concat([high - low, (high - close_s).abs(),
                    (low - close_s).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().dropna()
    atr_pct = float((atr < atr.iloc[-1]).mean()) if len(atr) >= 50 else 0.5

    ema20 = df["Close"].astype(float).ewm(span=20).mean()
    ema50 = df["Close"].astype(float).ewm(span=50).mean()
    trend_up = float(ema20.iloc[-1]) > float(ema50.iloc[-1])

    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    tr_s = tr.rolling(14).mean()
    plus_di  = 100 * (plus_dm.rolling(14).mean() / tr_s)
    minus_di = 100 * (minus_dm.rolling(14).mean() / tr_s)
    dx = (100 * (plus_di - minus_di).abs() /
          (plus_di + minus_di).replace(0, 1e-9)).fillna(0)
    adx = dx.rolling(14).mean()
    adx_last = float(adx.iloc[-1]) if len(adx) and math.isfinite(float(adx.iloc[-1])) else 0.0

    ema20_last = float(ema20.iloc[-1])
    ema50_last = float(ema50.iloc[-1])
    ema_spread = (abs(ema20_last - ema50_last) / max(abs(ema50_last), 1e-9))

    try:
        price_now = float(close[-1])
        price_20_ago = float(close[-21]) if len(close) >= 21 else float(close[0])
        momentum_pct = ((price_now - price_20_ago) / price_20_ago
                        if price_20_ago > 0 else 0.0)
    except Exception:
        momentum_pct = 0.0

    if atr_pct > 0.85:
        regime = "VOLATILE"
    elif atr_pct < 0.15:
        regime = "QUIET"
    elif hurst > 0.52:
        regime = "TREND_UP" if trend_up else "TREND_DOWN"
    elif adx_last > 28 and ema_spread > 0.003:
        regime = "TREND_UP" if trend_up else "TREND_DOWN"
    elif abs(momentum_pct) >= 0.03:
        regime = "TREND_UP" if momentum_pct > 0 else "TREND_DOWN"
    else:
        regime = "CHOP"

    return {
        "regime": regime,
        "hurst": round(hurst, 3),
        "vol_pct": round(atr_pct, 3),
        "momentum_pct": round(momentum_pct, 4),
    }


# ─────────────────────────────────────────────────────────────
# SUPERTREND
# ─────────────────────────────────────────────────────────────
def _supertrend(df: pd.DataFrame, period: int = 10,
                multiplier: float = 3.0) -> dict:
    try:
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)

        tr = pd.concat([high - low,
                        (high - close.shift()).abs(),
                        (low - close.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()

        hl2 = (high + low) / 2
        upper = hl2 + multiplier * atr
        lower = hl2 - multiplier * atr

        st = pd.Series(index=df.index, dtype=float)
        dir_ = pd.Series(index=df.index, dtype=int)
        st.iloc[0] = upper.iloc[0]
        dir_.iloc[0] = -1

        for i in range(1, len(df)):
            prev_st = st.iloc[i - 1]
            if close.iloc[i] > prev_st:
                st.iloc[i] = max(lower.iloc[i], prev_st) if dir_.iloc[i - 1] == 1 else lower.iloc[i]
                dir_.iloc[i] = 1
            else:
                st.iloc[i] = min(upper.iloc[i], prev_st) if dir_.iloc[i - 1] == -1 else upper.iloc[i]
                dir_.iloc[i] = -1

        flips_series = (dir_.diff().abs() > 0).astype(int)
        flips = int(flips_series.iloc[-500:].sum())

        return {
            "st_trend": "UP" if dir_.iloc[-1] == 1 else "DOWN",
            "st_value": float(st.iloc[-1]),
            "st_flips": flips,
        }
    except Exception as e:
        logger.debug(f"supertrend: {e}")
        return {"st_trend": "N/A", "st_value": 0.0, "st_flips": 0}


# ─────────────────────────────────────────────────────────────
# SESSION VWAP
# ─────────────────────────────────────────────────────────────
def _session_vwap(df: pd.DataFrame) -> float:
    if df.empty:
        return 0.0
    try:
        if not isinstance(df.index, pd.DatetimeIndex):
            df = df.copy()
            df.index = pd.to_datetime(df.index, utc=True)
        elif df.index.tz is None:
            df = df.copy()
            df.index = df.index.tz_localize("UTC")
        today = datetime.now(UTC).date()
        mask = df.index.date == today
        if mask.sum() < 3:
            mask = df.index.date == df.index.date[-1]
        sub = df[mask]
        tp = (sub["High"].astype(float) + sub["Low"].astype(float)
              + sub["Close"].astype(float)) / 3
        v = sub["Volume"].astype(float) if "Volume" in sub.columns else pd.Series(1.0, index=sub.index)
        denom = v.sum()
        return float((tp * v).sum() / denom) if denom > 0 else float(tp.iloc[-1])
    except Exception:
        return float(df["Close"].astype(float).iloc[-1])


# ─────────────────────────────────────────────────────────────
# ORDER BLOCKS + FVG (legacy midpoint)
# ─────────────────────────────────────────────────────────────
def _order_blocks(df: pd.DataFrame, lookback: int = 50) -> dict:
    if len(df) < lookback + 5:
        return {"ob_bull": 0.0, "ob_bear": 0.0, "fvg_bull": 0.0, "fvg_bear": 0.0}

    recent = df.iloc[-lookback:].reset_index(drop=True)
    price = float(df["Close"].astype(float).iloc[-1])
    ob_bull, ob_bear = 0.0, 0.0

    for i in range(2, len(recent) - 2):
        o, c = float(recent["Open"].iloc[i]), float(recent["Close"].iloc[i])
        c_next = float(recent["Close"].iloc[i + 1])
        if c < o and c_next > float(recent["High"].iloc[i]):
            lvl = float(recent["Low"].iloc[i])
            if lvl < price and lvl > ob_bull:
                ob_bull = lvl
        if c > o and c_next < float(recent["Low"].iloc[i]):
            lvl = float(recent["High"].iloc[i])
            if lvl > price and (ob_bear == 0 or lvl < ob_bear):
                ob_bear = lvl

    fvg_bull, fvg_bear = 0.0, 0.0
    for i in range(2, len(recent)):
        h1 = float(recent["High"].iloc[i - 2]); l1 = float(recent["Low"].iloc[i - 2])
        h3 = float(recent["High"].iloc[i]); l3 = float(recent["Low"].iloc[i])
        if l3 > h1:
            gap = (h1 + l3) / 2
            if gap < price and gap > fvg_bull:
                fvg_bull = gap
        if h3 < l1:
            gap = (h3 + l1) / 2
            if gap > price and (fvg_bear == 0 or gap < fvg_bear):
                fvg_bear = gap

    return {"ob_bull": ob_bull, "ob_bear": ob_bear,
            "fvg_bull": fvg_bull, "fvg_bear": fvg_bear}


# ─────────────────────────────────────────────────────────────
# LIQUIDITY SWEEP DETECTION (REV 3.7 — NEW)
# ─────────────────────────────────────────────────────────────
def _liquidity_sweep(df: pd.DataFrame, lookback: int = 20) -> dict:
    """
    Detect liquidity sweeps (stop hunts) on the most recent closed bar.

    Bullish sweep (BUY signal):
      - Current bar wicks BELOW the 20-bar low
      - But CLOSES back ABOVE it
      - Lower wick >= 2x body
      - Volume above 5-bar average

    Bearish sweep (SELL signal): mirror logic.
    """
    empty = {
        "sweep_bull": False, "sweep_bear": False,
        "sweep_bull_low": 0.0, "sweep_bear_high": 0.0,
        "sweep_strength": 0.0,
    }
    if df is None or len(df) < lookback + 6:
        return empty

    try:
        closed = df.iloc[:-1]                     # drop in-progress bar
        prior = closed.iloc[-(lookback + 1):-1]
        curr = closed.iloc[-1]

        prev_low = float(prior["Low"].min())
        prev_high = float(prior["High"].max())

        c_o = float(curr["Open"]);  c_h = float(curr["High"])
        c_l = float(curr["Low"]);   c_c = float(curr["Close"])

        body = abs(c_c - c_o)
        lower_wick = min(c_o, c_c) - c_l
        upper_wick = c_h - max(c_o, c_c)
        rng = c_h - c_l if c_h > c_l else 1e-9

        vol = df["Volume"].astype(float) if "Volume" in df.columns else None
        vol_ok = True
        if vol is not None and len(vol) >= 6:
            v_avg = float(vol.iloc[-6:-1].mean())
            v_now = float(vol.iloc[-1])
            if v_avg > 0:
                vol_ok = v_now >= v_avg

        # ── Bullish sweep ──
        if (c_l < prev_low and c_c > prev_low
                and lower_wick >= body * 2.0 and vol_ok):
            strength = min(1.0, lower_wick / rng)
            return {
                "sweep_bull": True, "sweep_bear": False,
                "sweep_bull_low": prev_low, "sweep_bear_high": 0.0,
                "sweep_strength": round(strength, 2),
            }

        # ── Bearish sweep ──
        if (c_h > prev_high and c_c < prev_high
                and upper_wick >= body * 2.0 and vol_ok):
            strength = min(1.0, upper_wick / rng)
            return {
                "sweep_bull": False, "sweep_bear": True,
                "sweep_bull_low": 0.0, "sweep_bear_high": prev_high,
                "sweep_strength": round(strength, 2),
            }
    except Exception as e:
        logger.debug(f"liquidity_sweep: {e}")

    return empty


# ─────────────────────────────────────────────────────────────
# FVG ZONES (REV 3.7 — NEW — full zone top/bottom, nearest-unfilled)
# ─────────────────────────────────────────────────────────────
def _fvg_zones(df: pd.DataFrame, min_gap_atr: float = 0.15,
               lookback: int = 100) -> list:
    """
    Return FVG zones as list of dicts (nearest first).
    Each zone: {"type": "bull"/"bear", "top":, "bottom":, "mid":,
                "idx":, "filled": bool, "gap_pct": float}
    """
    out: list = []
    if df is None or len(df) < 5:
        return out

    try:
        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)

        tr = pd.concat(
            [high - low, (high - close.shift()).abs(),
             (low - close.shift()).abs()], axis=1
        ).max(axis=1)
        atr = tr.rolling(14).mean()

        start = max(2, len(df) - lookback)
        price_now = float(close.iloc[-1])

        for i in range(start, len(df)):
            c1h = float(df["High"].iloc[i - 2])
            c1l = float(df["Low"].iloc[i - 2])
            c3h = float(df["High"].iloc[i])
            c3l = float(df["Low"].iloc[i])
            atr_v = float(atr.iloc[i]) if not pd.isna(atr.iloc[i]) else 0.0
            if atr_v <= 0:
                continue
            min_gap = atr_v * min_gap_atr

            # Bullish FVG
            if c3l - c1h >= min_gap:
                top, bottom = c3l, c1h
                later = df.iloc[i + 1:]
                filled = bool((later["Low"] <= bottom).any()) if len(later) else False
                out.append({
                    "type": "bull", "top": top, "bottom": bottom,
                    "mid": (top + bottom) / 2.0, "idx": i,
                    "filled": filled,
                    "gap_pct": (top - bottom) / price_now * 100 if price_now else 0,
                })

            # Bearish FVG
            if c1l - c3h >= min_gap:
                top, bottom = c1l, c3h
                later = df.iloc[i + 1:]
                filled = bool((later["High"] >= top).any()) if len(later) else False
                out.append({
                    "type": "bear", "top": top, "bottom": bottom,
                    "mid": (top + bottom) / 2.0, "idx": i,
                    "filled": filled,
                    "gap_pct": (top - bottom) / price_now * 100 if price_now else 0,
                })
    except Exception as e:
        logger.debug(f"fvg_zones: {e}")

    # Sort by distance from current price (nearest first)
    price_now = float(df["Close"].astype(float).iloc[-1])
    out.sort(key=lambda z: abs(z["mid"] - price_now))
    return out


# ─────────────────────────────────────────────────────────────
# MAIN INDICATOR COMPUTATION
# ─────────────────────────────────────────────────────────────
def calculate_pro_indicators(df: pd.DataFrame, tf: str,
                             symbol: str = "") -> Optional[dict]:
    if df is None or len(df) < 60:
        return None

    try:
        close_check = df["Close"].astype(float)
        nunique = close_check.nunique()
        if nunique < 5:
            logger.warning(
                f"[{symbol or '?'}] {tf}: frozen kline data detected "
                f"(nunique(Close)={nunique}) — skipping indicators"
            )
            return None
    except Exception as e:
        logger.debug(f"frozen-data check failed {symbol} {tf}: {e}")

    close = df["Close"].astype(float)
    high = df["High"].astype(float)
    low = df["Low"].astype(float)
    open_ = df["Open"].astype(float)
    volume = df["Volume"].astype(float) if "Volume" in df.columns else pd.Series(1.0, index=df.index)

    reg = _detect_regime(df)
    regime = reg["regime"]
    rcfg = _regime_config(regime)
    rsi_period = int(rcfg["rsi_period"])

    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(rsi_period).mean()
    loss = -delta.where(delta < 0, 0).rolling(rsi_period).mean().replace(0, 1e-9)
    rsi = 100 - (100 / (1 + (gain / loss)))

    rsi_min = rsi.rolling(rsi_period).min()
    rsi_max = rsi.rolling(rsi_period).max()
    stoch_rsi = ((rsi - rsi_min) / (rsi_max - rsi_min).replace(0, 1) * 100).fillna(50)

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    sma20 = close.rolling(20).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd_hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()

    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()
    atr10 = tr.rolling(10).mean()

    plus_dm = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    tr_s = tr.rolling(14).mean()
    plus_di = 100 * (plus_dm.rolling(14).mean() / tr_s)
    minus_di = 100 * (minus_dm.rolling(14).mean() / tr_s)
    dx = (100 * (plus_di - minus_di).abs() /
          (plus_di + minus_di).replace(0, 1e-9)).fillna(0)
    adx = dx.rolling(14).mean()

    obv = (np.sign(delta.fillna(0)) * volume).fillna(0).cumsum()
    obv_bullish = obv > obv.rolling(20).mean()
    vol_confirm = volume > volume.rolling(20).mean() * 1.1
    vol_mean = volume.rolling(20).mean()
    vol_std = volume.rolling(20).std()
    vol_z = (volume - vol_mean) / vol_std.replace(0, 1e-9)
    obv_slope = obv.diff(5)

    std20 = close.rolling(20).std()
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_width = (bb_upper - bb_lower) / sma20.replace(0, 1e-9)
    kc_upper = ema20 + 2 * atr10
    kc_lower = ema20 - 2 * atr10

    dc_high      = high.rolling(20).max()
    dc_low       = low.rolling(20).min()
    dc_high_slow = high.rolling(120).max()
    dc_low_slow  = low.rolling(120).min()

    tp = (high + low + close) / 3
    sma_tp = tp.rolling(20).mean()
    mad = tp.rolling(20).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    cci = (tp - sma_tp) / (0.015 * mad.replace(0, 1e-9))

    mf = tp * volume
    pos_mf = mf.where(tp > tp.shift(), 0).rolling(14).sum()
    neg_mf = mf.where(tp < tp.shift(), 0).rolling(14).sum()
    mfi = 100 - (100 / (1 + pos_mf / neg_mf.replace(0, 1e-9)))

    hh14 = high.rolling(14).max()
    ll14 = low.rolling(14).min()
    willr = -100 * (hh14 - close) / (hh14 - ll14).replace(0, 1e-9)
    roc = close.pct_change(10) * 100
    ema20_slope = ema20.diff()
    atr_ratio = atr / atr.rolling(100).mean().replace(0, 1e-9)

    price = _safe_last(close)
    atr_v = _safe_last(atr)

    cvd = _cvd_features(df)
    obs = _order_blocks(df)
    vwap_v = _session_vwap(df)
    st = _supertrend(df)

    # ═══════════════════════════════════════════════════════════
    #  REV 3.6 — FLAG-GATED MODERN INDICATORS
    # ═══════════════════════════════════════════════════════════
    if bool(_cc_get("use_taker_volume", False)):
        taker = _taker_features(df)
    else:
        taker = {"taker_buy_ratio": 0.5, "taker_ratio_z": 0.0}

    if bool(_cc_get("use_volume_profile", False)):
        vprofile = _volume_profile(df)
    else:
        vprofile = {"poc": 0.0, "vah": 0.0, "val": 0.0, "poc_dist_pct": 0.0}

    if bool(_cc_get("use_anchored_vwap", False)):
        avwap = _anchored_vwap(df)
    else:
        avwap = {"anchored_vwap": 0.0, "avwap_dist_pct": 0.0}

    # ═══════════════════════════════════════════════════════════
    #  REV 3.7 — LIQUIDITY SWEEP + FVG ZONES (NEW)
    # ═══════════════════════════════════════════════════════════
    if bool(_cc_get("use_liquidity_sweep", True)):
        sweep = _liquidity_sweep(df)
    else:
        sweep = {
            "sweep_bull": False, "sweep_bear": False,
            "sweep_bull_low": 0.0, "sweep_bear_high": 0.0,
            "sweep_strength": 0.0,
        }

    if bool(_cc_get("use_fvg_zones", True)):
        fvg_zones = _fvg_zones(df)
    else:
        fvg_zones = []

    fvg_nearest_bull = next(
        (z for z in fvg_zones if z["type"] == "bull" and not z["filled"]), None
    )
    fvg_nearest_bear = next(
        (z for z in fvg_zones if z["type"] == "bear" and not z["filled"]), None
    )

    # REV 3.6 — dead `liq` stub removed.
    ob_imbalance = {"bid_vol": 0.0, "ask_vol": 0.0, "imbalance": 0.0, "bias": "BALANCED"}
    funding_z = {"funding_now": 0.0, "funding_mean": 0.0, "funding_z": 0.0}

    if symbol:
        try:
            _funding_z_enabled = bool(_cc_get("use_funding_z", False))
        except Exception:
            _funding_z_enabled = False
        if _funding_z_enabled:
            funding_z = get_funding_rate_z(symbol)

    if symbol:
        try:
            ob_imbalance = get_order_book_imbalance(symbol)
        except Exception as _obe:
            logger.debug(f"OB imbalance {symbol}: {_obe}")

    return {
        "price": price, "atr": atr_v,
        "atr_pct": (atr_v / price * 100.0) if price > 0 else 0.8,
        "adx": _safe_last(adx), "rsi": _safe_last(rsi),
        "stoch_rsi": _safe_last(stoch_rsi, 50.0),
        "rsi_period": rsi_period,
        "ema20": _safe_last(ema20), "ema50": _safe_last(ema50),
        "sma20": _safe_last(sma20), "macd_hist": _safe_last(macd_hist),
        "obv_bullish": bool(obv_bullish.iloc[-1]) if len(obv_bullish) else False,
        "vol_confirm": bool(vol_confirm.iloc[-1]) if len(vol_confirm) else False,
        "vwap": vwap_v,
        "bb_upper": _safe_last(bb_upper), "bb_lower": _safe_last(bb_lower),
        "bb_width": _safe_last(bb_width),
        "kc_upper": _safe_last(kc_upper), "kc_lower": _safe_last(kc_lower),

        "dc_high":      _safe_last(dc_high),
        "dc_low":       _safe_last(dc_low),
        "dc_high_slow": _safe_last(dc_high_slow),
        "dc_low_slow":  _safe_last(dc_low_slow),

        "cci": _safe_last(cci), "mfi": _safe_last(mfi, 50.0),
        "willr": _safe_last(willr, -50.0), "roc": _safe_last(roc),
        "ema20_slope": _safe_last(ema20_slope),
        "atr_ratio": _safe_last(atr_ratio, 1.0),
        "vol_z": _safe_last(vol_z), "obv_slope": _safe_last(obv_slope),
        "open": _safe_last(open_), "high": _safe_last(high),
        "low": _safe_last(low), "close": price, "tf": tf,
        "divergence": _compute_divergence(close, rsi),

        "cvd_slope": cvd["cvd_slope"], "cvd_z": cvd["cvd_z"],
        "delta_z": cvd["delta_z"], "cvd_div": cvd["cvd_div"],
        "cvd_rising": cvd["cvd_rising"],
        "regime": regime, "hurst": reg["hurst"], "vol_pct": reg["vol_pct"],
        "momentum_pct": reg.get("momentum_pct", 0.0),
        "ob_bull": obs["ob_bull"], "ob_bear": obs["ob_bear"],
        "fvg_bull": obs["fvg_bull"], "fvg_bear": obs["fvg_bear"],

        "st_trend": st["st_trend"],
        "st_value": st["st_value"],
        "st_flips": st["st_flips"],

        # REV 3.6 — legacy liquidation keys (always 0/0/BALANCED).
        "long_liq_usd": 0.0,
        "short_liq_usd": 0.0,
        "liq_bias": "BALANCED",

        "ob_imbalance": ob_imbalance["imbalance"],
        "ob_bias": ob_imbalance["bias"],
        "ob_bid_vol": ob_imbalance["bid_vol"],
        "ob_ask_vol": ob_imbalance["ask_vol"],
        "killzone": _current_killzone(),

        "taker_buy_ratio": taker["taker_buy_ratio"],
        "taker_ratio_z": taker["taker_ratio_z"],
        "poc": vprofile["poc"],
        "vah": vprofile["vah"],
        "val": vprofile["val"],
        "poc_dist_pct": vprofile["poc_dist_pct"],
        "anchored_vwap": avwap["anchored_vwap"],
        "avwap_dist_pct": avwap["avwap_dist_pct"],
        "funding_now": funding_z["funding_now"],
        "funding_mean": funding_z["funding_mean"],
        "funding_z": funding_z["funding_z"],

        # ═══════════════════════════════════════════════════════
        #  REV 3.7 — LIQUIDITY SWEEP OUTPUT (NEW)
        # ═══════════════════════════════════════════════════════
        "sweep_bull": sweep["sweep_bull"],
        "sweep_bear": sweep["sweep_bear"],
        "sweep_bull_low": sweep["sweep_bull_low"],
        "sweep_bear_high": sweep["sweep_bear_high"],
        "sweep_strength": sweep["sweep_strength"],

        # ═══════════════════════════════════════════════════════
        #  REV 3.7 — FVG ZONES OUTPUT (NEW)
        # ═══════════════════════════════════════════════════════
        "fvg_zone_bull_top": fvg_nearest_bull["top"] if fvg_nearest_bull else 0.0,
        "fvg_zone_bull_bottom": fvg_nearest_bull["bottom"] if fvg_nearest_bull else 0.0,
        "fvg_zone_bear_top": fvg_nearest_bear["top"] if fvg_nearest_bear else 0.0,
        "fvg_zone_bear_bottom": fvg_nearest_bear["bottom"] if fvg_nearest_bear else 0.0,
        "fvg_zone_count_unfilled": sum(1 for z in fvg_zones if not z["filled"]),
    }


# ─────────────────────────────────────────────────────────────
# SHORT-TERM TREND FILTER (5m EMA) — 3-LAYER
# REV 1.4.14 (2026-10-01)
#   L1 STATE:   p > e9 > e21 (basic alignment)
#   L2 EVENT:   EMA20 freshly reclaimed in last 3 bars
#   L3 VOLUME:  last bar volume >= 20-bar average
# Fail-open on API/computation error.
# ─────────────────────────────────────────────────────────────
_trend_cache: dict[str, tuple[bool, str, float]] = {}
_trend_lock = threading.Lock()
_TREND_TTL = 30


def check_short_term_trend(pair: str, side: str) -> tuple[bool, str]:
    """
    3-layer 5m trend filter. BUY and SELL use symmetric logic.

    Returns (ok, reason). ok=False → caller must reject.
    """
    now = time.time()
    key = f"{pair}:{side}"
    with _trend_lock:
        h = _trend_cache.get(key)
        if h and now - h[2] < _TREND_TTL:
            return h[0], h[1]

    result = (True, "no check")

    if _get_klines is None:
        result = (True, "klines_unavail_failopen")
    else:
        try:
            df = _get_klines(pair, "5m", limit=60)
            if df is None or len(df) < 20:
                result = (True, "5m_data_short_failopen")
            else:
                df = df.iloc[:-1]      # drop unclosed bar
                close = df["Close"].astype(float)
                vol = (df["Volume"].astype(float)
                       if "Volume" in df.columns else None)

                e9  = close.ewm(span=9).mean()
                e21 = close.ewm(span=21).mean()
                e20 = close.ewm(span=20, adjust=False).mean()

                p_now   = float(close.iloc[-1])
                e9_now  = float(e9.iloc[-1])
                e21_now = float(e21.iloc[-1])

                # ── L1: STATE ──
                l1_ok = False
                l1_reason = "L1_skip"
                if side == "BUY":
                    l1_ok = p_now > e9_now > e21_now
                    l1_reason = "L1_ok" if l1_ok else "L1_no_uptrend"
                elif side == "SELL":
                    l1_ok = p_now < e9_now < e21_now
                    l1_reason = "L1_ok" if l1_ok else "L1_no_downtrend"

                if l1_reason == "L1_skip":
                    result = (True, "unknown_side")
                elif not l1_ok:
                    result = (False, l1_reason)
                else:
                    # ── L2: EVENT (fresh EMA20 reclaim) ──
                    rc = close.iloc[-3:].values
                    re = e20.iloc[-3:].values
                    pc = close.iloc[-6:-3].values
                    pe = e20.iloc[-6:-3].values

                    if side == "BUY":
                        above_recent = [c > e for c, e in zip(rc, re)]
                        was_below = any(c < e for c, e in zip(pc, pe))
                        l2_ok = (bool(above_recent[-1]) and
                                 any(above_recent) and was_below)
                        l2_reason = ("L2_fresh_reclaim" if l2_ok
                                     else "L2_no_fresh_reclaim")
                    else:
                        below_recent = [c < e for c, e in zip(rc, re)]
                        was_above = any(c > e for c, e in zip(pc, pe))
                        l2_ok = (bool(below_recent[-1]) and
                                 any(below_recent) and was_above)
                        l2_reason = ("L2_fresh_reject" if l2_ok
                                     else "L2_no_fresh_reject")

                    if not l2_ok:
                        result = (False, l2_reason)
                    else:
                        # ── L3: VOLUME ──
                        if vol is not None and len(vol) >= 20:
                            v_avg = float(vol.rolling(20).mean().iloc[-1])
                            v_now = float(vol.iloc[-1])
                            if v_avg > 0:
                                v_ratio = v_now / v_avg
                                l3_ok = v_now >= v_avg
                                if l3_ok:
                                    result = (
                                        True,
                                        f"{l1_reason}|{l2_reason}|L3_vol_{v_ratio:.2f}x"
                                    )
                                else:
                                    result = (
                                        False,
                                        f"L3_vol_weak_{v_ratio:.2f}x"
                                    )
                            else:
                                result = (
                                    True,
                                    f"{l1_reason}|{l2_reason}|L3_na"
                                )
                        else:
                            result = (
                                True,
                                f"{l1_reason}|{l2_reason}|L3_na"
                            )
        except Exception as e:
            logger.debug(f"5m filter {pair}: {type(e).__name__}: {e}")
            result = (True, f"5m_err_{type(e).__name__}_failopen")

    with _trend_lock:
        _trend_cache[key] = (result[0], result[1], now)
        if len(_trend_cache) > 200:
            cutoff = now - _TREND_TTL * 4
            for k in [k for k, v in _trend_cache.items() if v[2] < cutoff]:
                _trend_cache.pop(k, None)
    return result