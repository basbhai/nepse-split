# tasks.md — NEPSE Split: Execution

Format: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

---

## Phase 0 — Project setup (done this session)

- [x] **T00a** Split monolith into `nepse-decisionEngine/` + `nepse-trading/` folders
- [x] **T00b** Rename service: `nepse-engine` → `nepse-decisionEngine` (folder + docs + DB name)
- [x] **T00c** Create two local PostgreSQL databases: `nepse_decision_engine`, `nepse_trading`
- [x] **T00d** Root docs written/reconciled: `constitution.md`, `ARCHITECTURE.md`, `PRD.md`,
      `THREAT_MODEL.md`, `DATA_MODEL.md` — signal delivery endpoint reconciled, Python version fixed
- [x] **T00e** `.gitignore` + `.gitattributes` at root, `git add -A` staged
- [x] **T00f** `nepse-decisionEngine/schema.prisma` audited against production DB:
      6 dead tables dropped, ~40 dead columns removed, 3 tables got missing prod columns,
      `saved_queries` added. Codegen + migrations run → 48 tables in `nepse_decision_engine`

---

## Phase 1 — DB foundation

- [x] **T01** Write `nepse-trading/schema.prisma` with all 10 tables
      (users, user_channels, user_meroshare, user_broker, user_broker_links,
      user_signal_preferences, signal_deliveries + existing 4 paper tables)
      _File: nepse-trading/schema.prisma_

- [x] **T02** Run `python -m db.codegen` → regenerated `nepse-trading/db/schema.py`
      12 models generated (includes settings table added post-T01)

- [x] **T03** Write `nepse-trading/db/credentials.py`
      `encrypt_password(plaintext) → str`
      `decrypt_password(ciphertext) → str`
      Uses Fernet + `CREDENTIAL_ENCRYPTION_KEY` from env
      Raises `ValueError` if key missing, not silently broken

- [x] **T04** Run `python -m db.migrations` against local PostgreSQL
      nepse_trading: 13 tables created (12 trading + db_schema)
      nepse_decision_engine: 48 tables created

---

## Phase 2 — trading_api real implementations

- [x] **T05** `POST /api/v1/webhook/signal`
      Logs to signal_deliveries (dedup), returns delivered/skipped counts.
      Actual Telegram/Discord push handled by bot processes (T19–T21).
      _File: nepse-trading/trading_api.py_

- [x] **T06** `GET /api/v1/positions/summary`
      Returns open_count + slots_remaining for SYSTEM_BOT.
      sector_counts returns {} — share_sectors in engine DB (see T29).
      _File: nepse-trading/trading_api.py_

- [x] **T07** `GET /api/v1/briefing-data/{telegram_id}`
      Returns capital + open_positions + closed_today for one user.
      _File: nepse-trading/trading_api.py_

- [x] **T08** `GET /api/v1/users/approved`
      Returns all APPROVED users with telegram_id + discord_id.
      _File: nepse-trading/trading_api.py_

- [x] **T09** `POST /api/v1/positions/execute`
      Atomic buy: updates paper_capital, upserts paper_portfolio, appends paper_trade_log.
      _File: nepse-trading/trading_api.py_

---

## Phase 3 — registration + credential UI

### Auth architecture (engine = auth server)
Engine `dashboard_api.py` (port 8766) is the single auth server for ALL web UIs.
Trading `register_api.py` never duplicates auth — it calls the engine to validate sessions.

```
Engine (port 8766)
  POST /login          {password}          → nepse_session cookie  (admin)
  POST /user-login     {telegram_id, pin}  → nepse_session cookie  (per-user)
  GET  /api/v1/auth/validate-session       → {valid, type, user_id, telegram_id}
                         X-Service-Key + X-Session-Cookie header

Trading register_api (port 8767)
  _auth_guard(request) → calls engine validate-session
  unauthenticated admin routes → 302 to engine /login
  unauthenticated user routes  → 302 to engine /user-login
```

`_sessions` in engine extended to `{session_id: {expires_at, type, user_id, telegram_id}}`.
`users` table needs `pin_hash TEXT` column (SHA-256, set via Telegram `/setpin` command).
Telegram bot needs `/setpin <4-8 digit PIN>` command → writes `users.pin_hash`.

### Tasks

- [x] **T10** `POST /register` handler
      - Create users row (status=PENDING) + user_channels row
      - Return confirmation page
      - Idempotent: if platform_user_id already exists, return "already registered"
      _File: nepse-trading/register_api.py_

- [x] **T10a** Engine auth server extensions
      - `_sessions` extended to `{expires_at, type, user_id, telegram_id}`
      - `GET /api/v1/auth/validate-session` (X-Service-Key + X-Session-Cookie header)
        → `{valid, type, user_id, telegram_id}`
      - `/login` POST now writes typed session dict with type="admin"
      - `pin_hash TEXT` added to trading `users` table DDL + TABLE_COLUMNS + ALTER TABLE migration
      - NOTE: `/user-login` lives in register_api.py (port 8768) not engine — register_api
        has direct trading DB access; engine would need cross-service call for PIN validation.
        Admin sessions still flow through engine exclusively.
      _File: nepse-decisionEngine/web_ui/dashboard_api.py, nepse-trading/db/schema.py_

- [x] **T10b** Telegram bot `/setpin` command
      - Approved users only: `/setpin <4-8 digit PIN>`
      - `set_user_pin_hash()` in trading_core.py writes SHA-256 hash to users.pin_hash
        via user_channels JOIN
      - Reply: "✅ Web PIN set. Visit {TRADING_UI_URL}/credentials"
      _File: nepse-trading/modules/telegram_bot.py, modules/trading_core.py_

- [x] **T11** `credentials.html` page
      - Sections: Meroshare accounts, Broker accounts (with per-broker Links sub-section)
      - Each section: list existing + modal add/edit form
      - Password fields: write-only (leave blank to keep existing)
      _File: nepse-trading/web_ui/credentials.html_

- [x] **T12** Credential POST routes + auth guard
      - User guard: `_get_user_session()` checks `nepse_user_session` cookie; 401 on fail
      - `POST /credentials/meroshare` → upsert user_meroshare (encrypt password)
      - `POST /credentials/broker` → upsert user_broker (encrypt password)
      - `POST /credentials/broker/{id}/links` → insert user_broker_links
      - `DELETE /credentials/broker/{id}` → delete broker + cascade links
      - `DELETE /credentials/meroshare/{id}`, `DELETE /credentials/broker/{id}/links/{link_id}`
      _File: nepse-trading/register_api.py_

- [x] **T13** Admin routes + auth guard
      - Admin guard: calls engine `/api/v1/auth/validate-session`; 302 to engine /login if invalid
      - `GET /admin` serves admin.html
      - `POST /admin/approve/{user_id}` → approve_user() via trading_core
      - `POST /admin/deny/{user_id}` → deny_user() + users table BLOCKED
      - `GET /api/admin/data` → pending + all users with channels JSON
      _File: nepse-trading/register_api.py_

- [x] **T14** `admin.html` page
      - PENDING users table with approve/deny buttons + toast notifications
      - All users table: ID, name, status badge, channel tags (platform + ID + username), approved_at
      _File: nepse-trading/web_ui/admin.html_

---

## Phase 4 — wire engine coupling points

- [x] **T15** `filter_common._load_context()` — replace paper_portfolio direct query
      with `clients/trading_client.fetch_positions_summary()`
      On TradingServiceError: set sector_counts={}, assume max positions filled (fail closed)
      _File: nepse-decisionEngine/filter_common.py ~line 349_

- [x] **T16** `briefing.py` — ARCH CHANGE
      briefing.py moves fully to trading (T33). Engine no longer sends briefings.
      Trading calls engine `GET /api/v1/signals/today` for signal data.

- [x] **T17** `execution_monitor.py` live book read — ARCH CHANGE
      Engine no longer exposes portfolio via REST. execution_monitor reads from
      its own `paper_portfolio` table directly (trading DB). No engine call needed.

- [x] **T18** Engine signal notifier → POST to trading webhook
      `send_buy_signal` now calls `trading_client.push_signal()` → `/api/v1/webhook/signal`
      Trading service handles ALL fan-out (Telegram, Discord, SYSTEM_BOT execute)
      _File: nepse-decisionEngine/helper/notifier.py_

---

## Phase 5 — bot updates

- [x] **T19** Telegram `/register` command
      Direct DB write: creates `users` + `user_channels` rows (idempotent via UNIQUE on channel).
      `register_user()` in trading_core.py writes both paper_users and users/user_channels atomically.
      Reply includes credential setup URL from TRADING_UI_URL env var (default localhost:8767).
      _Files: nepse-trading/modules/trading_core.py, modules/telegram_bot.py_

- [x] **T20** `/signal` command → engine_client.fetch_signals()
      Replaced direct market_log SQL with engine_client.fetch_signals() REST call.
      On EngineServiceError: replies "engine unavailable — try again shortly".
      Richer display: confidence emoji, entry/SL/target prices, reasoning preview.
      _File: nepse-trading/modules/telegram_bot.py_

- [x] **T21** Signal push includes broker links
      _fmt_broker_links() helper added to trading_api.py.
      All active user_broker_links fetched in one bulk query before fan-out.
      Each user's message gets their own links footer appended (only if they have any).
      Format: 🔗 [Label1](url1) · [Label2](url2)
      _File: nepse-trading/trading_api.py_

- [x] **T22** `user_signal_preferences` default row on user approval
      approve_user() in trading_core.py looks up users.id via user_channels and:
      - Updates users.status = 'APPROVED', approved_at, approved_by
      - Inserts user_signal_preferences (notify_telegram=true, notify_discord=false,
        auto_execute=false) ON CONFLICT DO NOTHING
      Logs warning if user_channels row missing (pre-T19 registration), never crashes.
      _File: nepse-trading/modules/trading_core.py_

---

## Phase 6 — Two-database REST bridges (separate DB requires these before trading can run)

- [x] **T26** Engine `GET /api/v1/symbols/validate?q={query}`
      Replace direct `share_sectors` SQL in `trading_core.lookup_symbols()` with a REST call.
      Exact match first, ILIKE fallback, returns up to 8 results on partial match.
      _File: nepse-decisionEngine/dashboard_api.py_

- [x] **T27** Engine `GET /api/v1/market-watch/{symbol}/ltp`
      Expose latest `atrad_market_watch` row per symbol for execution_monitor.
      Returns: `{ ltp, vwap, bid_ask_ratio, dpr_proximity, low_dpr, high_dpr }`
      On no row today: return 404 (monitor falls back to last known price).
      _File: nepse-decisionEngine/dashboard_api.py_

- [x] **T28** Trading `GET /api/v1/system-account/trades`
      Replace direct `paper_trade_log` SQL read in engine's `auditor.py`.
      Returns unaudited rows for `telegram_id='SYSTEM_BOT'`, with `?audited=false&limit=100`.
      _File: nepse-trading/trading_api.py_

- [x] **T29** Update `trading_core.lookup_symbols()` to call engine REST (`T26`) instead of direct SQL.
      On EngineServiceError: raise `ValueError("symbol lookup unavailable")` — never pass raw input to SQL.
      _File: nepse-trading/modules/trading_core.py_

- [x] **T30** Update `execution_monitor._get_ltp()` to call engine REST (`T27`) instead of direct SQL.
      On EngineServiceError: use last known LTP from in-process cache, log warning.
      _File: nepse-trading/modules/execution_monitor.py_

- [x] **T31** Update `engine/auditor.py` to call trading REST (`T28`) instead of direct `paper_trade_log` SQL.
      On TradingServiceError: log + skip this audit cycle, do not crash.
      _File: nepse-decisionEngine/analysis/auditor.py_

---

## Phase 7 — Code cleanup (boundary enforcement)

These files exist in the wrong service and need to move/refactor.
Schemas and endpoints are already updated — this phase cleans the Python code.

- [x] **T32** Move `modules/meroshare.py` engine → trading
      Created nepse-trading/modules/meroshare.py with verify_live_holding + auth only.
      Engine meroshare.py updated with Z: improvements: login retry, close_from_edis,
      _write_portfolio missing_since logic, by_scrip EDIS tracking.
      close_from_edis wired in summary_workflow.py.
      ponytail: credentials still env-var; DB lookup from user_meroshare table when T24 lands.

- [x] **T33** Move `workflows/briefing.py` engine → trading
      Created nepse-trading/modules/briefing.py (paper mode: native trading DB).
      Created nepse-trading/modules/notifier.py (sync Telegram/Discord sends).
      Added POST /api/v1/briefing/run to trading_api.py.
      Added trigger_briefing() to trading_client.py.
      morning_workflow.py calls trading REST, falls back to local briefing if trading down.
      Engine briefing.py kept for live-mode fallback.

- [~] **T34** Strip direct Telegram/Discord sends from engine `notifier.py`
      BUY/WAIT signals already stripped (T18/T37).
      Remaining _send_admin_only (heartbeat, errors, EOD) kept direct for resilience —
      routing health alerts through trading creates a failure point for exactly the
      messages needed when trading is down. notify_admin() in trading_client.py
      available as an opt-in secondary channel when callers want it.

- [x] **T35** Trading `POST /api/v1/notify/admin`
      Added to trading_api.py. Sends to TELEGRAM_CHAT_ID + DISCORD_WEBHOOK_URL.
      trading_client.notify_admin() added as engine-side caller.
      _File: nepse-trading/trading_api.py_

- [!] **T36** Update engine `auditor.py` to write `trade_journal` via REST
      SKIPPED — trade_journal is engine-owned data (engine DB). Routing engine writes
      through the trading service has no architectural benefit and adds a failure point.
      The existing /api/v1/trade-journal endpoint in trading_api.py (if needed) is for
      trading-initiated writes, not engine audits.

- [x] **T37** Add `push_signal()` call to `trading_client.py` for WAIT signals
      `send_wait_signal` in notifier.py currently sends to admin only via Telegram.
      Push WAIT signals to trading too so users with WAIT preference get notified.
      _File: nepse-decisionEngine/clients/trading_client.py_

---

## Blocked / deferred

- [ ] **T23** Auto-execute via ATrade API — deferred until ATrade credential flow is tested manually first
- [ ] **T24** Meroshare auto-application for roadshows — deferred, needs Meroshare API research
- [ ] **T25** Web session with proper JWT — current cookie approach is good enough for personal use
