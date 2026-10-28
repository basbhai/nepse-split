"""
briefing.py — nepse-trading
Morning market briefing (10:30 AM NST). Trading service owns users + paper data.
Engine data (market state, signals, KPIs) fetched via engine REST — degrades
gracefully if engine is unreachable.

Paper mode: personalised brief per approved user via Telegram/Discord DM.
Live mode:  single brief to admin chat.

CLI:
  python -m modules.briefing        → build and send
  python -m modules.briefing print  → print to console only
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from db.connection import _db
from clients.engine_client import fetch_market_state, fetch_signals, EngineServiceError
from modules.notifier import send_tg, send_discord_dm

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BRIEFING] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

NST            = timezone(timedelta(hours=5, minutes=45))
MAX_MSG_LENGTH = 4000
STARTING_CAPITAL = 100_000.0


# ── Engine context (cached per run) ───────────────────────────────────────────

def _engine_state() -> dict:
    """Fetch market state from engine. Returns safe defaults on failure."""
    try:
        return fetch_market_state()
    except EngineServiceError as e:
        log.warning("engine_state unavailable: %s — using defaults", e)
        return {}


def _engine_signals() -> list:
    """Fetch today's BUY signals from engine. Empty list on failure."""
    try:
        return fetch_signals()
    except EngineServiceError as e:
        log.warning("engine_signals unavailable: %s", e)
        return []


# ── Paper data (native trading DB) ────────────────────────────────────────────

def _get_approved_users() -> list:
    try:
        with _db() as cur:
            cur.execute("""
                SELECT uc.platform_user_id AS telegram_id,
                       u.username, u.full_name,
                       (SELECT dc.platform_user_id FROM user_channels dc
                        WHERE dc.user_id = u.id AND dc.platform = 'DISCORD' LIMIT 1) AS discord_id
                FROM users u
                JOIN user_channels uc ON uc.user_id = u.id AND uc.platform = 'TELEGRAM'
                WHERE u.status = 'APPROVED'
                ORDER BY u.id
            """)
            return cur.fetchall()
    except Exception as e:
        log.error("approved_users failed: %s", e)
        return []


def _get_paper_portfolio(telegram_id: str) -> list:
    try:
        with _db() as cur:
            cur.execute("""
                SELECT symbol, total_shares, wacc, total_cost,
                       first_buy_date, updated_at
                FROM paper_portfolio
                WHERE telegram_id = %s AND status = 'OPEN'
                ORDER BY first_buy_date ASC
            """, (str(telegram_id),))
            return cur.fetchall()
    except Exception as e:
        log.error("paper_portfolio failed for %s: %s", telegram_id, e)
        return []


def _get_paper_capital(telegram_id: str) -> dict:
    try:
        with _db() as cur:
            cur.execute("""
                SELECT current_capital, total_realised_pnl,
                       total_trades, total_wins, total_losses, total_fees_paid
                FROM paper_capital
                WHERE telegram_id = %s
            """, (str(telegram_id),))
            row = cur.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        log.error("paper_capital failed for %s: %s", telegram_id, e)
        return {}


# ── Message builders ───────────────────────────────────────────────────────────

def _build_market_header(nst_now: datetime, state: dict) -> list:
    sep         = "━" * 22
    day_name    = nst_now.strftime("%A")
    date_str    = nst_now.strftime("%Y-%m-%d")
    market_st   = state.get("market_state", "UNKNOWN")
    combined    = int(float(state.get("combined_geo", "0") or 0))
    geo_emoji   = "🟢" if combined >= 2 else ("🟡" if combined >= -1 else "🔴")

    return [
        sep,
        "📊 *NEPSE MORNING BRIEF*",
        f"{day_name} {date_str} | {nst_now.strftime('%H:%M')} NST",
        sep,
        "",
        "📈 *MARKET*",
        f"State:  {market_st}",
        f"Geo:    {geo_emoji} {combined:+d}/10",
    ]


def _build_paper_brief_for_user(
    telegram_id: str,
    username: str,
    nst_now: datetime,
    state: dict,
    signals: list,
) -> str:
    cap       = _get_paper_capital(telegram_id)
    positions = _get_paper_portfolio(telegram_id)

    current_capital = float(cap.get("current_capital",    STARTING_CAPITAL) or STARTING_CAPITAL)
    realised_pnl    = float(cap.get("total_realised_pnl", 0) or 0)
    total_trades    = int(  cap.get("total_trades",        0) or 0)
    total_wins      = int(  cap.get("total_wins",          0) or 0)
    total_losses    = int(  cap.get("total_losses",        0) or 0)
    fees_paid       = float(cap.get("total_fees_paid",     0) or 0)
    total_invested  = sum(float(p.get("total_cost", 0) or 0) for p in positions)
    net_worth       = current_capital + total_invested
    user_win_rate   = round(total_wins / total_trades * 100, 1) if total_trades > 0 else 0.0

    lines = _build_market_header(nst_now, state)
    lines += [
        "",
        "📄 *PAPER TRADING MODE*",
        f"Hello {username or 'Trader'} 👋",
        "",
        "💰 *YOUR CAPITAL*",
        f"Liquid:    NPR {current_capital:,.0f}",
        f"Invested:  NPR {total_invested:,.0f}",
        f"Net worth: NPR {net_worth:,.0f}",
    ]

    pnl_emoji = "📈" if realised_pnl >= 0 else "📉"
    lines.append(f"Realised PnL: {pnl_emoji} NPR {realised_pnl:+,.0f}")
    if fees_paid > 0:
        lines.append(f"Total fees paid: NPR {fees_paid:,.0f}")

    lines += ["", "💼 *YOUR POSITIONS*"]
    if positions:
        for p in positions:
            shares = int(float(p.get("total_shares", 0) or 0))
            wacc   = float(p.get("wacc", 0) or 0)
            cost   = float(p.get("total_cost", 0) or 0)
            since  = p.get("first_buy_date", "?")
            lines.append(
                f"  📌 *{p.get('symbol')}* | {shares} sh @ NPR {wacc:,.2f}"
                f" | Cost: NPR {cost:,.0f} | Since: {since}"
            )
    else:
        lines.append("  No open positions")

    if total_trades > 0:
        lines += [
            "",
            "🏆 *YOUR PERFORMANCE*",
            f"Trades: {total_trades}  |  W: {total_wins}  L: {total_losses}",
            f"Win rate: {user_win_rate:.1f}%",
        ]

    if signals:
        lines += ["", "🤖 *TODAY'S SIGNALS*"]
        for s in signals[:3]:
            confidence = s.get("confidence", s.get("confidence_score", "?"))
            lines.append(
                f"  🔔 *{s.get('symbol')}* — {s.get('primary_signal', '?')} "
                f"| conf={confidence}"
            )
    else:
        lines += ["", "🤖 *SIGNALS*", "  No signals fired yet today"]

    if state.get("circuit_breaker", "false").lower() == "true":
        lines += [
            "",
            "🚨 *CIRCUIT BREAKER ACTIVE*",
            "All new BUY signals blocked. Manual review required.",
        ]

    lines += ["", "━" * 22]
    msg = "\n".join(lines)
    if len(msg) > MAX_MSG_LENGTH:
        msg = msg[:MAX_MSG_LENGTH - 20] + "\n\n_[truncated]_"
    return msg


# ── Sender ─────────────────────────────────────────────────────────────────────

def _send_to_user(telegram_id: str, message: str, discord_id: str = "") -> bool:
    notify_channel = os.getenv("NOTIFY_CHANNEL", "BOTH").upper()
    has_discord    = bool(discord_id and str(discord_id).strip() not in ("", "None", "null"))

    if has_discord and notify_channel in ("DISCORD", "BOTH"):
        ok = send_discord_dm(str(discord_id), message)
        if ok:
            return True
        log.warning("Discord DM failed for discord_id=%s, falling through to Telegram", discord_id)

    if notify_channel in ("TELEGRAM", "BOTH"):
        return send_tg(telegram_id, message)

    log.warning("No delivery method for telegram_id=%s (channel=%s)", telegram_id, notify_channel)
    return False


# ── Main runner ────────────────────────────────────────────────────────────────

def run(print_only: bool = False) -> bool:
    """
    Build and send morning briefing.
    Paper mode: personalised brief per approved user.
    Live mode: single brief to admin Telegram.
    """
    nst_now = datetime.now(tz=NST)
    state   = _engine_state()
    signals = _engine_signals()
    paper_mode = state.get("paper_mode", "true").lower() == "true"

    log.info("BRIEFING starting — %s NST | paper=%s",
             nst_now.strftime("%Y-%m-%d %H:%M"), paper_mode)

    if paper_mode:
        users = _get_approved_users()
        if not users:
            log.warning("No approved paper trading users — nothing to send.")
            return False

        log.info("Sending paper briefs to %d approved user(s).", len(users))
        results = []
        out = open(sys.stdout.fileno(), mode="w", encoding="utf-8", buffering=1, closefd=False)

        for user in users:
            telegram_id = str(user.get("telegram_id", ""))
            username    = user.get("username") or user.get("full_name") or telegram_id
            discord_id  = str(user.get("discord_id") or "")
            if not telegram_id:
                continue
            message = _build_paper_brief_for_user(telegram_id, username, nst_now, state, signals)
            if print_only:
                out.write(f"\n{'='*60}\n  Brief for: {username} ({telegram_id})\n{'='*60}\n")
                out.write(message + "\n")
                results.append(True)
            else:
                ok = _send_to_user(telegram_id, message, discord_id=discord_id)
                results.append(ok)
        return any(results)

    else:
        admin_id = os.getenv("TELEGRAM_CHAT_ID", "")
        # Live mode: minimal brief — live portfolio owned by engine meroshare
        lines = _build_market_header(nst_now, state)
        if signals:
            lines += ["", "🤖 *TODAY'S SIGNALS*"]
            for s in signals[:3]:
                lines.append(f"  🔔 *{s.get('symbol')}* — {s.get('primary_signal', '?')}")
        lines += ["", "━" * 22]
        message = "\n".join(lines)
        if print_only:
            print(message)
            return True
        return send_tg(admin_id, message)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    arg        = sys.argv[1] if len(sys.argv) > 1 else ""
    print_only = (arg == "print")
    success    = run(print_only=print_only)
    sys.exit(0 if success else 1)
