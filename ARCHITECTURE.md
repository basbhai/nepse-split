# ARCHITECTURE.md — nepse-trading
## Tech Stack, Data Flow, and Component Structure

**Project:** nepse-trading
**Depends on:** nepse-decisionEngine (via REST only — no shared Python imports)

---

## 1. Tech Stack

| Layer | Choice | Reason |
|---|---|---|
| Language | Python 3.12 | Matches engine; no migration cost |
| DB driver | psycopg2-binary | Matches engine; proven in production |
| DB | PostgreSQL local (`nepse_trading`) | Separate database from engine (`nepse_decision_engine`); cross-service data via REST only |
| ORM | None — raw SQL via `_db()` context manager | Same pattern as engine; `sheets.py` is the only abstraction |
| Schema source of truth | `schema.prisma` (paper tables only) | Same codegen + migrations pipeline as engine |
| REST framework | FastAPI | Already in use in engine's dashboard_api.py |
| Telegram bot | python-telegram-bot | Already in production |
| Discord bot | discord.py (app_commands) | Already built in discord_bot.py |
| Time zone | `zoneinfo.ZoneInfo("Asia/Kathmandu")` via config.py | NST = UTC+5:45 |
| Auth (service-to-service) | X-Service-Key header, shared bearer secret | Two daemons on same box; no need for full JWT |
| Auth (dashboard public) | Session cookie + DASHBOARD_AUTH_SECRET | Matches engine pattern |
| Deployment | systemd user services on `shanvi@pc` (Ubuntu 24) | Matches engine |
| Process model | Three separate systemd services (see section 6) | Crash isolation between bots and API |

---

## 2. Repository Layout

```
nepse-trading/
│
├── main.py                      Entry point: starts bots in threads, starts trading_api
│
├── trading_api.py               FastAPI app — port 8767
│                                Service-to-service endpoints (engine calls these)
│                                User-facing registration endpoint
│
├── config.py                    NST, shared constants (copied from engine, no changes)
├── calendar_guard.py            Trading day / holiday logic (copied, no changes)
├── sheets.py                    DB abstraction — paper tables only
│                                DBReadError + raise_on_error added
│                                Engine-owned table reads FORBIDDEN here
│
├── modules/
│   ├── trading_core.py          Platform-agnostic logic: fees, execute_buy, execute_sell,
│   │                            user management, capital state, circuit breakers
│   ├── telegram_bot.py          Telegram command handlers
│   ├── discord_bot.py           Discord slash command handlers
│   └── execution_monitor.py    Intraday position guard (30s cycle)
│
├── clients/
│   └── engine_client.py         All REST calls to nepse-decisionEngine
│                                fetch_signals(), fetch_market_state(),
│                                fetch_live_positions(), notify_executed()
│                                Raises EngineServiceError on failure
│                                5s timeout, 1 retry, then raises
│
├── db/
│   ├── __init__.py
│   ├── connection.py            Same DATABASE_URL, shared Postgres pool
│   ├── schema.py                AUTO-GENERATED — paper tables only
│   ├── codegen.py               Reads schema.prisma, writes db/schema.py
│   └── migrations.py            CREATE TABLE / ALTER TABLE ADD COLUMN
│
├── schema.prisma                paper_users, paper_capital,
│                                paper_portfolio, paper_trade_log only
│
├── web_ui/
│   ├── register.html            Discord/Telegram user registration form
│   └── register_api.py          FastAPI on port 8768 (or merged into trading_api)
│
├── setup/
│   ├── nepse-trading-bots.service      telegram_bot + discord_bot
│   ├── nepse-trading-api.service       trading_api (uvicorn port 8767)
│   └── nepse-execution-monitor.service execution_monitor
│
├── log_config.py                Per-module rotating file handler (same as engine)
├── requirements.txt
└── .env                         Secrets (never committed)
```

---

## 3. Service Boundary and REST API

### 3.1 What the engine exposes (engine calls, trading service consumes)

All routes on `dashboard_api.py` port 8766, new `/api/v1/*` prefix:

```
GET  /api/v1/signals/today
     Returns: today's market_log rows where outcome='PENDING' and action='BUY'
     Auth: X-Service-Key header

GET  /api/v1/market/state
     Returns: { market_state, circuit_breaker, geo_score, combined_geo,
                loss_streak, paper_mode }
     Auth: X-Service-Key header

GET  /api/v1/positions/live
     Returns: open rows from portfolio table (live book, engine-owned)
     Used by: execution_monitor in trading service for live position guard
     Auth: X-Service-Key header

POST /api/v1/webhook/trade-executed
     Body: { symbol, shares, price, telegram_id, timestamp, action }
     Called by: trading service after system account auto-executes a BUY
     Engine writes this to portfolio table for learning hub consumption
     Auth: X-Service-Key header

GET  /api/v1/symbols/validate?q={query}
     Returns: { symbol: str } or 404 if no match
     Used by: trading_core.lookup_symbols() — replaces direct share_sectors SQL
     Exact match first, ILIKE fallback (returns up to 8 results on partial match)
     Auth: X-Service-Key header

GET  /api/v1/market-watch/{symbol}/ltp
     Returns: { ltp, vwap, bid_ask_ratio, dpr_proximity, low_dpr, high_dpr }
     Used by: execution_monitor — replaces direct atrad_market_watch SQL read
     Returns latest row (most recent time for today's date)
     Auth: X-Service-Key header

GET  /api/v1/system-account/trades
     Returns: paper_trade_log rows for telegram_id='SYSTEM_BOT', unaudited only
     Used by: engine auditor.py — replaces direct paper_trade_log SQL read
     Query params: ?audited=false&limit=100
     Auth: X-Service-Key header
```

### 3.2 What the trading service exposes (trading service calls, engine consumes)

All routes on `trading_api.py` port 8767:

```
GET  /api/v1/users/approved
     Returns: list of { telegram_id, username, discord_id } for all APPROVED users
     Used by: engine briefing.py for broadcast loop
     Auth: X-Service-Key header

GET  /api/v1/briefing-data/{telegram_id}
     Returns: { capital: {...}, open_positions: [...], closed_today: [...] }
     Used by: engine briefing.py to build per-user morning brief
     Auth: X-Service-Key header

GET  /api/v1/positions/summary
     Returns: { open_count: int, sector_counts: dict[str, int], slots_remaining: int }
     Used by: engine filter_common._load_context() for sector concentration gate
     CRITICAL: on failure, engine must assume max positions filled (fail closed)
     Auth: X-Service-Key header

POST /api/v1/webhook/signal
     Body: { symbol, signal_date, action, composite_score, suggested_hold, primary_signal }
     Called by: engine notifier.py after every BUY signal
     Trading service: (1) fans out alert to all approved users per their preferences,
                      (2) auto-executes for SYSTEM account via trading_core.execute_buy,
                      (3) calls back to engine POST /api/v1/webhook/trade-executed,
                      (4) logs to signal_deliveries (dedup prevents double-send on restart)
     Auth: X-Service-Key header

POST /api/v1/positions/execute
     Body: { symbol, shares, price, telegram_id, timestamp }
     Called internally (by webhook/signal handler for SYSTEM account, or admin tooling)
     Writes paper_portfolio row (OPEN) + paper_trade_log row in one transaction
     Returns: { executed: bool, trade_id: int }
     Auth: X-Service-Key header

GET  /health
     Returns: { status: "ok", db: "ok"|"error" }
     No auth — used by systemd health checks
```

### 3.3 Auth pattern

```python
# Both services read from .env
ENGINE_API_SECRET = os.getenv("ENGINE_API_SECRET")

# engine_client.py (trading service)
headers = {"X-Service-Key": ENGINE_API_SECRET}

# clients/trading_client.py (engine)
headers = {"X-Service-Key": ENGINE_API_SECRET}

# trading_api.py middleware
def _verify_service_key(request: Request):
    key = request.headers.get("X-Service-Key", "")
    if not hmac.compare_digest(key, ENGINE_API_SECRET):
        raise HTTPException(status_code=401)
```

---

## 4. Data Flow

### 4.1 User paper trade flow

```
User sends /buy NABIL 10 380
  -> telegram_bot.py cmd_buy()
     -> trading_core.lookup_symbols() — validate symbol against share_sectors
     -> trading_core.market_gate_message() — block if market closed
     -> trading_core.get_paper_capital() — check available capital
     -> trading_core.get_paper_position() — check existing position
     -> Show confirmation embed (Telegram message or Discord button)
  -> User confirms
     -> trading_core.execute_buy() — atomic DB transaction
        -> UPDATE paper_capital (deduct total_out)
        -> INSERT or UPDATE paper_portfolio
        -> INSERT paper_trade_log (append-only)
     -> Send confirmation to user
```

### 4.2 Engine BUY signal -> auto-execute flow

```
nepse-decisionEngine emits BUY signal
  -> helper/notifier.py push_buy_signal()
     -> POST /api/v1/webhook/signal (trading service port 8767)
        -> trading_api.py handle_signal()
           -> fan-out: send alert to all approved users per signal_preferences
              -> log each delivery to signal_deliveries (dedup by UNIQUE constraint)
           -> trading_core.execute_buy(SYSTEM_ACCOUNT_TID, symbol, ...)
              -> paper_portfolio + paper_capital + paper_trade_log writes
           -> POST /api/v1/webhook/trade-executed back to engine
              -> engine writes to portfolio (live book for learning hub)
```

### 4.3 Morning briefing flow (engine calls trading service)

```
engine workflows/morning_workflow.py
  -> workflows/briefing.py run()
     -> clients/trading_client.fetch_approved_users()
        -> GET /api/v1/users/approved (trading service)
     -> for each user:
        -> clients/trading_client.fetch_briefing_data(telegram_id)
           -> GET /api/v1/briefing-data/{telegram_id}
        -> build per-user brief message
        -> send via notifier (Discord DM or Telegram)
```

### 4.4 Filter context flow (engine calls trading service for sector counts)

```
engine filter_common._load_context()
  -> clients/trading_client.fetch_positions_summary()
     -> GET /api/v1/positions/summary (trading service)
     -> Returns { open_count, sector_counts, slots_remaining }
  -> On EngineServiceError (trading service down):
     -> ctx["sector_position_counts"] = {}
     -> ctx["max_positions"] = int(get_setting("MAX_POSITIONS", "3"))
     -> log.error("positions/summary unavailable — assuming max slots filled")
     -> proceed with conservative default (NOT assume all slots free)
```

### 4.5 Execution monitor flow

```
nepse-execution-monitor.service (trading service)
  -> execution_monitor.py main loop (30s cycle)
     -> _load_paper_positions() — direct DB read from paper_portfolio
     -> clients/engine_client.fetch_live_positions()
        -> GET /api/v1/positions/live (engine)
        -> On EngineServiceError: log, skip live book, guard paper only
     -> For each position:
        -> Fetch LTP from atrad_market_watch (direct DB, shared Postgres)
        -> Compute GIS (pure math, no AI calls)
        -> If HARD_STOP or TRAIL_STOP triggered:
           -> Paper: trading_core.execute_sell(telegram_id, ...)
           -> Live: alert admin (auto-close only if LIVE_AUTO_CLOSE_ENABLED=true)
```

---

## 5. Module Responsibilities

### modules/trading_core.py
Single source of truth for all trading business logic. No Telegram or Discord
imports. Shared by both bots and execution_monitor.

Owns:
- `execute_buy(telegram_id, symbol, shares, price)` — atomic
- `execute_sell(telegram_id, symbol, shares, price)` — atomic
- `calc_buy_fees(price, shares)` and `calc_sell_fees(price, shares, wacc, hold_days)`
- `get_paper_capital(telegram_id)`, `get_paper_position(telegram_id, symbol)`
- `get_user(telegram_id)`, `is_approved(telegram_id)`, `register_user(...)`
- `approve_user(telegram_id)`, `deny_user(telegram_id)`
- `reset_user_data(telegram_id)` — atomic (wipe + reseed)
- `count_open_positions(telegram_id)`, `get_all_open_positions(telegram_id)`
- `market_gate_message()`, `circuit_label(telegram_id)`, `mode_label()`
- `_circuit_breakers` dict (per-session in-process, keyed by telegram_id)
- `MAX_POSITIONS = 15` (paper; live uses engine settings)

Does NOT own:
- Any Telegram or Discord API calls
- Any reads from engine-owned tables

### clients/engine_client.py
All outbound REST calls to nepse-decisionEngine. Raises `EngineServiceError` on
any failure (timeout, HTTP error, connection refused). Callers handle the
exception; they never let it crash the bot.

```python
class EngineServiceError(Exception): pass

def fetch_signals() -> list[dict]:        # GET /api/v1/signals/today
def fetch_market_state() -> dict:         # GET /api/v1/market/state
def fetch_live_positions() -> list[dict]: # GET /api/v1/positions/live
def notify_executed(payload: dict) -> bool: # POST /api/v1/webhook/trade-executed
```

### trading_api.py
FastAPI on port 8767. Service-to-service and registration endpoints only.
Not a user-facing dashboard. All routes verify X-Service-Key except /health
and /register (public by design).

### sheets.py (trading service copy)
Identical interface to engine's sheets.py. Key differences:
- `DBReadError` exception class defined here
- `raise_on_error=True` available on `read_tab`, `read_tab_where`, `get_setting`
- Only reads/writes paper_* tables and share_sectors (read-only for symbol lookup)
- Engine-owned table reads are FORBIDDEN — enforced by code review convention

---

## 6. Deployment and Process Model

### Systemd services

```
# Trading service (nepse-trading repo)
nepse-trading-bots.service
  ExecStart: python -m modules.telegram_bot & python -m modules.discord_bot
  Restart: always
  RestartSec: 10

nepse-trading-api.service
  ExecStart: uvicorn trading_api:app --host 0.0.0.0 --port 8767
  Restart: always

nepse-execution-monitor.service
  ExecStart: python -m modules.execution_monitor
  Restart: always
  RestartSec: 30

# Engine services (nepse-decisionEngine repo, unchanged)
nepse-trading.timer          main.py (10-min trading loop)
nepse-morning.timer          morning_workflow
nepse-summary.timer          summary_workflow
nepse-weekly.timer           weekly_workflow
nepse-dashboard.service      dashboard_api (port 8766)
```

### Port map

```
8765   engine: nrb_api.py
8766   engine: dashboard_api.py (includes /api/v1/* engine routes)
8767   trading: trading_api.py
8768   trading: register_api.py (or merged into 8767)
```

### Deployment workflow (same as engine)

```
Edit on Windows (D:\project\nepse-trading)
-> git push
-> git pull on shanvi@pc
-> systemctl --user restart nepse-trading-bots.service
                             nepse-trading-api.service
                             nepse-execution-monitor.service
```

---

## 7. Failure Modes and Handling

| Failure | Behavior |
|---|---|
| Trading service down during engine briefing | briefing logs error, skips paper section, still sends market header |
| Trading service down during filter_common context load | engine assumes max positions filled (fail closed, not fail open) |
| Engine down during /signal bot command | Bot replies "Engine unavailable — signals paused", does not crash |
| Engine down during execution_monitor | Monitor guards paper book only, logs live book unavailable |
| DB connection error in execute_buy | DBReadError raised, bot replies "Internal error, try again", trade NOT written |
| Discord bot crash | Telegram bot unaffected (separate process) |
| Telegram bot crash | Discord bot unaffected (separate process) |
| execution_monitor crash | Bots unaffected, systemd restarts monitor within 30s |

---

## 8. What Does NOT Change After Split

- schema.prisma as source of truth for each service's own tables
- Import convention: `from sheets import` for all DB access in both repos
- `_db()` context manager for atomic multi-table transactions only
- `market_log` append-only rule (engine owns it, trading service never touches it)
- Learning hub training source: system account paper_trade_log rows — engine auditor
  now reads these via `GET /api/v1/system-account/trades` (separate DB, REST required)

## 9. Database Setup (local)

```
# Create both databases (run once)
createdb nepse_decision_engine
createdb nepse_trading

# Engine migrations
cd nepse-decisionEngine && python -m db.migrations

# Trading migrations
cd nepse-trading && python -m db.migrations
```

Each service has its own `.env`:
```
# nepse-decisionEngine/.env
DATABASE_URL=postgresql://postgres:password@localhost:5432/nepse_decision_engine

# nepse-trading/.env
DATABASE_URL=postgresql://postgres:password@localhost:5432/nepse_trading
```
