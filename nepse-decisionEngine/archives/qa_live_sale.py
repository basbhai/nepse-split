# -*- coding: utf-8 -*-
"""QA the LIVE auto-sell path for SONA. Market is closed; every real side-effect
(ATrad SELL, Telegram) is stubbed — this only proves the routing + safety gates.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv; load_dotenv()
from datetime import date, timedelta
import modules.execution_monitor as em
import modules.atrad_scraper as scr
import modules.meroshare as mero
from sheets import run_raw_sql as q

PASS = "\033[32mPASS\033[0m"; FAIL = "\033[31mFAIL\033[0m"
fails = 0
def check(name, cond):
    global fails
    if not cond: fails += 1
    print(f"  [{PASS if cond else FAIL}] {name}")

# ── capture side-effects instead of performing them ──────────────────────────
alerts, orders = [], []
em._send_telegram = lambda t: alerts.append(t)
_real_place = getattr(scr, "place_sell_order", None)
def fake_place(symbol, shares, price):
    orders.append((symbol, shares, price))
    return {"code": "0", "description": "STUB accepted"}
scr.place_sell_order = fake_place
em_place = "modules.atrad_scraper" in str(em._auto_close_live.__code__.co_names) or True

# force the live kill-switch ON for the test only
_real_get_setting = em.get_setting
em.get_setting = lambda k, d=None: "true" if k == "LIVE_AUTO_CLOSE_ENABLED" else _real_get_setting(k, d)

sona = q("select symbol,shares,entry_price,peak_price,trail_active,entry_date,total_cost "
         "from portfolio where symbol='SONA' and status='OPEN'")
assert sona, "no open SONA live row"
r = sona[0]
entry = float(r["entry_price"]); sh = int(float(r["shares"]))
base = {"symbol": "SONA", "entry_price": entry, "shares": sh, "total_shares": sh,
        "book": "live", "first_buy_date": str(r["entry_date"])[:10],
        "peak_price": float(r["peak_price"] or entry),
        "trail_active": str(r["trail_active"]).lower() == "true"}
print(f"SONA live: entry {entry} x {sh}sh, bought {base['first_buy_date']}")

# ── 1. real state: T+3 lock -> guard returns None, nothing routed ────────────
print("\n1. Real SONA today (T+3 locked)")
reason = em._check_guard(dict(base), entry * 0.90, {})   # -10% crash
check("guard returns None while settlement-locked", reason is None)

# ── 2. settled + guard fires + Meroshare does NOT confirm -> refuse, no order ─
print("\n2. Settlement lock lifted, HARD_STOP fires, Meroshare unconfirmed")
alerts.clear(); orders.clear()
old = dict(base); old["first_buy_date"] = (date.today() - timedelta(days=30)).isoformat()
reason = em._check_guard(dict(old), entry * 0.90, {})
check("guard now returns HARD_STOP", reason == "HARD_STOP")
mero.verify_live_holding = lambda s, n: False        # unsettled / not in DEMAT
em._execute_guard_close(dict(old), "SONA", entry * 0.90, reason)
check("NO ATrad SELL placed (Meroshare unconfirmed)", orders == [])
check("alert warns Meroshare does not confirm", alerts and "does NOT confirm" in alerts[-1])

# ── 3. Meroshare confirms, but market closed -> no bids -> stays open, no order
print("\n3. Meroshare confirms, market closed (no bids)")
alerts.clear(); orders.clear()
mero.verify_live_holding = lambda s, n: True
scr.fetch_order_book = lambda s: {"bids": [], "asks": []}
em._execute_guard_close(dict(old), "SONA", entry * 0.90, "HARD_STOP")
check("NO order placed when book has no bids", orders == [])
check("alert says NO BIDS / stays open", alerts and "NO BIDS" in alerts[-1])

# ── 4. Meroshare confirms + live bids -> SELL submitted at best bid ──────────
print("\n4. Meroshare confirms + live bids present")
alerts.clear(); orders.clear()
scr.fetch_order_book = lambda s: {"bids": [{"price": 402.5, "qty": 500}], "asks": []}
em._execute_guard_close(dict(old), "SONA", entry * 0.90, "TRAILING_STOP")
check("exactly one SELL submitted", len(orders) == 1)
check("SELL is SONA / 80 sh / best bid 402.5", orders == [("SONA", 80, 402.5)])
check("alert confirms SELL submitted", alerts and "SELL submitted" in alerts[-1])

# ── 5. kill-switch OFF -> alert only, never an order ────────────────────────
print("\n5. LIVE_AUTO_CLOSE_ENABLED = false -> alert only")
alerts.clear(); orders.clear()
em.get_setting = lambda k, d=None: "false" if k == "LIVE_AUTO_CLOSE_ENABLED" else _real_get_setting(k, d)
em._execute_guard_close(dict(old), "SONA", entry * 0.90, "TRAILING_STOP")
check("kill-switch off -> NO order", orders == [])

# ── 6. the REAL DB setting (no monkeypatch) -> routes correctly ─────────────
print("\n6. Real LIVE_AUTO_CLOSE_ENABLED from settings (no monkeypatch)")
em.get_setting = _real_get_setting
real_val = _real_get_setting("LIVE_AUTO_CLOSE_ENABLED", "false")
print(f"   settings.LIVE_AUTO_CLOSE_ENABLED = {real_val!r}")
alerts.clear(); orders.clear()
mero.verify_live_holding = lambda s, n: True
scr.fetch_order_book = lambda s: {"bids": [{"price": 402.5, "qty": 500}], "asks": []}
em._execute_guard_close(dict(old), "SONA", entry * 0.90, "TAKE_PROFIT")
if str(real_val).lower() == "true":
    check("real switch ON -> one SELL submitted (mock)", orders == [("SONA", 80, 402.5)])
else:
    check("real switch OFF -> alert only, no order", orders == [])

print(f"\n{'ALL LIVE-SALE QA PASS' if fails == 0 else str(fails)+' FAILURES'}")
sys.exit(1 if fails else 0)
