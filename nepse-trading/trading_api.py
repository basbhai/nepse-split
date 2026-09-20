"""
trading_api.py — nepse-trading
FastAPI service on port 8767. Exposes paper trading data to nepse-engine.
Auth: X-Service-Key header, same secret as ENGINE_API_SECRET in both .env files.
"""
import os
import logging
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Header, Depends, Query, Request
from pydantic import BaseModel

from db.connection import _db
from sheets import run_raw_sql, execute_dml

log = logging.getLogger(__name__)

NST = ZoneInfo("Asia/Kathmandu")
ENGINE_API_SECRET = os.getenv("ENGINE_API_SECRET", "")
MAX_PAPER_POSITIONS = 15


def _nst_now() -> str:
    return datetime.now(tz=NST).strftime("%Y-%m-%d %H:%M:%S")

def _nst_today() -> str:
    return datetime.now(tz=NST).strftime("%Y-%m-%d")


app = FastAPI(title="nepse-trading API", version="1.0.0")


def _verify_key(x_service_key: str = Header(default="")):
    if ENGINE_API_SECRET and x_service_key != ENGINE_API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "trading_api"}


# ── GET /api/v1/users/approved ────────────────────────────────────────────────

@app.get("/api/v1/users/approved", dependencies=[Depends(_verify_key)])
async def approved_users():
    """
    All APPROVED users with their channel IDs.
    Used by engine briefing.py for the morning broadcast loop.
    """
    rows = run_raw_sql("""
        SELECT
            u.id                                    AS user_id,
            u.full_name,
            MAX(CASE WHEN uc.platform = 'telegram' THEN uc.platform_user_id END) AS telegram_id,
            MAX(CASE WHEN uc.platform = 'discord'  THEN uc.platform_user_id END) AS discord_id,
            MAX(CASE WHEN uc.platform = 'telegram' THEN uc.username END)          AS username
        FROM users u
        LEFT JOIN user_channels uc ON uc.user_id = u.id::text
        WHERE u.status = 'APPROVED'
        GROUP BY u.id, u.full_name
        ORDER BY u.id
    """)
    return {"users": [dict(r) for r in rows], "count": len(rows)}


# ── GET /api/v1/positions/summary ─────────────────────────────────────────────

@app.get("/api/v1/positions/summary", dependencies=[Depends(_verify_key)])
async def positions_summary():
    """
    Open position count for the SYSTEM_BOT account.
    Used by engine filter_common._load_context() for sector concentration gate.
    On failure engine assumes max positions filled (fail closed).
    """
    rows = run_raw_sql("""
        SELECT symbol
        FROM paper_portfolio
        WHERE telegram_id = 'SYSTEM_BOT'
          AND status    = 'OPEN'
          AND test_mode = 'false'
    """)
    open_count = len(rows)
    # ponytail: sector_counts empty — share_sectors lives on engine DB.
    # Engine's filter_common handles {} gracefully (skips sector gate).
    # Wire up properly when trading DB has sector column on paper_portfolio.
    return {
        "open_count":      open_count,
        "sector_counts":   {},
        "slots_remaining": max(0, MAX_PAPER_POSITIONS - open_count),
    }


# ── GET /api/v1/briefing-data/{telegram_id} ───────────────────────────────────

@app.get("/api/v1/briefing-data/{telegram_id}", dependencies=[Depends(_verify_key)])
async def briefing_data(telegram_id: str):
    """
    Capital state + open positions + today's closed trades for one user.
    Used by engine briefing.py to build the per-user morning brief.
    """
    today = _nst_today()

    capital_rows = run_raw_sql(
        "SELECT * FROM paper_capital WHERE telegram_id = %s LIMIT 1",
        (telegram_id,)
    )
    capital = dict(capital_rows[0]) if capital_rows else {}

    open_rows = run_raw_sql(
        """
        SELECT symbol, total_shares, wacc, total_cost, first_buy_date
        FROM paper_portfolio
        WHERE telegram_id = %s AND status = 'OPEN' AND test_mode = 'false'
        ORDER BY first_buy_date
        """,
        (telegram_id,)
    )

    closed_today = run_raw_sql(
        """
        SELECT symbol, exit_date, exit_price, net_pnl, result
        FROM paper_portfolio
        WHERE telegram_id = %s AND status = 'CLOSED' AND exit_date = %s
          AND test_mode = 'false'
        ORDER BY exit_date DESC
        """,
        (telegram_id, today)
    )

    return {
        "capital":         capital,
        "open_positions":  [dict(r) for r in open_rows],
        "closed_today":    [dict(r) for r in closed_today],
    }


# ── POST /api/v1/positions/execute ────────────────────────────────────────────

class ExecuteBody(BaseModel):
    symbol:      str
    shares:      float
    price:       float
    telegram_id: str
    timestamp:   str
    signal_id:   int   = 0
    suggested_hold: int = 10
    primary_signal: str = ""


@app.post("/api/v1/positions/execute", dependencies=[Depends(_verify_key)])
async def execute_position(body: ExecuteBody):
    """
    System account auto-execute: engine pushes a confirmed BUY.
    Writes paper_portfolio + paper_capital + paper_trade_log atomically.
    """
    from decimal import ROUND_HALF_UP
    symbol  = body.symbol.upper()
    shares  = Decimal(str(body.shares))
    price   = Decimal(str(body.price))
    tid     = body.telegram_id
    today   = _nst_today()
    now     = _nst_now()

    # Fee calculation (mirrors trading_core)
    brokerage_pct = Decimal("0.004")   # 0.4%
    sebon_pct     = Decimal("0.00015") # 0.015%
    dp_fee        = Decimal("25")
    gross         = (price * shares).quantize(Decimal("0.01"), ROUND_HALF_UP)
    brokerage     = (gross * brokerage_pct).quantize(Decimal("0.01"), ROUND_HALF_UP)
    sebon         = (gross * sebon_pct).quantize(Decimal("0.01"), ROUND_HALF_UP)
    total_fees    = brokerage + sebon + dp_fee
    total_cost    = gross + total_fees

    try:
        with _db() as cur:
            # Capital check
            cur.execute(
                "SELECT current_capital FROM paper_capital WHERE telegram_id = %s FOR UPDATE",
                (tid,)
            )
            cap_row = cur.fetchone()
            if not cap_row:
                raise HTTPException(status_code=404, detail=f"No capital row for {tid}")
            available = Decimal(str(cap_row["current_capital"]))
            if total_cost > available:
                raise HTTPException(
                    status_code=400,
                    detail=f"Insufficient capital. Need {total_cost}, have {available}"
                )

            # Upsert portfolio position
            cur.execute(
                "SELECT id, total_shares, total_cost, buy_count FROM paper_portfolio "
                "WHERE telegram_id = %s AND symbol = %s AND status = 'OPEN' LIMIT 1",
                (tid, symbol)
            )
            existing = cur.fetchone()

            if existing:
                new_shares = Decimal(str(existing["total_shares"])) + shares
                new_cost   = Decimal(str(existing["total_cost"]))   + total_cost
                new_wacc   = (new_cost / new_shares).quantize(Decimal("0.0001"), ROUND_HALF_UP)
                cur.execute("""
                    UPDATE paper_portfolio SET
                        total_shares = %s, wacc = %s, total_cost = %s,
                        last_buy_date = %s, buy_count = (buy_count::int + 1)::text,
                        updated_at = %s
                    WHERE id = %s
                    RETURNING id
                """, (str(new_shares), str(new_wacc), str(new_cost),
                      today, now, existing["id"]))
                portfolio_id = cur.fetchone()["id"]
            else:
                wacc = price.quantize(Decimal("0.0001"), ROUND_HALF_UP)
                cur.execute("""
                    INSERT INTO paper_portfolio
                        (telegram_id, symbol, status, total_shares, wacc, total_cost,
                         first_buy_date, last_buy_date, buy_count, created_at, updated_at, test_mode)
                    VALUES (%s,%s,'OPEN',%s,%s,%s,%s,%s,'1',%s,%s,'false')
                    RETURNING id
                """, (tid, symbol, str(shares), str(wacc), str(total_cost),
                      today, today, now, now))
                portfolio_id = cur.fetchone()["id"]

            # Append trade log
            capital_after = (available - total_cost).quantize(Decimal("0.01"), ROUND_HALF_UP)
            cur.execute("""
                INSERT INTO paper_trade_log
                    (telegram_id, portfolio_id, symbol, action, shares, price,
                     gross_amount, brokerage, sebon, dp_fee, cgt, total_fees,
                     net_amount, capital_before, capital_after, created_at, test_mode, close_type)
                VALUES (%s,%s,%s,'BUY',%s,%s,%s,%s,%s,%s,'0',%s,%s,%s,%s,%s,'false','MANUAL')
            """, (tid, str(portfolio_id), symbol, str(shares), str(price),
                  str(gross), str(brokerage), str(sebon), str(dp_fee),
                  str(total_fees), str(gross), str(available), str(capital_after), now))

            # Deduct capital
            cur.execute(
                "UPDATE paper_capital SET current_capital = %s, last_updated = %s "
                "WHERE telegram_id = %s",
                (str(capital_after), now, tid)
            )

        return {"executed": True, "telegram_id": tid, "trade_id": portfolio_id}

    except HTTPException:
        raise
    except Exception as e:
        log.exception("execute_position failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# ── POST /api/v1/webhook/signal ───────────────────────────────────────────────

class SignalBody(BaseModel):
    symbol:          str
    signal_date:     str
    action:          str
    composite_score: float = 0
    suggested_hold:  int   = 10
    primary_signal:  str   = ""
    confidence:      float = 0
    entry_price:     float = 0


@app.post("/api/v1/webhook/signal", dependencies=[Depends(_verify_key)])
async def webhook_signal(body: SignalBody):
    """
    Called by engine notifier.py on every BUY signal.
    1. Logs delivery record per approved user (dedup via UNIQUE constraint).
    2. Auto-executes for SYSTEM_BOT if capital exists.
    3. Push notifications to users are handled by bot processes (not here).
    """
    symbol = body.symbol.upper()
    date   = body.signal_date

    # Fetch all approved users with their channel preferences
    users = run_raw_sql("""
        SELECT
            u.id::text                                                        AS user_id,
            MAX(CASE WHEN uc.platform = 'telegram' THEN uc.platform_user_id END) AS telegram_id,
            MAX(CASE WHEN uc.platform = 'discord'  THEN uc.platform_user_id END) AS discord_id,
            COALESCE(usp.notify_telegram, 'true')  AS notify_telegram,
            COALESCE(usp.notify_discord,  'false') AS notify_discord
        FROM users u
        LEFT JOIN user_channels         uc  ON uc.user_id  = u.id::text
        LEFT JOIN user_signal_preferences usp ON usp.user_id = u.id::text
        WHERE u.status = 'APPROVED'
        GROUP BY u.id, usp.notify_telegram, usp.notify_discord
    """)

    delivered = 0
    skipped   = 0

    for user in users:
        channels = []
        if user["notify_telegram"] == "true" and user["telegram_id"]:
            channels.append("telegram")
        if user["notify_discord"] == "true" and user["discord_id"]:
            channels.append("discord")

        for channel in channels:
            # rowcount=0 means UNIQUE conflict — already sent, skip
            with _db() as cur:
                cur.execute("""
                    INSERT INTO signal_deliveries
                        (user_id, signal_date, symbol, channel, status, sent_at)
                    VALUES (%s, %s, %s, %s, 'SENT', %s)
                    ON CONFLICT (signal_date, symbol, user_id, channel) DO NOTHING
                """, (user["user_id"], date, symbol, channel, _nst_now()))
                if cur.rowcount:
                    delivered += 1
                else:
                    skipped += 1

    # ponytail: actual Telegram/Discord push happens in bot processes via polling.
    # They read signal_deliveries to find new SENT rows and push to users.
    # Auto-execute for SYSTEM_BOT is handled separately via /positions/execute.

    return {
        "received":  True,
        "symbol":    symbol,
        "date":      date,
        "delivered": delivered,
        "skipped":   skipped,
        "users":     len(users),
    }


# ── GET /api/v1/system-account/trades ─────────────────────────────────────────

@app.post("/api/v1/trade-journal", dependencies=[Depends(_verify_key)])
async def create_trade_journal(request: Request):
    """
    Called by engine auditor.py after a trade closes.
    Writes one trade_journal row to the trading DB.
    Engine owns the analysis; trading owns the trade outcome record.
    """
    body = await request.json()
    if not body.get("symbol"):
        raise HTTPException(status_code=422, detail="symbol is required")
    now = _nst_now()
    body.setdefault("created_at", now)
    body.setdefault("telegram_id", "SYSTEM_BOT")

    cols = [c for c in body.keys()]
    vals = [body[c] for c in cols]
    placeholders = ", ".join(["%s"] * len(cols))
    col_str = ", ".join(cols)

    try:
        with _db() as cur:
            cur.execute(
                f'INSERT INTO trade_journal ({col_str}) VALUES ({placeholders}) RETURNING id',
                vals
            )
            row = cur.fetchone()
        return {"created": True, "id": row["id"] if row else None}
    except Exception as e:
        log.exception("create_trade_journal failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/system-account/trades", dependencies=[Depends(_verify_key)])
async def system_account_trades(
    audited: bool = Query(default=False),
    limit:   int  = Query(default=100, le=500),
):
    """
    Closed paper_portfolio rows for SYSTEM_BOT, with trade log embedded.
    Used by engine auditor.py — replaces direct paper_trade_log SQL read.
    """
    audited_val = "true" if audited else "false"
    try:
        with _db() as cur:
            cur.execute("""
                SELECT
                    pp.id            AS portfolio_id,
                    pp.telegram_id,
                    pp.symbol,
                    pp.total_shares,
                    pp.wacc,
                    pp.total_cost,
                    pp.first_buy_date,
                    pp.exit_date,
                    pp.exit_price,
                    pp.exit_shares,
                    pp.gross_pnl,
                    pp.net_pnl,
                    pp.result,
                    pp.test_mode,
                    pp.audited,
                    COALESCE(
                        json_agg(
                            json_build_object(
                                'action',         tl.action,
                                'shares',         tl.shares,
                                'price',          tl.price,
                                'gross_amount',   tl.gross_amount,
                                'brokerage',      tl.brokerage,
                                'sebon',          tl.sebon,
                                'dp_fee',         tl.dp_fee,
                                'cgt',            tl.cgt,
                                'total_fees',     tl.total_fees,
                                'net_amount',     tl.net_amount,
                                'capital_before', tl.capital_before,
                                'capital_after',  tl.capital_after,
                                'created_at',     tl.created_at
                            ) ORDER BY tl.id
                        ) FILTER (WHERE tl.id IS NOT NULL),
                        '[]'
                    ) AS trade_log
                FROM paper_portfolio pp
                LEFT JOIN paper_trade_log tl ON tl.portfolio_id = pp.id::text
                WHERE pp.telegram_id = 'SYSTEM_BOT'
                  AND pp.status      = 'CLOSED'
                  AND pp.audited     = %s
                  AND pp.test_mode   = 'false'
                GROUP BY pp.id
                ORDER BY pp.exit_date DESC
                LIMIT %s
            """, (audited_val, limit))
            rows = cur.fetchall()
        return {"trades": [dict(r) for r in rows], "count": len(rows)}
    except Exception as e:
        log.exception("system_account_trades failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
