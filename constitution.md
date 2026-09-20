# constitution.md — NEPSE Split: The Rules

Read this before writing a single line of code.

## Stack

- **Python 3.12** — no 3.11 backcompat needed
- **PostgreSQL (local)** — two separate databases: `nepse_decision_engine` and `nepse_trading`
- **FastAPI + uvicorn** — all HTTP APIs (trading_api on 8767, dashboard_api on 8766)
- **psycopg2** — DB driver, raw SQL only, no ORM
- **python-telegram-bot** — Telegram bot
- **discord.py** — Discord bot
- **cryptography (Fernet)** — credential encryption, one server key from .env
- **httpx** — all inter-service REST calls (engine ↔ trading)
- **Jinja2 or plain HTML** — web UI, no frontend framework

## What is forbidden

- No SQLAlchemy, no Django ORM, no Tortoise — raw SQL via `_db()` context manager only
- No new pip packages without explicit approval
- No async DB calls — psycopg2 is sync, keep it sync throughout
- No ORM migrations — schema changes go through `db/migrations.py` + `schema.prisma`
- No inline styles in HTML — use a single CSS block per page
- No JavaScript frameworks — vanilla JS only in web UI
- No f-strings in SQL — always use parameterised queries (`%s` placeholders)

## Database names

| Service | Database | Env var |
|---|---|---|
| nepse-decisionEngine | `nepse_decision_engine` | `DATABASE_URL=postgresql://user:pass@localhost:5432/nepse_decision_engine` |
| nepse-trading | `nepse_trading` | `DATABASE_URL=postgresql://user:pass@localhost:5432/nepse_trading` |

Each service has its own `.env` with its own `DATABASE_URL`. Neither service connects to the other's database directly. Cross-service data goes through REST only.

Two engine tables are read-only by the trading service **via REST** (not direct SQL):
- `share_sectors` — trading calls `GET /api/v1/symbols/validate` for `lookup_symbols()`
- `atrad_market_watch` — execution_monitor calls `GET /api/v1/market-watch/{symbol}/ltp`

Engine auditor reads trading's system account trades **via REST** (not direct SQL):
- `paper_trade_log` — engine calls `GET /api/v1/system-account/trades`

## DB access rules (both services)

```python
from sheets import read_tab, write_row, upsert_row, update_row, run_raw_sql
from db.connection import _db   # ONLY for multi-table atomic transactions
```

- `sheets.py` for all single-table reads/writes
- `_db()` context manager for anything needing a transaction across tables
- Never raw `psycopg2.connect()` outside `db/connection.py`
- All money columns stored as TEXT, converted to `Decimal` at the Python layer

## Service ownership (enforced by convention)

### nepse-decisionEngine owns
All market data, signals, and analysis — zero user awareness, zero notifications.

| Group | Tables |
|---|---|
| Price & market data | `price_history`, `market_breadth`, `indicators`, `indicators_intraday` |
| Signals | `market_log`, `gate_misses`, `gate_proposals`, `filter_candidates_log` |
| Scraped data | `atrad_market_watch`, `broker_flow`, `broker_holdings`, `floorsheet`, `floorsheet_signals`, `stealth_signals` |
| Fundamentals | `fundamentals`, `fundamental_beta` |
| Macro / geo | `geopolitical_data`, `nepal_pulse`, `nrb_monthly`, `daily_context_log` |
| Learning | `learning_hub`, `claude_audit`, `accuracy_review_log`, `system_proposals` |
| Council | `monthly_council_*`, `monthly_override` |
| Reference | `share_sectors`, `candle_patterns`, `candle_signals`, `fd_rates`, `fd_rate_summary` |
| AI logs | `ai_call_log`, `agent_trace`, `wait_parse_log` |
| Config | `settings`, `db_schema`, `nepse_indices`, `saved_queries` |
| Research | `backtest_results`, `capital_allocation`, `financial_advisor`, `financials` |
| Political | `news_effect_patterns`, `pattern_validation_log` |

### nepse-trading owns
All user-facing data, portfolio, notifications, and trade outcomes.

| Group | Tables |
|---|---|
| Identity | `users`, `user_channels` |
| Credentials | `user_meroshare`, `user_broker`, `user_broker_links` |
| Preferences | `user_signal_preferences` |
| Paper trading | `paper_capital`, `paper_portfolio`, `paper_trade_log` |
| Trade outcomes | `trade_journal` — written by engine auditor via REST POST |
| Notifications | `signal_deliveries` |
| Logs | `auto_execute_log`, `credential_verify_log`, `admin_audit_log` |
| Config | `settings` (trading-specific) |

### Hard rules
- Engine NEVER writes to trading tables directly (separate DB — impossible anyway)
- Trading NEVER writes to engine tables directly
- Engine reads trading data → `GET /api/v1/system-account/trades` (T28)
- Trading reads engine data → `GET /api/v1/signals/today`, `/market/state`, `/symbols/validate`, `/market-watch/ltp`
- **All notifications** (Telegram, Discord, briefings, summaries) → through trading service only
- Engine has no TELEGRAM_BOT_TOKEN, no DISCORD_BOT_TOKEN in production

## Credential security

- All passwords stored encrypted with Fernet symmetric key
- Key lives in `.env` as `CREDENTIAL_ENCRYPTION_KEY` — never in DB
- Encrypt on write, decrypt on read, in `db/credentials.py` helper
- Never log decrypted credentials

## Coding style

- No comments explaining WHAT the code does — name things well instead
- One comment only when WHY is non-obvious (a constraint, a workaround, a known ceiling)
- Mark deliberate simplifications: `# ponytail: <ceiling> — upgrade when <trigger>`
- No docstrings longer than one line
- Dataclasses for structured return values, dicts for ad-hoc DB rows
- All datetimes in NST (`ZoneInfo("Asia/Kathmandu")`) unless explicitly UTC

## Port map

| Port | Service | Process |
|---|---|---|
| 8765 | engine | nrb_api.py |
| 8766 | engine | dashboard_api.py |
| 8767 | trading | trading_api.py |
| 8768 | trading | register_api (or merged into 8767) |
