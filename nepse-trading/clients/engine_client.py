"""
clients/engine_client.py — nepse-trading
Single module for all REST calls from the trading service to the engine.
Raises EngineServiceError on any failure — callers must degrade gracefully,
never crash the bot.
"""
import logging
import os
import httpx

log = logging.getLogger(__name__)

ENGINE_SERVICE_URL = os.getenv("ENGINE_SERVICE_URL", "http://localhost:8766")
_SECRET = os.getenv("ENGINE_API_SECRET", "")


class EngineServiceError(RuntimeError):
    pass


def _headers() -> dict:
    return {"X-Service-Key": _SECRET}


def fetch_signals() -> list:
    """
    Returns today's market_log PENDING BUYs.
    Bot /signal command shows "engine unavailable" on EngineServiceError — never crashes.
    """
    try:
        r = httpx.get(
            f"{ENGINE_SERVICE_URL}/api/v1/signals/today",
            headers=_headers(), timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise EngineServiceError(f"fetch_signals: {e}") from e


def fetch_market_state() -> dict:
    """Returns MARKET_STATE, circuit_breaker, geo_score."""
    try:
        r = httpx.get(
            f"{ENGINE_SERVICE_URL}/api/v1/market/state",
            headers=_headers(), timeout=5,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise EngineServiceError(f"fetch_market_state: {e}") from e



def fetch_symbol_lookup(query: str) -> list:
    """
    Validate / autocomplete a symbol against engine's share_sectors.
    Exact match first, then partial ILIKE up to 8 results.
    Raises EngineServiceError on failure.
    """
    try:
        r = httpx.get(
            f"{ENGINE_SERVICE_URL}/api/v1/symbols/validate",
            headers=_headers(), timeout=5, params={"q": query},
        )
        r.raise_for_status()
        return r.json().get("matches", [])
    except Exception as e:
        raise EngineServiceError(f"fetch_symbol_lookup: {e}") from e


def fetch_ltp(symbol: str):
    """
    Returns latest market-watch snapshot for symbol from engine DB.
    Keys: ltp, vwap, bid_ask_ratio, dpr_proximity, low_dpr, high_dpr, time.
    Returns None when no data today (404). Raises EngineServiceError on other failures.
    """
    try:
        r = httpx.get(
            f"{ENGINE_SERVICE_URL}/api/v1/market-watch/{symbol}/ltp",
            headers=_headers(), timeout=5,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise EngineServiceError(f"fetch_ltp({symbol}): {e}") from e
