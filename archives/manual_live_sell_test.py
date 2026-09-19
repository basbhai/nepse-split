#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MANUAL live ATrad SELL test — run by hand only.  NOT imported anywhere.

Exercises the real order path end to end:
    1. real ATrad ORDER + DATA login
    2. real getPortfolio on the ORDER account
    3. real order-book fetch for the chosen symbol
    4. assemble the exact SELL request that execution_monitor would submit
    5. DRY RUN by default (intercepts the HTTP POST, prints the form) —
       actually sends only with  --submit  AND a typed confirmation phrase.

Usage
-----
    cd /home/shanvi/nepse-engine
    venv/bin/python archives/manual_live_sell_test.py --symbol SONA --qty 1
    venv/bin/python archives/manual_live_sell_test.py --symbol SONA --qty 1 --price 400
    venv/bin/python archives/manual_live_sell_test.py --symbol SONA --qty 1 --submit   # REAL ORDER

--price omitted  -> uses the current best bid from the order book.
--submit         -> really places the order (market must be open); asks for a
                    confirmation phrase first.  Default qty is 1 share.

This bypasses the execution_monitor guards (T+3 lock, Meroshare holding check,
LIVE_AUTO_CLOSE_ENABLED). It talks to ATrad directly. Handle with care.
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

import modules.atrad_scraper as scr


CONFIRM_PHRASE = "SELL LIVE NOW"


def banner(txt):
    print("\n" + "=" * 68 + f"\n  {txt}\n" + "=" * 68)


def main():
    ap = argparse.ArgumentParser(description="Manual live ATrad SELL test")
    ap.add_argument("--symbol", required=True, help="e.g. SONA")
    ap.add_argument("--qty", type=int, default=1, help="shares to sell (default 1)")
    ap.add_argument("--price", type=float, default=None,
                    help="limit price; default = current best bid")
    ap.add_argument("--submit", action="store_true",
                    help="ACTUALLY place the order (otherwise dry run)")
    args = ap.parse_args()

    sym = args.symbol.upper().strip()

    # ---- 1. login ---------------------------------------------------------
    banner("1. ATrad login")
    ok_o = scr._order_login()
    ok_d = scr._data_login()
    print(f"  order login : {'OK' if ok_o else 'FAILED'}  (user={scr.ORDER_USERNAME})")
    print(f"  data  login : {'OK' if ok_d else 'FAILED'}  (user={scr.DATA_USERNAME})")
    print(f"  ACNT_ID     : {scr.ACNT_ID or '(unset)'}")
    if not ok_o:
        print("  cannot continue without an order session.")
        return 1

    # ---- 2. portfolio on the ORDER account ------------------------------
    banner("2. getPortfolio (ORDER account)")
    try:
        pf = scr.get_portfolio()
        holds = pf.get("holdings", [])
        if not holds:
            print("  (no holdings returned)")
        for h in holds:
            print(f"  {h.get('symbol','?'):8} qty={h.get('quantity','?'):>6} "
                  f"available={h.get('available_qty','?'):>6} ltp={h.get('ltp','?')}")
        match = next((h for h in holds if h.get("symbol") == sym), None)
        if match:
            print(f"\n  -> {sym}: holding {match.get('quantity')} "
                  f"(available {match.get('available_qty')})")
        else:
            print(f"\n  -> {sym} NOT in the ORDER-account portfolio "
                  f"(unsettled or held elsewhere).")
    except Exception as e:
        print(f"  getPortfolio failed: {e}")

    # ---- 3. order book -------------------------------------------------
    banner(f"3. order book — {sym}")
    book = scr.fetch_order_book(sym)
    bids = book.get("bids", []) if book else []
    asks = book.get("asks", []) if book else []
    print(f"  bids: {bids[:5]}")
    print(f"  asks: {asks[:5]}")
    best_bid = float(bids[0]["price"]) if bids else None
    price = args.price if args.price is not None else best_bid
    if price is None:
        print("\n  no best bid and no --price given — cannot build the order "
              "(market probably closed). Re-run with --price <n> to test the "
              "request assembly anyway.")
        return 1

    # ---- 4. assemble the SELL request --------------------------------
    banner("4. SELL request")
    print(f"  {sym}  SELL  {args.qty} @ {price:.2f}   (Day limit order)")

    if not args.submit:
        # DRY RUN — intercept the HTTP POST, run place_sell_order() for real
        captured = {}
        real_post = scr._order_session.post

        def fake_post(url, data=None, **kw):
            captured["url"] = url
            captured["form"] = data
            class _R:
                status_code = 200
                text = '{"code":"0","description":"DRY RUN — not sent"}'
                def json(self_):
                    return {"code": "0", "description": "DRY RUN — not sent"}
            return _R()

        scr._order_session.post = fake_post
        try:
            resp = scr.place_sell_order(sym, args.qty, price)
        finally:
            scr._order_session.post = real_post

        print("\n  --- would POST to ---")
        print(f"  {captured.get('url')}")
        print("  --- form payload ---")
        for k, v in (captured.get("form") or {}).items():
            print(f"    {k:22} = {v!r}")
        print(f"\n  parsed response (stubbed): {resp}")
        print("\n  DRY RUN complete. Nothing was sent. Add --submit to place it.")
        return 0

    # ---- 5. real submit ---------------------------------------------
    banner("5. ⚠️  REAL ORDER SUBMISSION")
    print(f"  About to place a REAL SELL: {sym} {args.qty} @ {price:.2f}")
    print(f"  Type exactly:  {CONFIRM_PHRASE}")
    try:
        typed = input("  > ").strip()
    except EOFError:
        typed = ""
    if typed != CONFIRM_PHRASE:
        print("  phrase mismatch — aborted, nothing sent.")
        return 1

    resp = scr.place_sell_order(sym, args.qty, price)
    print(f"\n  ATrad response: {resp}")
    print("  code '0' = accepted. Check your TMS terminal for the order.")
    return 0 if str(resp.get("code")) == "0" else 1


if __name__ == "__main__":
    sys.exit(main())
