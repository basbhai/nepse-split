#!/usr/bin/env python3
"""
modules/trade_origin.py — classify HOW each buy came about, so the learning hub
can score Claude's calls separately from overrides and ad hoc buys.

Origins (entry_origin):
  CLAUDE_BUY          latest Claude decision on the symbol is a BUY, <= BUY_WINDOW days old
  WAIT_CONDITION_MET  latest decision is a WAIT stamped CONDITION_MET / BOUGHT, <= WAIT_WINDOW days old
  OVERRIDE_WAIT       bought while a WAIT was still active (<= WAIT_WINDOW days old)
  OVERRIDE_AVOID      bought while an AVOID was active (<= WAIT_WINDOW days old)
  ADHOC               no usable Claude decision on the symbol (own idea)
Gemini pre-screen flags are NOT recommendations and are ignored.

Source of truth for the buy itself is the ATrad blotter (trade_fills), not
MeroShare, which only confirms DEMAT holdings ~T+3.

CLI:
  python -m modules.trade_origin backfill [--apply]   classify + fill the journal
  python -m modules.trade_origin daily    [--apply] [--days N]   ATrad order history -> trade_fills (touches ATrad)
  python -m modules.trade_origin selftest
"""
import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("trade_origin")

BUY_WINDOW = 5     # days: a BUY older than this is stale (lesson 127 uses >5)
WAIT_WINDOW = 10   # days a WAIT/AVOID is treated as still active


def _d(s) -> date:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def classify(decisions: list[dict], trade_date: str) -> tuple[str, str, int | None]:
    """decisions: market_log rows (id, date, action, outcome, last_reviewed_date) for one symbol.
    Returns (origin, ref_id, days_since_decision)."""
    td = _d(trade_date)
    rows = [r for r in decisions
            if r.get("action") in ("BUY", "WAIT", "AVOID") and r.get("date") and _d(r["date"]) <= td]
    if not rows:
        return "ADHOC", "", None
    last = max(rows, key=lambda r: (r["date"], int(r["id"])))
    age = (td - _d(last["date"])).days
    act, out = last["action"], (last.get("outcome") or "")
    if act == "BUY" and age <= BUY_WINDOW:
        return "CLAUDE_BUY", str(last["id"]), age
    if act == "WAIT" and age <= WAIT_WINDOW:
        met = out in ("CONDITION_MET", "BOUGHT")
        return ("WAIT_CONDITION_MET" if met else "OVERRIDE_WAIT"), str(last["id"]), age
    if act == "AVOID" and age <= WAIT_WINDOW:
        return "OVERRIDE_AVOID", str(last["id"]), age
    return "ADHOC", str(last["id"]), age   # decision exists but is stale


def _decisions(cur, symbol: str, trade_date: str) -> list[dict]:
    cur.execute(
        "SELECT id, date, action, outcome, last_reviewed_date FROM market_log "
        "WHERE symbol=%s AND action IN ('BUY','WAIT','AVOID') AND date <= %s "
        "ORDER BY date DESC, id DESC LIMIT 20", (symbol, trade_date[:10]))
    return [dict(r) for r in cur.fetchall()]


def origin_for(symbol: str, trade_date: str) -> tuple[str, str, int | None, bool]:
    """Origin for a buy. Uses a real ATrad fill if captured, else infers from the
    entry date. Returns (origin, ref_id, days, inferred)."""
    from db.connection import _db
    with _db() as cur:
        # nearest real BUY fill within +-7 days: the portfolio entry_date comes from a MeroShare sync
        # (T+3 after the fill) or a guessed market_log date, so it rarely equals the fill date exactly
        cur.execute("SELECT entry_origin, origin_ref_id, origin_days FROM trade_fills "
                    "WHERE symbol=%s AND side ILIKE 'B%%' AND entry_origin IS NOT NULL "
                    "AND abs(trade_date::date - %s::date) <= 7 "
                    "ORDER BY abs(trade_date::date - %s::date), id LIMIT 1", (symbol, trade_date[:10], trade_date[:10]))
        r = cur.fetchone()
        if r:
            return r["entry_origin"], r["origin_ref_id"] or "", (int(r["origin_days"]) if r["origin_days"] not in (None, "") else None), False
        o, ref, days = classify(_decisions(cur, symbol, trade_date), trade_date)
        return o, ref, days, True


# journal column <- indicators column, used to fill the entry snapshot when no BUY row exists
_SNAP = {"rsi_entry": "rsi_14", "macd_hist_entry": "macd_cross", "bb_signal_entry": "bb_signal",
         "ema_trend_entry": "ema_trend", "obv_trend_entry": "obv_trend", "atr_pct_entry": "atr_pct"}
# conf_score_entry is ShareSansar CONF (not tech_score) — left blank rather than filled with a different measure


def snapshot(cur, symbol: str, entry_date: str) -> dict:
    """Entry indicators from the latest `indicators` row on/before entry_date."""
    cur.execute("SELECT * FROM indicators WHERE symbol=%s AND date<=%s ORDER BY date DESC LIMIT 1",
                (symbol, entry_date[:10]))
    r = cur.fetchone()
    return {} if not r else {j: str(r[i]) for j, i in _SNAP.items() if r.get(i) not in (None, "")}


def backfill(apply: bool = False, only: str = "") -> list[dict]:
    """Classify every closed journal row; fill blank snapshot fields; set paper_mode from source table."""
    from db.connection import _db
    out = []
    with _db() as cur:
        cur.execute("SELECT * FROM trade_journal WHERE result IN ('WIN','LOSS') AND symbol LIKE %s ORDER BY id",
                    (only + "%",))
        rows = [dict(r) for r in cur.fetchall()]
        for t in rows:
            o, ref, days = classify(_decisions(cur, t["symbol"], t["entry_date"]), t["entry_date"])
            cur.execute("SELECT 1 FROM portfolio WHERE symbol=%s AND exit_date=%s LIMIT 1",
                        (t["symbol"], t["exit_date"]))
            live = cur.fetchone() is not None
            snap = {k: v for k, v in snapshot(cur, t["symbol"], t["entry_date"]).items() if not t.get(k)}
            upd = {"entry_origin": o, "origin_ref_id": ref, "origin_days": "" if days is None else str(days),
                   "origin_inferred": "true", "paper_mode": "false" if live else "true", **snap}
            out.append({"id": t["id"], "symbol": t["symbol"], "entry": t["entry_date"], "origin": o,
                        "age": days, "mode": "LIVE" if live else "paper", "snap_filled": sorted(snap)})
            if apply:
                cols = ", ".join(f"{k}=%s" for k in upd)
                cur.execute(f"UPDATE trade_journal SET {cols} WHERE id=%s", (*upd.values(), t["id"]))
    return out


def daily(apply: bool = False, days: int = 1) -> int:
    """Read ATrad order history for the last `days` days -> trade_fills (BUY and SELL);
    BUY fills are stamped with their origin. Opens the ORDER session (BS92009) —
    run once after close, never in a loop. Re-runs are safe (unique fill key)."""
    import atrad_scraper as atrad
    from db.connection import _db
    to = date.today()
    fills = atrad.fetch_order_history((to - timedelta(days=days)).isoformat(), to.isoformat())
    n = 0
    with _db() as cur:
        for f in fills:
            td = f["trade_date"][:10]
            buy = str(f["side"]).upper().startswith("B")
            o, ref, d = classify(_decisions(cur, f["symbol"], td), td) if buy else ("", "", None)
            log.info("%s %s %s x%s @%s -> %s", f["trade_date"], f["side"], f["symbol"], f["filled_qty"], f["price"], o or "-")
            if apply:
                cur.execute(
                    "INSERT INTO trade_fills (order_id, exchange_order_id, symbol, side, qty, price, value,"
                    " trade_ts, trade_date, entry_origin, origin_ref_id, origin_days) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (f["order_id"], f["exchange_order_id"], f["symbol"], f["side"], str(f["filled_qty"]),
                     str(f["price"]), str(f["value"]), str(f["timestamp"]), td, o, ref,
                     "" if d is None else str(d)))
                n += cur.rowcount
    return n


def entry_from_fills(symbol: str) -> dict:
    """Real entry facts for the open position: BUY fills since the symbol's last live
    journal exit -> {entry_date, allocation_npr, ref}. {} when no fills are stored.
    ponytail: no per-lot matching — a partial sell followed by a re-buy mixes lots."""
    from db.connection import _db
    with _db() as cur:
        cur.execute("SELECT COALESCE(MAX(exit_date),'') AS last FROM trade_journal "
                    "WHERE symbol=%s AND paper_mode='false'", (symbol,))
        last = cur.fetchone()["last"]
        cur.execute("SELECT trade_date, qty, price, origin_ref_id FROM trade_fills "
                    "WHERE symbol=%s AND side ILIKE 'B%%' AND trade_date > %s ORDER BY trade_ts",
                    (symbol, last))
        rows = cur.fetchall()
    if not rows:
        return {}
    return {"entry_date": rows[0]["trade_date"],
            "allocation_npr": f"{sum(float(r['qty']) * float(r['price']) for r in rows):.2f}",
            "ref": rows[0]["origin_ref_id"] or ""}


def selftest():
    d = lambda i, dt, a, o="": {"id": i, "date": dt, "action": a, "outcome": o}
    assert classify([], "2026-09-10")[0] == "ADHOC"
    assert classify([d(1, "2026-09-09", "BUY")], "2026-09-10")[0] == "CLAUDE_BUY"
    assert classify([d(1, "2026-08-01", "BUY")], "2026-09-10")[0] == "ADHOC"          # stale BUY
    assert classify([d(1, "2026-09-05", "WAIT", "CONDITION_MET")], "2026-09-10")[0] == "WAIT_CONDITION_MET"
    assert classify([d(1, "2026-09-05", "WAIT", "PENDING")], "2026-09-10")[0] == "OVERRIDE_WAIT"
    assert classify([d(1, "2026-09-09", "AVOID")], "2026-09-10")[0] == "OVERRIDE_AVOID"
    # a later WAIT supersedes an earlier BUY
    assert classify([d(1, "2026-09-08", "BUY"), d(2, "2026-09-09", "WAIT")], "2026-09-10")[0] == "OVERRIDE_WAIT"
    assert classify([d(1, "2026-09-11", "BUY")], "2026-09-10")[0] == "ADHOC"          # decision after the trade
    print("selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["backfill", "daily", "selftest"])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--days", type=int, default=1)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if a.cmd == "selftest":
        selftest()
    elif a.cmd == "backfill":
        for r in backfill(a.apply):
            print(r)
        print("APPLIED" if a.apply else "PREVIEW only (--apply to write)")
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        print("fills stored:", daily(a.apply, a.days))
