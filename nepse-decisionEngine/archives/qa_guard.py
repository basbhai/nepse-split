# -*- coding: utf-8 -*-
"""QA for the reworked _check_guard: arm +4 / band 2 / floor-min +2 / TP net +15,
no STOP_LOSS, no TARGET_HIT. Tests synthetic cases + the real open positions.
Market is closed -> no order is placed; this only exercises the decision logic.

Run:  cd /home/shanvi/nepse-engine && venv/bin/python archives/qa_guard.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv; load_dotenv()
from datetime import date, timedelta
import modules.execution_monitor as em
from modules.execution_monitor import _check_guard, _net_profit_pct
from sheets import run_raw_sql as q, get_setting

PASS = "\033[32mPASS\033[0m"; FAIL = "\033[31mFAIL\033[0m"
fails = 0
def check(name, got, want):
    global fails
    ok = got == want
    if not ok: fails += 1
    print(f"  [{PASS if ok else FAIL}] {name}: got={got!r} want={want!r}")

OLD = (date.today() - timedelta(days=30)).isoformat()   # past T+3

def pos(entry=100.0, shares=100, peak=None, trail=False, **kw):
    p = {"symbol": "TST", "entry_price": entry, "shares": shares,
         "first_buy_date": OLD, "peak_price": peak or entry, "trail_active": trail}
    p.update(kw); return p

def price_for_net(entry, shares, target_net):
    lo, hi = entry, entry * 3
    for _ in range(60):
        mid = (lo + hi) / 2
        if _net_profit_pct(entry, mid, shares) < target_net: lo = mid
        else: hi = mid
    return mid

print("=== 1. arming bar is net +4%, not +3% ===")
p32 = price_for_net(100, 100, 3.2); p42 = price_for_net(100, 100, 4.2)
g = pos(peak=p32); _check_guard(g, p32, {}); check("peak net +3.2% -> NOT armed", g["trail_active"], False)
g = pos(peak=p42); _check_guard(g, p42, {}); check("peak net +4.2% -> armed", g["trail_active"], True)

print(f"=== 2. take-profit at net +{em.TAKE_PROFIT_PCT:.0f}% ===")
tp = em.TAKE_PROFIT_PCT
p_lo = price_for_net(100, 100, tp - 0.1); p_hi = price_for_net(100, 100, tp + 0.1)
check(f"net +{tp-0.1:.1f}% -> no exit", _check_guard(pos(peak=p_lo), p_lo, {}), None)
check(f"net +{tp+0.1:.1f}% -> TAKE_PROFIT", _check_guard(pos(peak=p_hi), p_hi, {}), "TAKE_PROFIT")
check(f"net +{tp+0.1:.1f}% but STALE tick -> no exit", _check_guard(pos(peak=p_hi), p_hi, {}, ltp_is_fresh=False), None)

print("=== 3. trailing floor = max(+2%, peak_net - 2) ===")
peak5 = price_for_net(100, 100, 5.0)
now_35 = price_for_net(100, 100, 3.5); now_29 = price_for_net(100, 100, 2.9)
check("armed peak+5, now +3.5 (>floor 3) -> hold", _check_guard(pos(peak=peak5, trail=True), now_35, {}), None)
check("armed peak+5, now +2.9 (<floor 3) -> TRAILING_STOP", _check_guard(pos(peak=peak5, trail=True), now_29, {}), "TRAILING_STOP")
peak4 = price_for_net(100, 100, 4.0); now_21 = price_for_net(100, 100, 2.1); now_19 = price_for_net(100, 100, 1.9)
check("armed peak+4, now +2.1 (>min 2) -> hold", _check_guard(pos(peak=peak4, trail=True), now_21, {}), None)
check("armed peak+4, now +1.9 (<min 2) -> TRAILING_STOP", _check_guard(pos(peak=peak4, trail=True), now_19, {}), "TRAILING_STOP")

print("=== 4. no STOP_LOSS / no TARGET_HIT even if present on the row ===")
g = pos(); g["stop_loss"] = 98.0
check("price 97 with stop_loss 98 -> NOT STOP_LOSS", _check_guard(g, 97.0, {}), None)
g = pos(); g["target"] = 103.0
check("price 104 with target 103 -> NOT TARGET_HIT", _check_guard(g, 104.0, {}), None)

print("=== 5. hard stop still fires at raw -6%, on any tick ===")
check("raw -6.1% -> HARD_STOP", _check_guard(pos(), 93.9, {}), "HARD_STOP")
check("raw -6.1% STALE tick -> HARD_STOP (safety)", _check_guard(pos(), 93.9, {}, ltp_is_fresh=False), "HARD_STOP")
check("raw -5.9% -> no exit (dead zone)", _check_guard(pos(), 94.1, {}), None)

print("=== 6. T+3 settlement lock blocks everything ===")
fresh = pos(entry=100, peak=200); fresh["first_buy_date"] = date.today().isoformat()
check("brand-new position, price -50% -> no exit (T+3 lock)", _check_guard(fresh, 50.0, {}), None)

print("\n=== 7. REAL open positions (decision only, market closed) ===")
print(f"  LIVE_MONITOR_ENABLED={get_setting('LIVE_MONITOR_ENABLED','?')}  "
      f"LIVE_AUTO_CLOSE_ENABLED={get_setting('LIVE_AUTO_CLOSE_ENABLED','?')}")
rows = (q("select symbol,total_shares as shares,wacc as entry_price,peak_price,trail_active,"
          "first_buy_date,'paper' as book from paper_portfolio where status='OPEN'") +
        q("select symbol,shares,entry_price,peak_price,trail_active,entry_date as first_buy_date,"
          "'live' as book from portfolio where status='OPEN'"))
from calendar_guard import trading_days_since
for r in rows:
    entry = float(r["entry_price"]); sh = float(r["shares"])
    peak = float(r["peak_price"]) if r["peak_price"] else entry
    p = {"symbol": r["symbol"], "entry_price": entry, "shares": sh,
         "first_buy_date": str(r["first_buy_date"])[:10],
         "peak_price": peak, "trail_active": str(r["trail_active"]).lower() == "true",
         "book": r["book"]}
    tds = trading_days_since(date.fromisoformat(p["first_buy_date"]))
    locked = tds < em.SETTLEMENT_TRADING_DAYS
    hs = _check_guard(dict(p), entry * 0.939, {})
    tp_px = price_for_net(entry, sh, em.TAKE_PROFIT_PCT + 0.1)
    tpr = _check_guard(dict(p), tp_px, {})
    route = ("_auto_close_paper" if p["book"] == "paper"
             else ("_auto_close_live (REAL SELL)" if get_setting("LIVE_AUTO_CLOSE_ENABLED","false").lower() == "true"
                   else "_live_guard_close -> alert only"))
    print(f"\n  {r['symbol']:6} [{p['book']}] entry={entry:.2f} peak={peak:.2f} "
          f"trail_active={p['trail_active']} | T+3 {'LOCKED' if locked else 'clear'} (tds={tds})")
    print(f"     at raw -6.1%            -> {hs}{'  (blocked: T+3)' if locked and hs is None else ''}")
    print(f"     at net +{em.TAKE_PROFIT_PCT+0.1:.1f}% ({tp_px:.1f}) -> {tpr}{'  (blocked: T+3)' if locked and tpr is None else ''}")
    print(f"     if a guard fires       -> routes to {route}")

print(f"\n{'ALL SYNTHETIC TESTS PASS' if fails == 0 else str(fails)+' FAILURES'}")
sys.exit(1 if fails else 0)
