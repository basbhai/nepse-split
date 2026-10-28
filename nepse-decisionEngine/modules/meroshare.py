"""
meroshare.py
─────────────────────────────────────────────────────────────────────────────
NEPSE AI Engine
Purpose : Fetch live portfolio combining two working APIs:
          1. Meroshare waccReport   → cost basis (WACC, total cost) per scrip
          2. Meroshare myPortfolio  → real current DEMAT balance + live price

myPortfolio is CDSC-level and broker-agnostic — it reflects the true DEMAT
balance regardless of which broker executed the trade. ATrad's own
getPortfolio (BS92009) was tried first but only reflects trades executed
through that specific broker/TMS, so it silently misses anything bought
elsewhere — confirmed empty even when 5 real positions existed. Don't use
ATrad getPortfolio as the live-holdings source; myPortfolio is authoritative.

Auth:
  - Meroshare: POST to auth URL → token in response header

SOP:
  python meroshare.py               → full sync, write to Neon
  python meroshare.py status         → read from Neon only (no API call)
  python meroshare.py edis           → auto-EDIS: deliver pending sold shares
  python meroshare.py edis --dry-run → auto-EDIS, no mutating POSTs

CREDENTIALS NEEDED IN .env:
  MEROSHARE_USERNAME=
  MEROSHARE_PASSWORD=
  MEROSHARE_DP_ID=
  MEROSHARE_DEMAT=1301180000232764
  MEROSHARE_CLIENT_CODE=11800
─────────────────────────────────────────────────────────────────────────────
"""

import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
load_dotenv()

from sheets import read_tab, upsert_row, run_raw_sql, execute_dml, get_setting

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MEROSHARE] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

NST           = timezone(timedelta(hours=5, minutes=45))
DEMAT         = os.getenv("MEROSHARE_DEMAT", "1301180000232764")
CLIENT_CODE   = os.getenv("MEROSHARE_CLIENT_CODE", "11800")

AUTH_URL          = "https://webbackend.cdsc.com.np/api/meroShare/auth/"
WACC_URL          = "https://webbackend.cdsc.com.np/api/myPurchase/waccReport/"
WACC_SHARE_URL    = "https://webbackend.cdsc.com.np/api/myPurchase/share/"
WACC_SEARCH_URL   = "https://webbackend.cdsc.com.np/api/myPurchase/search/wacc/"
WACC_UPLOAD_URL   = "https://webbackend.cdsc.com.np/api/myPurchase/upload/"
WACC_VIEW_URL     = "https://webbackend.cdsc.com.np/api/myPurchase/view/"
MY_PORTFOLIO_URL  = "https://webbackend.cdsc.com.np/api/meroShareView/myPortfolio/"

# ── EDIS + holding-days (CGT) endpoints ──────────────────────────────────────
MYHOLDINGS_WACC_URL    = "https://webbackend.cdsc.com.np/api/myHoldings/wacc/"
MYHOLDINGS_URL         = "https://webbackend.cdsc.com.np/api/myHoldings/"
MYHOLDINGS_SAVE_URL    = "https://webbackend.cdsc.com.np/api/myHoldings/save/"
MYHOLDINGS_SUMMARY_URL = "https://webbackend.cdsc.com.np/api/myHoldings/summary/"
EDIS_CHECK_URL        = "https://webbackend.cdsc.com.np/api/EDIS/check/"
EDIS_ACTIVE_URL       = "https://webbackend.cdsc.com.np/api/EDIS/transfer/active/"
EDIS_DETAIL_URL       = "https://webbackend.cdsc.com.np/api/EDIS/transfer/detail/"   # + {settlementId}
EDIS_TRANSFER_CHECK_URL = "https://webbackend.cdsc.com.np/api/EDIS/transfer/check/"
EDIS_TRANSFER_URL     = "https://webbackend.cdsc.com.np/api/EDIS/transfer/"

MEROSHARE_HEADERS = {
    "Accept":        "application/json, text/plain, */*",
    "Content-Type":  "application/json",
    "Origin":        "https://meroshare.cdsc.com.np",
    "Referer":       "https://meroshare.cdsc.com.np/",
    "Connection":    "keep-alive",
    "User-Agent":    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}


# ══════════════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Holding:
    symbol:        str
    company:       str   = ""
    shares:        int   = 0
    wacc:          float = 0.0   # from waccReport
    total_cost:    float = 0.0   # from waccReport (totalCost)
    current_price: float = 0.0   # lastTradePrice from TMS
    prev_close:    float = 0.0   # closePrice from TMS
    current_value: float = 0.0   # shares × current_price
    pnl_npr:       float = 0.0   # current_value - total_cost
    pnl_pct:       float = 0.0   # (current_price - wacc) / wacc × 100
    day_change_pct:float = 0.0   # perChange from TMS (daily)
    high:          float = 0.0
    low:           float = 0.0
    source:        str   = "tms+meroshare"
    timestamp:     str   = field(default_factory=lambda:
                           datetime.now(tz=NST).strftime("%Y-%m-%d %H:%M:%S"))

    def to_dict(self) -> dict:
        return {k: str(v) for k, v in asdict(self).items()}


@dataclass
class PortfolioSummary:
    total_holdings:     int   = 0
    total_cost_npr:     float = 0.0
    total_value_npr:    float = 0.0
    total_pnl_npr:      float = 0.0
    total_pnl_pct:      float = 0.0
    holdings:           list  = field(default_factory=list)
    timestamp:          str   = field(default_factory=lambda:
                                 datetime.now(tz=NST).strftime("%Y-%m-%d %H:%M:%S"))


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — MEROSHARE AUTH
# ══════════════════════════════════════════════════════════════════════════════

LOGIN_MAX_ATTEMPTS  = 3
LOGIN_RETRY_SECONDS = 12


def _get_token() -> Optional[str]:
    payload = {
        "clientId": os.getenv("MEROSHARE_DP_ID"),
        "username": os.getenv("MEROSHARE_USERNAME"),
        "password": os.getenv("MEROSHARE_PASSWORD"),
    }
    # Meroshare answers a login within ~10s of the previous one with HTTP 500
    # "Unable to process request" (confirmed live 2026-09-24: 0s and 3s gaps
    # failed, 10s worked) — summary_workflow logs in for EDIS then again for
    # the portfolio sync back to back. Retry ONLY that 500, spaced out; a 401/403
    # (wrong credentials) is never retried, to avoid tripping an account lockout.
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        try:
            r = requests.post(AUTH_URL, json=payload,
                              headers=MEROSHARE_HEADERS, timeout=15)
            if r.status_code == 500 and attempt < LOGIN_MAX_ATTEMPTS:
                log.warning("Meroshare login 500 (attempt %d/%d) — retrying in %ds",
                            attempt, LOGIN_MAX_ATTEMPTS, LOGIN_RETRY_SECONDS)
                time.sleep(LOGIN_RETRY_SECONDS)
                continue
            if r.status_code != 200:
                log.error("Meroshare login failed: %d — %s", r.status_code, r.text[:200])
                return None
            token = r.headers.get("Authorization")
            if not token:
                log.error("Token not in response headers")
                return None
            log.info("Meroshare login OK")
            return token
        except Exception as e:
            log.error("Meroshare login error: %s", e)
            return None
    return None


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — WACC REPORT (symbols + shares + cost)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_wacc_report(token: str) -> dict[str, dict]:
    """
    Returns dict keyed by symbol:
    { "NABIL": { wacc, shares, total_cost }, ... }
    """
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        r = requests.post(WACC_URL, headers=headers,
                          json={"demat": DEMAT}, timeout=15)
        if r.status_code != 200:
            log.error("waccReport failed: %d", r.status_code)
            return {}

        data     = r.json()
        records  = data.get("waccReportResponse", [])
        result   = {}

        for rec in records:
            symbol = rec.get("scrip", "").upper().strip()
            if not symbol:
                continue
            result[symbol] = {
                "wacc":       float(rec.get("averageBuyRate", 0)),
                "shares":     int(rec.get("totalQuantity", 0)),
                "total_cost": float(rec.get("totalCost", 0)),
            }

        log.info("waccReport: %d positions", len(result))
        return result

    except Exception as e:
        log.error("waccReport error: %s", e)
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2B — AUTO-COMPLETE PENDING WACC
# A scrip only shows up in waccReport once its purchase-rate transactions
# have been confirmed. myPurchase/share/ lists scrips still waiting on that
# confirmation; for each one, search -> upload (confirm at the known rate,
# unchanged) -> view completes it, exactly mirroring the manual flow in the
# Meroshare UI.
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_pending_wacc_scrips(token: str) -> list[str]:
    """Scrips with unconfirmed WACC transactions. Empty list on any failure."""
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        r = requests.post(WACC_SHARE_URL, headers=headers,
                          json={"isFilterByAllScript": False}, timeout=15)
        if r.status_code != 200:
            log.warning("myPurchase/share failed: %d", r.status_code)
            return []
        return list(r.json() or [])
    except Exception as e:
        log.warning("myPurchase/share error: %s", e)
        return []


def _confirm_wacc_for_scrip(token: str, scrip: str) -> Optional[dict]:
    """
    Confirm all pending purchase-rate transactions for one scrip at their
    known rate (no rate changes), then return the finalized WACC summary.
    Returns None on any failure — never raises.
    """
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        r = requests.post(WACC_SEARCH_URL, headers=headers,
                          json={"demat": DEMAT, "scrip": scrip}, timeout=15)
        if r.status_code != 200:
            log.error("WACC search failed for %s: %d", scrip, r.status_code)
            return None

        pending = r.json().get("waccUpdateResponse", []) or []
        if not pending:
            log.info("  %-10s — no pending WACC transactions", scrip)
            return r.json().get("waccSummaryResponse")

        # Confirm at the rate Meroshare already knows (rate/userPrice
        # untouched) — this only accepts the pending transaction into the
        # WACC calc, it doesn't change the price. isEdit:True is literally
        # the "confirm" flag: the record comes back from search with it
        # False, and flipping it True + adding remarks is what the manual
        # Meroshare UI does when you click through the same confirmation.
        upload_payload = [{**rec, "isEdit": True, "remarks": ""} for rec in pending]

        r2 = requests.post(WACC_UPLOAD_URL, headers=headers, json=upload_payload, timeout=15)
        if r2.status_code != 202:
            log.error("WACC upload failed for %s: %d — %s", scrip, r2.status_code, r2.text[:200])
            return None
        log.info("  %-10s — WACC confirmed (%d transaction(s))", scrip, len(pending))

        r3 = requests.post(WACC_VIEW_URL, headers=headers,
                           json={"demat": DEMAT, "scrip": scrip}, timeout=15)
        if r3.status_code != 200:
            log.error("WACC view failed for %s: %d", scrip, r3.status_code)
            return None
        return r3.json()

    except Exception as e:
        log.error("WACC confirm error for %s: %s", scrip, e)
        return None


def _complete_pending_wacc(token: str) -> dict:
    """
    Auto-complete WACC confirmation for every scrip that's pending it.
    Returns {scrip: finalized_wacc_dict_or_None}. Never raises — a failure
    on one scrip doesn't block the rest or the caller's sync().
    """
    pending_scrips = _fetch_pending_wacc_scrips(token)
    if not pending_scrips:
        log.info("No pending WACC confirmations")
        return {}

    log.info("Pending WACC confirmations: %s", pending_scrips)
    results = {}
    for scrip in pending_scrips:
        results[scrip] = _confirm_wacc_for_scrip(token, scrip)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — MEROSHARE MY PORTFOLIO (authoritative shares + live price)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_my_portfolio(token: str) -> dict[str, dict]:
    """
    Real current DEMAT balance via CDSC meroShareView/myPortfolio —
    broker-agnostic (unlike ATrad's getPortfolio, which only reflects trades
    executed through that specific broker/TMS and silently misses anything
    bought elsewhere). This is the authoritative "what do I actually own"
    source. Cost basis (wacc/total_cost) still comes from Meroshare's
    waccReport — myPortfolio has no purchase-price data.

    Returns dict keyed by symbol:
    { "NABIL": { shares, current_price, market_value, company }, ... }
    """
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        r = requests.post(MY_PORTFOLIO_URL, headers=headers, json={
            "sortBy":   "script",
            "demat":    [DEMAT],
            "clientCode": CLIENT_CODE,
            "page":     1,
            "size":     200,
            "sortAsc":  True,
        }, timeout=15)
        if r.status_code != 200:
            log.error("myPortfolio failed: %d", r.status_code)
            return {}

        rows   = r.json().get("meroShareMyPortfolio", []) or []
        result = {}
        for row in rows:
            symbol = row.get("script", "").upper().strip()
            if not symbol:
                continue
            result[symbol] = {
                "shares":        int(row.get("currentBalance", 0) or 0),
                "current_price": float(row.get("lastTransactionPrice", 0) or 0),
                "market_value":  float(row.get("valueOfLastTransPrice", 0) or 0),
                "company":       row.get("scriptDesc", symbol),
            }
        log.info("myPortfolio: %d positions", len(result))
        return result
    except Exception as e:
        log.error("myPortfolio error: %s", e)
        return {}


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — MERGE + BUILD HOLDINGS
# ══════════════════════════════════════════════════════════════════════════════

def _build_holdings(wacc_data: dict, my_portfolio_data: dict) -> list[Holding]:
    """
    Only build holdings for symbols present in BOTH waccReport AND the real
    DEMAT balance (myPortfolio). Symbols in waccReport but not in myPortfolio
    = already sold, skip them. Shares and current_price come from myPortfolio
    (CDSC-level, broker-agnostic, live); wacc/total_cost stay Meroshare's
    waccReport (myPortfolio has no purchase-price data).
    """
    holdings = []

    for symbol, w in wacc_data.items():
        # ✅ Skip if not in the real DEMAT balance — means already sold
        if symbol not in my_portfolio_data:
            log.info("  %-10s — not in myPortfolio (already sold, skipping)", symbol)
            continue

        m             = my_portfolio_data[symbol]
        shares        = m["shares"]
        wacc          = w["wacc"]
        total_cost    = w["total_cost"]
        current_price = m.get("current_price", 0.0)
        # myPortfolio already gives valueOfLastTransPrice directly — prefer
        # that over shares*price so rounding always matches what CDSC itself
        # reports. Only fall back to computing it if that field is missing.
        current_value = m.get("market_value") or (shares * current_price if current_price > 0 else 0.0)
        pnl_npr       = current_value - total_cost if total_cost > 0 else 0.0
        pnl_pct       = (pnl_npr / total_cost * 100) if total_cost > 0 else 0.0

        h = Holding(
            symbol         = symbol,
            company        = m.get("company", symbol),
            shares         = shares,
            wacc           = round(wacc, 2),
            total_cost     = round(total_cost, 2),
            current_price  = round(current_price, 2),
            current_value  = round(current_value, 2),
            pnl_npr        = round(pnl_npr, 2),
            pnl_pct        = round(pnl_pct, 2),
            source         = "meroshare_portfolio+wacc",
        )
        holdings.append(h)

        log.info("  %-10s %4d shares  WACC %7.2f  LTP %7.2f  P&L %+.1f%%",
                 symbol, shares, wacc, current_price, pnl_pct)

    return holdings


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — WRITE TO NEON
# ══════════════════════════════════════════════════════════════════════════════

def _portfolio_entry_date(symbol: str, fallback: str) -> str:
    """
    Best-effort real purchase date for a NEW portfolio row: the most recent
    market_log BUY for the symbol, else `fallback` (today). Only used on INSERT
    — never on UPDATE — so an existing row's real entry_date is never clobbered.
    A too-recent date only over-holds the T+3 settlement gate for a few days.
    """
    try:
        rows = run_raw_sql(
            "SELECT date FROM market_log WHERE symbol = %s AND action = 'BUY' "
            "ORDER BY id DESC LIMIT 1",
            (symbol,),
        )
        if rows and rows[0].get("date"):
            return str(rows[0]["date"])[:10]
    except Exception as e:
        log.warning("entry_date lookup for %s failed: %s", symbol, e)
    return fallback


REOPEN_COOLDOWN_TRADING_DAYS = 5  # a CLOSED scrip isn't re-added from DEMAT before this
EDIS_FALLBACK_TRADING_DAYS   = 3  # missing from DEMAT this long with no EDIS seen → close


def _recently_closed(exit_date) -> bool:
    from calendar_guard import trading_days_since
    from datetime import date as _date
    try:
        return bool(exit_date) and trading_days_since(_date.fromisoformat(str(exit_date)[:10])) < REOPEN_COOLDOWN_TRADING_DAYS
    except ValueError:
        return False


def _more_than_sold(demat_shares, closed_row_shares) -> bool:
    """Meroshare showing strictly more than was sold means a fresh buy, not settling shares."""
    try:
        return float(demat_shares) > float(closed_row_shares or 0) > 0
    except (TypeError, ValueError):
        return False


def _write_portfolio(holdings: list[Holding]) -> int:
    """
    Sync the DEMAT holdings into `portfolio` (one row per symbol — the table has
    a UNIQUE(symbol) constraint).

    - Symbol already OPEN  → UPDATE cost-basis + market-data, clear missing_since.
    - Symbol exists but CLOSED (re-bought) → reopen: fresh entry_date + reset
      stop/peak/trail.
    - New symbol → INSERT.
    - OPEN symbol not in the DEMAT → first miss stamps missing_since; still missing
      after EDIS_FALLBACK_TRADING_DAYS trading days → CLOSED. Guard: if that would
      close >half the open book at once, treat as bad API response and close none.
    """
    from calendar_guard import trading_days_since
    from datetime import date as _date

    written        = 0
    today          = datetime.now(tz=NST).strftime("%Y-%m-%d")
    active_symbols = {h.symbol for h in holdings}

    try:
        open_rows = run_raw_sql(
            "SELECT id, symbol, missing_since FROM portfolio WHERE status = 'OPEN'"
        ) or []
        missing = [r for r in open_rows if r["symbol"] not in active_symbols]
        due = []
        for row in missing:
            ms = row.get("missing_since")
            try:
                gone_days = trading_days_since(_date.fromisoformat(str(ms)[:10])) if ms else 0
            except ValueError:
                gone_days = 0
            if not ms:
                execute_dml("UPDATE portfolio SET missing_since=%s WHERE id=%s",
                            (today, row["id"]))
                log.info("  %-10s — not in DEMAT (first miss %s), keeping OPEN until "
                         "EDIS or %d trading days", row["symbol"], today,
                         EDIS_FALLBACK_TRADING_DAYS)
            elif gone_days >= EDIS_FALLBACK_TRADING_DAYS:
                due.append(row)
        if len(due) > 1 and len(due) > len(open_rows) / 2:
            names = ", ".join(r["symbol"] for r in due)
            log.error("Sync would close %d of %d open rows at once (%s) — looks like a "
                      "bad Meroshare response, closing NONE", len(due), len(open_rows), names)
            try:
                from helper.notifier import send_telegram
                send_telegram(f"⚠️ *Portfolio sync guard* — {len(due)} of {len(open_rows)} "
                              f"open rows missing from Meroshare ({names}); closed none.",
                              parse_mode="Markdown")
            except Exception as _e:
                log.warning("guard alert failed: %s", _e)
            due = []
        for row in due:
            execute_dml(
                "UPDATE portfolio SET status='CLOSED', exit_date=%s, "
                "exit_reason=COALESCE(exit_reason,'SOLD_NOT_IN_DEMAT') WHERE id=%s",
                (today, row["id"]),
            )
            log.info("  %-10s → marked CLOSED (missing from DEMAT since %s)",
                     row["symbol"], row.get("missing_since"))
    except Exception as e:
        log.warning("Could not mark closed positions: %s", e)

    for h in holdings:
        try:
            existing = run_raw_sql(
                "SELECT id, status, exit_date, shares FROM portfolio WHERE symbol=%s LIMIT 1", (h.symbol,)
            )
            if (existing and str(existing[0].get("status", "")).upper() != "OPEN"
                    and _recently_closed(existing[0].get("exit_date"))
                    and not _more_than_sold(h.shares, existing[0].get("shares"))):
                log.info("  %-10s — closed %s, still in DEMAT (settling) — not re-added",
                         h.symbol, existing[0].get("exit_date"))
                continue
            if existing and str(existing[0].get("status", "")).upper() == "OPEN":
                ok = execute_dml(
                    "UPDATE portfolio SET entry_price=%s, shares=%s, total_cost=%s, "
                    "current_price=%s, current_value=%s, pnl_npr=%s, pnl_pct=%s, "
                    "demat_confirmed_date=COALESCE(demat_confirmed_date, %s), "
                    "missing_since=NULL "
                    "WHERE id=%s",
                    (h.wacc, h.shares, h.total_cost, h.current_price,
                     h.current_value, h.pnl_npr, h.pnl_pct, today, existing[0]["id"]),
                )
            elif existing:  # exists but CLOSED → reopen
                ok = execute_dml(
                    "UPDATE portfolio SET status='OPEN', entry_date=%s, entry_price=%s, "
                    "shares=%s, total_cost=%s, current_price=%s, current_value=%s, "
                    "pnl_npr=%s, pnl_pct=%s, peak_price=NULL, trail_active=NULL, "
                    "trail_stop=NULL, exit_date=NULL, exit_price=NULL, exit_reason=NULL, "
                    "missing_since=NULL, "
                    "demat_confirmed_date=%s "
                    "WHERE id=%s",
                    (_portfolio_entry_date(h.symbol, today), h.wacc, h.shares,
                     h.total_cost, h.current_price, h.current_value, h.pnl_npr,
                     h.pnl_pct, today, existing[0]["id"]),
                )
            else:
                ok = execute_dml(
                    "INSERT INTO portfolio "
                    "(symbol, entry_date, entry_price, shares, total_cost, "
                    " current_price, current_value, pnl_npr, pnl_pct, status, "
                    " demat_confirmed_date) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'OPEN',%s)",
                    (h.symbol, _portfolio_entry_date(h.symbol, today), h.wacc,
                     h.shares, h.total_cost, h.current_price, h.current_value,
                     h.pnl_npr, h.pnl_pct, today),
                )
            if ok:
                written += 1
        except Exception as e:
            log.warning("Failed to write %s: %s", h.symbol, e)

    return written


def close_from_edis(summary: dict, dry_run: bool = False) -> int:
    """
    Close (or shrink) live `portfolio` rows for scrips CDSC shows a sold-share
    EDIS settlement for — real proof of a sale, days before shares leave myPortfolio.
    Called by summary_workflow right after run_edis().

    - qty >= row shares → CLOSED, exit_reason 'SOLD_EDIS'
    - qty <  row shares → partial sale: shares reduced (only for succeeded settlements)
    Returns rows touched. Never raises.
    """
    if dry_run:
        return 0
    touched = 0
    today = datetime.now(tz=NST).strftime("%Y-%m-%d")
    try:
        for st in summary.get("settlements", []) or []:
            for sym, qty in (st.get("by_scrip") or {}).items():
                rows = run_raw_sql(
                    "SELECT id, shares, entry_price FROM portfolio "
                    "WHERE symbol=%s AND status='OPEN' LIMIT 1", (sym,))
                if not rows or qty <= 0:
                    continue
                held = float(rows[0].get("shares") or 0)
                if qty >= held:
                    ok = execute_dml(
                        "UPDATE portfolio SET status='CLOSED', exit_date=%s, "
                        "exit_reason=COALESCE(exit_reason,'SOLD_EDIS') WHERE id=%s",
                        (today, rows[0]["id"]))
                    log.info("  %-10s → CLOSED (EDIS %s: %.0f sh)", sym, st.get("settlement"), qty)
                elif st.get("ok"):
                    left = held - qty
                    ok = execute_dml(
                        "UPDATE portfolio SET shares=%s, total_cost=%s WHERE id=%s",
                        (str(int(left) if left == int(left) else left),
                         str(round(left * float(rows[0].get("entry_price") or 0), 2)),
                         rows[0]["id"]))
                    log.info("  %-10s → shares %.0f → %.0f (partial EDIS %s)",
                             sym, held, left, st.get("settlement"))
                else:
                    continue
                touched += 1 if ok else 0
    except Exception as e:
        log.error("close_from_edis error: %s", e)
    return touched

# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5B — AUTO-EDIS (Electronic Delivery Instruction Slip)
#
# When shares are SOLD (via any broker / TMS) CDSC creates a settlement
# obligation and the shares must be delivered from the DEMAT to the broker's
# pool account before T+2. That delivery authorisation is "EDIS" and is done
# in the Meroshare UI under My EDIS → Transfer Shares.
#
# The full flow, reverse-engineered from the live UI (2026-09-07):
#   GET  EDIS/check/                → 202 ok  |  409 "calculate holding days first"
#   (if 409) for each pending scrip:
#     GET  myHoldings/wacc/         → scrip → isin
#     POST myHoldings/              {isin, demat}  → per-lot rows
#     POST myHoldings/save/         [rows + isEdit:true + previous* + remarks]
#            ^ irreversible CGT declaration — we ONLY ever submit the
#              system-populated holding days (no manual override), which is
#              exactly what "just click through" does in the UI.
#   POST EDIS/transfer/active/      {demat}                 → settlement id(s)
#   for each settlement id:
#     GET  EDIS/transfer/detail/{id}                        → trade items
#     POST EDIS/transfer/check/     [items + isEdit:true]   → items + qty split
#     POST EDIS/transfer/           [checked items]         → 202 ACCEPTED
#
# No transaction PIN is required — the login JWT alone authorises it.
# Idempotent: with nothing pending, every step is a no-op.
# ══════════════════════════════════════════════════════════════════════════════

def _edis_status(token: str) -> tuple[int, str]:
    """GET EDIS/check/ → (status_code, message). 202 = ready, 409 = holding days pending."""
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        r = requests.get(EDIS_CHECK_URL, headers=headers, timeout=15)
        msg = ""
        try:
            msg = (r.json() or {}).get("message", "") or ""
        except Exception:
            msg = r.text[:200]
        return r.status_code, msg
    except Exception as e:
        log.error("EDIS/check error: %s", e)
        return 0, str(e)


def _holding_days_done(token: str, isin: str) -> bool:
    """
    True once holding days are already declared for `isin`.
    myHoldings/summary/ → 200 while a saved declaration exists, 500
    ("Current balance is not matched.") before one does.
    """
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        r = requests.post(MYHOLDINGS_SUMMARY_URL, headers=headers,
                          json={"isin": isin, "demat": DEMAT}, timeout=15)
        return r.status_code == 200 and bool((r.json() or {}).get("success"))
    except Exception:
        return False


def _pending_holding_day_scrips(token: str, check_msg: str = "") -> list[dict]:
    """
    Scrips still needing a holding-days (CGT) declaration before EDIS.

    The EDIS/check 409 message names exactly the undeclared scrips
    ("... FOR THE FOLLOWING SCRIPS TO PROCEED FURTHER: SONA, XYZ") — that's
    the precise list. myHoldings/wacc maps every EDIS-relevant scrip → isin
    (it keeps listing a scrip even after its holding days are saved), so it's
    used only to resolve isins. Falls back to the full wacc list if the
    message can't be parsed.
    Returns [{"scrip": .., "isin": ..}].
    """
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    isin_by_scrip: dict[str, str] = {}
    try:
        r = requests.get(MYHOLDINGS_WACC_URL, headers=headers, timeout=15)
        if r.status_code == 200:
            for x in (((r.json() or {}).get("data") or {}).get("waccResponses", []) or []):
                s, i = str(x.get("scrip", "")).upper().strip(), str(x.get("isin", "")).strip()
                if s and i:
                    isin_by_scrip[s] = i
    except Exception as e:
        log.warning("myHoldings/wacc error: %s", e)

    named: list[str] = []
    if check_msg and ":" in check_msg:
        tail = check_msg.rsplit(":", 1)[1]
        named = [p.strip().upper().rstrip(".") for p in tail.split(",") if p.strip()]

    scrips = named or list(isin_by_scrip)
    out = [{"scrip": s, "isin": isin_by_scrip.get(s, "")} for s in scrips]
    missing = [o["scrip"] for o in out if not o["isin"]]
    if missing:
        log.warning("Holding-days pending but no isin for: %s", ", ".join(missing))
    return [o for o in out if o["isin"]]


def _submit_holding_days(token: str, isin: str, dry_run: bool = False) -> bool:
    """
    Confirm the SYSTEM-POPULATED holding days for one scrip (no override).
    Irreversible. Skips cleanly if already declared. Returns True on success
    (or if nothing to do). Never raises.
    """
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        if _holding_days_done(token, isin):
            log.info("  %s — holding days already declared, skipping", isin)
            return True

        r = requests.post(MYHOLDINGS_URL, headers=headers,
                          json={"isin": isin, "demat": DEMAT}, timeout=15)
        rows = (r.json() or {}).get("data", []) or [] if r.status_code == 200 else []
        if r.status_code != 200 or not rows:
            # 500 "Data not available." also means there's nothing editable —
            # re-check summary/ to decide if that's fine or a real failure.
            ok = _holding_days_done(token, isin)
            log.log(logging.INFO if ok else logging.ERROR,
                    "  %s — myHoldings/ %d, no rows (%s)", isin, r.status_code,
                    "already done" if ok else "unexpected")
            return ok

        payload = [{
            **row,
            "isEdit": True,
            "previousHoldingDays": row.get("holdingDays"),
            "previousHoldingFlag": row.get("stLt"),
            "remarks": "",
        } for row in rows]

        total_qty = sum(float(x.get("qty", 0) or 0) for x in rows)
        if dry_run:
            log.info("  [DRY-RUN] would confirm holding days for %s (%d lot(s), %.0f sh)",
                     isin, len(rows), total_qty)
            return True

        r2 = requests.post(MYHOLDINGS_SAVE_URL, headers=headers, json=payload, timeout=20)
        if r2.status_code != 200 or not (r2.json() or {}).get("success"):
            log.error("myHoldings/save failed for %s: %d — %s", isin, r2.status_code, r2.text[:200])
            return False
        log.info("  ✅ holding days confirmed for %s (%d lot(s), %.0f sh)", isin, len(rows), total_qty)
        return True
    except Exception as e:
        log.error("_submit_holding_days(%s) error: %s", isin, e)
        return False


def _submit_edis_for_settlement(token: str, settlement_id: str, dry_run: bool = False) -> dict:
    """
    Run check + transfer for one settlement id.
    Returns {"settlement": id, "scrips": [..], "qty": N, "ok": bool, "detail": str}.
    """
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    res = {"settlement": settlement_id, "scrips": [], "qty": 0.0, "by_scrip": {},
           "ok": False, "detail": ""}
    try:
        r = requests.get(EDIS_DETAIL_URL + str(settlement_id), headers=headers, timeout=15)
        if r.status_code != 200:
            res["detail"] = f"detail HTTP {r.status_code}"
            return res
        items = r.json() or []
        if not items:
            res["ok"] = True
            res["detail"] = "nothing pending"
            return res

        res["scrips"] = sorted({str(it.get("obligation", {}).get("scriptCode", "")) for it in items})
        res["qty"]    = sum(float(it.get("transferQuantity", 0) or 0) for it in items)
        for it in items:
            sc = str(it.get("obligation", {}).get("scriptCode", "")).upper().strip()
            if sc:
                res["by_scrip"][sc] = res["by_scrip"].get(sc, 0.0) + float(it.get("transferQuantity", 0) or 0)

        check_payload = [{**it, "isEdit": True} for it in items]
        rc = requests.post(EDIS_TRANSFER_CHECK_URL, headers=headers, json=check_payload, timeout=20)
        if rc.status_code != 200:
            res["detail"] = f"transfer/check HTTP {rc.status_code} — {rc.text[:150]}"
            return res
        checked = rc.json() or check_payload

        if dry_run:
            res["ok"] = True
            res["detail"] = f"[DRY-RUN] would transfer {res['qty']:.0f} sh ({', '.join(res['scrips'])})"
            log.info("  %s", res["detail"])
            return res

        rt = requests.post(EDIS_TRANSFER_URL, headers=headers, json=checked, timeout=20)
        body = {}
        try:
            body = rt.json() or {}
        except Exception:
            pass
        if rt.status_code in (200, 202) and str(body.get("status", "")).upper() in ("", "ACCEPTED"):
            res["ok"] = True
            res["detail"] = body.get("message", "ACCEPTED")
            log.info("  ✅ EDIS %s — %.0f sh (%s): %s",
                     settlement_id, res["qty"], ", ".join(res["scrips"]), res["detail"])
        else:
            res["detail"] = f"transfer HTTP {rt.status_code} — {rt.text[:150]}"
            log.error("  ❌ EDIS %s failed: %s", settlement_id, res["detail"])
        return res
    except Exception as e:
        res["detail"] = str(e)
        log.error("_submit_edis_for_settlement(%s) error: %s", settlement_id, e)
        return res


def run_edis(dry_run: bool = False) -> dict:
    """
    Auto-EDIS: deliver every pending sold-share obligation to the broker pool
    account, first confirming holding days where CDSC requires it.

    Safe to call unconditionally every evening — a no-op when nothing is
    pending. Gated by the EDIS_AUTO setting (default "true", toggle on the
    dashboard under ⚡ Live Execution Monitor → "Auto-EDIS after live sales");
    the caller (summary_workflow) additionally only invokes this in LIVE mode.

    Returns a summary dict; never raises.
    """
    summary = {"ran": False, "holding_days": [], "settlements": [], "ok": True, "note": ""}

    if get_setting("EDIS_AUTO", "true").strip().lower() != "true":
        summary["note"] = "EDIS_AUTO disabled"
        log.info("Auto-EDIS disabled via EDIS_AUTO setting — skipped")
        return summary

    log.info("=" * 60)
    log.info("AUTO-EDIS — %s%s", datetime.now(tz=NST).strftime("%Y-%m-%d %H:%M NST"),
             "  [DRY-RUN]" if dry_run else "")
    log.info("=" * 60)

    token = _get_token()
    if not token:
        summary["ok"] = False
        summary["note"] = "no Meroshare token"
        return summary

    # ── Step 1: holding-days gate ────────────────────────────────────────────
    code, msg = _edis_status(token)
    if code == 409 or (code != 202 and "HOLDING DAYS" in msg.upper()):
        pending = _pending_holding_day_scrips(token, msg)
        if not pending:
            summary["ok"] = False
            summary["note"] = f"EDIS blocked ({code}) but no scrips resolved: {msg}"
            log.error(summary["note"])
            return summary
        log.info("Holding days pending for: %s", ", ".join(p["scrip"] for p in pending))
        for p in pending:
            ok = _submit_holding_days(token, p["isin"], dry_run=dry_run)
            summary["holding_days"].append({"scrip": p["scrip"], "ok": ok})
        if not dry_run:
            # The re-check is authoritative: if EDIS is now unblocked, any
            # per-scrip "failure" above was a benign already-done race.
            code, msg = _edis_status(token)
            if code != 202:
                summary["ok"] = False
                summary["note"] = f"still blocked after holding-days save ({code}): {msg}"
                log.error(summary["note"])
                return summary
        elif not all(h["ok"] for h in summary["holding_days"]):
            summary["ok"] = False
    elif code != 202:
        summary["note"] = f"EDIS/check returned {code}: {msg}"
        log.warning(summary["note"])
        # keep going — transfer/active will simply return nothing if truly unusable

    # ── Step 2: pending settlements ─────────────────────────────────────────
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        ra = requests.post(EDIS_ACTIVE_URL, headers=headers, json={"demat": DEMAT}, timeout=15)
        settlements = ra.json() or [] if ra.status_code == 200 else []
    except Exception as e:
        log.error("EDIS/transfer/active error: %s", e)
        settlements = []

    if not settlements:
        summary["note"] = summary["note"] or "no pending settlements"
        log.info("No pending EDIS settlements — nothing to deliver")
        _notify_edis(summary, dry_run)   # no-op unless holding days were touched
        return summary

    summary["ran"] = True
    log.info("Pending settlements: %s",
             ", ".join(s.get("settlementId", "?") for s in settlements))
    for s in settlements:
        sid = s.get("settlementId")
        if not sid:
            continue
        r = _submit_edis_for_settlement(token, sid, dry_run=dry_run)
        summary["settlements"].append(r)
        summary["ok"] = summary["ok"] and r["ok"]

    _notify_edis(summary, dry_run)
    return summary


def _notify_edis(summary: dict, dry_run: bool) -> None:
    """Telegram/Discord alert — only when EDIS actually acted or failed."""
    acted  = [s for s in summary.get("settlements", []) if s.get("scrips")]
    failed = [s for s in summary.get("settlements", []) if not s.get("ok")]
    hd     = summary.get("holding_days", [])
    if not acted and not failed and not hd:
        return
    try:
        from helper.notifier import send_telegram
        tag = "🧪 *Auto-EDIS (dry-run)*" if dry_run else "📤 *Auto-EDIS*"
        lines = [tag]
        for h in hd:
            lines.append(f"  {'✅' if h['ok'] else '❌'} holding days · {h['scrip']}")
        for s in summary.get("settlements", []):
            if not s.get("scrips"):
                continue
            icon = "✅" if s["ok"] else "❌"
            lines.append(f"  {icon} {', '.join(s['scrips'])} · {s['qty']:.0f} sh "
                         f"· `{s['settlement']}` — {s['detail']}")
        if summary.get("note"):
            lines.append(f"  _{summary['note']}_")
        send_telegram("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        log.error("EDIS notify failed: %s", e)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def sync() -> Optional[PortfolioSummary]:
    """Full sync: waccReport + myPortfolio (real DEMAT balance) → Neon. Called by capital_allocator."""
    log.info("=" * 60)
    log.info("PORTFOLIO SYNC — %s", datetime.now(tz=NST).strftime("%Y-%m-%d %H:%M NST"))
    log.info("=" * 60)

    # Step 1: Meroshare token
    token = _get_token()
    if not token:
        log.error("Cannot proceed without Meroshare token")
        return None

    # Step 1b: auto-complete any pending WACC confirmations first — a scrip
    # doesn't appear in waccReport until this is done.
    _complete_pending_wacc(token)

    # Step 2: WACC report — symbols + shares + cost
    wacc_data = _fetch_wacc_report(token)
    if not wacc_data:
        log.error("No WACC data returned")
        return None

    # Step 3: myPortfolio — real DEMAT balance + live price (broker-agnostic)
    my_portfolio_data = _fetch_my_portfolio(token)
    if not my_portfolio_data:
        log.warning("No myPortfolio data — holdings will be empty")

    # Step 4: Merge
    holdings = _build_holdings(wacc_data, my_portfolio_data)
    if not holdings:
        return PortfolioSummary()

    # Step 5: Write to Neon
    written = _write_portfolio(holdings)
    log.info("Written %d positions to Neon", written)

    # Step 6: Summary
    total_cost  = sum(h.total_cost    for h in holdings)
    total_value = sum(h.current_value for h in holdings)
    total_pnl   = total_value - total_cost
    pnl_pct     = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

    summary = PortfolioSummary(
        total_holdings  = len(holdings),
        total_cost_npr  = round(total_cost, 2),
        total_value_npr = round(total_value, 2),
        total_pnl_npr   = round(total_pnl, 2),
        total_pnl_pct   = round(pnl_pct, 2),
        holdings        = holdings,
    )

    log.info("✅ Sync complete — %d positions | Value NPR %.0f | P&L %+.1f%%",
            summary.total_holdings, summary.total_value_npr, summary.total_pnl_pct)
    return summary


def verify_live_holding(symbol: str, min_shares: float = 1) -> bool:
    """
    Confirm `symbol` is actually held (>= min_shares) right now, via a fresh
    myPortfolio call — the real, broker-agnostic DEMAT balance.

    Safety gate for the LIVE execution monitor: a position can look OPEN in
    the app's own `portfolio` table (e.g. from a stale import, or a symbol
    never actually bought through this app's trading engine) without the
    shares actually being there. Never auto-close a live position without
    this confirming it first.

    Fails closed — any error (login, network, missing symbol) returns False,
    never True. Never raises.
    """
    try:
        token = _get_token()
        if not token:
            log.error("verify_live_holding(%s): no Meroshare token", symbol)
            return False
        data = _fetch_my_portfolio(token)
        held = data.get(symbol.upper().strip(), {}).get("shares", 0)
        return held >= min_shares
    except Exception as e:
        log.error("verify_live_holding(%s) error: %s", symbol, e)
        return False


def get_portfolio_summary() -> Optional[PortfolioSummary]:
    """Read from Neon only — no API call. Called by briefing.py."""
    try:
        rows      = read_tab("portfolio")
        open_rows = [r for r in rows if r.get("status", "").upper() == "OPEN"]
        holdings  = []

        for r in open_rows:
            def sf(k): return float(r.get(k, 0) or 0)
            holdings.append(Holding(
                symbol        = r.get("symbol", ""),
                company       = r.get("company", ""),
                shares        = int(sf("shares")),
                wacc          = sf("wacc"),
                total_cost    = sf("total_cost"),
                current_price = sf("current_price"),
                prev_close    = sf("prev_close"),
                current_value = sf("current_value"),
                pnl_npr       = sf("pnl_npr"),
                pnl_pct       = sf("pnl_pct"),
                day_change_pct= sf("day_change_pct"),
            ))

        total_cost  = sum(h.total_cost    for h in holdings)
        total_value = sum(h.current_value for h in holdings)
        total_pnl   = total_value - total_cost
        pnl_pct     = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

        return PortfolioSummary(
            total_holdings  = len(holdings),
            total_cost_npr  = round(total_cost, 2),
            total_value_npr = round(total_value, 2),
            total_pnl_npr   = round(total_pnl, 2),
            total_pnl_pct   = round(pnl_pct, 2),
            holdings        = holdings,
        )
    except Exception as e:
        log.error("get_portfolio_summary failed: %s", e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""

    # python meroshare.py edis [--dry-run]  → run auto-EDIS
    if arg == "edis":
        dry = "--dry-run" in sys.argv[2:]
        res = run_edis(dry_run=dry)
        print(f"\n{'='*60}")
        print("  AUTO-EDIS" + ("  [DRY-RUN]" if dry else ""))
        print(f"{'='*60}")
        for h in res["holding_days"]:
            print(f"  holding days  {h['scrip']:<10} {'OK' if h['ok'] else 'FAIL'}")
        for s in res["settlements"]:
            print(f"  {', '.join(s['scrips']) or '-':<18} {s['qty']:>6.0f} sh  "
                  f"{s['settlement']}  {'OK' if s['ok'] else 'FAIL'} — {s['detail']}")
        if res.get("note"):
            print(f"  note: {res['note']}")
        print(f"{'='*60}\n")
        sys.exit(0 if res["ok"] else 1)

    summary = get_portfolio_summary() if arg == "status" else sync()

    if summary:
        print(f"\n{'='*60}")
        print(f"  PORTFOLIO SUMMARY")
        print(f"{'='*60}")
        print(f"  Positions : {summary.total_holdings}")
        print(f"  Cost      : NPR {summary.total_cost_npr:>12,.2f}")
        print(f"  Value     : NPR {summary.total_value_npr:>12,.2f}")
        print(f"  P&L       : NPR {summary.total_pnl_npr:>+12,.2f}  ({summary.total_pnl_pct:+.1f}%)")
        if summary.holdings:
            print(f"\n  {'Symbol':<10} {'Shares':>6} {'WACC':>8} {'LTP':>8} {'P&L%':>8} {'Day%':>7}")
            print("  " + "-" * 55)
            for h in sorted(summary.holdings, key=lambda x: x.pnl_pct, reverse=True):
                print(f"  {h.symbol:<10} {h.shares:>6} {h.wacc:>8.2f} "
                      f"{h.current_price:>8.2f} {h.pnl_pct:>+7.1f}% {h.day_change_pct:>+6.1f}%")
        print(f"{'='*60}\n")
    else:
        print("\n  No portfolio data\n")
        sys.exit(1)
