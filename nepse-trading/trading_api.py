"""
trading_api.py — nepse-trading
FastAPI service on port 8767. Exposes paper trading data to nepse-engine.
Auth: X-Service-Key header, same secret as ENGINE_API_SECRET in both .env files.
"""
import os
import logging
from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel

log = logging.getLogger(__name__)

ENGINE_API_SECRET = os.getenv("ENGINE_API_SECRET", "")

app = FastAPI(title="nepse-trading API", version="1.0.0")


def _verify_key(x_service_key: str = Header(default="")):
    if ENGINE_API_SECRET and x_service_key != ENGINE_API_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/v1/briefing-data/{telegram_id}", dependencies=[Depends(_verify_key)])
async def briefing_data(telegram_id: str):
    """paper_capital + paper_portfolio for one user (used by engine briefing.py)."""
    # TODO: implement — query paper_capital + paper_portfolio
    raise HTTPException(status_code=501, detail="Not implemented")


@app.get("/api/v1/users/approved", dependencies=[Depends(_verify_key)])
async def approved_users():
    """List of all APPROVED users from paper_users (for briefing broadcast)."""
    # TODO: implement — query paper_users WHERE status = 'APPROVED'
    raise HTTPException(status_code=501, detail="Not implemented")


@app.get("/api/v1/positions/summary", dependencies=[Depends(_verify_key)])
async def positions_summary():
    """
    Open position count + sector breakdown (for filter_common._load_context).
    Returns: { open_count, sector_counts, slots_remaining }
    """
    # TODO: implement — query paper_portfolio WHERE status = 'OPEN'
    raise HTTPException(status_code=501, detail="Not implemented")


class ExecuteBody(BaseModel):
    symbol: str
    shares: int
    price: float
    telegram_id: str
    timestamp: str


@app.post("/api/v1/positions/execute", dependencies=[Depends(_verify_key)])
async def execute_position(body: ExecuteBody):
    """
    System account auto-execute: engine pushes a confirmed BUY here.
    Trading service writes to paper_portfolio and paper_trade_log.
    """
    # TODO: implement — insert into paper_portfolio + paper_trade_log
    raise HTTPException(status_code=501, detail="Not implemented")
