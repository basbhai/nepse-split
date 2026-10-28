"""
workflows/summary_workflow.py — NEPSE AI Engine
─────────────────────────────────────────────────────────────────────────────
Unified EOD + nightly summary sequence. Runs at 9:00 PM NST.
Called by systemd timer: nepse-summary.timer

Previously two separate workflows:
  - eod_workflow.py (3:15 PM) — RETIRED, moved to archives/
  - summary_workflow.py (9:00 PM) — this file, now absorbs EOD steps

Why merged at 9 PM (not 3:15 PM):
  - Floorsheet data is complete by 9 PM (settlement confirmed)
  - Evening news cycle complete → better headlines for daily_context_log
  - Broker flow data stable (no intraday noise)
  - One fewer wake/sleep cycle — machine sleeps at ~3:05 PM, wakes 9 PM

Sequence:
    calendar_guard              → exit if today was not a trading day (guard, not a step)
    0.  auto_edis                    → (LIVE only) deliver today's sold shares via EDIS
    1.  nepse_indices               → scrape latest index values (backfill 5d)
    2.  share_sectors_sync          → catch newly-listed symbols missing a sector mapping
    3.  history_bootstrap           → today's EOD OHLCV → price_history
    4.  candle_detector             → detect 15 patterns on completed EOD OHLC → candle_signals
    5.  recommendation_tracker      → stamp WAIT/AVOID outcomes
    6.  auditor                     → close trades, causal attribution, KPIs
    7.  gate_miss_tracker           → stamp FALSE_BLOCK/CORRECT_BLOCK
    8.  floorsheet                  → today's full floorsheet scrape
    9.  broker_flow_scraper         → smart money accumulation/distribution
    10. stealth_scanner             → hidden accumulation / stealth signals
    11. nepal_pulse                 → fresh 9 PM headlines
    12. daily_context_summarizer    → collapse intraday data to one clean row
    13. backup_sync                 → sync updated tables local → Neon
    14. pg_backup                   → local PostgreSQL schema + full dump
    15. sleep_scheduler             → set RTC alarm 10:45 AM, suspend

─────────────────────────────────────────────────────────────────────────────
Run:
    python -m workflows.summary_workflow
    python -m workflows.summary_workflow --dry-run
    python -m workflows.summary_workflow --skip-guard
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SUMMARY] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
NST = timezone(timedelta(hours=5, minutes=45))


def _step(name: str, fn, dry_run: bool, *args, **kwargs) -> bool:
    log.info("── %s ...", name)
    if dry_run:
        log.info("   [DRY-RUN] skipped")
        return True
    t0 = time.time()
    try:
        fn(*args, **kwargs)
        log.info("   ✅ done (%.1fs)", time.time() - t0)
        return True
    except Exception as e:
        log.error("   ❌ FAILED (%.1fs): %s", time.time() - t0, e)
        return False


def run(dry_run: bool = False, skip_guard: bool = False) -> int:
    now = datetime.now(tz=NST)
    log.info("=" * 65)
    log.info("NEPSE SUMMARY WORKFLOW — %s NST", now.strftime("%Y-%m-%d %H:%M:%S"))
    log.info("=" * 65)

    # ── Calendar guard ────────────────────────────────────────────────────────
    if not skip_guard:
        try:
            from calendar_guard import is_trading_day, today_nst, now_nst, MARKET_CLOSE
            if not is_trading_day(today_nst()):
                log.info("Not a trading day — summary workflow skipped")
                return 0
            if now_nst().time() < MARKET_CLOSE:
                log.info(
                    "Market not closed yet (now=%s NST, close=%s NST) — summary "
                    "workflow skipped (likely a missed-run catch-up firing early)",
                    now_nst().strftime("%H:%M"), MARKET_CLOSE.strftime("%H:%M"),
                )
                return 0
        except Exception as e:
            log.error("calendar_guard failed: %s — aborting", e)
            return 2

    # ── Detect paper/live mode ────────────────────────────────────────────────
    paper_mode = True
    try:
        from sheets import get_setting
        paper_mode = get_setting("PAPER_MODE", "true").lower() == "true"
    except Exception:
        pass
    log.info("Mode: %s", "PAPER" if paper_mode else "LIVE")

    results = {}

    # ── Step 0: Auto-EDIS (unconditional — 2026-09-11) ────────────────────────
    # Any shares sold today (through any broker) create a CDSC settlement
    # obligation that must be delivered to the broker pool account before T+2.
    # meroshare.run_edis() confirms holding days where required, then submits
    # the delivery for every pending settlement — it queries CDSC's own
    # pending-settlements list and is its own no-op when nothing is due, so
    # it is safe (and correct) to call every evening regardless of PAPER_MODE.
    #
    # Previously gated on `not paper_mode`, on the assumption paper mode never
    # touches the real DEMAT. That assumption broke: the system now runs both
    # paper AND live trading concurrently, so PAPER_MODE no longer reliably
    # says whether a real sale happened today. Whether EDIS is needed should
    # be decided by CDSC's actual pending settlements (which run_edis() already
    # checks), not by our own mode flag — skipping this step on a day with a
    # real sale would leave those shares undelivered past T+2.
    def _auto_edis():
        from modules.meroshare import run_edis, close_from_edis
        res = run_edis(dry_run=False)
        close_from_edis(res, dry_run=False)
        if not res.get("ok"):
            raise RuntimeError(res.get("note") or "EDIS reported failure")
    results["auto_edis"] = _step("auto_edis (deliver sold shares)", _auto_edis, dry_run)

    # ── Step 0b: Live portfolio sync (unconditional — 2026-09-24) ─────────────
    # Pulls the real DEMAT balance (waccReport + myPortfolio) into `portfolio`.
    # Stamps demat_confirmed_date the first evening a bought scrip shows up in
    # Meroshare — the ONLY thing that releases a live position to
    # execution_monitor (T+3 alone is not enough; Meroshare can lag it). Also
    # closes rows for shares sold today. Runs after auto-EDIS so today's sells
    # are delivered first. Unconditional for the same reason as EDIS: PAPER_MODE
    # no longer says whether the real book changed today.
    def _live_portfolio_sync():
        from modules.meroshare import sync
        if sync() is None:
            raise RuntimeError("meroshare.sync() returned no data")
    results["live_portfolio_sync"] = _step("live_portfolio_sync (Meroshare → portfolio)", _live_portfolio_sync, dry_run)

    # ── Step 1: NEPSE indices ─────────────────────────────────────────────────
    def _nepse_indices():
        from modules.sharehub_scraper import run as run_indices
        from datetime import date
        from_date = (datetime.now() - timedelta(days=5)).date()
        run_indices(from_date=from_date, dry_run=False)
    results["nepse_indices"] = _step("nepse_indices", _nepse_indices, dry_run)

    # ── Step 1b: share_sectors sync (catch newly-listed symbols) ─────────────
    def _share_sectors_sync():
        from modules.share_sectors_sync import run as run_sector_sync
        run_sector_sync(dry_run=False)
    results["share_sectors_sync"] = _step(
        "share_sectors_sync (new listings → sector mapping)", _share_sectors_sync, dry_run
    )

    # ── Step 2: History bootstrap (today's EOD data) ──────────────────────────
    def _bootstrap():
        from modules.history_bootstrap import scrape_and_upsert
        scrape_and_upsert(dry_run=False)
    results["history"] = _step("history_bootstrap (today's EOD → price_history)", _bootstrap, dry_run)

    # ── Step 3: Candle detector (EOD patterns on completed OHLC) ─────────────
    def _candles():
        from modules.candle_detector import run as run_candles
        run_candles()
    results["candles"] = _step("candle_detector (15 patterns, EOD OHLC)", _candles, dry_run)

    # ── Step 4: Recommendation tracker ───────────────────────────────────────
    def _rec_tracker():
        from analysis.recommendation_tracker import run as run_tracker
        run_tracker(dry_run=False)
    results["rec_tracker"] = _step("recommendation_tracker", _rec_tracker, dry_run)

    # ── Step 5: Auditor ───────────────────────────────────────────────────────
    def _auditor():
        from analysis.auditor import run_eod_audit, run_paper_audit
        if paper_mode:
            run_paper_audit(dry_run=False)
        else:
            run_eod_audit(dry_run=False)
    results["auditor"] = _step(
        f"auditor ({'paper' if paper_mode else 'live'})", _auditor, dry_run
    )

    # ── Step 6: Gate miss tracker ─────────────────────────────────────────────
    def _gate_tracker():
        from analysis.gate_miss_tracker import run_eod
        run_eod(dry_run=False)
    results["gate_tracker"] = _step("gate_miss_tracker", _gate_tracker, dry_run)

    # ── Step 6b: Flush AI VETOs (Claude AVOIDs) to gate_misses ──────────────
    def _ai_veto_flush():
        from analysis.gate_miss_tracker import flush_ai_vetoes_to_db
        try:
            count = flush_ai_vetoes_to_db(dry_run=False)
            log.info("flush_ai_vetoes_to_db: %d rows written", count)
        except Exception as e:
            log.error("flush_ai_vetoes_to_db failed (non-fatal): %s", e)
    results["ai_veto_flush"] = _step("flush_ai_vetoes (AI_VETO → gate_misses)", _ai_veto_flush, dry_run)

    # ── Step 7: Floorsheet ────────────────────────────────────────────────────
    def _floorsheet():
        from modules.floorsheet_scraper import run_daily
        from datetime import date
        run_daily(target_date=date.today())
    results["floorsheet"] = _step("floorsheet (today)", _floorsheet, dry_run)

    # ── Step 8: Broker flow (smart money) ────────────────────────────────────
    def _broker_flow():
        from modules.broker_flow_scraper import run as run_broker_flow
        run_broker_flow(dry_run=False)
    results["broker_flow"] = _step(
        "broker_flow_scraper (accumulation/distribution)", _broker_flow, dry_run
    )

    # ── Step 9: Stealth accumulation scanner ─────────────────────────────────
    def _stealth_scanner():
        from modules.hidden_accum_scanner import run_scanner
        run_scanner(dry_run=False)
    results["stealth_scanner"] = _step(
        "hidden_accum_scanner (stealth signals)", _stealth_scanner, dry_run
    )

    # ── Step 10: Nepal pulse (fresh 9 PM headlines) ──────────────────────────
    def _nepal():
        from modules.nepal_pulse import run as run_pulse
        run_pulse()
    results["nepal_pulse"] = _step(
        "nepal_pulse (9 PM fresh headlines)", _nepal, dry_run
    )

    # ── Step 11: Daily context summarizer ────────────────────────────────────
    def _summarizer():
        from modules.daily_context_summarizer import run as run_summarizer
        run_summarizer()
    results["summarizer"] = _step("daily_context_summarizer", _summarizer, dry_run)

    # ── Step 11b: Daily system check → findings_log ──────────────────────────
    # Plain SQL checks always run. The `claude -p` agent layer (web search) runs
    # only when DAILY_CHECK_LLM=true.
    def _daily_check():
        from analysis.daily_check import run as run_daily
        from sheets import get_setting as _g
        run_daily(llm=_g("DAILY_CHECK_LLM", "false").strip().lower() == "true", apply=True, echo=False)
    results["daily_check"] = _step("daily_check (findings log; agent layer only if DAILY_CHECK_LLM=true)", _daily_check, dry_run)

    # ── Step 12: Backup sync ──────────────────────────────────────────────────
    def _backup():
        from modules.backup_sync import run as run_backup
        run_backup()
    results["backup"] = _step("backup_sync (local → Neon)", _backup, dry_run)

    # ── Step 13: Local PostgreSQL backup ──────────────────────────────────────
    def _pg_backup():
        import subprocess, os, shutil
        backup_dir = os.path.expanduser("~/nepse-backup")
        prev_dir = f"{backup_dir}/yesterday"
        url = "postgresql://postgres:nepse123@127.0.0.1:5432/nepse_engine"
        # dump to .new first so a failed dump never destroys the good backup
        try:
            subprocess.run(
                f"pg_dump {url} --schema-only -f {backup_dir}/schema.sql.new && "
                f"pg_dump {url} -f {backup_dir}/full.sql.new",
                shell=True, check=True
            )
        except Exception:
            for f in ("schema.sql.new", "full.sql.new"):
                if os.path.exists(f"{backup_dir}/{f}"):
                    os.remove(f"{backup_dir}/{f}")
            raise
        # rotate: drop old yesterday, today's current -> yesterday, new -> current
        shutil.rmtree(prev_dir, ignore_errors=True)
        os.makedirs(prev_dir)
        for f in ("schema.sql", "full.sql"):
            if os.path.exists(f"{backup_dir}/{f}"):
                shutil.move(f"{backup_dir}/{f}", f"{prev_dir}/{f}")
            os.rename(f"{backup_dir}/{f}.new", f"{backup_dir}/{f}")
    results["pg_backup"] = _step("pg_backup (schema + full → ~/nepse-backup)", _pg_backup, dry_run)

    # ── Summary log ───────────────────────────────────────────────────────────
    passed = sum(1 for v in results.values() if v)
    failed = sum(1 for v in results.values() if not v)
    log.info("─" * 65)
    log.info("Summary workflow complete — %d/%d steps OK", passed, passed + failed)
    if failed:
        log.warning("Failed steps: %s",
                    ", ".join(k for k, v in results.items() if not v))



    # ── Step 14: Sleep (always runs — even if steps failed) ───────────────────
    log.info("── sleep_scheduler (set RTC 10:45 AM, suspend) ...")
    if not dry_run:
        try:
            from setup.sleep_scheduler import _schedule_wake, _suspend_now, _next_wake_time
            next_wake = _next_wake_time()
            _schedule_wake(next_wake)
            log.info("   RTC alarm set for %s NST", next_wake.strftime("%Y-%m-%d %H:%M"))
            _suspend_now()
        except Exception as e:
            log.error("   sleep_scheduler failed: %s", e)
    else:
        log.info("   [DRY-RUN] Would set RTC alarm and suspend")

    log.info("=" * 65)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    from log_config import attach_file_handler
    attach_file_handler(__name__)
    parser = argparse.ArgumentParser(description="NEPSE unified EOD + summary workflow")
    parser.add_argument("--dry-run",    action="store_true")
    parser.add_argument("--skip-guard", action="store_true")
    args = parser.parse_args()
    sys.exit(run(dry_run=args.dry_run, skip_guard=args.skip_guard))
