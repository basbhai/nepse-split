"""
modules/atrad_scraper.py — ATrad TMS Live Market Data
Fully fixed numeric conversion + safe column handling.
"""

import os
import ast
import time
import random
import string
import logging
import requests
import numpy as np
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from typing import Optional
from sheets import write_row, upsert_row
from config import NST

load_dotenv()

log = logging.getLogger("atrad_scraper")

BASE_URL    = "https://tms.roadshowsecurities.com.np/atsweb"
WATCH_ID    = os.getenv("TMS_ROADSHOW_WATCH_ID", "11120")
BOOK_DEF_ID = "1"

# ORDER account — place_order/place_sell_order, order book, live portfolio sync.
# Everything that touches or informs an actual order stays on this account.
ORDER_USERNAME = os.getenv("TMS_ROADSHOW_USER")
ORDER_PASSWORD = os.getenv("TMS_ROADSHOW_PASS")
ACNT_ID        = os.getenv("TMS_ROADSHOW_ACNT_ID", "")

# DATA account — market watch, trade stats, index, single-symbol quotes, tick
# trades. Segregated so bulk data polling never shares a session with orders.
# Falls back to the order creds if unset, so a missing .env entry degrades to
# the old single-account behavior instead of crashing.
DATA_USERNAME  = os.getenv("TMS_ROADSHOW_DATA_USER", ORDER_USERNAME)
DATA_PASSWORD  = os.getenv("TMS_ROADSHOW_DATA_PASS", ORDER_PASSWORD)

# Backwards-compatible aliases — some callers still import USERNAME/PASSWORD.
USERNAME = ORDER_USERNAME
PASSWORD = ORDER_PASSWORD

ORDER_BUY  = 1   # actionSelect=1 — confirmed via getUCCActiveBlotterData ("action":"BUY")
ORDER_SELL = 2   # actionSelect=2 — live-confirmed 2026-09-11: SONA HARD_STOP auto-close
                 # accepted, and portfolio row closed same day with exit_reason=STOP_LOSS
                 # and a real negative pnl (a mixed-up BUY would have grown the position
                 # instead), not just an ATrad "accepted" response

_order_session    = None
_data_session      = None
_order_last_login  = None
_data_last_login   = None

# Wall-clock (time.time()) of the last moment each session was CONFIRMED good —
# set on a successful login and on a successful checkUserSession ping. Used by
# _ensure_*_session() to force a revalidation once the session is older than
# SESSION_REVALIDATE_SEC, instead of trusting a non-None session forever (an
# ATrad session expires server-side long before the object becomes None, and a
# stale one just returns an HTML login page that _parse() can't read — which is
# how a trailing-stop auto-close silently failed with "order book fetch failed").
_order_last_verified = 0.0
_data_last_verified  = 0.0
SESSION_REVALIDATE_SEC = 240


def _get_headers() -> dict:
    return {
        "User-Agent"       : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept"           : "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With" : "XMLHttpRequest",
        "Referer"          : f"{BASE_URL}/login",
    }


def _parse(r: requests.Response) -> dict:
    text = r.text.strip()
    text = (text
            .replace("true",  "True")
            .replace("false", "False")
            .replace("null",  "None"))
    try:
        return ast.literal_eval(text)
    except Exception as e:
        log.error(f"Parse error: {e} | raw: {text[:200]}")
        return {}


def _safe_float(val: str, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(",", ""))
    except Exception:
        return default


def _login(kind: str, username: str, password: str) -> bool:
    """
    Shared login implementation for both accounts. kind is 'order' or 'data'
    — it only decides which pair of module-level globals gets the resulting
    session, so ORDER and DATA logins never write into each other's slot.
    """
    global _order_session, _data_session, _order_last_login, _data_last_login
    global _order_last_verified, _data_last_verified
    s = requests.Session()
    s.headers.update(_get_headers())

    try:
        r = s.get(
            f"{BASE_URL}/login",
            params={
                "action"           : "login",
                "format"           : "json",
                "txtUserName"      : username,
                "txtPassword"      : password,
                "dojo.preventCache": str(int(time.time() * 1000)),
            },
            timeout=15,
        )
        data = _parse(r)

        if data.get("code") == "0":
            # Only this account's session/timestamp pair gets updated — the
            # other account's session is left completely untouched.
            if kind == "order":
                _order_session       = s
                _order_last_login    = datetime.now(NST)
                _order_last_verified = time.time()
            else:
                _data_session        = s
                _data_last_login     = datetime.now(NST)
                _data_last_verified  = time.time()
            log.info(f"ATrad {kind} login success | user={username} | watchID={data.get('watchID', WATCH_ID)} | broker={data.get('broker_code')}")
            return True
        else:
            log.error(f"ATrad {kind} login failed: {data}")
            return False
    except Exception as e:
        log.error(f"ATrad {kind} login exception: {e}")
        return False


def _order_login() -> bool:
    return _login("order", ORDER_USERNAME, ORDER_PASSWORD)


def _data_login() -> bool:
    return _login("data", DATA_USERNAME, DATA_PASSWORD)


def _keepalive(kind: str, session, username: str) -> bool:
    """
    Ping one account's existing session to keep it alive; re-login (fresh
    session for that account only) if it's missing, expired, or the ping
    itself fails. `session`/`username` are passed in rather than looked up
    by `kind` so the caller controls exactly which account gets checked.
    """
    global _order_last_verified, _data_last_verified
    if session is None:
        return _order_login() if kind == "order" else _data_login()
    try:
        r = session.get(
            f"{BASE_URL}/login",
            params={
                "action"           : "checkUserSession",
                "format"           : "json",
                "txtUserName"      : username,
                "dojo.preventCache": str(int(time.time() * 1000)),
            },
            timeout=10,
        )
        data = _parse(r)
        if data.get("code") == "0":
            if kind == "order":
                _order_last_verified = time.time()
            else:
                _data_last_verified = time.time()
            return True
        log.warning(f"ATrad {kind} session expired — re-logging in")
        return _order_login() if kind == "order" else _data_login()
    except Exception as e:
        log.error(f"ATrad {kind} keepalive failed: {e} — attempting re-login")
        return _order_login() if kind == "order" else _data_login()


def _ensure_order_session() -> bool:
    if _order_session is None:
        return _order_login()
    if time.time() - _order_last_verified >= SESSION_REVALIDATE_SEC:
        # Non-None but possibly stale server-side — ping (re-logs in on expiry).
        return _keepalive("order", _order_session, ORDER_USERNAME)
    return True


def _ensure_data_session() -> bool:
    if _data_session is None:
        return _data_login()
    if time.time() - _data_last_verified >= SESSION_REVALIDATE_SEC:
        return _keepalive("data", _data_session, DATA_USERNAME)
    return True


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC PRIMING API — login()/keepalive()/_ensure_session().
#
# These prime ONLY the DATA account (20240300188). The ORDER account (BS92009)
# is deliberately NOT touched here: it is reserved exclusively for live order
# placement and is opened lazily by _ensure_order_session() inside place_order()
# / place_sell_order() (and get_portfolio()) at the moment it is actually
# needed. Nothing else — no monitor, bot, MCP server or workflow — should ever
# hold a BS session open. Every fetch/order function below also self-ensures
# its own specific session, so correctness never depends on these being called.
# ═══════════════════════════════════════════════════════════════════════════

def login() -> bool:
    return _data_login()


def keepalive() -> bool:
    return _keepalive("data", _data_session, DATA_USERNAME)


def _ensure_session() -> bool:
    return _ensure_data_session()


# ====================== FIXED MARKET WATCH ======================
def fetch_market_watch(write_db: bool = True) -> pd.DataFrame:
    if not _ensure_data_session():
        log.error("ATrad fetch_market_watch: no session")
        return pd.DataFrame()

    now_nst = datetime.now(NST)

    try:
        r = _data_session.get(
            f"{BASE_URL}/watch",
            params={
                "action"           : "fullWatch",
                "format"           : "json",
                "exchange"         : "NEPSE",
                "bookDefId"        : BOOK_DEF_ID,
                "watchId"          : WATCH_ID,
                "lastUpdatedId"    : "0",
                "dojo.preventCache": str(int(time.time() * 1000)),
            },
            timeout=30,
        )
        data = _parse(r)

        if data.get("code") != "0":
            log.error(f"fullWatch failed: {data.get('description')}")
            return pd.DataFrame()

        records = data["data"]["watch"]
        if not records:
            log.warning("fullWatch returned empty watch list")
            return pd.DataFrame()

        df = pd.DataFrame(records)
        if "ltp" in df.columns:
            df = df.drop(columns=["ltp"])

        # Rename columns
        col_map = {
            "security": "symbol", "tradeprice": "ltp", "openingprice": "open",
            "highpx": "high", "lowpx": "low", "vwap": "vwap", "avgprice": "avg_price",
            "bidprice": "bid_price", "bidqty": "bid_qty", "askprice": "ask_price",
            "askqty": "ask_qty", "totvolume": "volume", "totturnover": "turnover",
            "tottrades": "total_trades", "netchange": "net_change", "perchange": "pct_change",
            "lowdpr": "low_dpr", "highdpr": "high_dpr", "popdprlow": "prev_dpr_low",
            "popdprhigh": "prev_dpr_high", "maxalqty": "max_al_qty", "lasttradedtime": "time",
        }
        df = df.rename(columns=col_map).reset_index(drop=True)

        # ── SAFE NUMERIC CONVERSION (This was the bug) ─────────────────────
        num_cols = ["ltp", "open", "high", "low", "vwap", "avg_price",
                    "bid_price", "bid_qty", "ask_price", "ask_qty",
                    "volume", "total_trades", "net_change", "pct_change",
                    "low_dpr", "high_dpr", "prev_dpr_low", "prev_dpr_high"]

        for col in num_cols:
            if col in df.columns:
                vals = [str(x).replace(",", "") for x in df[col].tolist()]
                arr = pd.to_numeric(vals, errors="coerce")
                df[col] = np.where(np.isnan(arr), 0.0, arr)
            else:
                df[col] = 0.0

        df = df.loc[:, ~df.columns.duplicated()]

        # ── Derived signals ───────────────────────────────────────────────
        ltp      = df["ltp"].to_numpy()
        vwap     = df["vwap"].to_numpy()
        bid_qty  = df["bid_qty"].to_numpy()
        ask_qty  = df["ask_qty"].to_numpy()
        low_dpr  = df["low_dpr"].to_numpy()
        high_dpr = df["high_dpr"].to_numpy()

        vwap_dev = np.where(vwap > 0, (ltp - vwap) / vwap, 0.0).round(6)
        df.insert(len(df.columns), "vwap_dev", vwap_dev)

        total_depth   = bid_qty + ask_qty
        bid_ask_ratio = np.where(total_depth > 0, bid_qty / total_depth, 0.5).round(4)
        df.insert(len(df.columns), "bid_ask_ratio", bid_ask_ratio)

        dpr_range     = high_dpr - low_dpr
        dpr_proximity = np.where(dpr_range > 0, (ltp - low_dpr) / dpr_range, 0.5).round(4)
        df.insert(len(df.columns), "dpr_proximity", dpr_proximity)

        # Date & Time
        df["date"] = now_nst.strftime("%Y-%m-%d")
        if "time" not in df.columns or df["time"].eq("").all():
            df["time"] = now_nst.strftime("%H:%M:%S")

        if write_db and not df.empty:
            _write_market_watch(df, now_nst)

        log.info(f"✅ ATrad market watch: {len(df)} symbols fetched successfully")
        return df

    except Exception as e:
        log.error(f"fetch_market_watch exception: {e}", exc_info=True)
        return pd.DataFrame()


def _write_market_watch(df: pd.DataFrame, now_nst: datetime) -> None:
    keep_cols = [
        "date", "time", "symbol", "ltp", "open", "high", "low", "vwap", "avg_price",
        "bid_price", "bid_qty", "ask_price", "ask_qty", "volume", "turnover",
        "total_trades", "net_change", "pct_change", "low_dpr", "high_dpr",
        "prev_dpr_low", "prev_dpr_high", "max_al_qty", "vwap_dev",
        "bid_ask_ratio", "dpr_proximity",
    ]
    existing = [c for c in keep_cols if c in df.columns]
    rows = df[existing].astype(str).to_dict(orient="records")

    fallback_time = now_nst.strftime("%H:%M:%S")
    for row in rows:
        if not row.get("time"):
            row["time"] = fallback_time
        try:
            upsert_row("atrad_market_watch", row, conflict_columns=["date", "time", "symbol"])
        except Exception as e:
            log.debug(f"Write skip {row.get('symbol')}: {e}")


def fetch_trade_stats() -> dict:
    """
    Fetch authoritative advancing/declining/unchanged counts from ATrad getTradeStats.
    Returns empty dict on any failure — never raises.
    """
    if not _ensure_data_session():
        log.warning("fetch_trade_stats: no session")
        return {}
    try:
        r = _data_session.get(
            f"{BASE_URL}/marketdetails",
            params={
                "action":            "getTradeStats",
                "format":            "json",
                "dojo.preventCache": str(int(time.time() * 1000)),
            },
            timeout=15,
        )
        data = _parse(r)
        if data.get("code") != "0":
            log.warning("fetch_trade_stats: non-zero code: %s", data.get("code"))
            return {}
        d = data.get("data", {})
        return {
            "advancing": int(_safe_float(d.get("up",        0))),
            "declining": int(_safe_float(d.get("down",      0))),
            "unchanged": int(_safe_float(d.get("unchanged", 0))),
            "total":     int(_safe_float(d.get("total",     0))),
        }
    except Exception as e:
        log.warning("fetch_trade_stats failed: %s", e)
        return {}


def fetch_nepse_index() -> dict:
    """
    Fetch NEPSE composite index value and market totals from ATrad getSectorDataAll.
    Returns empty dict on any failure — never raises.
    """
    if not _ensure_data_session():
        log.warning("fetch_nepse_index: no session")
        return {}
    try:
        r = _data_session.get(
            f"{BASE_URL}/sector",
            params={
                "action":            "getSectorDataAll",
                "format":            "json",
                "exchange":          "NEPSE",
                "sectorId":          "NEPSE",
                "sectorIdSL":        "SENSIND",
                "dojo.preventCache": str(int(time.time() * 1000)),
            },
            timeout=15,
        )
        data = _parse(r)
        if data.get("code") != "0":
            log.warning("fetch_nepse_index: non-zero code: %s", data.get("code"))
            return {}
        sector_list = data.get("data", {}).get("sector", [])
        if not sector_list:
            log.warning("fetch_nepse_index: empty sector list")
            return {}
        s = sector_list[0]
        return {
            "nepse_index":          _safe_float(s.get("pr1", 0)),
            "nepse_change_abs":     _safe_float(s.get("n1",  0)),
            "nepse_change_pct":     _safe_float(s.get("p1",  0)),
            "sensitive_index":      _safe_float(s.get("pr2", 0)),
            "sensitive_change_abs": _safe_float(s.get("n2",  0)),
            "sensitive_change_pct": _safe_float(s.get("p2",  0)),
            "total_volume":         int(_safe_float(s.get("v",   0))),
            "total_turnover":       _safe_float(s.get("to",  0)),
            "total_trades":         int(_safe_float(s.get("tr",  0))),
            "market_cash_in":       str(s.get("marketCashIn",    "") or ""),
            "market_cash_in_val":   str(s.get("marketCashInVal", "") or ""),
            "market_cash_out_val":  str(s.get("marketCashOutVal","") or ""),
        }
    except Exception as e:
        log.warning("fetch_nepse_index failed: %s", e)
        return {}


# fetch_order_book runs on the DATA session (20240300188). The order book is
# read-only market data — it informs auto-close pricing / GIS but places no
# order — so the ORDER account (BS92009) stays reserved exclusively for live
# order placement and is never opened just to poll depth. Confirmed: the data
# account has full getOrderBook access.
def fetch_order_book(symbol: str) -> dict:
    if not _ensure_data_session(): return {}

    def _get() -> dict:
        r = _data_session.get(f"{BASE_URL}/marketdetails", params={
            "action": "getOrderBook", "format": "json", "security": symbol,
            "board": "1", "dojo.preventCache": str(int(time.time() * 1000))
        }, timeout=15)
        return _parse(r)

    try:
        data = _get()
        if data.get("code") != "0":
            # Empty/non-"0" almost always means a session that expired between
            # revalidations (or a server-side reset) handed us an HTML login
            # page. Force a fresh DATA login and retry ONCE — this path feeds
            # trailing-stop / hard-stop auto-close, so a silent {} here means a
            # stop that never executes.
            log.warning("fetch_order_book %s: empty/non-0 response — forcing re-login and retrying once", symbol)
            if _data_login():
                data = _get()
            if data.get("code") != "0":
                return {}

        book = data["data"]["orderbook"][0]
        bids = book.get("bid", [])
        asks = book.get("ask", [])

        tb = ta = 0.0
        imb = bb = ba = sp = sp_pct = 0.0
        if bids:
            bid_arr = np.array([[_safe_float(b.get("price",0)), _safe_float(b.get("qty",0))] for b in bids])
            tb = float(bid_arr[:,1].sum())
            bb = float(bid_arr[0,0])
        if asks:
            ask_arr = np.array([[_safe_float(a.get("price",0)), _safe_float(a.get("qty",0))] for a in asks])
            ta = float(ask_arr[:,1].sum())
            ba = float(ask_arr[0,0])
        td = tb + ta
        imb = tb / td if td > 0 else 0.5
        if bb > 0 and ba > 0:
            sp = ba - bb
            sp_pct = (sp / bb * 100) if bb > 0 else 0.0

        return {"symbol":symbol, "bids":bids, "asks":asks, "total_bid_qty":float(tb),"total_ask_qty":float(ta),
                "imbalance":round(imb,4), "best_bid":bb, "best_ask":ba, "spread":round(sp,2), "spread_pct":round(sp_pct,4),
                "fetched_at":datetime.now(NST).strftime("%H:%M:%S")}
    except Exception as e:
        log.error(f"fetch_order_book {symbol}: {e}")
        return {}

def get_ltp_live(symbol: str) -> Optional[dict]:
    """
    Fetch live price + market data for a single symbol via getQuickWatch.
    Used by execution_monitor (30-sec loop) — always fresh, no DB read.
    Returns dict with ltp, vwap, bid_price, bid_qty, ask_price, ask_qty,
    low_dpr, high_dpr, volume, or None on failure.
    """
    if not _ensure_data_session():
        return None
    try:
        r = _data_session.get(
            f"{BASE_URL}/watch",
            params={
                "action":                "getQuickWatch",
                "format":                "json",
                "exchange":              "NEPSE",
                "bookDefId":             "1",
                "securityid":            symbol,
                "watchId":               WATCH_ID,
                "isquickwatchsecurity":  "true",
                "lastUpdatedId":         "0",
                "dojo.preventCache":     str(int(time.time() * 1000)),
            },
            timeout=10,
        )
        data = _parse(r)
        if data.get("code") != "0":
            return None

        w = data.get("data", {}).get("watch", [])
        if not w:
            return None
        w = w[0]

        def _f(key):
            try:
                return float(str(w.get(key, "0")).replace(",", "") or 0)
            except (ValueError, TypeError):
                return 0.0

        # ATrad's getQuickWatch stopped populating "ltp" (always "0.00") sometime
        # around 2026-08. The live last-trade price is in "tradeprice"; fall back
        # to that, then avgprice / vwap for a no-trade symbol, so the monitor
        # doesn't drop to the stale DB row on every single cycle.
        ltp = _f("ltp") or _f("tradeprice") or _f("avgprice") or _f("vwap")

        return {
            "symbol":    symbol,
            "ltp":       ltp,
            "vwap":      _f("vwap"),
            "bid_price": _f("bidprice"),
            "bid_qty":   _f("bidqty"),
            "ask_price": _f("askprice"),
            "ask_qty":   _f("askqty"),
            "low_dpr":   _f("lowdpr"),
            "high_dpr":  _f("highdpr"),
            "volume":    _f("totvolume"),
            "vwap_dev":  (ltp - _f("vwap")) / _f("vwap") if _f("vwap") > 0 else 0.0,
            # Exchange's own last-traded-time for this symbol ("HH:MM:SS" NST in
            # fullWatch; getQuickWatch uses the same key). The caller uses its
            # age to tell a live quote from a frozen one — getQuickWatch keeps
            # returning the last snapshot with code=0 when the feed dies / the
            # symbol halts / the data session soft-expires. "" when absent.
            "lasttradedtime": str(
                w.get("lasttradedtime") or w.get("lasttradetime")
                or w.get("lasttrdtime") or ""
            ).strip(),
        }
    except Exception as e:
        log.error("get_ltp_live(%s): %s", symbol, e)
        return None


def fetch_trades_of_day(symbol: str) -> list:
    """
    Fetch tick trades for symbol via ATrad getTradesOfDay (DATA session).
    Returns list of dicts with price, qty, timestamp keys. Fails silently.
    """
    if not _ensure_data_session():
        return []
    try:
        r = _data_session.get(
            f"{BASE_URL}/marketdetails",
            params={
                "action": "getTradesOfDay", "format": "json",
                "security": symbol, "board": "1",
                "dojo.preventCache": str(int(time.time() * 1000)),
            },
            timeout=15,
        )
        data = _parse(r)
        if data.get("code") != "0":
            return []
        trades_raw = data.get("data", {}).get("trade", [])
        result = []
        for t in trades_raw:
            try:
                result.append({
                    "price":     float(t.get("price") or t.get("rate") or 0),
                    "qty":       float(t.get("qty") or t.get("quantity") or 0),
                    "timestamp": float(t.get("timestamp") or 0),
                })
            except (TypeError, ValueError):
                continue
        return result
    except Exception as e:
        log.error("fetch_trades_of_day(%s): %s", symbol, e)
        return []


def get_portfolio(portfolio_asset: str = "EQUITY") -> dict:
    """
    Fetch broker-side holdings via ATrad getPortfolio (ORDER session,
    BS92009). This only reflects positions that were bought/sold *through
    this broker's own TMS* — it silently misses anything held in the same
    DEMAT that was traded via a different broker. NOT the live-portfolio
    source (see modules/meroshare.py's myPortfolio instead, which is
    CDSC-level and broker-agnostic). Useful for RSL-specific reconciliation
    around actual order placement, not for "what do I own" queries.

    Returns {"holdings": [...], "total_market_value": float, "total_quantity": int}.
    Each holding dict has: symbol, quantity, available_qty, ltp, market_value,
    pending_buy, pending_sell. Empty holdings list on any failure or when the
    account genuinely has no positions traded through this broker.

    NOTE: this is the only non-order-placement function on the BS92009 (ORDER)
    account. It has no callers in the running system and exists only for
    manual reconciliation immediately around a live order. Do NOT wire it into
    any monitor/bot/workflow loop — that would defeat the "BS open only for
    live trades" rule. Use get_ltp_live / fetch_order_book (DATA account) or
    modules/meroshare.py for anything else.
    """
    if not _ensure_order_session():
        log.warning("get_portfolio: no session")
        return {"holdings": [], "total_market_value": 0.0, "total_quantity": 0}
    try:
        r = _order_session.get(
            f"{BASE_URL}/client",
            params={
                "action":                  "getPortfolio",
                "exchange":                "NEPSE",
                "broker":                  "RSL",
                "format":                  "json",
                "portfolioAsset":          portfolio_asset,
                "portfolioClientAccount":  ORDER_USERNAME,
                "dojo.preventCache":       str(int(time.time() * 1000)),
            },
            timeout=20,
        )
        data = _parse(r)
        if data.get("code") != "0":
            log.warning("get_portfolio: non-zero code: %s", data.get("code"))
            return {"holdings": [], "total_market_value": 0.0, "total_quantity": 0}

        d          = data.get("data", {})
        rows       = d.get("portfolios", []) or []
        # ATrad returns these as single-element lists (e.g. "markerValTot":
        # [8489.0]), not scalars — confirmed by direct API testing, not a
        # typo. Indexing [0] below is intentional, matching that shape.
        market_tot = d.get("markerValTot", [0.0])
        qty_tot    = d.get("quantityTot", [0])

        holdings = []
        for row in rows:
            holdings.append({
                "symbol":         row.get("security", ""),
                "quantity":       int(_safe_float(row.get("quantity", 0))),
                "available_qty":  int(_safe_float(row.get("availableQty", 0))),
                "ltp":            _safe_float(row.get("lastTraded", 0)),
                "market_value":   _safe_float(row.get("marketValue", 0)),
                "pending_buy":    int(_safe_float(row.get("pendBuy", 0))),
                "pending_sell":   int(_safe_float(row.get("pendSell", 0))),
            })

        return {
            "holdings":            holdings,
            "total_market_value":  _safe_float(market_tot[0]) if market_tot else 0.0,
            "total_quantity":      int(_safe_float(qty_tot[0])) if qty_tot else 0,
        }
    except Exception as e:
        log.error("get_portfolio failed: %s", e)
        return {"holdings": [], "total_market_value": 0.0, "total_quantity": 0}


def fetch_my_trades(security_id: str = "ALL", client_order_id: str = "",
                    side: str = "ALL") -> list:
    """
    Fetch this account's OWN executed fills via ATrad getMyTradesFilled (ORDER
    session, BS92009) — the actual broker trade blotter, not a market-wide
    tape. This is the ground truth for "did my order really fill, and at what
    price": each row is one partial or full fill, so sum `qty` and take the
    qty-weighted average of `price` across rows sharing an `order_id` to get
    the true executed quantity/price for an order that filled in parts.

    Returns a list of dicts, newest first: {order_id, exchange_order_id,
    symbol, side, order_qty, filled_qty, remainder, price, value, timestamp
    (unix seconds), trade_date (str)}. Empty list on any failure or no fills.

    NOTE: like get_portfolio(), this is an ORDER-account (BS92009) read. It
    may ONLY be called from within execution_monitor's live-exit follow-up,
    or from modules/trade_origin.daily() — ONE read per trading day after
    close (user-approved 2026-10-03 to capture buy fills and classify their origin)
    (_work_live_pending_exit), and only on that function's own long cooldown
    (see _LIVE_EXIT_RECHECK_SEC) — i.e. strictly in the aftermath of a live
    order this same process just placed, never on an independent schedule.
    That preserves the "BS session open only around live trades" rule described
    on get_portfolio() above; it does not relax it.
    """
    if not _ensure_order_session():
        log.warning("fetch_my_trades: no session")
        return []
    client_label = os.getenv(
        "TMS_ROADSHOW_CLIENT_LABEL",
        f"{ORDER_USERNAME} ( BASANTA SHRESTHA-202105112274903)",
    )

    def _call() -> dict:
        r = _order_session.get(
            f"{BASE_URL}/client",
            params={
                "action": "getMyTradesFilled", "format": "json",
                "txtSide": side, "txtTif": "ALL", "txtPlacedBy": "ALL",
                "txtClientOrderId": client_order_id,
                "txtSecurityId": security_id, "txtAssetType": "EQUITY",
                "txtContraBroker": "", "txtClient": client_label,
                "pageNumber": "1",
                "dojo.preventCache": str(int(time.time() * 1000)),
            },
            timeout=20,
        )
        return _parse(r)

    try:
        data = _call()
        if data.get("code") != "0":
            # checkUserSession (what _ensure_order_session's keepalive ping
            # uses) has been observed to report code=0 "valid" on a BS92009
            # session that getMyTradesFilled itself then rejects with the
            # login page HTML (confirmed 2026-09-22: 3 consecutive recheck
            # cycles silently failed this way, leaving a fully-filled PHCL
            # exit unreconciled for 90+ min). checkUserSession is therefore
            # not a reliable proxy for this specific endpoint — force one
            # real re-login and retry here instead of trusting it.
            log.warning(
                "fetch_my_trades: non-zero code: %s — forcing re-login and retrying once",
                data.get("code"),
            )
            if _order_login():
                data = _call()
            if data.get("code") != "0":
                log.warning("fetch_my_trades: still non-zero code after re-login: %s", data.get("code"))
                return []
        rows = data.get("data", {}).get("blotterdata", []) or []
        out = []
        for row in rows:
            try:
                ts = datetime.strptime(
                    str(row.get("tradeDate", "")), "%Y-%m-%d %H:%M:%S.%f"
                ).replace(tzinfo=NST).timestamp()
            except ValueError:
                ts = 0.0
            out.append({
                "order_id":          row.get("clientorderid", ""),
                "exchange_order_id": row.get("exchangeorderid", ""),
                "symbol":            row.get("securitycode", ""),
                "side":              row.get("action", ""),
                "order_qty":         _safe_float(row.get("orderQuantity", 0)),
                "filled_qty":        _safe_float(row.get("filledquantity", 0)),
                "remainder":         _safe_float(row.get("remainder", 0)),
                "price":             _safe_float(row.get("orderprice", 0)),
                "value":             _safe_float(row.get("ordervalue", 0)),
                "timestamp":         ts,
                "trade_date":        str(row.get("tradeDate", "")),
            })
        return out
    except Exception as e:
        log.error("fetch_my_trades failed: %s", e)
        return []


# ═══════════════════════════════════════════════════════════════════════════
# LIVE ORDER PLACEMENT  (PAPER_MODE=false only)
# ═══════════════════════════════════════════════════════════════════════════

def _gen_order_id(length: int = 10) -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=length))


def place_order(
    symbol:     str,
    action:     int,        # ORDER_BUY=1  ORDER_SELL=2
    qty:        int,
    price:      float,
    tif:        int = 16,   # 16 = Day order
    tif_days:   int = 1,
    order_type: int = 1,    # 1 = confirmed working via live test
    board:      int = 1,
) -> dict:
    """
    Submit a BUY or SELL to ATrad via submitOrder (POST form-data).
    Returns parsed ATrad response. code='0' means accepted.
    Raises RuntimeError if no session.
    """
    if not _ensure_order_session():
        raise RuntimeError("ATrad session unavailable — cannot place order")

    order_id     = _gen_order_id()
    action_label = "BUY" if action == ORDER_BUY else "SELL"

    form = {
        "action":            "submitOrder",
        "format":            "json",
        "brokerClient":      "1",
        "acntid":            ACNT_ID,
        "txtsenttoapproval": "no",
        "duplicateOrderId":  order_id,
        "product":           "web",
        "clientAcc":         ORDER_USERNAME,
        "actionSelect":      str(action),
        "txtSecurity":       symbol,
        "spnQuantity":       str(qty),
        "spnPrice":          str(price),
        "cmbTif":            str(tif),
        "cmbTifDays":        str(tif_days),
        "cmbBoard":          str(board),
        "hiddenSpnCseFee":   "0.02",
        "brokerClientVal":   "1",
        "spnStopPrice":      "",
        "cmbTypeOfOrder":    str(order_type),
        "txtContraBroker_":  "",
        "counterBrokerVal":  "",
    }

    log.info("[LIVE ORDER] %s %s × %d @ %.2f | localID=%s",
             action_label, symbol, qty, price, order_id)

    try:
        r    = _order_session.post(f"{BASE_URL}/order", data=form, timeout=20)
        data = _parse(r)
    except Exception as e:
        log.error("[LIVE ORDER] POST failed %s %s: %s", action_label, symbol, e)
        raise

    if data.get("code") == "0":
        log.info("[LIVE ORDER] ACCEPTED %s %s @ %.2f | %s",
                 action_label, symbol, price, data.get("description", "ok"))
    else:
        log.error("[LIVE ORDER] REJECTED %s %s: code=%s desc=%s",
                  action_label, symbol, data.get("code"), data.get("description"))

    return data


def place_sell_order(symbol: str, qty: int, price: float) -> dict:
    """Sell qty shares at price (day limit order). Used by execution_monitor auto-close."""
    return place_order(symbol, action=ORDER_SELL, qty=qty, price=price)


def place_buy_order(symbol: str, qty: int, price: float) -> dict:
    """
    Buy qty shares at price (day limit order). Not called from anywhere yet —
    no automated BUY pipeline exists (see modules/atrad_tms_endpoints.md: live
    BUYs are currently placed manually). Mirrors place_sell_order()'s shape so
    a future automated-BUY path has a ready symmetric entry point.
    """
    return place_order(symbol, action=ORDER_BUY, qty=qty, price=price)


def run():
    if not login():
        log.error("ATrad login failed")
        return None
    return fetch_market_watch(write_db=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    from log_config import attach_file_handler
    attach_file_handler(__name__)
    df = run()
    if df is not None and not df.empty:
        print(f"\n✅ Fetched {len(df)} symbols")
        print(df[["symbol", "ltp", "vwap", "vwap_dev", "bid_ask_ratio", "dpr_proximity"]].head(10).to_string())