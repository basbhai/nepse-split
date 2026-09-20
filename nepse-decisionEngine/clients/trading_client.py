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
