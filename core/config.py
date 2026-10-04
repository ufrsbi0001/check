"""
config.py — Environment / infrastructure configuration ONLY.

REV 2.0.1 (2026-10-03) — PROXY HARDENING:
  ✅ _ATTR_TO_CC_KEY renamed to _PROXIED_TRADING_ATTRS and expanded
     to an EXPLICIT full map of every trading attribute this module
     proxies to core.config_center.GLOBAL. This is now the single
     source of truth for the proxy surface.
  ✅ __getattr__ now raises a CLEAR AttributeError on contract
     violation (missing CC key) instead of an opaque failure at the
     first read site deep in the codebase.
  ✅ public_dict() no longer routes through self.<attr> proxy chain —
     it reads _PROXIED_TRADING_ATTRS directly from config_center.GLOBAL.
     No duplicate attribute list to keep in sync.
  ✅ Docstring documents the proxy contract for future maintainers.

REV 2.0.0 (2026-10-02) — CONFIG UNIFICATION:
  ✅ Trading parameters MOVED OUT to core/config_center.py (sole
     source of truth). This file now holds only:
       • secrets (API keys, web token, flask key, telegram)
       • paths (csv/log/state files, data dir)
       • deployment flags (demo_mode, testnet, dry_run, host, port)
       • coin universe resolution (coins_config / BOT_COINS)
       • API pagination settings
       • log level
  ✅ Backward compat: Config dataclass exposes moved fields as
     read-through proxies (via __getattr__) → core.config_center.GLOBAL.
     So existing code `CONFIG.leverage` continues to work, but the
     authoritative value lives in config_center.
  ✅ REMOVED: the old min_confidence sync block. config_center now
     reads MIN_CONFIDENCE / CC_MIN_CONFIDENCE itself.
  ✅ Legacy .env names (LEVERAGE, RISK_PERCENT, MIN_CONFIDENCE, ...)
     still work — they're mapped inside config_center._LEGACY_ENV_MAP.

Design goals:
  • Frozen dataclass — immutable after load, thread-safe reads
  • Fail-fast validation at import (env-level only)
  • public_dict() for /api/config (never leaks secrets)
  • All file paths anchored to DATA_DIR
  • Coin universe sourced EXCLUSIVELY from coins_config.py or .env
  • NO trading param duplicated here — proxy only, source in CC
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Tuple

from dotenv import load_dotenv

load_dotenv()

__all__ = [
    "Config",
    "ConfigError",
    "CONFIG",
    "load_config",
    "print_config_banner",
]


# ─────────────────────────────────────────────────────────────
# ENV PARSING HELPERS
# ─────────────────────────────────────────────────────────────
class ConfigError(ValueError):
    """Raised when an env var is present but malformed, or fails sanity checks."""


_TRUTHY = frozenset({"true", "1", "yes", "on", "y"})
_FALSY  = frozenset({"false", "0", "no", "off", "n"})


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    v = raw.strip().lower()
    if v in _TRUTHY:
        return True
    if v in _FALSY:
        return False
    print(
        f"[config] WARNING: {key}={raw!r} is not a recognized boolean; "
        f"using default {default}. Accepted: "
        f"true/1/yes/on/y or false/0/no/off/n.",
        file=sys.stderr,
    )
    return default


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as e:
        raise ConfigError(f"{key}={raw!r} is not a valid integer") from e


def _str(key: str, default: str = "") -> str:
    raw = os.getenv(key)
    if raw is None:
        return default
    return raw.strip()


def _csv_upper(key: str) -> Tuple[str, ...]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return ()
    return tuple(x.strip().upper() for x in raw.split(",") if x.strip())


def _csv_str(key: str, default: str) -> Tuple[str, ...]:
    raw = os.getenv(key, default).strip()
    if not raw:
        return (default,)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _clean_key(s: str) -> str:
    if not s:
        return ""
    return s.strip().strip('"').strip("'")


def _strip_usdt(sym: str) -> str:
    return sym[:-4] if sym.endswith("USDT") else sym


# ─────────────────────────────────────────────────────────────
# PROXY CONTRACT — trading params owned by config_center
# ─────────────────────────────────────────────────────────────
# Each entry maps a legacy CONFIG.<attr> name to its authoritative
# key inside core.config_center.GLOBAL.
#
# ►►► THIS DICT IS THE SINGLE SOURCE OF TRUTH FOR THE PROXY SURFACE ◄◄◄
#
# Adding a new trading param?
#   1. Add it to core.config_center.GLOBAL (with default + validation).
#   2. Add its mapping here if you want legacy `CONFIG.<name>` access.
#   3. Do NOT reintroduce a dataclass field — that reopens the
#      duplication wound REV 2.0.0 closed.
#
# Removing a param?
#   1. Delete it from config_center.GLOBAL.
#   2. Delete its entry here.
#   3. Fix any call sites still using CONFIG.<name> → use CC.get().
_PROXIED_TRADING_ATTRS: dict[str, str] = {
    # Legacy attr name  →  config_center.GLOBAL key
    "leverage":                    "leverage",
    "risk_percent":                "risk_percent",
    "max_open_positions":          "max_open_positions",
    "max_same_side_positions":     "max_same_side_positions",
    "max_daily_loss_trades":       "max_daily_loss_trades",
    "max_daily_drawdown_percent":  "max_daily_drawdown_percent",
    "max_account_drawdown":        "max_account_drawdown",
    "max_hold_minutes":            "hold_minutes",  # renamed in CC
    "min_confidence":              "min_confidence",
    "min_adx":                     "min_adx",
    "require_htf_agreement":       "require_htf_agreement",
    "use_5m_trend_filter":         "use_5m_trend_filter",
    "max_trades_per_coin_per_day": "max_trades_per_coin_per_day",
    "htf_align_relaxed":           "htf_align_relaxed",
    "cooldown_after_sl_min":       "cooldown_after_sl_min",
    "cooldown_after_tp_min":       "cooldown_after_tp_min",
    "partial_close_usdt":          "partial_close_usdt",
    "use_taker_volume":            "use_taker_volume",
    "use_volume_profile":          "use_volume_profile",
    "use_anchored_vwap":           "use_anchored_vwap",
    "use_funding_z":               "use_funding_z",
    "use_mtf_confluence":          "use_mtf_confluence",
    "kz_bypass":                   "kz_bypass",
    "use_spread_filter":           "use_spread_filter",
    "max_spread_pct":              "max_spread_pct",
    "max_spread_trend":            "max_spread_trend",
    "max_spread_range":            "max_spread_range",
    "max_spread_volatility":       "max_spread_volatility",
}


# ─────────────────────────────────────────────────────────────
# CONFIG DATACLASS — ENV FIELDS ONLY
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Config:
    # ── Credentials ─────────────────────────────────────────
    api_key: str
    api_secret: str
    web_token: str
    flask_secret_key: str

    # ── Trading environment ─────────────────────────────────
    demo_mode: bool
    testnet: bool
    dry_run: bool

    # ── Symbols ─────────────────────────────────────────────
    bot_coins: Tuple[str, ...]
    blacklisted_coins: FrozenSet[str]

    # ── Telegram ────────────────────────────────────────────
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str

    # ── Web server ──────────────────────────────────────────
    host: str
    port: int
    allowed_origins: Tuple[str, ...]

    # ── API pagination ──────────────────────────────────────
    history_lookback_days: int
    history_limit: int
    income_page_size: int
    max_income_pages: int
    fills_page_size: int
    max_fills_pages: int

    # ── File paths ──────────────────────────────────────────
    csv_file: str
    csv_exit_file: str
    loss_file: str
    active_trades_file: str
    cooldown_file: str
    coin_rotation_file: str
    pause_file: str

    log_level: str

    # ─────────────────────────────────────────────────────────
    # PROXY: trading params → config_center.GLOBAL
    # ─────────────────────────────────────────────────────────
    # Only called when normal attribute lookup fails, i.e. for
    # legacy reads like CONFIG.leverage that no longer have a
    # dataclass field. Unknown names raise a clean AttributeError;
    # known-but-missing CC keys raise a contract-violation error.
    # ─────────────────────────────────────────────────────────
    def __getattr__(self, name: str):
        cc_key = _PROXIED_TRADING_ATTRS.get(name)
        if cc_key is None:
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}. "
                f"If this is a trading param, add it to "
                f"config.py:_PROXIED_TRADING_ATTRS AND "
                f"config_center.GLOBAL."
            )
        # Lazy import — avoids circular import at module load.
        from core import config_center as _cc
        if cc_key not in _cc.GLOBAL:
            raise AttributeError(
                f"CONFIG.{name} → config_center.GLOBAL[{cc_key!r}] is "
                f"MISSING. This is a proxy-contract violation: the key "
                f"is advertised in _PROXIED_TRADING_ATTRS but absent "
                f"from config_center. Fix config_center defaults."
            )
        return _cc.GLOBAL[cc_key]

    # ── Derived ─────────────────────────────────────────────
    @property
    def log_dir(self) -> str:
        return str(Path(self.csv_file).parent / "Logs")

    @property
    def data_dir(self) -> Path:
        return Path(self.csv_file).parent

    @property
    def keys_present(self) -> bool:
        return bool(self.api_key and self.api_secret)

    @property
    def token_present(self) -> bool:
        return bool(self.web_token)

    @property
    def trading_mode_label(self) -> str:
        if self.demo_mode:
            return "DEMO (demo.binance.com)"
        if self.testnet:
            return "TESTNET (testnet.binance.vision)"
        return "MAINNET — REAL MONEY"

    def public_dict(self) -> dict:
        """Safe for /api/config — never leaks secrets.

        Trading params are read DIRECTLY from config_center.GLOBAL via
        _PROXIED_TRADING_ATTRS, not via self.<attr> proxy chain. This
        keeps the mapping in exactly one place and avoids cascading
        __getattr__ calls.
        """
        from core import config_center as _cc

        missing = [k for k in _PROXIED_TRADING_ATTRS.values()
                   if k not in _cc.GLOBAL]
        if missing:
            raise ConfigError(
                "public_dict(): config_center.GLOBAL is missing proxied "
                "keys: " + ", ".join(missing)
            )

        out = {
            # ── env ──
            "demo_mode": self.demo_mode,
            "testnet": self.testnet,
            "dry_run": self.dry_run,
            "trading_mode": self.trading_mode_label,
            "coins": list(self.bot_coins),
            "coin_count": len(self.bot_coins),
            "keys_present": self.keys_present,
            "token_present": self.token_present,
            "history_lookback_days": self.history_lookback_days,
        }
        # ── proxied trading params ──
        for attr, cc_key in _PROXIED_TRADING_ATTRS.items():
            out[attr] = _cc.GLOBAL[cc_key]
        return out


# ─────────────────────────────────────────────────────────────
# LOADER
# ─────────────────────────────────────────────────────────────
def _resolve_base_dir() -> Path:
    raw = os.getenv("DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else (Path.cwd() / "data")
    try:
        base.mkdir(parents=True, exist_ok=True)
        (base / "Logs").mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ConfigError(
            f"Failed to prepare DATA_DIR at {base!s}: "
            f"{type(e).__name__}: {e}"
        ) from e
    return base


def _resolve_coins(blacklist: FrozenSet[str]) -> Tuple[str, ...]:
    """
    REV 1.4.1 — Fail-fast coin resolution. NO silent hardcoded fallback.

    Priority:
      1. coins_config.enabled_coins()   ← primary source of truth
      2. .env BOT_COINS                  ← explicit override / fallback

    If BOTH fail:
      → raises ConfigError with clear remediation guidance.
    """
    # ── Priority 1: coins_config ──
    try:
        from core.coins_config import enabled_coins
        enabled = tuple(
            sym for sym in enabled_coins()
            if _strip_usdt(sym) not in blacklist
        )
        if enabled:
            return enabled
        print(
            "[config] coins_config loaded but produced no eligible coins "
            "(all disabled or blacklisted) — falling back to BOT_COINS.",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"[config] coins_config load failed: {e}",
            file=sys.stderr,
        )

    # ── Priority 2: .env BOT_COINS ──
    raw = os.getenv("BOT_COINS", "").strip()
    if raw:
        parsed = tuple(
            (c if c.endswith("USDT") else c + "USDT")
            for c in (x.strip().upper() for x in raw.split(",") if x.strip())
        )
        filtered = tuple(c for c in parsed if _strip_usdt(c) not in blacklist)
        if filtered:
            return filtered
        print(
            "[config] BOT_COINS set in .env but all entries blacklisted/empty.",
            file=sys.stderr,
        )

    # ── No silent fallback — fail loud ──
    raise ConfigError(
        "No coins resolved. Fix ONE of the following:\n"
        "  • Ensure coins_config.py loads correctly and at least one\n"
        "    coins/*.py has ENABLED = True, OR\n"
        "  • Set BOT_COINS in .env (comma-separated, e.g.\n"
        "    BOT_COINS=BTCUSDT,ETHUSDT,SOLUSDT)\n"
        "\n"
        "Note: REV 1.4.1 removed the hardcoded _DEFAULT_COINS fallback\n"
        "because it silently traded wrong per-coin SL/TP values when\n"
        "coins_config failed to load."
    )


def load_config() -> Config:
    base = _resolve_base_dir()

    blacklist = frozenset(_csv_upper("BLACKLISTED_COINS"))
    coins = _resolve_coins(blacklist)

    cfg = Config(
        # ── Credentials ──
        api_key=_clean_key(os.getenv("BINANCE_API_KEY") or os.getenv("API_KEY") or ""),
        api_secret=_clean_key(
            os.getenv("BINANCE_API_SECRET") or os.getenv("SECRET_KEY") or ""
        ),
        web_token=_str("WEB_TOKEN"),
        flask_secret_key=_str("FLASK_SECRET_KEY") or os.urandom(24).hex(),

        # ── Deployment flags ──
        demo_mode=_bool("DEMO_MODE", True),
        testnet=_bool("TESTNET", False),
        dry_run=_bool("DRY_RUN", False),

        # ── Universe ──
        bot_coins=coins,
        blacklisted_coins=blacklist,

        # ── Telegram ──
        telegram_enabled=_bool("TELEGRAM_ENABLED", False),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),

        # ── Web server ──
        host=_str("HOST", "0.0.0.0"),
        port=_int("PORT", 5000),
        allowed_origins=_csv_str("ALLOWED_ORIGINS", "*"),

        # ── API pagination ──
        history_lookback_days=_int("HISTORY_LOOKBACK_DAYS", 30),
        history_limit=_int("HISTORY_LIMIT", 500),
        income_page_size=_int("INCOME_PAGE_SIZE", 1000),
        max_income_pages=_int("MAX_INCOME_PAGES", 20),
        fills_page_size=_int("FILLS_PAGE_SIZE", 1000),
        max_fills_pages=_int("MAX_FILLS_PAGES", 10),

        # ── Paths ──
        csv_file=str(base / "trading_log.csv"),
        csv_exit_file=str(base / "trades_exit.csv"),
        loss_file=str(base / "daily_tracker.json"),
        active_trades_file=str(base / "active_trades.json"),
        cooldown_file=str(base / "cooldowns.json"),
        coin_rotation_file=str(base / "coin_rotation.json"),
        pause_file=str(base / "pause.txt"),

        # ── Logging ──
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )

    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    """Env-level validation ONLY. Trading params are validated in
    core.config_center._validate()."""
    errors: list[str] = []

    if not 1 <= cfg.port <= 65535:
        errors.append(f"PORT={cfg.port} — must be 1–65535")
    if cfg.history_lookback_days < 1:
        errors.append("HISTORY_LOOKBACK_DAYS must be ≥1")
    if cfg.history_limit < 1:
        errors.append("HISTORY_LIMIT must be ≥1")
    if cfg.income_page_size < 1:
        errors.append("INCOME_PAGE_SIZE must be ≥1")
    if cfg.max_income_pages < 1:
        errors.append("MAX_INCOME_PAGES must be ≥1")
    if cfg.fills_page_size < 1:
        errors.append("FILLS_PAGE_SIZE must be ≥1")
    if cfg.max_fills_pages < 1:
        errors.append("MAX_FILLS_PAGES must be ≥1")

    if errors:
        raise ConfigError(
            "Configuration validation failed:\n  • " + "\n  • ".join(errors)
        )


# ─────────────────────────────────────────────────────────────
# SINGLETON
# ─────────────────────────────────────────────────────────────
CONFIG: Config = load_config()


# ─────────────────────────────────────────────────────────────
# BANNER HELPERS
# ─────────────────────────────────────────────────────────────
def _partial_close_label() -> str:
    pc = CONFIG.partial_close_usdt   # proxied → config_center
    if pc == 0:
        return "DISABLED (TP1/TP2 mode)"
    return f"${pc:.2f} profit (70% lock)"


def _spread_label() -> str:
    if not CONFIG.use_spread_filter:
        return "OFF"
    return (
        f"ON (global {CONFIG.max_spread_pct:.2f}% | "
        f"trend {CONFIG.max_spread_trend:.2f}% | "
        f"range {CONFIG.max_spread_range:.2f}% | "
        f"vol {CONFIG.max_spread_volatility:.2f}%)"
    )


def _trend_filter_label() -> str:
    if not CONFIG.use_5m_trend_filter:
        return "OFF"
    return "ON (trend-following only — counter-trend bypass)"


def print_config_banner() -> None:
    sep = "=" * 60
    coins_str = f"({', '.join(CONFIG.bot_coins)})" if CONFIG.bot_coins else ""
    lines = [
        sep,
        "  TRADING DESK · CONFIG LOADED",
        sep,
        f"  Mode:           {CONFIG.trading_mode_label}",
        f"  Dry run:        {'YES (no orders sent)' if CONFIG.dry_run else 'no'}",
        f"  Coins:          {len(CONFIG.bot_coins)} {coins_str}",
        f"  Leverage:       {CONFIG.leverage}×",
        f"  Risk / trade:   {CONFIG.risk_percent}%",
        f"  Max open:       {CONFIG.max_open_positions} "
        f"(per-side: {CONFIG.max_same_side_positions})",
        f"  Daily cap:      {CONFIG.max_daily_loss_trades} losses / "
        f"{CONFIG.max_daily_drawdown_percent}% DD",
        f"  Partial close:  {_partial_close_label()}",
        f"  Min ADX:        {CONFIG.min_adx} "
        f"(fallback — runtime per-family floors printed by future.py)",
        f"  Min confidence: {CONFIG.min_confidence}",
        f"  Max hold:       {CONFIG.max_hold_minutes} min",
        f"  History window: {CONFIG.history_lookback_days} days",
        f"  Modern flags:   taker={CONFIG.use_taker_volume} "
        f"vp={CONFIG.use_volume_profile} "
        f"avwap={CONFIG.use_anchored_vwap} "
        f"fz={CONFIG.use_funding_z} "
        f"mtf={CONFIG.use_mtf_confluence}",
        f"  5m trend filter: {_trend_filter_label()}",
        f"  HTF align:      "
        f"{'RELAXED (e20 OR price)' if CONFIG.htf_align_relaxed else 'STRICT (e20 AND price)'}",
        f"  Killzone mode:  "
        f"{'BYPASS (24/7)' if CONFIG.kz_bypass else 'ENFORCED (NY sessions only)'}",
        f"  Spread filter:  {_spread_label()}",
        f"  API keys:       {'set' if CONFIG.keys_present else 'MISSING'}",
        f"  WEB_TOKEN:      {'set' if CONFIG.token_present else 'MISSING'}",
        f"  Data dir:       {Path(CONFIG.csv_file).parent}",
        sep,
    ]
    for line in lines:
        print(line, file=sys.stdout)


if __name__ == "__main__":
    import json
    print_config_banner()
    print("\nPublic config (safe to expose to UI):")
    print(json.dumps(CONFIG.public_dict(), indent=2))