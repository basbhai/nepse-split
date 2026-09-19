# -*- coding: utf-8 -*-
"""
scripts/buy_signal_replay.py
Replay every BUY signal in market_log through execution_monitor guard logic
using price_history (daily OHLC). Reports exit reason, exit price, and P&L.

Guard constants mirror execution_monitor.py exactly (2026-08-31 re-validation):
  HARD_STOP      : raw -6%  from entry  (fires on day's LOW)
  TAKE_PROFIT    : net +15% from entry  (fires on day's HIGH)
  TRAIL_ARM      : net +4%  peak        (arm threshold)
  TRAIL_FLOOR    : peak_net - 2%  (floor, min net +2%)
  TRAIL_TIER2    : peak_net >= 8%  → gap 1%
  TRAIL_TIER3    : peak_net >= 10% → gap 0.5%

Daily simulation order per bar:
  1. Check HARD_STOP on LOW  (unbounded-loss safety, any tick)
  2. Update peak from HIGH   (fresh tick — peak only moves on high)
  3. Check TAKE_PROFIT on HIGH
  4. Check TRAILING_STOP on LOW (if trail armed)
  5. No exit → continue

Fees (mirrors trading_core.calc_sell_fees approximate model):
  Sell brokerage: tiered (0.64% ≤50k, 0.40% ≤500k, 0.36% ≤2M, 0.20% ≤10M, 0.10% above)
  SEBON         : 0.015%
  DP fee        : NPR 25
  CGT           : 10% of net gain (short-term, as per execution_monitor comment)
  Cost basis    : entry_price × shares  (buy fees already sunk, not in guard calc)
"""

import json
import sys
from datetime import date, timedelta
from typing import Optional

import requests

REPORTER_URL = "http://192.168.1.200:8766/reporter/run"

# ── Guard constants (must match execution_monitor.py) ─────────────────────────
HARD_STOP_PCT       = -6.0
TAKE_PROFIT_PCT     = 15.0
TRAIL_ACTIVATE_PCT  =  3.0
TRAIL_ARM_MARGIN    =  1.0        # arm bar = 3 + 1 = net +4%
TRAIL_FLOOR_PCT     =  2.0
TRAIL_MIN_FLOOR_PCT =  2.0
TRAIL_TIER2_PCT     =  8.0
TRAIL_TIER2_FLOOR   =  1.0
TRAIL_TIER3_PCT     = 10.0
TRAIL_TIER3_FLOOR   =  0.5


# ── Helpers ───────────────────────────────────────────────────────────────────

def _query(sql: str) -> list[dict]:
    r = requests.post(REPORTER_URL, json={"sql": sql}, timeout=30)
    r.raise_for_status()
    return r.json().get("rows", [])


def _brokerage_rate(value: float) -> float:
    if value <= 50_000:      return 0.0064
    if value <= 500_000:     return 0.0040
    if value <= 2_000_000:   return 0.0036
    if value <= 10_000_000:  return 0.0020
    return 0.0010


def net_pct(entry: float, price: float, shares: float) -> float:
    """Net profit % after sell fees + CGT. Mirrors _net_profit_pct logic."""
    if entry <= 0 or shares <= 0:
        return (price - entry) / entry * 100 if entry > 0 else 0.0
    sell_value   = price * shares
    brokerage    = sell_value * _brokerage_rate(sell_value)
    sebon        = sell_value * 0.00015
    dp           = 25.0
    gross_profit = (price - entry) * shares
    fees         = brokerage + sebon + dp
    cgt          = max(0.0, gross_profit - fees) * 0.10
    net_pnl      = gross_profit - fees - cgt
    cost_basis   = entry * shares
    return (net_pnl / cost_basis) * 100


def trail_floor(peak_net: float) -> float:
    """Compute trailing floor net % given peak_net. Mirrors _check_guard tiering."""
    if peak_net >= TRAIL_TIER3_PCT:
        gap = TRAIL_TIER3_FLOOR
    elif peak_net >= TRAIL_TIER2_PCT:
        gap = TRAIL_TIER2_FLOOR
    else:
        gap = TRAIL_FLOOR_PCT
    return max(TRAIL_MIN_FLOOR_PCT, peak_net - gap)


def price_for_net_pct(entry: float, shares: float, target_net_pct: float,
                       lo: float = 0.1, hi: float = 100_000.0) -> float:
    """Binary-search for the exit price that yields exactly target_net_pct net."""
    for _ in range(60):
        mid = (lo + hi) / 2
        if net_pct(entry, mid, shares) < target_net_pct:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ── Main simulation ────────────────────────────────────────────────────────────

def simulate_position(sym: str, entry: float, shares: float,
                      entry_date: str) -> dict:
    """
    Walk price_history from entry_date and apply guard logic.
    Returns dict with exit_reason, exit_price, exit_date, pnl_pct, days_held.
    """
    rows = _query(f"""
        SELECT date, high, low, close
        FROM price_history
        WHERE symbol = '{sym}' AND date >= '{entry_date}'
        ORDER BY date ASC
    """)

    if not rows:
        return {"exit_reason": "NO_DATA", "exit_price": None,
                "exit_date": None, "pnl_pct": None, "days_held": None}

    peak_price   = entry
    trail_active = False
    arm_bar      = TRAIL_ACTIVATE_PCT + TRAIL_ARM_MARGIN  # net +4%

    for row in rows:
        d   = row["date"]
        hi  = float(row["high"]  or 0)
        lo  = float(row["low"]   or 0)
        cl  = float(row["close"] or 0)

        if hi <= 0 or lo <= 0:
            continue

        entry_d    = date.fromisoformat(entry_date)
        current_d  = date.fromisoformat(d[:10])
        days_held  = (current_d - entry_d).days

        # 1. HARD STOP on low (raw %)
        raw_low_pct = (lo - entry) / entry * 100
        if raw_low_pct <= HARD_STOP_PCT:
            exit_price = entry * (1 + HARD_STOP_PCT / 100)
            return {
                "exit_reason": "HARD_STOP",
                "exit_price":  round(exit_price, 2),
                "exit_date":   d[:10],
                "pnl_pct":     round(net_pct(entry, exit_price, shares), 2),
                "days_held":   days_held,
            }

        # 2. Update peak from today's high (fresh tick)
        if hi > peak_price:
            peak_price = hi

        peak_net = net_pct(entry, peak_price, shares)
        if peak_net >= arm_bar:
            trail_active = True

        # 3. TAKE PROFIT on high
        high_net = net_pct(entry, hi, shares)
        if high_net >= TAKE_PROFIT_PCT:
            exit_price = price_for_net_pct(entry, shares, TAKE_PROFIT_PCT)
            return {
                "exit_reason": "TAKE_PROFIT",
                "exit_price":  round(exit_price, 2),
                "exit_date":   d[:10],
                "pnl_pct":     round(TAKE_PROFIT_PCT, 2),
                "days_held":   days_held,
            }

        # 4. TRAILING STOP on low (if armed)
        if trail_active:
            floor_pct = trail_floor(peak_net)
            low_net   = net_pct(entry, lo, shares)
            if low_net <= floor_pct:
                exit_price = price_for_net_pct(entry, shares, floor_pct)
                return {
                    "exit_reason": "TRAILING_STOP",
                    "exit_price":  round(exit_price, 2),
                    "exit_date":   d[:10],
                    "pnl_pct":     round(floor_pct, 2),
                    "days_held":   days_held,
                    "peak_net":    round(peak_net, 2),
                }

    # No exit triggered — still open at last close
    last_close = float(rows[-1]["close"] or 0)
    last_date  = rows[-1]["date"][:10]
    entry_d    = date.fromisoformat(entry_date)
    last_d     = date.fromisoformat(last_date)
    return {
        "exit_reason": "STILL_OPEN",
        "exit_price":  round(last_close, 2),
        "exit_date":   last_date,
        "pnl_pct":     round(net_pct(entry, last_close, shares), 2),
        "days_held":   (last_d - entry_d).days,
        "peak_net":    round(net_pct(entry, peak_price, shares), 2),
        "trail_armed": trail_active,
    }


def run():
    print("Fetching all BUY signals from market_log …")
    buys = _query("""
        SELECT id, symbol, entry_price, shares, date, outcome, stop_loss, target
        FROM market_log
        WHERE action = 'BUY'
        ORDER BY date ASC, id ASC
    """)
    print(f"Found {len(buys)} BUY signals.\n")

    col = "{:<5} {:<10} {:<12} {:<8} {:<10} {:<16} {:<12} {:<10} {:<10} {:<10}"
    header = col.format(
        "ID", "SYMBOL", "ENTRY_DATE", "ENTRY", "SHARES",
        "EXIT_REASON", "EXIT_DATE", "EXIT_PRICE", "NET_PNL%", "DAYS"
    )
    sep = "-" * len(header)
    print(header)
    print(sep)

    for b in buys:
        sym        = b["symbol"]
        entry      = float(b["entry_price"] or 0)
        shares     = float(b["shares"] or 0)
        entry_date = b["date"][:10]
        ml_id      = b["id"]

        if entry <= 0:
            print(col.format(ml_id, sym, entry_date, "-", "-", "NO_ENTRY_PRICE", "-", "-", "-", "-"))
            continue

        result = simulate_position(sym, entry, shares, entry_date)

        reason      = result["exit_reason"]
        exit_date   = result.get("exit_date") or "-"
        exit_price  = result.get("exit_price")
        pnl         = result.get("pnl_pct")
        days        = result.get("days_held")
        peak_note   = ""
        if "peak_net" in result:
            peak_note = f"  [peak net {result['peak_net']:+.1f}%"
            if "trail_armed" in result:
                peak_note += f", trail={'ON' if result['trail_armed'] else 'OFF'}"
            peak_note += "]"

        ep_str  = f"{exit_price:.2f}" if exit_price is not None else "-"
        pnl_str = f"{pnl:+.2f}%" if pnl is not None else "-"
        d_str   = str(days) if days is not None else "-"

        print(col.format(
            ml_id, sym, entry_date,
            f"{entry:.1f}", f"{int(shares) if shares else '-'}",
            reason, exit_date, ep_str, pnl_str, d_str
        ) + peak_note)

    print(sep)
    print(f"\nConstants used: HARD_STOP={HARD_STOP_PCT}% raw | TAKE_PROFIT={TAKE_PROFIT_PCT}% net | "
          f"TRAIL_ARM=net+{TRAIL_ACTIVATE_PCT + TRAIL_ARM_MARGIN}% | "
          f"TRAIL_FLOOR=net+{TRAIL_MIN_FLOOR_PCT}% (tiered: >{TRAIL_TIER2_PCT}%->{TRAIL_TIER2_FLOOR}%, "
          f">{TRAIL_TIER3_PCT}%->{TRAIL_TIER3_FLOOR}%)")


if __name__ == "__main__":
    run()
