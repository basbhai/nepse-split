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

| Table group | Owner | Rule |
|---|---|---|
| `users`, `user_*`, `paper_*`, `signal_deliveries` | nepse-trading | Engine never writes these |
| `market_log`, `portfolio`, `indicators`, everything else | nepse-decisionEngine | Trading never writes these |

Trading reads engine tables only via REST (`clients/engine_client.py`).
Engine reads trading tables only via REST (`clients/trading_client.py`).

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
