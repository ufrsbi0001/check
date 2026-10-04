# PATCH NOTES — applied on top of Bot.zip (HEAD 103bd71, 4 Oct 2026)

This package contains your full source tree with the changes below applied.
It deliberately does NOT contain `.env`, `.git/` or `data/`:
- `.env` / `.git/config` hold live secrets (see "You must do").
- `data/` holds your live state (`active_trades.json`, `daily_tracker.json`...). Unzipping over it would overwrite it with stale files.

Copy the changed files into your repo (or `git apply changes.patch`). Keep your own `.env`.

## What changed

| ID | File | Change |
|----|------|--------|
| P1 | core/config_center.py | `max_fill_slippage_pct` (default 0.50) is now a registered key: env `MAX_FILL_SLIPPAGE_PCT`, validated to [0.05, 5.0], shown in the diagnostic print. `orders/entry.py` already read it with a 0.50 fallback, but it was never registered, so it could not be tuned. Default behaviour is unchanged. |
| P2 | orders/manage.py | `_orphan_watch_tick()` — every ~30s the manager loop looks for non-zero exchange positions that the bot is not tracking. Confirmed on 2 consecutive checks => CRITICAL log + Telegram. In-flight entries are safe (the symbol is reserved in `bot_tracked_symbols` before the order is sent). Optional `ORPHAN_WATCH_AUTO_ADOPT=true` also runs `sync_existing_positions()` (default OFF, because it would also adopt positions you open by hand). `ORPHAN_WATCH_ENABLED=false` disables it. |
| P2 | config_center.py | keys `orphan_watch_enabled` (True), `orphan_watch_auto_adopt` (False) + env mapping. |
| P3 | orders/manage.py | Errors from `manage_single_trade`, `reconcile_active_trade` and the fast-reconcile were logged at DEBUG (invisible at INFO). Now WARNING. |
| P3 | orders/entry.py, exit.py, manage.py, repair.py | 9 `except: pass` blocks that swallowed exchange-API errors now log a WARNING with the function name and error. No control-flow change. |
| P3 | scripts/verify.py | Ignores comments and docstrings (it used to print [+] for a fix that only existed in a comment). Added P1/P2 checks. |
| P3 | pytest.ini | Moved from `scripts/` to the repo root, where pytest actually reads it. |
| P3 | requirements.txt, .env.example, CHANGELOG.md | New. Requirements list real imports (versions NOT guessed — run `pip freeze`). `.env.example` is your .env with every key/secret/token/chat-id blanked. CHANGELOG is generated from the REV headers in the source. |
| T  | tests/test_execution_paths.py | 12 behaviour tests against a fake exchange: normal fill, lost ack + late fill (no resend), lost ack + nothing found (UNKNOWN, no resend), terminal reject (fresh cid), -4005 reject, -1003 retry with the same cid, orphan watch (confirm, no repeat alert, reset, disabled, auto-adopt gating), config keys. |

## Test results (this sandbox)

- 30 tests pass (18 existing + 12 new). python-binance and pytest are not installed here, so I ran them with a stub for `binance` and a small runner; run `python -m pytest` on your machine for the real result.
- Mutation check: I re-introduced the old double-entry bug (resend with a fresh cid on UNKNOWN) into a scratch copy; `test_timeout_and_nothing_found_returns_unknown_without_resend` failed as it should.
- All modules compile. Nothing was run against Binance (demo or live).

## Not done (and why)

- **`place_order_fixed` (1,131 lines), `manage_single_trade`, `main_loop` were NOT split.** Doing that without behaviour tests on the surrounding steps would trade a known-working path for an unverified one. Next step: write fake-exchange tests for the SL/TP/slippage branches first, then extract one step at a time.
- **Entry SL is still quantity-based `reduceOnly`** (only repair/adoption use `closePosition`). Switching needs a demo-account test of what Binance does after TP1 partial fills.
- **`_cc_get_num` copies** (6 files, 3 lines each) left as they are — they are trivially identical, so deduplicating is cosmetic and touches 6 live files.
- The remaining ~110 silent `except ...: pass/continue` handlers (non-API: parsing, file I/O, cleanup) are untouched.
- Strategy/filter logic and the new VWAP/FVG/sweep/divergence filters were not changed. Those flags are ON in your `.env`; compare them in paper/shadow mode before trusting them with live size.
- Dependency versions are unpinned (see requirements.txt).

## You must do (code cannot do this)

1. Rotate the Binance API key/secret, the GitHub token embedded in `.git/config`, `WEB_TOKEN` and `FLASK_SECRET_KEY`. They were in the zips you uploaded.
2. Set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`. While they are empty, every CRITICAL alert (UNKNOWN entry, orphan position, DD-HALT) goes nowhere — including the new orphan alert.
3. Check on the exchange that XPL, AVAX and CRV (SELL) from `active_trades.json` are really open and have a stop.
