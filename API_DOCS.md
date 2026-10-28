# NEPSE AI — Complete API Reference

Two services, two databases, one shared secret for service-to-service calls.

| Service | Port | Database | Base URL |
|---------|------|----------|----------|
| **nepse-decisionEngine** (Dashboard API) | `8766` | `nepse_engine` | `http://localhost:8766` |
| **nepse-trading** (Trading API) | `8767` | `nepse_trading` | `http://localhost:8767` |

---

## Auth

### Dashboard API (port 8766)
- **Local / LAN / Tailnet**: no auth required — trusted automatically
- **Public Funnel (Tailscale)**: session cookie from `POST /login` OR `X-Api-Key: <DASHBOARD_AUTH_SECRET>` header
- **Service-to-service** (`/api/v1/*` routes): `X-Service-Key: <ENGINE_API_SECRET>` header

### Trading API (port 8767)
- **All routes**: `X-Service-Key: <ENGINE_API_SECRET>` header

**Shared secret** (both `.env` files):
```
ENGINE_API_SECRET=0959086f767c2f14a9d6af02c91112048de79951c93590c638df8f42f0467cf3
```

---

---

# SERVICE 1 — nepse-decisionEngine Dashboard API
## Base URL: `http://localhost:8766`

---

## System

### `GET /health`
```
No auth required.
Response: { "status": "ok", "service": "dashboard_api" }
```

### `GET /robots.txt`
```
No auth required.
Response: plain text
```

---

## Auth (Public Funnel only)

### `GET /login`
Serves the HTML login page.

### `POST /login`
```json
Body:   { "password": "string" }
Response (200): { "status": "ok" }   + sets nepse_session cookie (30 days)
Response (401): { "detail": "Incorrect password" }
```

### `POST /logout`
Clears session cookie.
```json
Response: { "status": "ok" }
```

---

## Static Pages

### `GET /` or `GET /dashboard`
Serves `dashboard.html` (full trading dashboard UI).

### `GET /nrb`
Serves `nrb_entry.html` (NRB data entry form).

### `GET /register`
Serves `register.html` (user registration page).

---

## User Registration

### `POST /register`
Register or update a user for paper trading bots.
```json
Body:
{
  "full_name":   "string",          // required
  "telegram_id": "string | null",   // at least one of these required
  "discord_id":  "string | null"
}

Response (created): { "status": "created", "telegram_id": "string" }
Response (updated): { "status": "updated", "id": number }
```

---

## Dashboard Data

All dashboard endpoints are **cached** (server-side 2 min, client Cache-Control 2 min).

### `GET /dashboard/summary`
Top-level portfolio + signal summary.
```json
Response:
{
  "paper_capital":   number,
  "open_positions":  number,
  "unrealized_pnl":  number,
  "trade_journal": {
    "total_trades": number,
    "wins":         number,
    "losses":       number,
    "win_rate":     number,
    "avg_return":   number
  },
  "market_state":   "BULL | BEAR | CRISIS | RECOVERY | NEUTRAL | null",
  "nepal_score":    number,
  "geo_score":      number,
  "nepse_30d":      [ { "date": "YYYY-MM-DD", "current_value": number, "change_pct": number } ],
  "recent_signals": [ { "date": "...", "symbol": "...", "action": "BUY|WAIT|AVOID", ... } ]
}
```

### `GET /dashboard/market_log`
Latest 10 Claude analyst signals.
```json
Response: { "rows": [ { ...market_log row... } ], "count": number }
```

### `GET /dashboard/broker_flow`
Top 8 accumulation + top 8 distribution from the latest broker_flow date.
```json
Response:
{
  "date": "YYYY-MM-DD",
  "accumulation": [ { "symbol", "name", "acc_broker_count_1d", "acc_qty_1d", "acc_amount_1d", "acc_top_broker_1d", "net_flow_1d" } ],
  "distribution":  [ { "symbol", "name", "dist_broker_count_1d", "dist_qty_1d", ... } ]
}
```

### `GET /dashboard/learning_hub`
Active lessons from the learning hub.
```json
Response:
{
  "lessons":            [ { ...learning_hub row... } ],
  "superseded_lessons": { "id": { ...row... } }
}
```

### `GET /dashboard/audit`
Claude accuracy audit log (all rows).
```json
Response: { "rows": [ { ...claude_audit row... } ], "count": number }
```

### `GET /dashboard/gate_tracker`
Gate miss analysis — false blocks, correct blocks, false block rate by category.
```json
Response:
{
  "overall": {
    "total": number, "pending": number, "stamped": number,
    "false_block_rate": number, "would_have_caught_rate": number
  },
  "by_category": [ { "gate_category", "decision", "total", "false_block_rate", ... } ],
  "recent_false_blocks": [ { "date", "symbol", "gate_category", "outcome_return_pct" } ]
}
```

### `GET /dashboard/council`
Latest monthly council run — log, agenda, checklist, system proposals.
```json
Response:
{
  "run_month":  "YYYY-MM",
  "log":        [ { ...monthly_council_log row... } ],
  "agenda":     [ { ...monthly_council_agenda row... } ],
  "checklist":  [ { ...monthly_council_checklist row... } ],
  "proposals":  [ { ...system_proposals row... } ]
}
```

### `GET /dashboard/trades`
Full trade_journal + full paper_portfolio history.
```json
Response:
{
  "trade_journal":   [ { ...trade_journal row... } ],
  "paper_portfolio": [ { ...paper_portfolio row... } ]
}
```

### `GET /dashboard/portfolio`
Open positions (paper + live) with real-time PnL.
```json
Response:
{
  "open_positions": [
    {
      "book":               "paper | live",
      "symbol":             "string",
      "total_shares":       number,
      "wacc":               number,     // all-in cost per share
      "total_cost":         number,
      "first_buy_date":     "YYYY-MM-DD",
      "current_ltp":        number,
      "unrealized_pnl_npr": number,
      "unrealized_pnl_pct": number,
      "unrealized_pnl_net": true        // net of sell fees + CGT
    }
  ],
  "cash_balance": number,
  "live_flags":   { "monitor": bool, "auto_close": bool }
}
```

---

## Stealth Signals (Broker Accumulation)

### `GET /stealth/watching`
Brokers currently in WATCHING status.
```json
Response: { "rows": [ { ...stealth_signals row..., "days_watching": number } ], "count": number }
```

### `GET /stealth/triggered`
Signals that have triggered — includes live return %.
```json
Response: { "rows": [ { ...stealth_signals row..., "days_since_trigger": number, "live_return_pct": number } ], "count": number }
```

### `GET /stealth/history`
Closed stealth signals with P&L.
```json
Response:
{
  "rows":  [ { ...row..., "days_held": number, "result": "WIN|LOSS|BREAKEVEN" } ],
  "count": number,
  "summary": { "total_closed": number, "win_rate": number, "avg_return": number, "median_return": number }
}
```

### `GET /stealth/stats`
Per-broker stats + overall summary.
```json
Response:
{
  "broker_stats": [
    { "broker_id", "broker_name", "signal_count", "trigger_rate", "avg_return", "win_rate", "validated_hit_rate" }
  ],
  "overall": { "total_signals", "watching", "triggered", "closed", "hit_rate", "avg_return" }
}
```

---

## Logs

### `GET /dashboard/logs/list`
List available engine log files.
```json
Response: { "files": [ { "filename", "path", "date", "time", "size" } ] }
```

### `GET /dashboard/logs/file?path=<relative-path>`
Read a log file (HTML/raw content truncated for safety).
```json
Response: { "lines": ["string", ...] }
```

---

## Reporter (SQL Sandbox)
Only SELECT queries allowed. Dangerous keywords blocked.

### `GET /reporter/tables`
List all tables in public schema.
```json
Response: { "tables": ["market_log", "settings", ...] }
```

### `GET /reporter/sectors`
Distinct sector values from stocks table.
```json
Response: { "sectors": ["commercial banking", "hydropower", ...] }
```

### `GET /reporter/queries`
List saved queries.
```json
Response: { "queries": [ { "id", "name", "description", "created_at" } ] }
```

### `GET /reporter/queries/{id}`
Get one saved query.
```json
Response: { "id": number, "name": "string", "description": "string", "sql": "string" }
```

### `POST /reporter/queries`
Save a query.
```json
Body:     { "name": "string", "description": "string", "sql": "SELECT ..." }
Response: { "id": number, "name": "string" }
```

### `POST /reporter/run`
Run a SQL query. Use `:param_name` syntax in SQL; pass values in `params`.
```json
Body:
{
  "sql":    "SELECT * FROM market_log WHERE symbol = :symbol LIMIT 10",
  "params": { "symbol": "NABIL" }
}
Response: { "rows": [ {...} ], "count": number }
```

### `GET /reporter/query?sql=SELECT...&param=value`
GET version of reporter/run. Named params work the same way.
```
Example: GET /reporter/query?sql=SELECT * FROM market_log WHERE symbol = :s LIMIT 5&s=NABIL
Response: { "sql": "...", "params": {...}, "rows": [...], "count": number }
```

### `POST /reporter/download/csv`
Download query results as CSV.
```json
Body:     { "sql": "SELECT ...", "params": {}, "filename": "export" }
Response: CSV file stream
```

### `POST /reporter/download/xlsx`
Download query results as Excel.
```json
Body:     { "sql": "SELECT ...", "params": {}, "filename": "export" }
Response: XLSX file stream
```

---

## NRB Data Entry

### `GET /nrb/periods`
All existing NRB periods (for duplicate detection in UI).
```json
Response: { "periods": [ { "period", "fiscal_year", "month_number", "inserted_at" } ] }
```

### `POST /nrb/upsert`
Save one month of NRB macroeconomic data.
```json
Body (all optional except first 3):
{
  "period":       "2081-08",     // required — Nepali fiscal month
  "fiscal_year":  "2081/82",     // required
  "month_number": "8",           // required
  "is_annual":    "false",
  "policy_rate":             "5.0",
  "bank_rate":               "6.5",
  "crr_percentage":          "4.0",
  "slr_percentage":          "11.0",
  "lending_rate_pct":        "10.2",
  "deposit_rate_pct":        "6.8",
  "interbank_rate_pct":      "4.5",
  "tbill_91d_rate_pct":      "4.2",
  "cpi_inflation":           "3.8",
  "credit_growth_rate":      "8.5",
  "private_sector_credit_growth_yoy_pct": "8.5",
  "m2_growth_yoy_pct":       "9.0",
  "deposit_growth_yoy_pct":  "10.2",
  "npl_ratio_pct":           "3.2",
  "liquidity_injected_billion": "45.0",
  "liquidity_status":        "ADEQUATE",
  "remittance_yoy_change_pct": "12.5",
  "remittance_total_billion_npr": "120.0",
  "fx_reserve_months":       "8.5",
  "usd_npr_rate":            "133.5",
  "bop_overall_balance_usd_m": "250.0",
  "bop_current_account_usd_m": "-150.0",
  "bop_capital_account_usd_m": "400.0",
  "bop_trade_deficit_usd_m": "600.0",
  "bop_status":              "SURPLUS",
  "bop_trend":               "IMPROVING",
  "bop_impact_on_nepse":     "POSITIVE",
  "nepse_index_value":       "2850.0",
  "market_cap_billion_npr":  "4200.0",
  "overall_sentiment":       "BULLISH",
  "forward_guidance":        "NRB maintaining accommodative stance",
  "key_risks":               "Inflation uptick, remittance slowdown"
}

Response:
{ "status": "success", "period": "2081-08", "message": "...", "fields_written": number }
```

---

## Settings

### `GET /settings`
All dashboard-controllable engine settings + v2 stats.
```json
Response:
{
  "FILTER_V2_ENABLED":     "false",
  "ENGINE_V1_ENABLED":     "true",
  "ENGINE_V2_ENABLED":     "true",
  "ENGINE_V3_ENABLED":     "false",
  "TELEGRAM_ENABLED":      "false",
  "NOTIFY_CHANNEL":        "BOTH",
  "MARKET_STATE":          "BULL",
  "CLAUDE_ANALYST_BACKEND":"openrouter",
  "CLAUDE_MAX_CALLS_PER_DAY": "3",
  "PAPER_MODE":            "true",
  "CIRCUIT_BREAKER":       "",
  "v2_stats": { "total": number, "rescue": number, "today": number },
  ...
}
```

### `PATCH /settings/{key}`
Update one setting. Only whitelisted keys allowed.
```
Allowed keys:
  FILTER_V2_ENABLED, FILTER_V2_TOP_N, FILTER_V2_GATE_RESCUE
  ENGINE_V1_ENABLED, ENGINE_V2_ENABLED, ENGINE_V3_ENABLED, V3_GEMINI_ENABLED
  TELEGRAM_ENABLED, NOTIFY_CHANNEL
  INDIA_NEPAL_RELATIONS, GULF_STABILITY, NRB_STANCE, GOVT_STABILITY
  LOAD_SHEDDING_HRS, SEBON_EVENT, BUDGET_EVENT
  CLAUDE_MAX_CALLS_PER_DAY, GEMINI_MAX_FLAGS, CLAUDE_ANALYST_BACKEND
  COUNCIL_USE_FREE_STACK
  EMA_PULLBACK_ENABLED, EMA_PULLBACK_WEIGHT
  UPTREND_PULLBACK_ENABLED, UPTREND_PULLBACK_WEIGHT
  Trading_Days, RISK_REWARD_GATE_ENABLED
  DASHBOARD_PUBLIC_AUTH_REQUIRED
  LIVE_MONITOR_ENABLED, LIVE_AUTO_CLOSE_ENABLED, EDIS_AUTO

Body:     { "value": "string" }
Response: { "key": "string", "value": "string", "updated": true }
```

### `POST /settings/test-telegram`
Send a test message to the configured Telegram chat.
```json
Response: { "sent": true }
```

### `POST /settings/reset-circuit-breaker`
Clear the circuit breaker flag.
```json
Response: { "reset": true }
```

### `GET /settings/trading-mode`
Check current paper/live mode.
```json
Response:
{
  "paper_mode":     true,
  "mode":           "PAPER",
  "confirm_phrase": "SWITCH TO LIVE"
}
```

### `POST /settings/trading-mode/switch`
Switch between paper and live mode. Restarts execution monitor.
```json
Body:     { "confirm": "SWITCH TO LIVE" }   // must match confirm_phrase exactly
Response: { "switched": true, "new_mode": "LIVE", "monitor_restarted": bool }
```

---

## Engine Versions

### `GET /engine-versions`
Current filter engine activation state.
```json
Response:
{
  "v1": { "enabled": true,  "name": "Filter v1 (Snapshot)",    "description": "..." },
  "v2": { "enabled": true,  "name": "Filter v2 (Progression)",  "description": "..." },
  "v3": { "enabled": false, "name": "Filter v3 (Oversold Recovery)", "gemini_enabled": false }
}
```

### `POST /engine-versions`
Toggle filter engine versions. At least one must remain enabled.
```json
Body:     { "v1": true, "v2": true, "v3": false, "v3_gemini": false }
Response: { "success": true, "v1": true, "v2": true, "v3": false, "v3_gemini": false }
```

---

## Service-to-Service API — Engine (called by nepse-trading)
**Auth**: `X-Service-Key: <ENGINE_API_SECRET>`

### `GET /api/v1/symbols/validate?q=NABIL`
Validate or fuzzy-match a symbol against share_sectors.
```json
Response (exact):  { "matches": ["NABIL"], "exact": true }
Response (fuzzy):  { "matches": ["NABILBF", "NABF"], "exact": false }
```

### `GET /api/v1/market-watch/{symbol}/ltp`
Latest intraday tick for a symbol (today only).
```json
Response: { "ltp": number, "vwap": number, "bid_ask_ratio": number, "dpr_proximity": number }
Response (404): no tick today
```

### `GET /api/v1/signals/today`
Today's pending BUY signals from market_log.
```json
Response:
{
  "date":    "YYYY-MM-DD",
  "count":   number,
  "signals": [
    {
      "id", "symbol", "sector", "action", "confidence",
      "entry_price", "stop_loss", "target",
      "primary_signal", "reasoning", "market_state", "composite_score"
    }
  ]
}
```

### `GET /api/v1/market/state`
Current engine market configuration.
```json
Response:
{
  "paper_mode":      "true | false",
  "circuit_breaker": "false | <reason>",
  "market_state":    "BULL | BEAR | CRISIS | RECOVERY | NEUTRAL",
  "geo_score":       "string",
  "combined_geo":    "string",
  "loss_streak":     "string",
  "fd_rate_pct":     "string"
}
```

---

---

# SERVICE 2 — nepse-trading Trading API
## Base URL: `http://localhost:8767`
## Auth: ALL routes require `X-Service-Key: <ENGINE_API_SECRET>`

---

## System

### `GET /health`
```json
Response: { "status": "ok", "service": "trading_api" }
```

---

## Users

### `GET /api/v1/users/approved`
All APPROVED users with their Telegram + Discord channel IDs.
```json
Response:
{
  "count": number,
  "users": [
    {
      "user_id":     "string",
      "full_name":   "string",
      "telegram_id": "string | null",
      "discord_id":  "string | null",
      "username":    "string | null"
    }
  ]
}
```

---

## Positions

### `GET /api/v1/positions/summary`
SYSTEM_BOT open position count. Used by engine for sector concentration gate.
```json
Response:
{
  "open_count":      number,
  "sector_counts":   {},
  "slots_remaining": number       // MAX_PAPER_POSITIONS (15) - open_count
}
```

### `POST /api/v1/positions/execute`
Auto-execute a BUY for SYSTEM_BOT. Writes paper_portfolio + paper_capital + paper_trade_log atomically.
```json
Body:
{
  "symbol":        "NABIL",
  "shares":        10,
  "price":         1240.00,
  "telegram_id":   "SYSTEM_BOT",
  "timestamp":     "2026-09-21 14:30:00",
  "signal_id":     123,           // optional
  "suggested_hold": 17,           // optional
  "primary_signal": "MACD"        // optional
}

Response: { "executed": true, "telegram_id": "SYSTEM_BOT", "trade_id": number }
Errors:
  404 — no capital row for telegram_id
  400 — insufficient capital
```

---

## Briefing Data

### `GET /api/v1/briefing-data/{telegram_id}`
Capital state + open positions + today's closed trades for one user.
```json
Response:
{
  "capital": {
    "telegram_id": "string",
    "current_capital": number,
    ...
  },
  "open_positions": [
    { "symbol", "total_shares", "wacc", "total_cost", "first_buy_date" }
  ],
  "closed_today": [
    { "symbol", "exit_date", "exit_price", "net_pnl", "result" }
  ]
}
```

---

## Signal Pipeline

### `POST /api/v1/webhook/signal`
**The core signal delivery endpoint.** Called by engine `notifier.py` on every analyst result.

- **BUY / WAIT** → fans out to all APPROVED users via Telegram DM + Discord DM (parallel push)
- **AVOID** → admin-only push to `TELEGRAM_CHAT_ID`
- Dedup: `UNIQUE(signal_date, symbol, user_id, channel)` prevents double-sends

```json
Body:
{
  // Required
  "symbol":       "NABIL",
  "signal_date":  "2026-09-21",
  "action":       "BUY | WAIT | AVOID",

  // Signal metadata
  "confidence":      78.0,
  "entry_price":     1240.0,
  "stop_loss":       1190.0,
  "target":          1380.0,
  "risk_reward":     2.8,
  "allocation_npr":  12000.0,
  "shares":          9,
  "breakeven":       1258.0,
  "suggested_hold":  17,
  "primary_signal":  "MACD",
  "composite_score": 82.5,
  "sector":          "commercial banking",
  "reasoning":       "MACD bullish cross + BB support...",
  "lesson_applied":  "MACD 17-day hold confirmed",
  "wait_condition":  "RSI < 35 AND volume > 1.5x avg"   // for WAIT only
}

Response (BUY/WAIT):
{
  "received":  true,
  "symbol":    "NABIL",
  "action":    "BUY",
  "date":      "2026-09-21",
  "delivered": 2,           // signal_deliveries rows written
  "sent_ok":   2,           // successful Telegram/Discord pushes
  "skipped":   0,           // already sent (dedup)
  "users":     1            // approved users found
}

Response (AVOID):
{
  "received":  true,
  "symbol":    "UPPER",
  "action":    "AVOID",
  "delivered": 1
}
```

**Message format per action:**

| Action | Recipients | Format |
|--------|-----------|--------|
| BUY | All approved users | Full trade card: entry/stop/target/R:R/sector/reasoning/lesson |
| WAIT | All approved users | Brief: confidence, reasoning, trigger condition, watch price |
| AVOID | Admin only | Single line: confidence, primary signal, reasoning |

---

## System Account Trades

### `GET /api/v1/system-account/trades?audited=false&limit=100`
Closed SYSTEM_BOT positions with embedded trade log. Used by engine auditor.
```json
Query params:
  audited: bool (default false) — filter by audited status
  limit:   int  (default 100, max 500)

Response:
{
  "count": number,
  "trades": [
    {
      "portfolio_id",  "telegram_id",    "symbol",
      "total_shares",  "wacc",           "total_cost",
      "first_buy_date","exit_date",       "exit_price",
      "exit_shares",   "gross_pnl",      "net_pnl",
      "result",        "test_mode",       "audited",
      "trade_log": [
        {
          "action", "shares", "price", "gross_amount",
          "brokerage", "sebon", "dp_fee", "cgt",
          "total_fees", "net_amount",
          "capital_before", "capital_after", "created_at"
        }
      ]
    }
  ]
}
```

---

## Trade Journal

### `POST /api/v1/trade-journal`
Write one trade outcome record. Called by engine auditor after a trade closes.
```json
Body (flexible — any columns from trade_journal table):
{
  "symbol":        "NABIL",
  "telegram_id":   "SYSTEM_BOT",     // defaults to SYSTEM_BOT
  "result":        "WIN | LOSS",
  "return_pct":    "12.5",
  "entry_date":    "2026-09-01",
  "exit_date":     "2026-09-18",
  "hold_days":     17,
  ...
}

Response: { "created": true, "id": number }
```

---

---

# Signal Flow — End to End

```
claude_analyst.py
  → produces AnalystResult (BUY / WAIT / AVOID)
  → calls notifier.send_buy_signal() / send_wait_signal()

notifier.py
  → calls trading_client.push_signal()         [httpx POST, timeout 10s]
  → on TradingServiceError: logs + continues    [never crashes pipeline]

trading_client.py  (nepse-decisionEngine/clients/)
  → POST http://localhost:8767/api/v1/webhook/signal
  → X-Service-Key header

trading_api.py  (nepse-trading/)
  → formats BUY / WAIT / AVOID message
  → writes signal_deliveries per user (UNIQUE dedup)
  → asyncio.gather → sends Telegram DM + Discord DM in parallel
  → returns { delivered, sent_ok, skipped, users }
```

---

# Postman Setup

## Collection Variables
| Variable | Value |
|----------|-------|
| `engine_base` | `http://localhost:8766` |
| `trading_base` | `http://localhost:8767` |
| `service_key` | `0959086f767c2f14a9d6af02c91112048de79951c93590c638df8f42f0467cf3` |
| `dashboard_key` | `7bpIngnhlf1i9jfISBSjGWasZMRzWuSh5CB0mFaSb48` |

## Headers
For all trading API requests:
```
X-Service-Key: {{service_key}}
Content-Type: application/json
```

For engine `/api/v1/*` routes:
```
X-Service-Key: {{service_key}}
```

For engine dashboard public routes (Tailscale only):
```
X-Api-Key: {{dashboard_key}}
```

---

# Database Ownership

| Table | DB | Owner |
|-------|----|-------|
| `market_log` | `nepse_engine` | Engine |
| `settings` | `nepse_engine` | Engine |
| `learning_hub` | `nepse_engine` | Engine |
| `trade_journal` | `nepse_engine` | Engine |
| `gate_misses` | `nepse_engine` | Engine |
| `broker_flow` | `nepse_engine` | Engine |
| `stealth_signals` | `nepse_engine` | Engine |
| `nepse_indices` | `nepse_engine` | Engine |
| `nepal_pulse` | `nepse_engine` | Engine |
| `geopolitical_data` | `nepse_engine` | Engine |
| `price_history` | `nepse_engine` | Engine |
| `atrad_market_watch` | `nepse_engine` | Engine |
| `share_sectors` | `nepse_engine` | Engine |
| `nrb_monthly` | `nepse_engine` | Engine |
| `monthly_council_log` | `nepse_engine` | Engine |
| `paper_portfolio` | `nepse_trading` | Trading |
| `paper_capital` | `nepse_trading` | Trading |
| `paper_trade_log` | `nepse_trading` | Trading |
| `users` | `nepse_trading` | Trading |
| `user_channels` | `nepse_trading` | Trading |
| `user_signal_preferences` | `nepse_trading` | Trading |
| `signal_deliveries` | `nepse_trading` | Trading |
