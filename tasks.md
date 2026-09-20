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

- [ ] **T10** `POST /register` handler
      - Create users row (status=PENDING) + user_channels row
      - Return confirmation page
      - Idempotent: if platform_user_id already exists, return "already registered"
      _File: nepse-trading/register_api.py_

- [ ] **T11** `credentials.html` page
      - Sections: Meroshare accounts, Broker accounts, Links
      - Each section: list existing + add new form
      - Password fields: write-only (show *** for existing, re-enter to change)
      _File: nepse-trading/web_ui/credentials.html_

- [ ] **T12** Credential POST routes
      - `POST /credentials/meroshare` → upsert user_meroshare (encrypt password)
      - `POST /credentials/broker` → upsert user_broker (encrypt password)
      - `POST /credentials/broker/{id}/links` → insert user_broker_links
      - `DELETE /credentials/broker/{id}` → delete broker + cascade deletes links
      _File: nepse-trading/register_api.py_

- [ ] **T13** Session auth (simple cookie)
      - On `/credentials` load: check session cookie
      - Cookie set at registration confirmation or on `/login` with platform_user_id + a PIN
      - Admin check: verify user_id matches ADMIN_USER_ID env var
      _File: nepse-trading/register_api.py_

- [ ] **T14** `admin.html` page
      - List of PENDING users with approve/deny buttons
      - List of all users with their linked accounts
      `POST /admin/approve/{user_id}` + `POST /admin/deny/{user_id}`
      _File: nepse-trading/web_ui/admin.html_

---

## Phase 4 — wire engine coupling points

- [ ] **T15** `filter_common._load_context()` — replace paper_portfolio direct query
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

- [ ] **T19** Telegram `/register` command
      POST to register_api or direct DB write: create user_channels row
      Reply with confirmation + web URL for credential setup

- [ ] **T20** `/signal` command → engine_client.fetch_signals()
      On EngineServiceError: reply "engine unavailable, try again shortly" — never crash

- [ ] **T21** Signal push includes broker links
      When fanning out T05, include user's active broker links in the message
      Format: clickable URLs grouped by link_type

- [ ] **T22** `user_signal_preferences` default row on user approval
      When admin approves a user, auto-create their preferences row with defaults
      (notify_telegram=true, notify_discord=false, auto_execute=false)

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

- [ ] **T29** Update `trading_core.lookup_symbols()` to call engine REST (`T26`) instead of direct SQL.
      On EngineServiceError: raise `ValueError("symbol lookup unavailable")` — never pass raw input to SQL.
      _File: nepse-trading/modules/trading_core.py_

- [ ] **T30** Update `execution_monitor._get_ltp()` to call engine REST (`T27`) instead of direct SQL.
      On EngineServiceError: use last known LTP from in-process cache, log warning.
      _File: nepse-trading/modules/execution_monitor.py_

- [ ] **T31** Update `engine/auditor.py` to call trading REST (`T28`) instead of direct `paper_trade_log` SQL.
      On TradingServiceError: log + skip this audit cycle, do not crash.
      _File: nepse-decisionEngine/analysis/auditor.py_

---

## Phase 7 — Code cleanup (boundary enforcement)

These files exist in the wrong service and need to move/refactor.
Schemas and endpoints are already updated — this phase cleans the Python code.

- [ ] **T32** Move `modules/meroshare.py` engine → trading
      Trading has `user_meroshare` credentials. meroshare.py uses them for live sync.
      Engine has no user credentials — it cannot use meroshare.py in production.
      _Move: nepse-decisionEngine/modules/meroshare.py → nepse-trading/modules/meroshare.py_

- [ ] **T33** Move `workflows/briefing.py` engine → trading
      Briefing sends morning portfolio + signal summary to users.
      Trading owns users, portfolio, and all notifications.
      Briefing calls engine `GET /api/v1/signals/today` for signal data.
      _Move: nepse-decisionEngine/workflows/briefing.py → nepse-trading/modules/briefing.py_

- [ ] **T34** Strip direct Telegram/Discord sends from engine `notifier.py`
      Engine sends BUY signals via `push_signal()` (done — T18).
      Remove remaining direct Telegram sends for: WAIT signals, summaries, heartbeat.
      Replace with calls to `POST /api/v1/notify/admin` on trading (see T35).
      _File: nepse-decisionEngine/helper/notifier.py_

- [ ] **T35** Trading `POST /api/v1/notify/admin`
      Engine calls this for non-signal admin alerts (errors, heartbeat, EOD summary).
      Trading sends via admin's Telegram/Discord channel.
      Body: { message: str, parse_mode: str, level: "info"|"warn"|"error" }
      _File: nepse-trading/trading_api.py_

- [ ] **T36** Update engine `auditor.py` to write `trade_journal` via REST
      Replace direct INSERT into `trade_journal` (engine DB) with
      POST to `trading_client.create_trade_journal(row)` → `/api/v1/trade-journal`
      _File: nepse-decisionEngine/analysis/auditor.py_

- [ ] **T37** Add `push_signal()` call to `trading_client.py` for WAIT signals
      `send_wait_signal` in notifier.py currently sends to admin only via Telegram.
      Push WAIT signals to trading too so users with WAIT preference get notified.
      _File: nepse-decisionEngine/clients/trading_client.py_

---

## Blocked / deferred

- [ ] **T23** Auto-execute via ATrade API — deferred until ATrade credential flow is tested manually first
- [ ] **T24** Meroshare auto-application for roadshows — deferred, needs Meroshare API research
- [ ] **T25** Web session with proper JWT — current cookie approach is good enough for personal use
