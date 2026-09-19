# -*- coding: utf-8 -*-
"""
modules/execution_monitor.py — NEPSE AI Engine
═══════════════════════════════════════════════════════════════════════════════
Real-time intraday position intelligence. Runs as a separate process during
market hours (10:45 AM – 3:00 PM NST, Mon–Fri).

Two responsibilities in one loop:
  1. GUARD     (every 30s)  — hard stop -6%, trailing stop (activates net +3%, floor peak-2%, min 3%)
  2. INTELLIGENCE (every 60s) — GIS score: OBI + TIR + VWAP + Microprice → HOLD/CAUTION/EXIT

Mathematical framework (validated across Gemini, ChatGPT, DeepSeek):
  OBI  — exponential decay weighted order book imbalance (top 10 levels, λ=0.5)
  TIR  — tick rule trade imbalance (5-min rolling, qty≥10 filter)
  VWAP — tanh clamped at ±2% deviation
  Micro— Stoikov microprice using W_bid/W_ask from OBI computation

  GIS  = conf × (0.30×OBI + 0.30×TIR + 0.20×VWAP + 0.20×Micro)
         + 0.20×circuit_risk + 0.20×spread_score

State machine with hysteresis (DeepSeek framework):
  HOLD_STRONG → HOLD if S < 0.25
  HOLD        → CAUTION if S < -0.05
  CAUTION     → EXIT_EARLY if S < -0.25
  EXIT_EARLY  → IMMEDIATE if S < -0.45
  (recovery requires stronger positive signal to prevent whipsawing)

NEPSE-specific:
  - DPR magnet: within 1.5% of upper circuit → suppress exits unless depth collapses
  - Opening 15 min: VWAP + Microprice only (no OBI/TIR — order book chaotic)
  - Closing 15 min: full score, double spread + circuit penalty
  - Odd lot filter: drop ticks where qty < 10
  - Pre-session fragility: loaded from yesterday's floorsheet_signals at startup
  - Confidence multiplicative: low volume → score attenuated, not just penalised

Architecture:
  - No AI calls — pure math only
  - Reads open positions from paper_portfolio (PAPER_MODE) or portfolio (LIVE)
  - LTP from atrad_market_watch DB (no API call for price)
  - getOrderBook + getTradesOfDay called per open position (all open positions)
  - Telegram alert on state change only (never spam)
  - Paper guard trigger → alert + paper-ledger close. Live guard trigger →
    alert, plus a REAL ATrad SELL when LIVE_AUTO_CLOSE_ENABLED is "true"
    (default false; also gated by T+3 lock and a Meroshare holding check).
    GIS intelligence exits (EXIT_EARLY / IMMEDIATE) are always alert-only.
  - Exit price: HARD_STOP and the last 15 min of the session cross the spread
    at the best bid (immediate); every other trigger posts a patient limit at
    the lowest ask so a stale/collapsed bid can't landslide the fill. Paper
    books the ask limit only when a real fill is reachable (LTP hits it, or it
    crosses to the bid near close); live rests it as a Day order (no fill feed
    yet). See _guard_exit_price / _open_guard_exit / _work_pending_exit.

Run:
  python -m modules.execution_monitor          # normal
  python -m modules.execution_monitor --once   # single cycle, exit (for testing)
═══════════════════════════════════════════════════════════════════════════════
"""

import logging
import math
import os
import sys
import time
import argparse
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional
from modules.atrad_scraper import get_ltp_live
from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

NST = timezone(timedelta(hours=5, minutes=45))

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

GUARD_INTERVAL_SEC       = 30     # how often guard + intelligence runs
INTELLIGENCE_INTERVAL_SEC = 60   # how often TIR (5-min tick window) recomputes
MARKET_OPEN_H, MARKET_OPEN_M   = 10, 45
MARKET_CLOSE_H, MARKET_CLOSE_M = 15,  0

# Stop / trail parameters.
# 2026-08-31: re-validated via a 453,116-trade backtest (every symbol-day in
# price_history 2019-07..2026-08, 644 symbols, both intraday-path assumptions,
# net-of-cost P&L, same exit logic as _check_guard()). Findings: arming later and
# trailing tighter both help monotonically; the floor minimum never binds while
# arm >= band; the whole parameter effect is ~0.1%/trade and smaller than the
# daily-vs-intraday path uncertainty. Operator-chosen config below:
#   arm net +4%  ·  give-back band 2%  ·  floor never below net +2%
#   take-profit at net +15%  ·  no fixed stop_loss / target (only the -6% hard
#   stop below the arm level). Supersedes commit 4beb351.
# The backtest preferred an even tighter take-profit (+10-12% raw) on the
# unconditional universe; +15% net is the operator's let-winners-run compromise.
# Report: https://claude.ai/code/artifact/cc2933d1-28d3-48dc-8a10-98870c21d562
HARD_STOP_PCT      = -6.0   # raw % from entry → immediate exit (fires on any tick)
TAKE_PROFIT_PCT    = 15.0   # net % profit → immediate exit (fresh tick only)
TRAIL_ACTIVATE_PCT =  3.0   # base; arm bar = TRAIL_ACTIVATE_PCT + TRAIL_ARM_MARGIN

# Arming bar = TRAIL_ACTIVATE_PCT + TRAIL_ARM_MARGIN = net +4%. Peak NET profit
# must clear this before trail_active latches (one-way flag) — not just graze it.
# Positions that peak between 0 and +4% net fall through to the -6% hard stop
# only; there is no fixed stop_loss any more (2026-08-31). Once armed, the floor
# trails at max(TRAIL_MIN_FLOOR_PCT, peak_net − gap), so an armed trade can only
# exit via TRAILING_STOP, TAKE_PROFIT or HARD_STOP.
TRAIL_ARM_MARGIN   =  1.0   # % net profit above TRAIL_ACTIVATE_PCT required to arm

# A guard decision (peak update, trail arm, trail fire) must run on a FRESH
# tick. When get_ltp_live() is down we fall back to the latest atrad_market_watch
# DB row; if that row is older than this, the scraper is stale and only the hard
# stop (unbounded-loss safety) is allowed to act on it — peak/trail logic is
# skipped so a frozen price can't arm or trip the trail. See _check_guard().
GUARD_STALE_LTP_SEC = 300

# The live getQuickWatch quote can ALSO freeze — ATrad keeps returning the last
# snapshot with code=0 when the feed dies, the symbol halts, or the data
# session soft-expires. A single read cannot tell that from a live quote, so
# _live_quote_is_fresh() applies the same rule the DB-fallback path uses, via
# two independent checks:
#   1. lasttradedtime age > GUARD_STALE_LTP_SEC  (when the field is parseable)
#   2. the whole quote payload byte-identical for >= GUARD_FROZEN_QUOTE_SEC
#      (the backstop for when lasttradedtime is missing/unparseable — exactly
#       when we'd otherwise be blindest). A genuinely thin-but-live stock still
#      moves bid/ask/volume between trades, so #2 does not fire on low
#      liquidity alone.
# On a stale live quote the guard is restricted to the hard stop, same as a
# stale DB row (see _check_guard(ltp_is_fresh=...)).
GUARD_FROZEN_QUOTE_SEC = 180

# Before acting on a guard trigger, the execution price (best bid in the live
# order book) is checked against the LTP that fired the guard:
#   TRAILING_STOP / TAKE_PROFIT — abort + alert if the bid is more than this %
#       AWAY from the trigger in either direction (stale trigger or collapsed
#       book; a profit exit is never worth trading on inconsistent data).
#   HARD_STOP — still executes when the bid is BELOW the trigger (cutting an
#       unbounded loss beats waiting), but aborts + alerts if the bid is more
#       than this % ABOVE the trigger (the -6% was stale/frozen and the real
#       market is well above it — don't dump into a phantom).
# This is the last backstop against a false trigger turning into a real
# cash loss on the live book.
GUARD_MAX_TRIGGER_SLIP_PCT = 3.0

# NEPSE T+3 settlement: shares aren't sellable until 3 trading days after
# purchase (e.g. a Thu buy settles the following Tue — Fri/Mon/Tue = 3 under
# a Mon-Fri week). No exit reason — hard stop, take-profit or trailing — can
# fire before that, since the exchange wouldn't accept the sell order anyway.
# See calendar_guard.trading_days_since().
SETTLEMENT_TRADING_DAYS = 3
TRAIL_FLOOR_PCT     =  2.0  # give-back band: once armed, floor = peak_net − this
TRAIL_MIN_FLOOR_PCT =  2.0  # ...but the floor never sits below this net %. With
                            # arm +4 / band 2 it only binds exactly at peak +4%.

# 2026-08-24: tiered trailing gap — the floor tightens as peak net profit
# climbs, so bigger winners give back proportionally less. Below TRAIL_TIER2_PCT
# the gap stays at the base TRAIL_FLOOR_PCT (2%).
TRAIL_TIER2_PCT    =  8.0   # % net profit → gap tightens to TRAIL_TIER2_FLOOR_PCT
TRAIL_TIER2_FLOOR_PCT =  1.0
TRAIL_TIER3_PCT    = 10.0   # % net profit → gap tightens to TRAIL_TIER3_FLOOR_PCT
TRAIL_TIER3_FLOOR_PCT =  0.5

# Anomalous-tick guard: a single odd-lot/fat-finger print (e.g. LTP=115 when
# the real price is ~101) must not be trusted for peak-tracking or stop
# checks. A tick counts as "suspect" only when it deviates sharply from BOTH
# VWAP (volume-weighted, so one small bad trade can't move it) AND the last
# confirmed LTP. A suspect tick is held for one cycle awaiting confirmation.
SPIKE_VWAP_DEV_MAX =  8.0   # % — |ltp-vwap|/vwap beyond this is suspect
SPIKE_JUMP_MAX_PCT =  6.0   # % — jump vs last confirmed LTP beyond this is suspect
SPIKE_CONFIRM_PCT  =  1.0   # % — how close the next tick must be to the pending spike to confirm it

# GIS weights
W_OBI   = 0.30
W_TIR   = 0.30
W_VWAP  = 0.20
W_MICRO = 0.20

# Penalty weights
W_CIRCUIT = 0.20
W_SPREAD  = 0.20

# OBI exponential decay (λ=0.5 → level 10 ≈ 1% weight)
OBI_LAMBDA   = 0.5
OBI_LEVELS   = 10

# VWAP clamp threshold (2% → tanh saturates at ±1)
VWAP_CLAMP   = 0.02

# Minimum 5-min volume for full TIR confidence
TIR_MIN_VOL  = 500
TIR_WINDOW_S = 300   # 5 minutes

# Spread thresholds (basis points)
SPREAD_OK_BP  = 20
SPREAD_MAX_BP = 80

# DPR magnet zone
DPR_MAGNET_PCT = 0.015   # within 1.5% of upper circuit

# Signal state hysteresis thresholds
# Each state has (up_threshold, down_threshold) to move to next state
# These create asymmetric bands — once defensive, need stronger reversal to recover
STATE_THRESHOLDS = {
    # (threshold_to_recover_from_this_state, threshold_to_enter_this_state)
    "HOLD_STRONG": {"drop_to_hold":     0.25},
    "HOLD":        {"rise_to_strong":   0.45, "drop_to_caution": -0.05},
    "CAUTION":     {"rise_to_hold":     0.25, "drop_to_exit":    -0.25},
    "EXIT_EARLY":  {"rise_to_caution":  0.05, "drop_to_immed":   -0.45},
    "IMMEDIATE":   {"rise_to_exit":     0.05},
}

# Instant exit (skip 2-cycle persistence)
INSTANT_EXIT_S   = -0.45
INSTANT_EXIT_TIR = -0.50

# Liquidity shock threshold
LIQUIDITY_DROP_THRESH = -0.35

# How often a resting/uncertain LIVE exit re-checks ATrad's own trade blotter
# (fetch_my_trades) once a live sell has been submitted. NOT every monitor
# cycle (30s) — that hammered the ORDER session and fired a fresh duplicate
# sell + Telegram alert every cycle against an already-filled position (the
# 2026-09-11 SONA incident: 11 duplicate live orders + 11 alerts in 5 minutes
# for a position that had actually filled on the very first attempt).
# Operator instruction 2026-09-11: check every 30 min, don't spam.
LIVE_EXIT_RECHECK_SEC = 30 * 60
# Escalate to the operator only after this many silent recheck cycles with no
# fill confirmed (4 * 30min = ~2h) — not on every cycle.
LIVE_EXIT_ESCALATE_AFTER = 4

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS (after env loaded)
# ─────────────────────────────────────────────────────────────────────────────

from db.connection import _db
from sheets import get_setting, run_raw_sql, execute_dml


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 1 — TIME HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _now_nst() -> datetime:
    return datetime.now(tz=NST)


_last_calendar_check_date = None  # tracks when calendar_guard was last refreshed


def _is_market_open() -> bool:
    """
    Market-hours gate. Also defers to calendar_guard for the trading-day
    check (weekday schedule + gazette/ad-hoc holidays) before ever looking
    at wall-clock time. Without this, this long-running process — it starts
    once and runs for days without restarting — kept evaluating the guard
    on later weekends/holidays purely because the wall-clock hour fell in
    [MARKET_OPEN, MARKET_CLOSE), regardless of the day. That's exactly what
    happened on 2026-09-04 (Krishna Janmashtami, a hardcoded gazette
    holiday): no real market data exists, and a garbage/suspect tick got
    misread as a genuine -90% price move, firing repeated false HARD_STOP
    alerts every cycle.
    Refreshes calendar_guard's ad-hoc closures once per calendar day (not
    every 30s cycle) so a same-day bandh flagged after this process started
    is still picked up without needing a restart.
    """
    global _last_calendar_check_date
    import calendar_guard
    today = calendar_guard.today_nst()
    if _last_calendar_check_date != today:
        calendar_guard.reload_trading_days()
        calendar_guard.reload_closures()
        _last_calendar_check_date = today
    if not calendar_guard.is_trading_day(today):
        return False

    now = _now_nst()
    open_  = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0)
    close_ = now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
    return open_ <= now <= close_


def _market_phase() -> str:
    """Return OPENING (first 15 min), CLOSING (last 15 min), or NORMAL."""
    now   = _now_nst()
    open_ = now.replace(hour=MARKET_OPEN_H,  minute=MARKET_OPEN_M,  second=0, microsecond=0)
    close_= now.replace(hour=MARKET_CLOSE_H, minute=MARKET_CLOSE_M, second=0, microsecond=0)
    if now < open_ + timedelta(minutes=15):
        return "OPENING"
    if now > close_ - timedelta(minutes=15):
        return "CLOSING"
    return "NORMAL"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 2 — POSITION LOADER
# ═══════════════════════════════════════════════════════════════════════════

def _load_paper_positions() -> list[dict]:
    rows = run_raw_sql("""
        SELECT pp.id, pp.telegram_id, pp.symbol,
               pp.wacc         AS entry_price,
               pp.total_shares AS shares,
               pp.total_cost,
               pp.first_buy_date,
               pp.updated_at,
               pp.peak_price,
               pp.trail_active
        FROM paper_portfolio pp
        WHERE pp.status = 'OPEN'
        ORDER BY pp.first_buy_date ASC
    """) or []
    for row in rows:
        row["book"] = "paper"
        _enrich_from_market_log(row)     # rehydrate peak_price / trail_active
    return rows


def _load_live_positions() -> list[dict]:
    rows = run_raw_sql("""
        SELECT id, symbol, entry_price, shares,
               entry_date, total_cost, peak_price, trail_active, trail_stop,
               demat_confirmed_date
        FROM portfolio
        WHERE status = 'OPEN'
        ORDER BY entry_date ASC
    """) or []
    for row in rows:
        row["book"] = "live"
        # Normalise the date key _check_guard / _enrich_from_market_log expect.
        row["first_buy_date"] = row.get("entry_date")
        row["telegram_id"] = None
        _enrich_from_market_log(row)     # rehydrate peak_price / trail_active
    return rows


def _load_open_positions() -> list[dict]:
    """
    Load every OPEN position from BOTH books in one list. Each row is tagged
    row["book"] = "paper" | "live". A guard trigger closes paper in the ledger;
    for live it places a real ATrad SELL when LIVE_AUTO_CLOSE_ENABLED is "true",
    otherwise alert-only (see _live_guard_close). The live book is skipped
    entirely if LIVE_MONITOR_ENABLED != "true".
    """
    positions: list[dict] = []
    try:
        positions.extend(_load_paper_positions())
    except Exception as e:
        log.error("load paper positions failed: %s", e)
    if get_setting("LIVE_MONITOR_ENABLED", "true").strip().lower() == "true":
        try:
            positions.extend(_load_live_positions())
        except Exception as e:
            log.error("load live positions failed: %s", e)
    return positions


def _enrich_from_market_log(row: dict) -> None:
    """Restore persisted trail state (peak_price, trail_active) for this position.

    The guard no longer uses market_log stop_loss / target (removed 2026-08-31 —
    only the -6% hard stop, +20% take-profit and the trailing stop fire), so this
    just rehydrates the trail bookkeeping each cycle.
    """
    try:
        # Restore persisted trail state; fall back to entry if never written.
        # price_history catch-up is done once at startup via _catchup_peak_from_history(),
        # not here — this runs every 30s cycle and the history query would be wasteful.
        entry_price = float(row.get("entry_price") or 0)
        saved_peak  = row.get("peak_price")
        saved_trail = row.get("trail_active")
        true_peak   = float(saved_peak) if saved_peak else entry_price
        row["peak_price"]   = true_peak
        if saved_trail:
            row["trail_active"] = str(saved_trail).lower() == "true"
        else:
            # Same net-of-cost arming bar as _check_guard()/_catchup_peak_from_history()
            # — must match or trail_active can get latched True here off a raw price
            # move that never actually cleared the arm threshold (it's a one-way
            # flag, never reset to False once set).
            shares = float(row.get("shares") or row.get("total_shares") or 0)
            if row.get("first_buy_date"):
                from modules.trading_core import hold_days
                hd = hold_days(row["first_buy_date"])
            else:
                hd = 0
            row["trail_active"] = _net_profit_pct(entry_price, true_peak, shares, hd) >= TRAIL_ACTIVATE_PCT + TRAIL_ARM_MARGIN
    except Exception as e:
        log.warning("enrich_from_market_log(%s): %s", row.get("symbol"), e)
        row["peak_price"]   = float(row.get("entry_price") or 0)
        row["trail_active"] = False


def _persist_trail_state(pos: dict, peak_price: float, trail_active: bool) -> None:
    """
    Write peak_price + trail_active back to the row's own table (paper_portfolio
    or portfolio), matched by primary key so a symbol held in both books — or
    twice in one book — never cross-writes.

    execute_dml() (not run_raw_sql) because a bare UPDATE with no RETURNING makes
    run_raw_sql's fetchall() raise and _db() roll back — the silent-fail bug this
    function's history documents.
    """
    table  = "portfolio" if pos.get("book") == "live" else "paper_portfolio"
    row_id = pos.get("id")
    if row_id is None:
        log.warning("persist_trail_state(%s): no row id — skipped", pos.get("symbol"))
        return
    ok = execute_dml(
        f"UPDATE {table} SET peak_price = %s, trail_active = %s WHERE id = %s",
        (str(peak_price), str(trail_active).lower(), row_id),
    )
    if not ok:
        log.warning("persist_trail_state(%s/%s): execute_dml failed",
                    pos.get("book"), pos.get("symbol"))


def _catchup_peak_from_history(positions: list[dict]) -> None:
    """
    Called once at startup. For each open position, query price_history for the
    MAX(high) since entry_date and promote peak_price + trail_active in the DB if
    the historical high exceeds the stored peak. Covers sessions the monitor missed.
    """
    for pos in positions:
        symbol     = pos.get("symbol", "")
        entry_date = pos.get("first_buy_date") or ""
        entry_price = float(pos.get("entry_price") or pos.get("wacc") or 0)
        db_peak    = float(pos.get("peak_price") or entry_price)
        shares     = float(pos.get("shares") or pos.get("total_shares") or 0)
        if not symbol or not entry_date:
            continue
        try:
            rows = run_raw_sql("""
                SELECT MAX(CAST(high AS numeric)) AS max_high
                FROM price_history
                WHERE symbol = %s AND date >= %s
            """, (symbol, entry_date))
            if not rows or not rows[0].get("max_high"):
                continue
            hist_peak = float(rows[0]["max_high"])
            if hist_peak > db_peak:
                # Same net-of-cost arming bar as _check_guard() — must match or this
                # can latch trail_active True off a raw move that never actually
                # cleared the arm threshold (it's a one-way flag, never reset to
                # False once set). hist_peak is a DAILY intraday high, so the
                # ARM_MARGIN cushion matters even more here.
                from modules.trading_core import hold_days
                hd    = hold_days(entry_date)
                trail = _net_profit_pct(entry_price, hist_peak, shares, hd) >= TRAIL_ACTIVATE_PCT + TRAIL_ARM_MARGIN
                _persist_trail_state(pos, hist_peak, trail)
                pos["peak_price"]   = hist_peak
                pos["trail_active"] = trail
                log.info("Catchup %s: peak promoted %.2f → %.2f (trail_active=%s)",
                         symbol, db_peak, hist_peak, trail)
        except Exception as e:
            log.warning("catchup_peak_from_history(%s): %s", symbol, e)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 3 — LTP & MARKET WATCH (DB read — no API call)
# ═══════════════════════════════════════════════════════════════════════════

def _get_ltp_from_db(symbol: str) -> Optional[dict]:
    """
    Read latest row for symbol from atrad_market_watch.
    Returns dict with ltp, vwap, vwap_dev, bid_price, bid_qty, ask_price, ask_qty,
    low_dpr, high_dpr, dpr_proximity, or None if not found.
    """
    try:
        rows = run_raw_sql("""
            SELECT ltp, vwap, vwap_dev, bid_price, bid_qty, ask_price, ask_qty,
                   low_dpr, high_dpr, dpr_proximity, time
            FROM atrad_market_watch
            WHERE symbol = %s
            ORDER BY id DESC LIMIT 1
        """, (symbol,))
        if not rows:
            return None
        r = rows[0]
        return {k: float(v) if v not in (None, "", "None") else 0.0 for k, v in r.items()
                if k != "time"} | {"time": r.get("time", "")}
    except Exception as e:
        log.error("get_ltp_from_db(%s): %s", symbol, e)
        return None


def _ltp_age_seconds(time_str: str) -> Optional[float]:
    """
    Age in seconds of an atrad_market_watch row given its `time` column, which
    is a clock time only ("HH:MM:SS[.ffffff]") in NST. Combined with today's NST
    date. Returns None if unparseable. A tiny negative (clock skew) clamps to 0;
    a large negative (row is from before midnight / stale day) returns a big age.
    """
    if not time_str:
        return None
    s = str(time_str).strip()
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s, fmt).time()
            break
        except ValueError:
            t = None
    if t is None:
        return None
    now = datetime.now(tz=NST)
    stamped = now.replace(hour=t.hour, minute=t.minute, second=t.second,
                          microsecond=t.microsecond)
    age = (now - stamped).total_seconds()
    if age < -1:
        # stamped is in the "future" today → the row is from a previous day
        return age + 86400
    return max(0.0, age)


def _live_quote_is_fresh(symbol: str, mw: dict, state: dict) -> bool:
    """
    Decide whether a getQuickWatch quote is live or frozen.

    getQuickWatch keeps serving the last snapshot with code=0 when the feed
    dies / the symbol halts / the data session soft-expires — a single read
    can't tell that from a real quote, so this checks across cycles. Mirrors
    the DB-fallback freshness rule so the live path is no longer the one hole
    where a stale price can arm or fire the trail (2026-09-07 GILB incident).

      1. lasttradedtime age  — if the exchange's own last-trade time for the
         symbol is older than GUARD_STALE_LTP_SEC, the price is stale. Skipped
         when the field is missing or in an unparseable format.
      2. payload freeze      — if ltp/bid/ask/vwap/volume are byte-identical to
         the previous cycle and have stayed that way for at least
         GUARD_FROZEN_QUOTE_SEC, the feed is frozen. This is the backstop for
         when (1) can't be evaluated.

    Mutates state["q_fp"] / state["q_fp_since"]. Returns True when it can't
    tell yet (first time this fingerprint is seen). A False result restricts
    the guard to the hard stop only for this cycle.
    """
    now = time.time()
    fp = (
        round(float(mw.get("ltp") or 0), 4),
        round(float(mw.get("bid_price") or 0), 4),
        round(float(mw.get("ask_price") or 0), 4),
        round(float(mw.get("vwap") or 0), 4),
        round(float(mw.get("volume") or 0), 4),
    )
    if state.get("q_fp") != fp:
        state["q_fp"] = fp
        state["q_fp_since"] = now
        frozen = False
    else:
        frozen = (now - state.get("q_fp_since", now)) >= GUARD_FROZEN_QUOTE_SEC

    ltt_age     = _ltp_age_seconds(mw.get("lasttradedtime", ""))
    trade_stale = ltt_age is not None and ltt_age > GUARD_STALE_LTP_SEC

    if frozen or trade_stale:
        log.warning(
            "%s: live quote STALE (frozen=%s for %ds, last_trade_age=%s) — "
            "guard restricted to hard stop this cycle",
            symbol, frozen, int(now - state.get("q_fp_since", now)),
            "?" if ltt_age is None else int(ltt_age),
        )
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 4 — PRE-SESSION FRAGILITY FLAG (from yesterday's floorsheet_signals)
# ═══════════════════════════════════════════════════════════════════════════

def _load_fragility_flags(symbols: list[str]) -> dict[str, bool]:
    """
    Load yesterday's floorsheet_signals broker_concentration for open positions.
    Returns {symbol: is_fragile} where is_fragile = broker_concentration > 0.6
    for 3+ consecutive days (single dominant buyer).
    Called once at startup — not every cycle.
    """
    flags = {s: False for s in symbols}
    if not symbols:
        return flags
    try:
        placeholders = ",".join(["%s"] * len(symbols))
        rows = run_raw_sql(f"""
            SELECT symbol, broker_concentration, date
            FROM floorsheet_signals
            WHERE symbol IN ({placeholders})
            ORDER BY symbol, date DESC
        """, tuple(symbols))

        from itertools import groupby
        for symbol, group in groupby(rows, key=lambda r: r["symbol"]):
            recent = list(group)[:5]   # last 5 trading days
            high_conc_days = sum(
                1 for r in recent
                if float(r.get("broker_concentration") or 0) > 0.6
            )
            # Fragile if dominant broker for 3+ of last 5 days
            flags[symbol] = high_conc_days >= 3
            if flags[symbol]:
                log.info("FRAGILE flag set for %s (%d/5 days high broker concentration)",
                         symbol, high_conc_days)
    except Exception as e:
        log.warning("load_fragility_flags failed: %s — all flags = False", e)
    return flags


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 5 — MATH: OBI (Exponential Decay Weighted Order Book Imbalance)
# ═══════════════════════════════════════════════════════════════════════════

def _compute_obi(bids: list, asks: list) -> tuple[float, float, float, float, float]:
    """
    Exponential decay weighted OBI using top OBI_LEVELS levels.
    λ=0.5 → level 1 weight=1.0, level 10 weight≈0.011

    Returns:
        obi      ∈ [-1, 1]
        W_bid    (weighted bid qty — used for microprice)
        W_ask    (weighted ask qty — used for microprice)
        best_bid (level 1 bid price)
        best_ask (level 1 ask price)
    """
    if not bids or not asks:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    def _safe(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    W_bid = 0.0
    W_ask = 0.0

    for i, b in enumerate(bids[:OBI_LEVELS]):
        w = math.exp(-OBI_LAMBDA * i)
        W_bid += w * _safe(b.get("qty", 0))

    for i, a in enumerate(asks[:OBI_LEVELS]):
        w = math.exp(-OBI_LAMBDA * i)
        W_ask += w * _safe(a.get("qty", 0))

    total = W_bid + W_ask
    obi   = (W_bid - W_ask) / total if total > 0 else 0.0

    best_bid = _safe(bids[0].get("price", 0)) if bids else 0.0
    best_ask = _safe(asks[0].get("price", 0)) if asks else 0.0

    return obi, W_bid, W_ask, best_bid, best_ask


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 6 — MATH: Microprice (Stoikov)
# ═══════════════════════════════════════════════════════════════════════════

def _compute_microprice(W_bid: float, W_ask: float,
                        best_bid: float, best_ask: float,
                        ltp: float) -> float:
    """
    Stoikov microprice using exponential-decay weighted quantities from OBI.
    microprice = mid + (imbalance - 0.5) × spread

    Returns micro_score ∈ [-1, 1]:
        positive → upward pressure (microprice above LTP)
        negative → downward pressure (microprice below LTP)
    """
    if best_bid <= 0 or best_ask <= 0 or ltp <= 0:
        return 0.0

    mid        = (best_bid + best_ask) / 2.0
    spread     = best_ask - best_bid
    total      = W_bid + W_ask
    imbalance  = W_bid / total if total > 0 else 0.5
    microprice = mid + (imbalance - 0.5) * spread

    if ltp <= 0:
        return 0.0

    micro_dev = (microprice - ltp) / ltp
    # tanh clamp at 1% deviation → ±1
    return math.tanh(micro_dev / 0.01)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 7 — MATH: TIR (Tick Rule Trade Imbalance, 5-min rolling)
# ═══════════════════════════════════════════════════════════════════════════

def _compute_tir(trades: list) -> tuple[float, float]:
    """
    Tick rule aggressor classification on trades from last TIR_WINDOW_S seconds.
    Filters out odd lots (qty < 10).
    Returns:
        tir     ∈ [-1, 1]  (buy pressure minus sell pressure)
        v_total (total volume in window — used for confidence factor)
    """
    if not trades:
        return 0.0, 0.0

    now_ts = time.time()
    cutoff = now_ts - TIR_WINDOW_S

    # Filter to window and non-odd-lot
    recent = []
    for t in trades:
        try:
            qty = float(t.get("qty", 0) or t.get("quantity", 0) or 0)
            if qty < 10:
                continue
            ts = t.get("timestamp", 0) or t.get("time", 0)
            if ts and float(ts) < cutoff:
                continue
            recent.append({"price": float(t.get("price", 0) or t.get("rate", 0) or 0),
                           "qty":   qty})
        except (TypeError, ValueError):
            continue

    if not recent:
        return 0.0, 0.0

    v_buy   = 0.0
    v_sell  = 0.0
    last_dir = "buy"   # initial assumption: first tick is buy
    prev_price = None

    for tick in recent:
        price = tick["price"]
        qty   = tick["qty"]
        if prev_price is None:
            direction = last_dir
        elif price > prev_price:
            direction = "buy"
        elif price < prev_price:
            direction = "sell"
        else:
            direction = last_dir   # zero-tick carry forward

        if direction == "buy":
            v_buy += qty
        else:
            v_sell += qty

        last_dir   = direction
        prev_price = price

    v_total = v_buy + v_sell
    tir     = (v_buy - v_sell) / v_total if v_total > 0 else 0.0
    return tir, v_total


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 8 — MATH: VWAP Score
# ═══════════════════════════════════════════════════════════════════════════

def _compute_vwap_score(ltp: float, vwap: float) -> float:
    """
    VWAP deviation score, tanh clamped at ±2%.
    Returns ∈ [-1, 1].
    """
    if vwap <= 0 or ltp <= 0:
        return 0.0
    dev = (ltp - vwap) / vwap
    return math.tanh(dev / VWAP_CLAMP)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 9 — MATH: Penalty Scores (Circuit Risk + Spread)
# ═══════════════════════════════════════════════════════════════════════════

def _compute_circuit_risk(ltp: float, low_dpr: float, high_dpr: float) -> float:
    """
    Penalty when approaching upper circuit (last 10% of band).
    Returns ∈ [-1, 0].
    """
    band = high_dpr - low_dpr
    if band <= 0 or high_dpr <= 0:
        return 0.0
    upper_dist = (high_dpr - ltp) / band
    if upper_dist < 0.10:
        return -((0.10 - upper_dist) / 0.10)
    return 0.0


def _compute_spread_score(best_bid: float, best_ask: float, mid: float,
                          phase: str = "NORMAL") -> float:
    """
    Liquidity penalty for a wide bid/ask spread. Returns ∈ [-1, 0].

    The CLOSING-phase doubling is applied ONCE by the caller (_compute_gis /
    run_monitor via penalty_mult), so this function must NOT double it too —
    doing both stacked to 4x and pushed healthy positions toward INSTANT_EXIT.
    `phase` is kept only for signature compatibility.
    """
    if mid <= 0 or best_bid <= 0 or best_ask <= 0:
        return 0.0
    spread_bps = (best_ask - best_bid) / mid * 10_000
    return -min(1.0, max(0.0, (spread_bps - SPREAD_OK_BP) / (SPREAD_MAX_BP - SPREAD_OK_BP)))


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 10 — COMPOSITE GIS SCORE
# ═══════════════════════════════════════════════════════════════════════════

def _compute_gis(
    ltp: float,
    vwap: float,
    low_dpr: float,
    high_dpr: float,
    bids: list,
    asks: list,
    trades: list,
    phase: str,
    is_fragile: bool,
) -> dict:
    """
    Compute Global Intraday Score (GIS) for one symbol.
    Returns dict with all component scores + final S + TIR (for instant-exit check).
    """
    result = {
        "obi": 0.0, "tir": 0.0, "vwap_score": 0.0, "micro_score": 0.0,
        "circuit_risk": 0.0, "spread_score": 0.0, "conf": 0.0,
        "S": 0.0, "v_total": 0.0,
        "best_bid": 0.0, "best_ask": 0.0, "W_bid": 0.0, "W_ask": 0.0,
        "dpr_magnet": False,
    }

    # ── DPR magnet check ─────────────────────────────────────────────────────
    band = high_dpr - low_dpr
    upper_dist_pct = (high_dpr - ltp) / ltp if ltp > 0 else 1.0
    dpr_magnet = (upper_dist_pct < DPR_MAGNET_PCT) and (band > 0)
    result["dpr_magnet"] = dpr_magnet

    # ── OBI + microprice ──────────────────────────────────────────────────────
    obi, W_bid, W_ask, best_bid, best_ask = _compute_obi(bids, asks)
    result.update({"obi": obi, "W_bid": W_bid, "W_ask": W_ask,
                   "best_bid": best_bid, "best_ask": best_ask})

    micro_score = _compute_microprice(W_bid, W_ask, best_bid, best_ask, ltp)
    result["micro_score"] = micro_score

    # ── TIR ──────────────────────────────────────────────────────────────────
    tir, v_total = _compute_tir(trades)
    result["tir"]     = tir
    result["v_total"] = v_total

    # ── VWAP ─────────────────────────────────────────────────────────────────
    vwap_score = _compute_vwap_score(ltp, vwap)
    result["vwap_score"] = vwap_score

    # ── Confidence (multiplicative) ───────────────────────────────────────────
    conf = min(1.0, v_total / TIR_MIN_VOL)
    result["conf"] = conf

    # ── Penalties ────────────────────────────────────────────────────────────
    mid          = (best_bid + best_ask) / 2.0 if best_bid and best_ask else ltp
    circuit_risk = _compute_circuit_risk(ltp, low_dpr, high_dpr)
    spread_score = _compute_spread_score(best_bid, best_ask, mid, phase)
    result["circuit_risk"] = circuit_risk
    result["spread_score"] = spread_score

    # ── Phase-adjusted weights ────────────────────────────────────────────────
    if phase == "OPENING":
        # First 15 min: no microstructure — VWAP + microprice only
        S_base = 0.50 * vwap_score + 0.50 * micro_score
        S_adj  = S_base   # no confidence factor (no tick data yet)
    elif dpr_magnet:
        # Near upper circuit: OBI suppressed, TIR dominates
        S_base = 0.00 * obi + 1.00 * tir + 0.0 * vwap_score + 0.0 * micro_score
        S_adj  = conf * S_base
    else:
        w_obi = W_OBI
        w_tir = W_TIR
        # Fragile flag (high broker concentration from floorsheet): reduce OBI, boost TIR
        if is_fragile:
            w_obi -= 0.10
            w_tir += 0.10
        S_base = w_obi * obi + w_tir * tir + W_VWAP * vwap_score + W_MICRO * micro_score
        S_adj  = conf * S_base

    # Closing phase: double the liquidity penalties (circuit + spread). This is
    # the ONLY place the CLOSING doubling is applied — the score functions
    # themselves stay in [-1, 0].
    penalty_mult = 2.0 if phase == "CLOSING" else 1.0
    S = S_adj + penalty_mult * W_CIRCUIT * circuit_risk + penalty_mult * W_SPREAD * spread_score

    # Clamp to [-1, 1]
    S = max(-1.0, min(1.0, S))
    result["S"] = S

    return result


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 11 — STATE MACHINE WITH HYSTERESIS
# ═══════════════════════════════════════════════════════════════════════════

STATES = ["HOLD_STRONG", "HOLD", "CAUTION", "EXIT_EARLY", "IMMEDIATE"]

def _next_state(current: str, S: float) -> str:
    """
    State transition with hysteresis (asymmetric bands).
    Requires 2-cycle persistence (handled by caller).
    """
    if current == "HOLD_STRONG":
        if S < 0.25:  return "HOLD"

    elif current == "HOLD":
        if S > 0.45:   return "HOLD_STRONG"
        if S < -0.05:  return "CAUTION"

    elif current == "CAUTION":
        if S > 0.25:   return "HOLD"
        if S < -0.25:  return "EXIT_EARLY"

    elif current == "EXIT_EARLY":
        if S > 0.05:   return "CAUTION"
        if S < -0.45:  return "IMMEDIATE"

    elif current == "IMMEDIATE":
        if S > 0.05:   return "EXIT_EARLY"

    return current   # no change


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 12 — GUARD: Hard Stop + Trailing Stop
# ═══════════════════════════════════════════════════════════════════════════

def _sanitize_ltp(symbol: str, ltp: float, vwap: float, vwap_dev: float, state: dict) -> float:
    """
    Filter a single anomalous tick out of the price used for peak-tracking /
    guard checks. Mutates state["last_confirmed_ltp"] / state["pending_spike"]
    in place (state is the symbol's entry in the caller's trail_state dict).
    Returns the LTP to actually use this cycle.
    """
    last_confirmed = state.get("last_confirmed_ltp") or ltp
    jump_pct = abs(ltp - last_confirmed) / last_confirmed * 100 if last_confirmed > 0 else 0.0
    suspect = abs(vwap_dev) * 100 > SPIKE_VWAP_DEV_MAX and jump_pct > SPIKE_JUMP_MAX_PCT

    if not suspect:
        state["last_confirmed_ltp"] = ltp
        state["pending_spike"] = None
        return ltp

    pending = state.get("pending_spike")
    if pending is not None:
        if abs(ltp - pending) / pending * 100 <= SPIKE_CONFIRM_PCT:
            log.warning("%s: spike %.2f confirmed on follow-up tick — accepting", symbol, ltp)
            state["last_confirmed_ltp"] = ltp
            state["pending_spike"] = None
            return ltp
        log.warning("%s: anomalous tick %.2f did not confirm (pending was %.2f) — "
                     "using last confirmed %.2f", symbol, ltp, pending, last_confirmed)
        state["pending_spike"] = None
        return last_confirmed

    log.warning("%s: suspect tick %.2f (vwap_dev=%.1f%%, jump=%.1f%% vs last confirmed %.2f) "
                 "— holding for confirmation", symbol, ltp, vwap_dev * 100, jump_pct, last_confirmed)
    state["pending_spike"] = ltp
    return last_confirmed


def _net_profit_pct(entry: float, price: float, shares: float, hold_days_count: int = 0) -> float:
    """
    Real profit % net of brokerage/SEBON/DP fees and CGT (default: 10%
    short-term rate — positions this bot holds are effectively always
    <=365 days) if the position were closed at `price` right now.
    Mirrors the same fee model trading_core.execute_sell() uses to book
    net_pnl, so this stays consistent with the rest of the system's P&L.
    Falls back to raw price % if shares aren't available (e.g. legacy
    live positions with no shares recorded).
    """
    if entry <= 0 or shares <= 0:
        return (price - entry) / entry * 100 if entry > 0 else 0.0
    try:
        from decimal import Decimal
        from modules.trading_core import calc_sell_fees
        d_entry, d_price, d_shares = Decimal(str(entry)), Decimal(str(price)), Decimal(str(shares))
        fees       = calc_sell_fees(d_price, d_shares, d_entry, hold_days_count)
        cost_basis = d_entry * d_shares
        return float(fees["net_pnl"] / cost_basis * 100)
    except Exception as e:
        log.warning("_net_profit_pct fallback to raw %%: %s", e)
        return (price - entry) / entry * 100


def _check_guard(pos: dict, ltp: float, state_store: dict,
                 ltp_is_fresh: bool = True) -> Optional[str]:
    """
    Pure math guard. Priority order:
      1. T+3 settlement lock — nothing fires until shares are sellable.
      2. HARD_STOP     — raw price <= HARD_STOP_PCT (-6%) from entry. Any tick.
      3. TAKE_PROFIT   — net profit >= TAKE_PROFIT_PCT (+15%). Fresh tick only.
      4. TRAILING_STOP — once armed (peak net >= +4%), net P&L drops to the floor
                         = max(TRAIL_MIN_FLOOR_PCT, peak_net - tiered_gap).
    No fixed stop_loss and no target (removed 2026-08-31): between -6% raw and the
    +4% arm bar nothing fires; the only profit exits are TAKE_PROFIT and the trail.
    Returns exit reason string or None. Mutates pos["peak_price"] /
    pos["trail_active"] in place.

    ltp_is_fresh: False when `ltp` is a stale atrad_market_watch fallback
    (live quote API was down and the DB row is older than GUARD_STALE_LTP_SEC).
    A stale tick may still trip the hard stop (unbounded-loss safety), but must
    NOT move the peak, arm/fire the trail, or trip the take-profit — a frozen
    price otherwise acts at a phantom level.
    """
    entry = float(pos.get("entry_price") or pos.get("wacc") or 0)
    if entry <= 0 or ltp <= 0:
        return None

    # T+3 settlement lock — no exit path fires before shares are actually
    # sellable, real loss or not. Released early if meroshare.py's sync has
    # already confirmed the shares present in Meroshare's myPortfolio
    # (real CDSC demat balance) — that's actual proof of settlement, so a
    # fixed day-count is only the fallback for when sync data is stale/
    # unavailable, never a reason to keep waiting once truly settled.
    if not pos.get("demat_confirmed_date"):
        entry_date_str = pos.get("first_buy_date") or pos.get("entry_date")
        if entry_date_str:
            from calendar_guard import trading_days_since
            from datetime import date as _date
            try:
                entry_d = _date.fromisoformat(str(entry_date_str)[:10])
                if trading_days_since(entry_d) < SETTLEMENT_TRADING_DAYS:
                    return None
            except ValueError:
                pass

    profit_pct = (ltp - entry) / entry * 100

    # Hard stop: HARD_STOP_PCT from entry (always fires regardless of GIS, and
    # regardless of tick freshness — capping an unbounded loss beats waiting for
    # a clean quote).
    if profit_pct <= HARD_STOP_PCT:
        return "HARD_STOP"

    # ── Trailing / take-profit bookkeeping. Everything below is on NET profit
    #    (after brokerage, SEBON, DP fee and CGT).
    shares = float(pos.get("shares") or pos.get("total_shares") or 0)
    if pos.get("first_buy_date"):
        from modules.trading_core import hold_days
        hold_days_count = hold_days(pos["first_buy_date"])
    else:
        hold_days_count = 0

    peak = float(pos.get("peak_price") or entry)
    if ltp_is_fresh and ltp > peak:
        pos["peak_price"] = ltp
        peak = ltp

    peak_net_pct = _net_profit_pct(entry, peak, shares, hold_days_count)
    if ltp_is_fresh and peak_net_pct >= TRAIL_ACTIVATE_PCT + TRAIL_ARM_MARGIN:
        pos["trail_active"] = True

    ltp_net_pct = _net_profit_pct(entry, ltp, shares, hold_days_count)

    # Take-profit ceiling — net +15%, any arm state, fresh tick only (a frozen
    # spike must not book the exit early).
    if ltp_is_fresh and ltp_net_pct >= TAKE_PROFIT_PCT:
        return "TAKE_PROFIT"

    if pos.get("trail_active") and ltp_is_fresh:
        # Once armed the floor trails at peak_net − gap, but never below
        # TRAIL_MIN_FLOOR_PCT (net +2%). With arm +4 / band 2 the minimum only
        # binds exactly at peak +4%; above that the floor tracks the peak. The
        # gap tightens for bigger winners (tier 2 / tier 3).
        if peak_net_pct >= TRAIL_TIER3_PCT:
            trail_gap = TRAIL_TIER3_FLOOR_PCT
        elif peak_net_pct >= TRAIL_TIER2_PCT:
            trail_gap = TRAIL_TIER2_FLOOR_PCT
        else:
            trail_gap = TRAIL_FLOOR_PCT
        floor_net_pct = max(TRAIL_MIN_FLOOR_PCT, peak_net_pct - trail_gap)
        if ltp_net_pct <= floor_net_pct:
            return "TRAILING_STOP"

    return None


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 13 — TELEGRAM ALERTS
# ═══════════════════════════════════════════════════════════════════════════

def _send_telegram(text: str) -> None:
    """Send alert to admin via notifier (respects NOTIFY_CHANNEL setting)."""
    try:
        from helper.notifier import send_telegram
        send_telegram(text, parse_mode="Markdown")
    except Exception as e:
        log.error("Alert send failed: %s", e)


def _format_guard_alert(symbol: str, reason: str, ltp: float,
                        entry: float, book: str, live_auto_close: bool = False) -> str:
    pnl_pct = (ltp - entry) / entry * 100 if entry > 0 else 0
    mode    = "📄 PAPER" if book == "paper" else "⚡ LIVE"
    emoji   = {"HARD_STOP": "🛑", "TRAILING_STOP": "🔒",
               "TAKE_PROFIT": "🎯"}.get(reason, "⚠️")
    if book == "paper":
        tail = ""
    elif live_auto_close:
        tail = "\n🤖 Auto-close armed — submitting SELL (see next alert)."
    else:
        tail = "\n⚠️ Auto-close OFF — close this manually at your broker."
    return (
        f"{emoji} *{reason}* — {symbol} | {mode}\n"
        f"LTP: {ltp:.2f} | Entry: {entry:.2f} | P&L: {pnl_pct:+.1f}%{tail}"
    )


def _format_intel_alert(symbol: str, old_state: str, new_state: str,
                        gis: dict, book: str) -> str:
    mode  = "📄 PAPER" if book == "paper" else "⚡ LIVE"
    emoji = {
        "HOLD_STRONG": "🟢", "HOLD": "🟢",
        "CAUTION": "🟡", "EXIT_EARLY": "🔴", "IMMEDIATE": "🚨"
    }.get(new_state, "❓")
    S    = gis["S"]
    obi  = gis["obi"]
    tir  = gis["tir"]
    vwap = gis["vwap_score"]
    mic  = gis["micro_score"]
    conf = gis["conf"]
    mag  = " 🧲DPR" if gis.get("dpr_magnet") else ""
    return (
        f"{emoji} *{new_state}* — {symbol} | {mode}{mag}\n"
        f"Score: {S:+.3f} (was {old_state})\n"
        f"OBI: {obi:+.2f} | TIR: {tir:+.2f} | VWAP: {vwap:+.2f} | Micro: {mic:+.2f}\n"
        f"Confidence: {conf:.0%}"
    )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 14 — LIQUIDITY SHOCK DETECTOR
# ═══════════════════════════════════════════════════════════════════════════

def _check_liquidity_shock(symbol: str, current_depth: float,
                           prev_depth_store: dict) -> bool:
    """
    Detect sudden depth collapse (>35% drop in total order book depth).
    Returns True if liquidity shock detected.
    """
    prev = prev_depth_store.get(symbol, current_depth)
    shock = False
    if prev > 0:
        drop = (current_depth - prev) / prev
        if drop < LIQUIDITY_DROP_THRESH:
            shock = True
            log.warning("%s: Liquidity shock detected (depth drop %.1f%%)",
                        symbol, drop * 100)
    prev_depth_store[symbol] = current_depth
    return shock


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 15 — AUTO-CLOSE HANDLERS
# ═══════════════════════════════════════════════════════════════════════════

def _bid_consistent_with_trigger(symbol: str, best_bid: float, trigger_ltp: float,
                                 reason: str) -> bool:
    """
    Sanity-check the execution price against the LTP that fired the guard,
    just before a (real, for live) SELL goes out.

    A best bid far from the trigger LTP means the trigger was stale/frozen or
    the book has since collapsed — acting on it turns a false trigger into a
    real fill at a price the guard never evaluated (the 2026-09-07 GILB case:
    trigger LTP 1121.40, best bid 1095, an hour-old frozen snapshot on both).

      TRAILING_STOP / TAKE_PROFIT — a profit exit is never worth trading on
        inconsistent data: abort if the bid is more than
        GUARD_MAX_TRIGGER_SLIP_PCT away in EITHER direction.
      HARD_STOP — cutting an unbounded loss still beats waiting, so a bid
        BELOW the trigger proceeds; only a bid well ABOVE the trigger aborts
        (the -6% was stale and the real market is far higher).

    Returns True to proceed, False to abort (caller alerts + leaves open).
    A non-positive bid or trigger returns True — the no-bids / fetch-failed
    guards in the caller own that case.
    """
    if best_bid <= 0 or trigger_ltp <= 0:
        return True
    slip_pct = (trigger_ltp - best_bid) / trigger_ltp * 100   # +ve: bid below trigger
    if reason == "HARD_STOP":
        ok = slip_pct >= -GUARD_MAX_TRIGGER_SLIP_PCT           # only a bid far ABOVE aborts
    else:
        ok = abs(slip_pct) <= GUARD_MAX_TRIGGER_SLIP_PCT
    if not ok:
        log.warning("%s %s: best bid %.2f vs trigger LTP %.2f (%.1f%% slip) — "
                    "outside %.0f%% tolerance, refusing auto-close",
                    symbol, reason, best_bid, trigger_ltp, slip_pct,
                    GUARD_MAX_TRIGGER_SLIP_PCT)
    return ok


def _guard_exit_price(book: dict, trigger_ltp: float, near_close: bool,
                      urgent: bool) -> tuple:
    """
    Decide where to post a guard-triggered exit sell.

      urgent (HARD_STOP) or near_close  → best bid: cross the spread for an
          immediate fill. HARD_STOP is unbounded-loss protection (speed beats
          price); the last 15 min of the session (near_close) forces the exit
          out before the Day order would expire unfilled.
      everything else  → lowest ask: a patient limit that never sells below the
          visible offer, so a stale / collapsed bid can't cause a landslide
          fill. This is the 2026-09-07 GILB fix — that close hit bid 1095 when
          the LTP was 1121. Falls back to the trigger LTP if the book has no
          asks at all.

    Returns (price, style_label, is_immediate). is_immediate is True when the
    price crosses the spread (fills now); False for a resting ask limit.
    (None, reason, False) when the book can't support the chosen side.
    """
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if urgent or near_close:
        if not bids:
            return None, "no bids in market", False
        label = "best bid — near close, forcing exit" if near_close else "best bid — hard stop"
        return float(bids[0]["price"]), label, True
    if asks:
        return float(asks[0]["price"]), "lowest ask — patient limit", False
    # No asks. Only post against LTP if the book is otherwise alive (has bids) —
    # a fully empty book means no fill, so leave the position open.
    if bids and trigger_ltp and trigger_ltp > 0:
        return float(trigger_ltp), "LTP — book has no asks", False
    return None, "no asks in market", False


def _book_paper_exit(pos: dict, symbol: str, price: float, reason: str,
                     style: str, sym_state: dict) -> None:
    """
    Record the paper SELL at `price` in the ledger and clear pending state.
    close_type: AUTO_HARD_STOP / AUTO_TAKE_PROFIT / AUTO_TRAILING_STOP.
    """
    from modules.trading_core import execute_sell
    from decimal import Decimal

    sym_state.pop("pending_exit", None)
    telegram_id = pos.get("telegram_id")
    shares = Decimal(str(pos.get("total_shares") or pos.get("shares") or 0))
    if not telegram_id or shares <= 0:
        log.error("Paper exit %s: missing telegram_id or shares", symbol)
        _send_telegram(f"⚠️ Auto-close FAILED for *{symbol}* ({reason}) — no telegram\\_id or shares. Close manually.")
        return
    try:
        r = execute_sell(int(telegram_id), symbol, shares, Decimal(str(price)),
                         close_type=f"AUTO_{reason}")
        em = "🟢" if r["result"] == "WIN" else ("🔴" if r["result"] == "LOSS" else "⚪")
        _send_telegram(
            f"🤖 *AUTO-CLOSE* — {reason}\n\n"
            f"✅ *SELL recorded*\n"
            f"{em} *{symbol}* — {r['result']}\n"
            f"Exit: {float(price):.2f} ({style}) | Shares: {int(shares)}\n"
            f"Net P&L:    NPR {float(r['fees']['net_pnl']):+,.2f}\n"
            f"CGT paid:   NPR {float(r['fees']['cgt']):,.2f}\n"
            f"Capital now: NPR {float(r['cap_after']):,.2f}"
        )
        log.info("Auto-closed %s | reason=%s | price=%s | style=%s | pnl=%s",
                 symbol, reason, price, style, r["fees"]["net_pnl"])
    except Exception as e:
        log.error("Paper exit execute_sell failed for %s: %s", symbol, e)
        _send_telegram(f"⚠️ Auto-close FAILED for *{symbol}* ({reason}): {e}. Close manually.")


def _submit_live_exit(pos: dict, symbol: str, price: float, reason: str,
                      style: str, sym_state: dict, immediate: bool) -> None:
    """
    Submit a REAL ATrad SELL at `price`. Gated by LIVE_AUTO_CLOSE_ENABLED
    (get_setting() returns "false" on any DB error, so an unreadable setting
    never results in a live order) and a Meroshare holding check (fails closed).

    ALWAYS marks sym_state["pending_exit"] after a submit attempt (fixed
    2026-09-11 — previously only the non-immediate/resting path set it, so an
    immediate/urgent HARD_STOP left nothing set and the guard just re-fired
    _submit_live_exit again next cycle, resubmitting a live sell for shares
    that may already be gone; see the SONA incident this fix responds to).
    Follow-up fill confirmation against ATrad's own trade blotter happens in
    _work_live_pending_exit on a long cooldown (LIVE_EXIT_RECHECK_SEC) — not
    every cycle.
    """
    from modules.atrad_scraper import place_sell_order
    from modules.meroshare import verify_live_holding

    if get_setting("LIVE_AUTO_CLOSE_ENABLED", "false").strip().lower() != "true":
        log.warning("[LIVE] %s %s — auto-close OFF, alert only (close manually)", reason, symbol)
        return

    shares = int(float(pos.get("total_shares") or pos.get("shares") or 0))
    if shares <= 0:
        _send_telegram(f"🚨 *[LIVE] {reason}* — *{symbol}*\nNo shares in position. Close manually immediately!")
        return
    if not verify_live_holding(symbol, shares):
        log.error("[LIVE] %s: not confirmed in Meroshare portfolio — refusing to sell", symbol)
        _send_telegram(
            f"🚨 *[LIVE] {reason}* — *{symbol}*\n"
            f"Meroshare's real portfolio does NOT confirm {shares} shares held. "
            f"Refusing to auto-sell — verify manually."
        )
        return

    try:
        result = place_sell_order(symbol, shares, float(price))
    except Exception as e:
        log.error("[LIVE] %s place_sell_order error: %s", symbol, e)
        _send_telegram(f"🚨 *[LIVE] AUTO-CLOSE ERROR* — {reason}\n*{symbol}*: {e}\nClose manually immediately!")
        return

    # Always mark pending — success or rejection — so the guard does not
    # re-fire and resubmit a fresh live sell every ~30s cycle. Follow-up fill
    # confirmation happens in _work_live_pending_exit on a long cooldown.
    now = time.time()
    sym_state["pending_exit"] = {
        "reason": reason, "target": float(price), "since": now, "live": True,
        "shares": shares, "last_checked": now, "checks": 0,
        "submitted": result.get("code") == "0",
    }

    if result.get("code") == "0":
        _send_telegram(
            f"🤖 *[LIVE] AUTO-CLOSE* — {reason}\n\n"
            f"✅ *SELL submitted to ATrad*\n"
            f"*{symbol}* — {shares} shares @ {float(price):.2f} ({style})\n"
            f"Status: {result.get('description', 'accepted')}\n"
            f"⏳ Confirming the fill against the trade blotter in the background "
            f"(~every 30 min) — will report once closed, no need to check in."
        )
        log.info("[LIVE] Auto-closed %s | reason=%s | price=%.2f | style=%s | shares=%d",
                 symbol, reason, float(price), style, shares)
    else:
        log.error("[LIVE] %s rejected: code=%s desc=%s",
                  symbol, result.get("code"), result.get("description"))
        _send_telegram(
            f"🚨 *[LIVE] AUTO-CLOSE FAILED* — {reason}\n"
            f"*{symbol}* rejected by ATrad: {result.get('description', str(result))}\n"
            f"Will re-check and retry roughly every 30 min — "
            f"close manually now if this needs to happen immediately."
        )


def _open_guard_exit(pos: dict, symbol: str, trigger_ltp: float, reason: str,
                     sym_state: dict, near_close: bool) -> None:
    """
    First action when a guard fires. HARD_STOP and the last 15 min of the
    session cross the spread at the best bid for an immediate exit; every other
    trigger posts a patient limit at the lowest ask (paper: booked only once a
    real fill is reachable — see _work_pending_exit; live: a resting Day order).
    """
    from modules.atrad_scraper import fetch_order_book

    is_live = pos.get("book") == "live"
    tag     = "🚨 *[LIVE] " if is_live else "⚠️ *"
    urgent  = reason == "HARD_STOP"

    book = fetch_order_book(symbol)
    if not book:
        # fetch_order_book() returns {} on ANY failure, not just an empty book.
        log.error("%s exit %s: order book fetch failed", symbol, reason)
        _send_telegram(f"{tag}{reason}* — *{symbol}* — order book unavailable "
                       f"(fetch failed). Position stays open. Close manually.")
        return

    price, style, immediate = _guard_exit_price(book, float(trigger_ltp), near_close, urgent)
    if price is None:
        log.warning("%s exit %s: %s — position stays open", symbol, reason, style)
        _send_telegram(f"{tag}{reason}* — *{symbol}* — {style}. Position stays open. "
                       f"Close manually when liquidity returns.")
        return

    # A spread-crossing fill must line up with the trigger price — a stale/frozen
    # trigger or a collapsed book otherwise dumps at a phantom level. A resting
    # ask limit can't landslide, so it skips this check.
    if immediate and not _bid_consistent_with_trigger(symbol, price, float(trigger_ltp), reason):
        _send_telegram(f"{tag}{reason}* — *{symbol}* — best bid {price:.2f} is out of line "
                       f"with the trigger {float(trigger_ltp):.2f} "
                       f"(>{GUARD_MAX_TRIGGER_SLIP_PCT:.0f}% slip). No sell placed — "
                       f"position stays open, review manually.")
        return

    if is_live:
        _submit_live_exit(pos, symbol, price, reason, style, sym_state, immediate)
        return

    # paper: an immediate (or already-marketable — the ask sits at/below where
    # trades are printing) exit books now; otherwise the limit rests and
    # _work_pending_exit books it on a real fill.
    if immediate or float(trigger_ltp) > price:
        _book_paper_exit(pos, symbol, price, reason, style, sym_state)
    else:
        sym_state["pending_exit"] = {"reason": reason, "target": float(price),
                                     "since": time.time(), "live": False}
        _send_telegram(
            f"🤖 *{reason}* — *{symbol}* | 📄 PAPER\n"
            f"Exit posted at {price:.2f} ({style}). Waiting for a fill — "
            f"crosses to the bid if still unfilled near close."
        )
        log.info("Paper exit %s pending at %.2f (%s)", symbol, price, style)


def _work_pending_exit(pos: dict, symbol: str, ltp: float, sym_state: dict,
                       near_close: bool) -> None:
    """
    Follow-up cycles while an exit limit is resting (a guard already fired).
    Paper: book the sell at the target once LTP reaches it, or cross to the
    best bid in the last 15 min of the session. Live: left to ATrad — no fill
    feed yet (see _submit_live_exit).

    pending_exit lives only in memory; a monitor restart forgets it and the
    guard simply re-fires and re-posts the limit next cycle.
    """
    pe = sym_state.get("pending_exit")
    if not pe:
        return
    if pe.get("live"):
        _work_live_pending_exit(pos, symbol, sym_state)
        return

    target = float(pe["target"])
    reason = pe["reason"]
    if ltp >= target:
        _book_paper_exit(pos, symbol, target, reason, "lowest ask — filled", sym_state)
        return
    if near_close:
        from modules.atrad_scraper import fetch_order_book
        book = fetch_order_book(symbol) or {}
        bids = book.get("bids", [])
        if not bids:
            log.warning("Paper exit %s: near close, still unfilled, no bids to cross to", symbol)
            _send_telegram(
                f"⚠️ *{reason}* — *{symbol}* | 📄 PAPER\n"
                f"Near close, exit unfilled at {target:.2f} and NO BIDS to cross to. Stays open."
            )
            return
        _book_paper_exit(pos, symbol, float(bids[0]["price"]), reason,
                         "best bid — crossed near close", sym_state)


def _reconcile_live_close(symbol: str, avg_price: float, shares: float,
                          reason: str) -> bool:
    """
    Write a CONFIRMED live fill into `portfolio` + `market_log`, using the
    real quantity-weighted average executed price from ATrad's own trade
    blotter (fetch_my_trades) — not the intended order price — and the real
    NEPSE fee/CGT schedule (trading_core.calc_sell_fees: brokerage tiers +
    0.015% SEBON + NPR 25 DP + CGT on profit). This is the automated version
    of the manual reconciliation done for the 2026-09-11 SONA/MEN incidents.
    Returns True on a successful DB write.
    """
    from decimal import Decimal
    from datetime import date
    from modules.trading_core import calc_sell_fees

    try:
        rows = run_raw_sql(
            "SELECT entry_price, entry_date FROM portfolio "
            "WHERE symbol=%s AND status='OPEN'", (symbol,))
        if not rows:
            log.error("[LIVE] reconcile %s: no OPEN portfolio row to close", symbol)
            return False
        wacc = Decimal(str(rows[0]["entry_price"]))
        entry_date = str(rows[0]["entry_date"])[:10]
        try:
            hold_days = (date.today() - date.fromisoformat(entry_date)).days
        except ValueError:
            hold_days = 0

        f = calc_sell_fees(Decimal(str(avg_price)), Decimal(str(shares)), wacc, hold_days)
        cost_basis = wacc * Decimal(str(shares))
        pnl_pct = (f["net_pnl"] / cost_basis * 100) if cost_basis else Decimal("0")
        today = _now_nst().strftime("%Y-%m-%d")

        ok = execute_dml(
            "UPDATE portfolio SET status='CLOSED', exit_date=%s, exit_price=%s, "
            "exit_reason=%s, current_price=%s, current_value=%s, pnl_npr=%s, "
            "pnl_pct=%s WHERE symbol=%s AND status='OPEN'",
            (today, str(avg_price), reason, str(avg_price),
             str(f["net_proceeds"]), str(f["net_pnl"]), str(pnl_pct), symbol),
        )
        execute_dml(
            "UPDATE market_log SET exit_price=%s, exit_date=%s, exit_reason=%s, "
            "outcome=%s, actual_pnl=%s WHERE symbol=%s AND outcome='PENDING'",
            (str(avg_price), today, reason,
             ("WIN" if f["net_pnl"] > 0 else "LOSS"), str(f["net_pnl"]), symbol),
        )
        log.info("[LIVE] Reconciled close %s | avg=%.2f | shares=%s | net_pnl=%s",
                 symbol, avg_price, shares, f["net_pnl"])

        # run_eod_audit() only journals positions it finds still OPEN — this
        # position is already CLOSED above, so it would be silently skipped
        # at the next EOD pass and trade_journal would stay blank (the
        # 2026-09-11 SONA gap). Journal it now instead of waiting.
        try:
            from analysis.auditor import record_live_close_journal
            record_live_close_journal(symbol, float(avg_price), today, reason)
        except Exception as e:  # noqa: BLE001
            log.error("[LIVE] trade_journal write failed for %s: %s", symbol, e)

        return bool(ok)
    except Exception as e:  # noqa: BLE001
        log.error("[LIVE] _reconcile_live_close(%s) failed: %s", symbol, e)
        return False


def _work_live_pending_exit(pos: dict, symbol: str, sym_state: dict) -> None:
    """
    Follow-up on a live sell already submitted (or attempted). Checks ATrad's
    own trade blotter (fetch_my_trades) at most once every
    LIVE_EXIT_RECHECK_SEC — NOT every ~30s monitor cycle. That cadence change
    (operator instruction, 2026-09-11) is the actual fix for the SONA
    incident: 11 duplicate live sells + 11 Telegram alerts fired in 5 minutes
    against a position that had already fully filled on the first attempt,
    because nothing before this checked the blotter before retrying.

    Confirmed fill -> reconciles the DB with the real average price and clears
    pending_exit. No fill after LIVE_EXIT_ESCALATE_AFTER silent checks -> one
    escalation, not a message per check. A prior rejection with zero fill
    retries the submission — at most once per cooldown window.
    """
    pe = sym_state.get("pending_exit")
    if not pe:
        return
    now = time.time()
    if now - pe.get("last_checked", 0) < LIVE_EXIT_RECHECK_SEC:
        return
    pe["last_checked"] = now
    pe["checks"] = pe.get("checks", 0) + 1

    from modules.atrad_scraper import fetch_my_trades
    try:
        fills = fetch_my_trades(symbol, side="SELL")
    except Exception as e:
        log.error("[LIVE] fetch_my_trades(%s) failed: %s", symbol, e)
        return

    since = pe["since"] - 120  # small clock-skew margin
    relevant = [f for f in fills if f["timestamp"] >= since]
    filled_qty = sum(f["filled_qty"] for f in relevant)
    wanted = pe.get("shares", 0)

    if wanted > 0 and filled_qty >= wanted:
        total_value = sum(f["filled_qty"] * f["price"] for f in relevant)
        avg_price = total_value / filled_qty
        ok = _reconcile_live_close(symbol, avg_price, filled_qty, pe["reason"])
        sym_state.pop("pending_exit", None)
        if ok:
            _send_telegram(
                f"✅ *[LIVE] CONFIRMED FILLED* — *{symbol}*\n"
                f"{int(filled_qty)} shares @ avg {avg_price:.2f}. "
                f"Position closed & recorded in the database."
            )
        else:
            _send_telegram(
                f"⚠️ *[LIVE] {symbol}* filled on the blotter ({int(filled_qty)} @ "
                f"avg {avg_price:.2f}) but the database update failed — "
                f"close it manually in `portfolio`."
            )
        return

    if pe["checks"] >= LIVE_EXIT_ESCALATE_AFTER:
        _send_telegram(
            f"⚠️ *[LIVE] {pe['reason']}* — *{symbol}* still not filled after "
            f"{pe['checks']} checks (~{pe['checks'] * LIVE_EXIT_RECHECK_SEC // 60} min). "
            f"Please check TMS directly."
        )
        pe["checks"] = 0  # don't re-alert again next cooldown too
        return

    if not pe.get("submitted", True) and filled_qty == 0:
        # the earlier attempt was rejected outright and nothing has filled
        # since — retry submission, gated to once per cooldown by this
        # function only running on that cadence.
        from modules.atrad_scraper import fetch_order_book
        book = fetch_order_book(symbol)
        if book:
            new_price, style, immediate = _guard_exit_price(
                book, float(pe["target"]), False, True)
            if new_price is not None:
                _submit_live_exit(pos, symbol, new_price, pe["reason"], style,
                                  sym_state, immediate)


def _execute_guard_close(pos: dict, symbol: str, ltp: float, reason: str,
                         sym_state: dict, near_close: bool) -> None:
    """Open a guard-triggered exit (paper or live, routed inside _open_guard_exit)."""
    _open_guard_exit(pos, symbol, ltp, reason, sym_state, near_close)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 16 — MAIN MONITOR LOOP
# ═══════════════════════════════════════════════════════════════════════════

def run_monitor(once: bool = False, force_open: bool = False) -> None:
    """
    Main execution monitor loop. Guards PAPER (paper_portfolio) and LIVE
    (portfolio) positions in the same pass — each position row is tagged
    pos["book"]. Paper guard trigger → auto-close in the paper ledger. Live
    guard trigger → alert, plus a real ATrad SELL when LIVE_AUTO_CLOSE_ENABLED
    is "true" (see _live_guard_close; also gated by the T+3 lock and a Meroshare
    holding check). GIS intelligence exits stay alert-only for live.
    once=True       → one full cycle over ALL positions, then exit (for testing)
    force_open=True → bypass the market-hours check (off-hours smoke tests)
    """
    log.info("=" * 65)
    log.info("EXECUTION MONITOR starting | books=PAPER%s | live_auto_close=%s",
             "+LIVE" if get_setting("LIVE_MONITOR_ENABLED", "true").strip().lower() == "true" else "",
             get_setting("LIVE_AUTO_CLOSE_ENABLED", "false").strip().lower())
    log.info("=" * 65)

    from modules.atrad_scraper import fetch_order_book, _ensure_data_session

    # Per-symbol rolling state
    intel_states:    dict[str, str]          = {}   # current GIS state per symbol
    pending_states:  dict[str, tuple]        = {}   # (proposed_state, cycle_count)
    prev_depth:      dict[str, float]        = {}   # for liquidity shock detection
    tir_cache:       dict[str, tuple]        = {}   # (tir, v_total, computed_at)
    fragility_flags: dict[str, bool]         = {}   # loaded once at startup
    fragility_loaded: bool                   = False
    # peak_price/trail_active per symbol, carried across cycles. _load_open_positions
    # reloads fresh rows from the DB every cycle (entry_price, not peak), so without
    # this the trailing stop would forget any peak gain 30s after it happened.
    trail_state:     dict[str, dict]         = {}

    last_intel_time: float = 0.0

    # Prime ONLY the data session (price, order book, trades). The ORDER account
    # (BS92009) is opened lazily by place_sell_order() at the moment of a live
    # sell and never held open just to run the monitor — in paper mode it is
    # never touched at all.
    _ensure_data_session()

    # One-time startup: promote peak_price from price_history for any sessions missed
    _startup_positions = _load_open_positions()
    if _startup_positions:
        _catchup_peak_from_history(_startup_positions)
        log.info("Startup peak catch-up complete for %d position(s)", len(_startup_positions))

    while True:
        cycle_start = time.time()

        if not force_open and not _is_market_open():
            if once:
                log.info("Market closed — exiting (--once mode)")
                return
            log.info("Market closed — sleeping 60s")
            time.sleep(60)
            continue

        phase = _market_phase()

        # ── Load open positions (both books, each tagged pos["book"]) ──────────
        positions = _load_open_positions()
        if not positions:
            log.info("No open positions — sleeping %ds", GUARD_INTERVAL_SEC)
            if once:
                return
            time.sleep(GUARD_INTERVAL_SEC)
            continue

        symbols     = [p.get("symbol", "") for p in positions]
        uniq_syms   = sorted({s for s in symbols if s})
        pos_keys    = {f'{p.get("book", "paper")}:{p.get("symbol", "")}' for p in positions}

        # ── Restore carried-over peak/trail state (fresh rows reset both to entry) ─
        # sym_state also carries frozen-quote + sanitize keys now, and may exist
        # before peak_price/trail_active are set (an early `continue` in the LTP
        # step), so read them defensively rather than by bare subscript.
        for pos in positions:
            saved = trail_state.get(f'{pos.get("book", "paper")}:{pos.get("symbol", "")}')
            if saved:
                if saved.get("peak_price") is not None:
                    pos["peak_price"] = saved["peak_price"]
                if "trail_active" in saved:
                    pos["trail_active"] = saved["trail_active"]
        trail_state = {k: trail_state[k] for k in trail_state if k in pos_keys}

        # ── Load fragility flags once at startup or when positions change ─────
        if not fragility_loaded or set(uniq_syms) != set(fragility_flags.keys()):
            fragility_flags  = _load_fragility_flags(uniq_syms)
            fragility_loaded = True

        # ── Decide whether to recompute TIR this cycle ────────────────────────
        now_ts          = time.time()
        run_intelligence = (now_ts - last_intel_time) >= INTELLIGENCE_INTERVAL_SEC

        # ─────────────────────────────────────────────────────────────────────
        # Per-symbol processing
        # ─────────────────────────────────────────────────────────────────────
        for pos in positions:
            symbol = pos.get("symbol", "")
            if not symbol:
                continue
            pos_book = pos.get("book", "paper")
            pos_key  = f"{pos_book}:{symbol}"

            # ── 1. Get LTP — live getQuickWatch API, fall back to atrad_market_watch
            #      DB row if the live call is down or comes back empty/zero, so the
            #      guard doesn't go completely blind on a live-API hiccup.
            sym_state = trail_state.setdefault(pos_key, {})
            mw = get_ltp_live(symbol)
            ltp_is_fresh = True
            if not mw or float(mw.get("ltp") or 0) <= 0:
                db_mw = _get_ltp_from_db(symbol)
                if db_mw and float(db_mw.get("ltp") or 0) > 0:
                    age = _ltp_age_seconds(db_mw.get("time", ""))
                    ltp_is_fresh = age is not None and age <= GUARD_STALE_LTP_SEC
                    log.warning("%s: live LTP unavailable — using DB fallback (time=%s, age=%ss, fresh=%s)",
                                symbol, db_mw.get("time", "?"),
                                "?" if age is None else int(age), ltp_is_fresh)
                    mw = db_mw
                else:
                    log.warning("%s: no live or DB market watch data — skipping", symbol)
                    continue
            else:
                # Live quote in hand — but a frozen getQuickWatch snapshot reads
                # exactly like a live one on a single call. Restrict the guard to
                # the hard stop when it isn't actually moving.
                ltp_is_fresh = _live_quote_is_fresh(symbol, mw, sym_state)

            ltp      = mw["ltp"]
            vwap     = mw["vwap"]
            vwap_dev = mw.get("vwap_dev", 0.0)
            low_dpr  = mw["low_dpr"]
            high_dpr = mw["high_dpr"]
            entry    = float(pos.get("entry_price") or pos.get("wacc") or 0)

            if ltp <= 0:
                log.warning("%s: LTP=0 — skipping", symbol)
                continue

            # ── 2. GUARD — hard stop / trailing (every cycle) ─────────────────
            # Sanitize the tick first so a single anomalous print (fat-finger
            # odd-lot trade) can't be trusted for peak-tracking or stop checks.
            guard_ltp  = _sanitize_ltp(symbol, ltp, vwap, vwap_dev, sym_state)
            near_close = phase == "CLOSING"

            if sym_state.get("pending_exit"):
                # A guard already fired for this symbol — the exit limit is
                # resting. Don't re-evaluate the guard; just work the order
                # (book on fill / cross to bid near close — paper only).
                _work_pending_exit(pos, symbol, guard_ltp, sym_state, near_close)
            else:
                guard_reason = _check_guard(pos, guard_ltp, {}, ltp_is_fresh=ltp_is_fresh)
                if guard_reason:
                    _lac = (pos_book == "live" and
                            get_setting("LIVE_AUTO_CLOSE_ENABLED", "false").strip().lower() == "true")
                    alert = _format_guard_alert(symbol, guard_reason, guard_ltp, entry, pos_book, _lac)
                    log.warning(alert.replace("*", "").replace("_", " "))
                    _send_telegram(alert)
                    _execute_guard_close(pos, symbol, guard_ltp, guard_reason,
                                         sym_state, near_close)
                    # trail_state[pos_key] is NOT popped here. A close can be
                    # refused or left resting (no bids, book unavailable, bid
                    # inconsistent with the trigger, patient ask limit), leaving
                    # the position OPEN; wiping its frozen-quote / peak / pending
                    # state would hand it a fresh grace window every cycle. When
                    # the sell really books, the row leaves paper_portfolio /
                    # portfolio and the pos_keys filter prunes the state.
                    # Don't skip intelligence after guard — still want GIS info.
                else:
                    sym_state.update({
                        "peak_price":   pos.get("peak_price"),
                        "trail_active": pos.get("trail_active", False),
                    })
                    _persist_trail_state(
                        pos,
                        pos.get("peak_price") or float(pos.get("entry_price") or 0),
                        pos.get("trail_active", False),
                    )

            # ── 3. INTELLIGENCE — GIS score ───────────────────────────────────
            if run_intelligence:
                # Fetch order book (live API call)
                book   = fetch_order_book(symbol)
                bids   = book.get("bids", [])
                asks   = book.get("asks", [])

                # Fetch trades (live API call) — only if not opening phase
                if phase != "OPENING":
                    raw_trades = fetch_trades(symbol) if hasattr(fetch_trades, "__call__") else []
                    # Cache TIR result
                    tir, v_total = _compute_tir(raw_trades)
                    tir_cache[symbol] = (tir, v_total, now_ts)
                else:
                    tir, v_total = 0.0, 0.0
                    tir_cache[symbol] = (0.0, 0.0, now_ts)
            else:
                # Use cached TIR, fetch fresh order book only
                book  = fetch_order_book(symbol)
                bids  = book.get("bids", [])
                asks  = book.get("asks", [])
                cached = tir_cache.get(symbol, (0.0, 0.0, 0))
                tir, v_total = cached[0], cached[1]

            # A failed/empty order-book fetch (session hiccup, non-"0" API code, network
            # blip) must NOT be treated as real zero depth — that previously computed as
            # a ~100% "liquidity shock" and force-escalated the state to EXIT_EARLY on a
            # fetch failure, not a real market event. Skip intelligence for this symbol
            # this cycle instead; the guard (hard stop/trailing) above already ran.
            if not book:
                log.warning("%s: order book fetch failed — skipping intelligence this cycle", symbol)
                continue

            # Depth for liquidity shock check
            total_depth = book.get("total_bid_qty", 0) + book.get("total_ask_qty", 0)
            liq_shock   = _check_liquidity_shock(symbol, total_depth, prev_depth)

            # Compute GIS
            # Build trade dicts from cache if needed
            dummy_trades = [{"price": ltp, "qty": v_total, "timestamp": now_ts}] if v_total else []
            gis = _compute_gis(
                ltp=ltp, vwap=vwap,
                low_dpr=low_dpr, high_dpr=high_dpr,
                bids=bids, asks=asks,
                trades=dummy_trades if not run_intelligence else [],
                phase=phase,
                is_fragile=fragility_flags.get(symbol, False),
            )
            # Inject cached TIR into gis if not freshly computed
            if not run_intelligence:
                gis["tir"]     = tir
                gis["v_total"] = v_total
                gis["conf"]    = min(1.0, v_total / TIR_MIN_VOL)
                # Recompute S with cached TIR
                S_base = W_OBI * gis["obi"] + W_TIR * tir + W_VWAP * gis["vwap_score"] + W_MICRO * gis["micro_score"]
                S_adj  = gis["conf"] * S_base
                penalty_mult = 2.0 if phase == "CLOSING" else 1.0
                S = S_adj + penalty_mult * W_CIRCUIT * gis["circuit_risk"] + penalty_mult * W_SPREAD * gis["spread_score"]
                gis["S"] = max(-1.0, min(1.0, S))

            S   = gis["S"]
            tir = gis["tir"]

            # DPR magnet: suppress exit signals (but not hard stops already handled above)
            dpr_magnet = gis.get("dpr_magnet", False)

            # ── 4. Instant exit override (skip 2-cycle persistence) ───────────
            if S < INSTANT_EXIT_S and tir < INSTANT_EXIT_TIR and not dpr_magnet:
                old_state = intel_states.get(symbol, "HOLD")
                if old_state != "IMMEDIATE":
                    intel_states[symbol] = "IMMEDIATE"
                    alert = _format_intel_alert(symbol, old_state, "IMMEDIATE", gis, pos_book)
                    log.warning(alert.replace("*", "").replace("_", " "))
                    _send_telegram(alert)
                    if pos_book == "live":
                        log.warning("[LIVE] IMMEDIATE EXIT signal %s — alert only (close manually)", symbol)
                continue

            # Liquidity shock: force upgrade toward EXIT
            if liq_shock:
                current = intel_states.get(symbol, "HOLD")
                upgrade_map = {"HOLD_STRONG": "CAUTION", "HOLD": "CAUTION",
                               "CAUTION": "EXIT_EARLY"}
                upgraded = upgrade_map.get(current, current)
                if upgraded != current:
                    intel_states[symbol] = upgraded
                    alert = (f"⚠️ *LIQUIDITY SHOCK* — {symbol}\n"
                             f"Depth collapsed. State: {current} → {upgraded}")
                    _send_telegram(alert)

            # DPR magnet: only allow CAUTION at most during magnet zone
            if dpr_magnet:
                proposed = _next_state(intel_states.get(symbol, "HOLD"), S)
                if proposed in ("EXIT_EARLY", "IMMEDIATE"):
                    # Check if bid depth collapsed (>40% drop) or microprice negative
                    bid_collapse  = (prev_depth.get(f"{symbol}_bid", total_depth) > 0 and
                                     total_depth / prev_depth.get(f"{symbol}_bid", total_depth) < 0.60)
                    micro_neg     = gis["micro_score"] < 0
                    if not (bid_collapse or micro_neg):
                        proposed = "CAUTION"   # suppress to CAUTION in magnet zone
                intel_states[symbol] = proposed
                prev_depth[f"{symbol}_bid"] = total_depth

            # ── 5. 2-cycle persistence state machine ─────────────────────────
            current_state = intel_states.get(symbol, "HOLD")
            proposed      = _next_state(current_state, S)

            if proposed != current_state:
                prev_prop, count = pending_states.get(symbol, (proposed, 0))
                if prev_prop == proposed:
                    count += 1
                else:
                    count = 1
                pending_states[symbol] = (proposed, count)

                if count >= 2:
                    # Confirmed — transition
                    old_state = current_state
                    intel_states[symbol] = proposed
                    pending_states.pop(symbol, None)

                    alert = _format_intel_alert(symbol, old_state, proposed, gis, pos_book)
                    log.info(alert.replace("*", "").replace("_", " "))
                    _send_telegram(alert)

                    if proposed in ("EXIT_EARLY", "IMMEDIATE") and pos_book == "live":
                        log.warning("[LIVE] EXIT signal for %s — alert only (close manually)", symbol)
            else:
                pending_states.pop(symbol, None)   # reset pending if back to current

            log.info("%s | S=%+.3f | OBI=%+.2f TIR=%+.2f VWAP=%+.2f Micro=%+.2f | "
                     "State=%s | Phase=%s | Fragile=%s",
                     symbol, S, gis["obi"], gis["tir"], gis["vwap_score"],
                     gis["micro_score"], intel_states.get(symbol, "HOLD"),
                     phase, fragility_flags.get(symbol, False))

        if run_intelligence:
            last_intel_time = now_ts

        # ── Cycle timing ──────────────────────────────────────────────────────
        elapsed = time.time() - cycle_start
        log.info("Monitor cycle: %.2fs | %d positions | phase=%s",
                 elapsed, len(positions), phase)

        if once:
            log.info("--once mode: exiting after single cycle")
            return

        sleep_time = max(0, GUARD_INTERVAL_SEC - elapsed)
        time.sleep(sleep_time)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION 17 — FETCH TRADES (wrapper — uses atrad_scraper)
# ═══════════════════════════════════════════════════════════════════════════

def fetch_trades(symbol: str) -> list:
    """
    Fetch tick trades for symbol via ATrad getTradesOfDay (DATA session).
    Returns list of dicts with price, qty, timestamp keys.
    Fails silently — returns [] on error.
    """
    try:
        from modules.atrad_scraper import fetch_trades_of_day
        return fetch_trades_of_day(symbol)
    except Exception as e:
        log.error("fetch_trades(%s): %s", symbol, e)
        return []


# ═══════════════════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════
def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from log_config import attach_file_handler
    attach_file_handler(__name__)

    parser = argparse.ArgumentParser(description="NEPSE Execution Monitor")
    parser.add_argument("--once", action="store_true",
                        help="Single cycle then exit (testing)")
    parser.add_argument("--force-open", action="store_true",
                        help="Bypass the market-hours check (off-hours smoke tests)")
    args = parser.parse_args()

    # Pre-flight — exit immediately if there is nothing to guard in either book
    positions = _load_open_positions()
    if not positions:
        log.info("No open positions (paper or live) — execution monitor exiting")
        sys.exit(0)

    n_paper = sum(1 for p in positions if p.get("book") == "paper")
    n_live  = sum(1 for p in positions if p.get("book") == "live")
    log.info("Found %d open position(s) — %d paper, %d live — starting monitor",
             len(positions), n_paper, n_live)

    try:
        run_monitor(once=args.once, force_open=args.force_open)
    except KeyboardInterrupt:
        log.info("Interrupted by user — monitor stopped")


if __name__ == "__main__":
    main()
