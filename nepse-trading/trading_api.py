"""
trading_api.py — nepse-trading
FastAPI service on port 8767. Exposes paper trading data to nepse-engine.
Auth: X-Service-Key header, same secret as ENGINE_API_SECRET in both .env files.
"""
import asyncio
import os
import re
import logging
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Header, Depends, Query, Request
from pydantic import BaseModel, Field

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


def _is_paper_mode() -> bool:
    try:
        rows = run_raw_sql("SELECT value FROM settings WHERE key = 'PAPER_MODE' LIMIT 1")
        return bool(rows) and rows[0]["value"].lower() == "true"
    except Exception:
        return True


# ── Push helpers ──────────────────────────────────────────────────────────────

async def _send_tg(chat_id: str, text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        return False
    if len(text) > 4000:
        text = text[:3990] + "\n...(truncated)"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            )
            if resp.status_code == 200:
                return True
            if resp.status_code == 400 and "parse" in resp.text.lower():
                resp2 = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
                return resp2.status_code == 200
            log.error("TG push %s: HTTP %d — %s", chat_id, resp.status_code, resp.text[:150])
            return False
    except Exception as e:
        log.error("TG push %s: %s", chat_id, e)
        return False


async def _send_discord_dm(discord_id: str, text: str) -> bool:
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token or not discord_id:
        return False
    discord_text = re.sub(r'(?<!\*)\*(?!\*)([^*]+)\*(?!\*)', r'**\1**', text)
    if len(discord_text) > 1900:
        discord_text = discord_text[:1900] + "\n...(truncated)"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://discord.com/api/v10/users/@me/channels",
                headers=headers, json={"recipient_id": str(discord_id)},
            )
            if r.status_code != 200:
                log.error("Discord DM channel: HTTP %d for %s", r.status_code, discord_id)
                return False
            channel_id = r.json().get("id")
            r2 = await client.post(
                f"https://discord.com/api/v10/channels/{channel_id}/messages",
                headers=headers, json={"content": discord_text},
            )
            return r2.status_code in (200, 201)
    except Exception as e:
        log.error("Discord DM %s: %s", discord_id, e)
        return False


# ── Signal formatters ─────────────────────────────────────────────────────────

def _fmt_buy(body) -> str:
    sep        = "▬" * 24
    paper      = "[SIMULATION] " if _is_paper_mode() else ""
    rr_emoji   = "🎯" if (body.risk_reward or 0) >= 2.0 else "⚠️"
    conf_emoji = "🟢" if (body.confidence or 0) >= 70   else "🟡"
    lines = [sep, f"{paper}🚨 *BUY SIGNAL — {body.symbol}*", sep]
    if body.sector:
        lines.append(f"🏭 Sector:      {body.sector.title()}")
    lines += [
        f"{conf_emoji} Confidence:  *{body.confidence:.0f}%*  |  Signal: {body.primary_signal}",
        "",
        f"💰 Entry:       *NPR {body.entry_price:,.0f}*",
    ]
    if body.stop_loss:
        lines.append(f"🛑 Stop Loss:   NPR {body.stop_loss:,.0f}")
    if body.target:
        lines.append(f"🎯 Target:      NPR {body.target:,.0f}")
    if body.breakeven:
        lines.append(f"⚖️  Breakeven:   NPR {body.breakeven:,.0f}")
    if body.risk_reward:
        lines.append(f"{rr_emoji} R/R Ratio:   *{body.risk_reward:.1f}x*")
    lines.append("")
    if body.shares:
        lines.append(f"📦 Shares:      {body.shares}")
    if body.allocation_npr:
        lines.append(f"💵 Allocation:  NPR {body.allocation_npr:,.0f}")
    if body.suggested_hold:
        lines.append(f"⏳ Hold:        ~{body.suggested_hold} days")
    if body.reasoning:
        lines += ["", "🧠 *Reasoning:*", f"_{body.reasoning[:1000]}_"]
    if body.lesson_applied and body.lesson_applied not in ("NONE", "", "none"):
        lines.append(f"\n📚 *Lesson:* _{body.lesson_applied[:300]}_")
    lines.append(sep)
    return "\n".join(lines)


def _fmt_wait(body) -> str:
    paper = "[SIMULATION] " if _is_paper_mode() else ""
    lines = [
        f"⏸ {paper}*WATCH — {body.symbol}*",
        f"📊 Conf: {body.confidence:.0f}% | Signal: {body.primary_signal}",
    ]
    if body.reasoning:
        lines.append(f"💬 _{body.reasoning[:300]}_")
    if body.wait_condition and body.wait_condition not in ("NONE", ""):
        lines.append(f"🔑 Trigger: _{body.wait_condition[:200]}_")
    if body.entry_price:
        lines.append(f"💰 Watch price: NPR {body.entry_price:,.0f}")
    return "\n".join(lines)


def _fmt_avoid(body) -> str:
    paper = "[SIMULATION] " if _is_paper_mode() else ""
    return (
        f"🚫 {paper}*AVOID — {body.symbol}*\n"
        f"📊 Conf: {body.confidence:.0f}% | {body.primary_signal}\n"
        f"💬 _{body.reasoning[:200]}_"
    )


def _fmt_broker_links(links: list) -> str:
    """Append user's active broker links as a Markdown footer line."""
    if not links:
        return ""
    parts = []
    for lk in links:
        label = (lk.get("label") or lk.get("link_type") or "Link").strip()
        url   = (lk.get("url") or "").strip()
        if url:
            parts.append(f"[{label}]({url})")
    if not parts:
        return ""
    return "\n🔗 " + " · ".join(parts)


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
    shares:      float = Field(gt=0)
    price:       float = Field(gt=0)
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
    stop_loss:       float = 0
    target:          float = 0
    reasoning:       str   = ""
    sector:          str   = ""
    risk_reward:     float = 0
    allocation_npr:  float = 0
    shares:          int   = 0
    breakeven:       float = 0
    wait_condition:  str   = ""
    lesson_applied:  str   = ""


@app.post("/api/v1/webhook/signal", dependencies=[Depends(_verify_key)])
async def webhook_signal(body: SignalBody):
    """
    Called by engine notifier.py on every BUY/WAIT/AVOID signal.
    BUY/WAIT  → fan out to all approved users via Telegram DM + Discord DM.
    AVOID     → admin-only push (TELEGRAM_CHAT_ID env).
    Dedup per (signal_date, symbol, user_id, channel) prevents double-sends.
    """
    symbol = body.symbol.upper()
    date   = body.signal_date
    action = body.action.upper()

    if action == "BUY":
        msg = _fmt_buy(body)
    elif action == "WAIT":
        msg = _fmt_wait(body)
    else:
        msg = _fmt_avoid(body)

    # AVOID → admin only, no user-level tracking needed
    if action == "AVOID":
        admin_id = os.getenv("TELEGRAM_CHAT_ID", "")
        pushed = 0
        if admin_id:
            ok = await _send_tg(admin_id, msg)
            pushed = 1 if ok else 0
        log.info("AVOID %s: admin push %s", symbol, "ok" if pushed else "skipped/failed")
        return {"received": True, "symbol": symbol, "action": action, "delivered": pushed}

    # BUY / WAIT → fan out to all approved users
    users = run_raw_sql("""
        SELECT
            u.id::text AS user_id,
            MAX(CASE WHEN uc.platform = 'telegram' THEN uc.platform_user_id END) AS telegram_id,
            MAX(CASE WHEN uc.platform = 'discord'  THEN uc.platform_user_id END) AS discord_id,
            COALESCE(usp.notify_telegram, 'true')  AS notify_telegram,
            COALESCE(usp.notify_discord,  'false') AS notify_discord
        FROM users u
        LEFT JOIN user_channels          uc  ON uc.user_id  = u.id::text
        LEFT JOIN user_signal_preferences usp ON usp.user_id = u.id::text
        WHERE u.status = 'APPROVED'
        GROUP BY u.id, usp.notify_telegram, usp.notify_discord
    """)

    # Fetch each user's active broker links in one query for link footer
    user_ids = [u["user_id"] for u in users] if users else []
    broker_link_rows = run_raw_sql("""
        SELECT ubl.user_id, ubl.link_type, ubl.label, ubl.url
        FROM user_broker_links ubl
        JOIN user_broker ub ON ub.id::text = ubl.broker_id
        WHERE ubl.is_active = 'true' AND ub.is_active = 'true'
          AND ubl.user_id = ANY(%s)
        ORDER BY ubl.user_id, ubl.link_type, ubl.id
    """, (user_ids,)) if user_ids else []

    links_by_user: dict[str, list] = {}
    for lk in (broker_link_rows or []):
        links_by_user.setdefault(lk["user_id"], []).append(lk)

    delivered  = 0
    skipped    = 0
    push_coros = []

    for user in users:
        user_msg = msg + _fmt_broker_links(links_by_user.get(user["user_id"], []))

        if user["notify_telegram"] == "true" and user["telegram_id"]:
            with _db() as cur:
                cur.execute("""
                    INSERT INTO signal_deliveries
                        (user_id, signal_date, symbol, channel, status, sent_at)
                    VALUES (%s, %s, %s, 'telegram', 'PENDING', %s)
                    ON CONFLICT (signal_date, symbol, user_id, channel) DO NOTHING
                """, (user["user_id"], date, symbol, _nst_now()))
                if cur.rowcount:
                    delivered += 1
                    push_coros.append(_send_tg(user["telegram_id"], user_msg))
                else:
                    skipped += 1

        if user["notify_discord"] == "true" and user["discord_id"]:
            with _db() as cur:
                cur.execute("""
                    INSERT INTO signal_deliveries
                        (user_id, signal_date, symbol, channel, status, sent_at)
                    VALUES (%s, %s, %s, 'discord', 'PENDING', %s)
                    ON CONFLICT (signal_date, symbol, user_id, channel) DO NOTHING
                """, (user["user_id"], date, symbol, _nst_now()))
                if cur.rowcount:
                    delivered += 1
                    push_coros.append(_send_discord_dm(user["discord_id"], user_msg))
                else:
                    skipped += 1

    sent_ok = 0
    if push_coros:
        results = await asyncio.gather(*push_coros, return_exceptions=True)
        sent_ok = sum(1 for r in results if r is True)
        log.info("Signal %s %s: pushed %d/%d ok", action, symbol, sent_ok, len(results))

    return {
        "received":  True,
        "symbol":    symbol,
        "action":    action,
        "date":      date,
        "delivered": delivered,
        "sent_ok":   sent_ok,
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


@app.post("/api/v1/briefing/run", dependencies=[Depends(_verify_key)])
async def run_briefing():
    """
    Trigger morning briefing from engine's morning_workflow.
    Runs in a background thread (briefing sends are sync/blocking).
    """
    import asyncio
    from modules.briefing import run as _briefing_run
    ok = await asyncio.get_event_loop().run_in_executor(None, _briefing_run)
    return {"ok": ok}


class NotifyAdminBody(BaseModel):
    message:    str
    parse_mode: str  = "Markdown"
    level:      str  = "info"    # info | warn | error


@app.post("/api/v1/notify/admin", dependencies=[Depends(_verify_key)])
async def notify_admin(body: NotifyAdminBody):
    """
    Engine calls this for non-signal admin alerts (errors, heartbeat, EOD summary).
    Sends to admin Telegram chat + Discord webhook. Fire-and-forget — never raises.
    """
    admin_id     = os.getenv("TELEGRAM_CHAT_ID", "")
    discord_url  = os.getenv("DISCORD_WEBHOOK_URL", "")
    tg_ok        = False
    discord_ok   = False

    if admin_id:
        tg_ok = await _send_tg(admin_id, body.message)

    if discord_url:
        discord_text = re.sub(r'(?<!\*)\*(?!\*)([^*]+)\*(?!\*)', r'**\1**', body.message)
        if len(discord_text) > 2000:
            discord_text = discord_text[:1990] + "\n...(truncated)"
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                resp = await client.post(discord_url, json={"content": discord_text})
                discord_ok = resp.status_code in (200, 204)
        except Exception as e:
            log.error("notify_admin discord: %s", e)

    return {"tg": tg_ok, "discord": discord_ok}


@app.post("/api/v1/system-account/trades/{portfolio_id}/audited",
          dependencies=[Depends(_verify_key)])
async def mark_trade_audited(portfolio_id: int):
    """Stamp audited='true' on a closed SYSTEM_BOT paper_portfolio row."""
    try:
        with _db() as cur:
            cur.execute(
                "UPDATE paper_portfolio SET audited = 'true' "
                "WHERE id = %s AND telegram_id = 'SYSTEM_BOT'",
                (portfolio_id,),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="trade not found")
        return {"updated": True, "portfolio_id": portfolio_id}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("mark_trade_audited(%s) failed: %s", portfolio_id, e)
        raise HTTPException(status_code=500, detail=str(e))
