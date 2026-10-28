"""
meroshare.py — nepse-trading
Trading-service subset: only what execution_monitor needs from the Meroshare API.
No DB writes. Full sync/EDIS lives in nepse-decisionEngine/modules/meroshare.py.

ponytail: DEMAT/CLIENT_CODE read from env, not user_meroshare table — single
          user for now; wire DB lookup here when multi-user support lands (T24).
"""

import logging
import os
import time
from datetime import timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

NST         = timezone(timedelta(hours=5, minutes=45))
DEMAT       = os.getenv("MEROSHARE_DEMAT", "1301180000232764")
CLIENT_CODE = os.getenv("MEROSHARE_CLIENT_CODE", "11800")

AUTH_URL         = "https://webbackend.cdsc.com.np/api/meroShare/auth/"
MY_PORTFOLIO_URL = "https://webbackend.cdsc.com.np/api/meroShareView/myPortfolio/"

MEROSHARE_HEADERS = {
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin":       "https://meroshare.cdsc.com.np",
    "Referer":      "https://meroshare.cdsc.com.np/",
    "Connection":   "keep-alive",
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
}

LOGIN_MAX_ATTEMPTS  = 3
LOGIN_RETRY_SECONDS = 12


def _get_token() -> Optional[str]:
    payload = {
        "clientId": os.getenv("MEROSHARE_DP_ID"),
        "username": os.getenv("MEROSHARE_USERNAME"),
        "password": os.getenv("MEROSHARE_PASSWORD"),
    }
    # Meroshare 500s on rapid successive logins (~10s gap needed, confirmed live 2026-09-24).
    for attempt in range(1, LOGIN_MAX_ATTEMPTS + 1):
        try:
            r = requests.post(AUTH_URL, json=payload, headers=MEROSHARE_HEADERS, timeout=15)
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
                log.error("Meroshare: token not in response headers")
                return None
            log.info("Meroshare login OK")
            return token
        except Exception as e:
            log.error("Meroshare login error: %s", e)
            return None
    return None


def _fetch_my_portfolio(token: str) -> dict[str, dict]:
    """Real DEMAT balance via CDSC myPortfolio. Returns {symbol: {shares, ...}}."""
    headers = {**MEROSHARE_HEADERS, "Authorization": token}
    try:
        r = requests.post(MY_PORTFOLIO_URL, headers=headers, json={
            "sortBy": "script", "demat": [DEMAT], "clientCode": CLIENT_CODE,
            "page": 1, "size": 200, "sortAsc": True,
        }, timeout=15)
        if r.status_code != 200:
            log.error("myPortfolio failed: %d", r.status_code)
            return {}
        result = {}
        for row in (r.json().get("meroShareMyPortfolio") or []):
            symbol = row.get("script", "").upper().strip()
            if symbol:
                result[symbol] = {"shares": int(row.get("currentBalance", 0) or 0)}
        return result
    except Exception as e:
        log.error("myPortfolio error: %s", e)
        return {}


def verify_live_holding(symbol: str, min_shares: float = 1) -> bool:
    """
    Confirm symbol is held (>= min_shares) in the real DEMAT right now.
    Safety gate for live auto-close — fails closed on any error. Never raises.
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
