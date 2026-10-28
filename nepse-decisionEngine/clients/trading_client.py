"""
clients/trading_client.py — nepse-engine
Single module for all REST calls from the engine to the trading service.
Raises TradingServiceError on any failure — callers must handle it explicitly.
"""
import logging
import os
import httpx

log = logging.getLogger(__name__)

TRADING_SERVICE_URL = os.getenv("TRADING_SERVICE_URL", "http://localhost:8767")
_SECRET = os.getenv("ENGINE_API_SECRET", "")


class TradingServiceError(RuntimeError):
    pass


def _headers() -> dict:
    return {"X-Service-Key": _SECRET}


def fetch_positions_summary() -> dict:
    """
    Returns { open_count: int, sector_counts: dict, slots_remaining: int }.
    Callers must fail closed on TradingServiceError — never assume slots free.
    """
    try:
        r = httpx.get(
            f"{TRADING_SERVICE_URL}/api/v1/positions/summary",
            headers=_headers(), timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise TradingServiceError(f"fetch_positions_summary: {e}") from e


def fetch_briefing_data(telegram_id: str) -> dict:
    """Returns combined paper_capital + paper_portfolio for one user."""
    try:
        r = httpx.get(
            f"{TRADING_SERVICE_URL}/api/v1/briefing-data/{telegram_id}",
            headers=_headers(), timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise TradingServiceError(f"fetch_briefing_data({telegram_id}): {e}") from e


def fetch_approved_users() -> list:
    """Returns list of approved users for briefing broadcast."""
    try:
        r = httpx.get(
            f"{TRADING_SERVICE_URL}/api/v1/users/approved",
            headers=_headers(), timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise TradingServiceError(f"fetch_approved_users: {e}") from e


def push_signal(
    symbol: str,
    signal_date: str,
    action: str,
    composite_score: float = 0,
    confidence: float = 0,
    entry_price: float = 0,
    primary_signal: str = "",
    suggested_hold: int = 10,
    stop_loss: float = 0,
    target: float = 0,
    reasoning: str = "",
    sector: str = "",
    risk_reward: float = 0,
    allocation_npr: float = 0,
    shares: int = 0,
    breakeven: float = 0,
    wait_condition: str = "",
    lesson_applied: str = "",
) -> dict:
    """
    Push a BUY/WAIT/AVOID signal to the trading service.
    Trading service fans out to all approved users via Telegram/Discord DM.
    On TradingServiceError: log and continue — never crash the engine pipeline.
    """
    try:
        r = httpx.post(
            f"{TRADING_SERVICE_URL}/api/v1/webhook/signal",
            headers=_headers(), timeout=10,
            json={
                "symbol":          symbol,
                "signal_date":     signal_date,
                "action":          action,
                "composite_score": composite_score,
                "confidence":      confidence,
                "entry_price":     entry_price,
                "primary_signal":  primary_signal,
                "suggested_hold":  suggested_hold,
                "stop_loss":       stop_loss,
                "target":          target,
                "reasoning":       reasoning,
                "sector":          sector,
                "risk_reward":     risk_reward,
                "allocation_npr":  allocation_npr,
                "shares":          shares,
                "breakeven":       breakeven,
                "wait_condition":  wait_condition,
                "lesson_applied":  lesson_applied,
            },
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise TradingServiceError(f"push_signal({symbol} {action}): {e}") from e


def notify_signal_executed(
    symbol: str, shares: int, price: float, telegram_id: str, timestamp: str
) -> None:
    """Notify trading service that the system account executed a BUY."""
    try:
        r = httpx.post(
            f"{TRADING_SERVICE_URL}/api/v1/positions/execute",
            headers=_headers(), timeout=5,
            json={
                "symbol": symbol, "shares": shares, "price": price,
                "telegram_id": telegram_id, "timestamp": timestamp,
            },
        )
        r.raise_for_status()
    except Exception as e:
        raise TradingServiceError(f"notify_signal_executed({symbol}): {e}") from e


def trigger_briefing() -> bool:
    """
    Ask trading service to send morning briefing to all approved users.
    Returns True on success. Logs and returns False on TradingServiceError.
    """
    try:
        r = httpx.post(
            f"{TRADING_SERVICE_URL}/api/v1/briefing/run",
            headers=_headers(), timeout=60,
        )
        r.raise_for_status()
        return r.json().get("ok", False)
    except Exception as e:
        raise TradingServiceError(f"trigger_briefing: {e}") from e


def notify_admin(message: str, parse_mode: str = "Markdown", level: str = "info") -> None:
    """
    Send an admin alert (error, heartbeat, EOD summary) via the trading service.
    Non-blocking — logs on failure, never raises. Use as a secondary channel;
    engine's own _send_admin_only remains the primary for resilience.
    """
    try:
        r = httpx.post(
            f"{TRADING_SERVICE_URL}/api/v1/notify/admin",
            headers=_headers(), timeout=5,
            json={"message": message, "parse_mode": parse_mode, "level": level},
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("notify_admin: %s", e)


def fetch_unaudited_paper_trades(limit: int = 500) -> list:
    """
    Returns closed, unaudited SYSTEM_BOT paper_portfolio rows from trading service.
    Used by auditor.py run_paper_audit() — replaces direct paper_portfolio SQL.
    Raises TradingServiceError on failure — caller must log + skip the audit cycle.
    """
    try:
        r = httpx.get(
            f"{TRADING_SERVICE_URL}/api/v1/system-account/trades",
            headers=_headers(), timeout=10,
            params={"audited": "false", "limit": limit},
        )
        r.raise_for_status()
        return r.json().get("trades", [])
    except Exception as e:
        raise TradingServiceError(f"fetch_unaudited_paper_trades: {e}") from e


def mark_paper_trade_audited(portfolio_id: int) -> bool:
    """
    Stamp audited='true' on a paper_portfolio row in the trading service.
    Returns True on success. Never raises — auditor logs and continues on failure.
    """
    try:
        r = httpx.post(
            f"{TRADING_SERVICE_URL}/api/v1/system-account/trades/{portfolio_id}/audited",
            headers=_headers(), timeout=5,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("mark_paper_trade_audited(%s): %s", portfolio_id, e)
        return False
