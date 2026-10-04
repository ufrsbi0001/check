"""
run.py — Entry point for the trading bot.

REV 1.2 (2026-10-04) — MUTUAL-EXCLUSION DOC:
  ✅ Added explicit warning that `python future.py` performs a
     port-liveness check and will refuse to start if this process
     (the web dashboard) is already bound to CONFIG.port.
  ✅ Clarified that this guard is best-effort, not a lock — always
     stop one instance before starting the other.

REV 1.1 (2026-10-04) — ENTRY-POINT DISAMBIGUATION:
  This is the RECOMMENDED entry point for normal use. It starts:
    • Web dashboard (Flask) on the configured HOST:PORT
    • Trading engine (future.main_loop) in a background thread
    • Trade manager loop (orders.trade_manager_loop)
    • Analytics background worker

  Direct `python future.py` is a DEV-ONLY alternative that skips the
  web dashboard. It reads API keys from stdin if not in .env. Do NOT
  run both simultaneously — they will double-submit orders.

  ⚠️  MUTUAL EXCLUSION: Do NOT run `python run.py` and
      `python future.py` simultaneously. `future.py` performs a
      port-liveness check (added REV 1.4.35) and will refuse to
      start with exit code 2 if this web dashboard is already
      bound to CONFIG.port — but the check is best-effort, not
      a lock. Always stop one instance before starting the other.

  If you only want the web UI (no engine), that mode is not supported
  by design — the engine is always started by `bot_runner()` in
  web/app.py when the dashboard's Start button is clicked.

Usage:
    python run.py               # normal
    python future.py            # dev-only, no dashboard
"""
from web.app import main


if __name__ == "__main__":
    main()