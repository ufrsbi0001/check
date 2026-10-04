# CHANGELOG

Generated from the `REV` headers inside each source file (original headers were left untouched).
Newest first. Details for each REV are still in that file's header.

## 2026-10-04 — post-P3 fixes (this session)
- `market/indicators.py` REV 3.7.1 — LIQUIDITY SWEEP DOUBLE-DROP FIX
  (`_liquidity_sweep` was dropping the in-progress bar a second time,
  evaluating the 2nd-to-last closed bar; sweeps now fire on the latest
  closed bar)
- `web/app.py` REV 1.5.3 — TRADE-MANAGER LIFECYCLE FIX
  (`bot_runner` finally-block now calls `request_stop()` before joining
  the trade-manager thread; previously the join always timed out and
  left the tm_thread alive across restarts, blocking the next Start
  click with a 429 and — worse — allowing two managers on one account)
- `web/app.py` REV 1.5.3 — removed dead module-level `stop_flag`
- `core/config.py` REV 2.0.2 — SECURE DEFAULT HOST
  (default was `0.0.0.0` — anyone on LAN could reach the dashboard;
  now `127.0.0.1`, opt-in to expose via `HOST=0.0.0.0` in `.env`)
- `core/client.py` REV 11.4 — HEDGE-MODE FAIL-FAST
  (`set_client_keys` / `startup_checks` now RAISE on HEDGE mode instead
  of logging and proceeding; previously the bot booted and every order
  failed with -4061)
- `future.py` REV 1.4.37 — DEAD `_cfg` CLEANUP
  (removed unused `_cfg = _i.get_trading_config()` in `main_loop`;
  saves one deepcopy of GLOBAL per startup)
- `scripts/test_multi.py` REV 1.1 — PATH BOOTSTRAP + CLOSED-ONLY DATA
  (script now works from any cwd; passes `df.iloc[:-1]` to indicators
  to match production behaviour)

## 2026-10-04 — patch set P1-P3 (previous delivery)
- manage.py: orphan-position watch; manager-loop errors logged at WARNING
- config_center.py: `max_fill_slippage_pct`, `orphan_watch_*` keys
- entry/exit/manage/repair: 9 silent `except: pass` around exchange calls now log
- tests/test_execution_paths.py, scripts/verify.py (comment-aware), pytest.ini, requirements.txt


## 2026-10-04
- `signals/decision_engine.py` REV 4.5 — RSI DIVERGENCE + FVG RETEST FILTERS
- `signals/decision_engine.py` REV 4.4 — LIQUIDITY SWEEP FILTER (retained)
- `signals/base.py` REV 23.8 — VWAP MEAN REVERSION OVERLAY
- `run.py` REV 1.2 — MUTUAL-EXCLUSION DOC
- `run.py` REV 1.1 — ENTRY-POINT DISAMBIGUATION
- `orders/repair.py` REV 1.7.3 — ADOPTED SL FROM CONFIG (Point 4)
- `orders/repair.py` REV 1.7.2 — CRITICAL ADOPTION + FAST-RECONCILE FIXES
- `orders/manage.py` REV 1.10.1 — FAST-RECONCILE + SESSION-LOCK FIXES
- `orders/exit.py` REV 1.7.1 — DETERMINISTIC EMERGENCY-CLOSE CID (C4)
- `orders/entry.py` REV 1.9.2 — CRITICAL DOUBLE-ENTRY + SIZING GUARD FIXES
- `orders/entry.py` REV 1.9.1 — FILTER-BUST RECOMPUTE + MINOR CLEANUP (retained).
- `market/indicators.py` REV 3.7 — LIQUIDITY SWEEP + FVG ZONES (NEW)
- `future.py` REV 1.4.36 — LOG BANNER SYNC TO 9-FILTER ENGINE
- `future.py` REV 1.4.35 — DEV-MODE ENTRY GUARD (Point 1) (retained).
- `future.py` REV 1.4.34 — OBSERVABILITY UPGRADE (Point 5) (retained).
- `future.py` REV 1.4.33 — ADOPTED SL FROM CONFIG (Point 4) (retained).
- `future.py` REV 1.4.32.1 — ADOPTION SL HARDENING (retained).
- `future.py` REV 1.4.32 — IMMEDIATE ADOPTION SL (C3) (retained).
- `core/config_center.py` REV 6.1 — RSI DIVERGENCE + FVG RETEST FILTERS
- `core/config_center.py` REV 6.0 — LIQUIDITY SWEEP + FVG + VWAP REVERSION
- `core/config_center.py` REV 5.9 — ADOPTED FALLBACK SL TO CONFIG (Point 4).
- `core/config_center.py` REV 5.8 — SPLIT OVERSIZE WARN/ABORT KEYS (C5).
- `core/coins_config.py` REV 23.4 — LOGGER COMPLETENESS + DOCSTRING CLARITY
- `core/client.py` REV 11.3 — CRITICAL-PATH TIMEOUT ISOLATION (A1)

## 2026-10-03
- `web/app.py` REV 1.5.2 — RATE-LIMIT RELAXATION FOR READS
- `web/app.py` REV 1.5.1 — ERROR VISIBILITY + CACHING (retained)
- `web/app.py` REV 1.5.0 — DEFENSIVE HARDENING (retained).
- `web/analytics.py` REV 1.9 — SHARED-SESSION SAFETY
- `web/analytics.py` REV 1.8 — PERF + CONFIG + ROBUSTNESS
- `signals/volatility.py` REV 23.3 — DEAD CODE CLEANUP
- `signals/trend.py` REV 23.3 — DEAD CODE CLEANUP
- `signals/router.py` REV 19.19 — RESOLUTION + LOGGING HARDENING
- `signals/range.py` REV 23.3 — DEAD CODE CLEANUP
- `signals/momentum.py` REV 23.3 — DEAD CODE CLEANUP
- `signals/decision_engine.py` REV 4.3 — CORRECTNESS HARDENING (retained)
- `signals/base.py` REV 23.7 — RANGE-BOUNDS FIELD FIX (retained)
- `signals/base.py` REV 23.6 — RANGE-BOUNDS + ZERO-COERCION FIX
- `orders/utils.py` REV 23.1 — DEAD CONSTANT CLEANUP + SL SELECTION CLARITY
- `orders/repair.py` REV 1.7.1 — ADOPTED ORPHAN FALLBACK SL (retained).
- `orders/repair.py` REV 1.7.0 — ORPHAN ADOPTION + FAIL-SAFE RECONCILE (retained).
- `orders/repair.py` REV 1.6.1 — TP QTY NORMALIZATION FIX (retained).
- `orders/manage.py` REV 1.9.1 — DEFENSIVE HARDENING (retained).
- `orders/manage.py` REV 1.10.0 — RACE-SAFE SL LIFECYCLE (retained).
- `orders/exit.py` REV 1.7.0 — FAILURE-PATH HARDENING (retained).
- `orders/exit.py` REV 1.6.1 — CORRECTNESS HARDENING (retained).
- `orders/entry.py` REV 1.9.0 — HARDENING PASS (retained).
- `orders/entry.py` REV 1.8.1 — ZERO-COERCION FIX (retained).
- `orders/__init__.py` REV 23.1 — LIVE BACKWARD-COMPAT FOR DEAD CONFIG SNAPSHOTS
- `market/indicators.py` REV 3.6 — FLAG GATING + DIV-BY-ZERO + DEAD STUB CLEANUP
- `future.py` REV 1.4.31 — ADOPTION + LOCK FINALIZATION (retained).
- `future.py` REV 1.4.30 — DD KILL-SWITCH + ORPHAN ADOPTION (retained).
- `future.py` REV 1.4.29 — SNAPSHOT-TO-LIVE FIX (retained).
- `core/state.py` REV 1.3.19 — CONCURRENCY + FAIL-CLOSED HARDENING
- `core/state.py` REV 1.3.18 — FAIL-FAST SAFETY-KEY VERIFICATION (retained).
- `core/config_center.py` REV 5.7 — DOUBLE-BOOTSTRAP GUARD (retained).
- `core/config_center.py` REV 5.6 — RUNTIME SAFETY + ENV PRIORITY HARDENING (retained).
- `core/config.py` REV 2.0.1 — PROXY HARDENING
- `core/coins_config.py` REV 23.3 — CIRCULAR IMPORT FIX (retained)
- `core/coins_config.py` REV 23.2 — FALLBACK CONSOLIDATION (retained).
- `core/client.py` REV 11.2 — EXECUTOR POOL HARDENING (retained).
- `core/client.py` REV 11.1 — HARDENING PASS (retained).
- `core/client.py` REV 11.0 — KLINES CACHE + LOCK HYGIENE (retained).

## 2026-10-02
- `web/app.py` REV 1.4.2 — DAILY LOSS DASHBOARD WIRING.
- `web/app.py` REV 1.4.1 — TIME_EXIT TOGGLE API.
- `web/app.py` REV 1.4.0 — PHASE 4 WEB MIGRATION.
- `signals/volatility.py` REV 23.2 — PHASE 3 CLEANUP.
- `signals/volatility.py` REV 23.0 — UNIFIED CONFIG CLEANUP.
- `signals/trend.py` REV 23.2 — PHASE 3 CLEANUP.
- `signals/trend.py` REV 23.0 — UNIFIED CONFIG CLEANUP.
- `signals/router.py` REV 19.18 — DOCSTRING PATH + LOG PREFIX FIX.
- `signals/range.py` REV 23.2 — PHASE 3 CLEANUP.
- `signals/range.py` REV 23.0 — UNIFIED CONFIG CLEANUP.
- `signals/momentum.py` REV 23.2 — PHASE 3 CLEANUP.
- `signals/momentum.py` REV 23.0 — UNIFIED CONFIG CLEANUP.
- `signals/decision_engine.py` REV 4.2 — LIVE CONFIG READS (retained).
- `signals/decision_engine.py` REV 4.1 — PHASE 1 CLEANUP (retained).
- `signals/decision_engine.py` REV 4.0 — CENTRALIZED CONFIG.
- `signals/base.py` REV 23.5 — DOCSTRING PATH FIX.
- `signals/base.py` REV 23.4 — LIVE CONFIG READS + DEAD IMPORT CLEANUP.
- `signals/base.py` REV 23.3 — COSMETIC CLEANUP.
- `signals/base.py` REV 23.2 — PHASE 3 CLEANUP.
- `signals/base.py` REV 23.1 — PHASE 2 CLEANUP.
- `signals/base.py` REV 23.0 — UNIFIED CONFIG CLEANUP.
- `signals/base.py` REV 22.2 — VOL CLASS CAP ADJUSTMENT.
- `signals/base.py` REV 22.1 — CONFIG CENTER INTEGRATION.
- `orders/utils.py` REV 23.0 — UNIFIED CONFIG CLEANUP.
- `orders/repair.py` REV 1.6.0 — UNIFIED CONFIG CLEANUP (retained).
- `orders/manage.py` REV 1.9.0 — RUNTIME TOGGLE AWARENESS (retained).
- `orders/manage.py` REV 1.8.0 — TIME_EXIT TOGGLE GATE (retained).
- `orders/manage.py` REV 1.7.1 — UNIFIED CONFIG CLEANUP (retained).
- `orders/manage.py` REV 1.7.0 — CONFIG CENTER INTEGRATION (retained).
- `orders/exit.py` REV 1.6.0 — RUNTIME TOGGLE AWARENESS (retained).
- `orders/exit.py` REV 1.5.6 — DAILY-LOSS COUNT CLARITY (retained).
- `orders/entry.py` REV 1.8.0 — RUNTIME TOGGLE AWARENESS (retained).
- `orders/__init__.py` REV 23.0 — UNIFIED CONFIG CLEANUP.
- `market/indicators.py` REV 3.5 — DEAD IMPORT + LIVE FUNDING_Z READ.
- `market/indicators.py` REV 3.4 — DEAD FUNCTION CLEANUP.
- `market/indicators.py` REV 3.3 — FULLY DELEGATED TO config_center.
- `market/indicators.py` REV 3.2 — UNIFIED CONFIG (Option B).
- `core/state.py` REV 1.3.17 — CONFIG CLEANUP (retained).
- `core/state.py` REV 1.3.16 — DAILY TRACKER DATE-ROLLOVER FIX (retained).
- `core/config_center.py` REV 5.5 — REDUNDANT FILTER SHADOW CLEANUP (retained).
- `core/config_center.py` REV 5.3 — ENTRY/EXIT CONSTANTS PROMOTED (retained).
- `core/config_center.py` REV 5.2 — TRAILING SL KEYS RE-ADDED (retained).
- `core/config_center.py` REV 5.1 — DEAD KEY CLEANUP (retained).
- `core/config_center.py` REV 5.0 — CONFIG UNIFICATION (retained).
- `core/config.py` REV 2.0.0 — CONFIG UNIFICATION
- `core/coins_config.py` REV 23.1 — FALLBACK VALUE ALIGNMENT (retained).
- `core/coins_config.py` REV 23.0 — UNIFIED CONFIG SOURCE (retained).
- `core/coins_config.py` REV 22.0 — FAMILY-LEVEL BASELINES (retained).
- `core/client.py` REV 10.9 — COSMETIC CLEANUP (retained).

## 2026-10-01
- `signals/base.py` REV 22.0 — PHASE 1 (3 CHANGES).
- `market/indicators.py` REV 1.4.14 — 3-LAYER 5m TREND FILTER.
- `market/indicators.py` REV 1.4.13 — PHASE 1 ORDERBOOK ENABLE.

## 2026-09-30
- `signals/volatility.py` REV 21.2 — FAMILY-SPECIFIC TUNING.
- `signals/trend.py` REV 21.2 — FAMILY-SPECIFIC TUNING.
- `signals/range.py` REV 21.2 — FAMILY-SPECIFIC TUNING.
- `signals/momentum.py` REV 21.2 — FAMILY-SPECIFIC TUNING.
- `core/coins_config.py` REV 21.7 — FALLBACK RR FIX (retained).

## 2026-09-29
- `orders/exit.py` REV 1.5.5 — DUPLICATE-CLOSE REASON TRACKING (retained).
- `market/indicators.py` REV 1.4.9 — REGIME MOMENTUM OVERRIDE.
- `market/indicators.py` REV 1.4.12 — 5M TREND CACHE TTL EXTENSION.
- `market/indicators.py` REV 1.4.11 — DEAD HELPER REMOVAL.
- `market/indicators.py` REV 1.4.10 — DEAD CODE CLEANUP.
- `core/coins_config.py` REV 19.16 — DEAD DYNAMIC ROUTING REMOVED.

## 2026-09-28
- `web/app.py` REV 1.3.16 — DEAD-PROXY FALLOUT CLEANUP.
- `web/app.py` REV 1.3.15 — MANUAL CLOSE FROM UI.
- `web/app.py` REV 1.3.14 — DECISION ENGINE STATS EXPOSED.
- `web/analytics.py` REV 1.7 — FALLBACK LOOP CONSISTENCY FIX.
- `web/analytics.py` REV 1.6 — SILENT CLIENT-WAIT FOR WORKER.
- `signals/router.py` REV 19.17 — DYNAMIC ROUTING REVERTED (CRITICAL FIX).
- `signals/router.py` REV 19.16 — DYNAMIC FAMILY ROUTING. [superseded]
- `orders/utils.py` REV 1.5.0 — SPLIT FROM orders.py.
- `orders/manage.py` REV 1.6.2 — NAKED-SL WINDOW FIX (retained).
- `orders/manage.py` REV 1.6.0 — CONTINUOUS TRAILING STOP-LOSS (retained).
- `orders/__init__.py` REV 1.5.0 — SPLIT FROM orders.py.
- `market/indicators.py` REV 1.4.8 — REGIME MULTIPLIERS + SLOW DONCHIAN.
- `market/indicators.py` REV 1.4.7 — GATED UNUSED API CALLS.
- `market/indicators.py` REV 1.4.6 — LEGACY SIGNAL BLOCK DELETED.
- `market/indicators.py` REV 1.4.5 — SHARED HTF ALIGNMENT FUNCTION.

## 2026-09-26
- `web/analytics.py` REV 1.5 — FILLS/ORDERS RATE-LIMIT PAGE-SKIP FIX.
- `web/analytics.py` REV 1.4 — PAGINATION ROBUSTNESS.
- `web/analytics.py` REV 1.3 — ALIGNED WITH history.py REV 3.7.
- `web/analytics.py` REV 1.2 — aligned with history.py REV 3.3.
- `signals/router.py` REV 19.15 — EAGER-FALLBACK FIX + CONSOLIDATION.
- `market/indicators.py` REV 1.4.1 — DEAD CONSTANT CLEANUP.
- `core/coins_config.py` REV 19.14 — GET_CAPS() FALLBACK RATIO FIX.
- `core/coins_config.py` REV 19.13 — NORMALIZE WIRING + PUBLIC SURFACE.

## 2026-09-25
- `signals/router.py` REV 19.14 — IDIOMATIC IMPORT.
- `signals/router.py` REV 19.12 — HARDENING PASS.
- `coins/__main__.py` REV 19.12 — Lets you run `python -m coins` to inspect
- `coins/__init__.py` REV 19.12 — RECURSIVE LOADING

## 2026-09-24
- `web/analytics.py` REV 1.1 — double-fetch fix.
- `web/analytics.py` REV 1.0 — initial release.
- `market/indicators.py` REV 1.4.0 — MODERN INDICATOR PACK (Phase 1).
- `core/state.py` REV 1.3.9 — WRITE-LOCK RACE + TEMP-FILE UNIQUENESS FIX.

## 2026-09-23
- `market/indicators.py` REV 1.3.4 — MIN_RR FIX.
- `core/state.py` REV 1.3.8 — WRITE LOCK ADDED.
- `core/state.py` REV 1.3.5 — ATOMIC WRITES + PEAK FIX.

## 2026-09-22
- `market/indicators.py` REV 1.3.3 — SENTIMENT + DEAD CODE FIX.
- `market/indicators.py` REV 1.3.2 — BE STOP FIXED.
- `market/indicators.py` REV 1.3.1 — R-SCALED KEYS NOW FALLBACK-ONLY.
- `core/coins_config.py` REV 19.0 — FILE-PER-COIN ARCHITECTURE.