# tasks.md — NEPSE Split: Execution

Format: `[ ]` not started · `[~]` in progress · `[x]` done · `[!]` blocked

---

## Phase 1 — DB foundation

- [ ] **T01** Write `nepse-trading/schema.prisma` with all 10 tables
      (users, user_channels, user_meroshare, user_broker, user_broker_links,
      user_signal_preferences, signal_deliveries + existing 4 paper tables)
      _File: nepse-trading/schema.prisma_

- [ ] **T02** Run `python -m db.codegen` → regenerate `nepse-trading/db/schema.py`
      Verify TABLE_DDL has all 10 tables

- [ ] **T03** Write `nepse-trading/db/credentials.py`
      `encrypt_password(plaintext) → str`
      `decrypt_password(ciphertext) → str`
      Uses Fernet + `CREDENTIAL_ENCRYPTION_KEY` from env
      Raises `ValueError` if key missing, not silently broken

- [ ] **T04** Run `python -m db.migrations` against Neon
      Verify all 10 tables created, indexes in place
      Check CASCADE on user_broker_links.broker_id

---

## Phase 2 — trading_api real implementations

- [ ] **T05** `POST /api/v1/webhook/signal`
      - Read all users WHERE status=APPROVED + their signal_preferences + user_channels
      - For each: check signal_deliveries dedup, send via right channel, log delivery
      - On channel send failure: log error_msg, status=FAILED, continue to next user
      _File: nepse-trading/trading_api.py_

- [ ] **T06** `GET /api/v1/positions/summary`
      - Query paper_portfolio WHERE status=OPEN AND test_mode=false
      - Join share_sectors for sector_counts
      - Return { open_count, sector_counts, slots_remaining }
      _File: nepse-trading/trading_api.py_

- [ ] **T07** `GET /api/v1/briefing-data/{telegram_id}`
      - Query paper_capital + paper_portfolio for one user
      _File: nepse-trading/trading_api.py_

- [ ] **T08** `GET /api/v1/users/approved`
      - Query users + user_channels for all APPROVED users
      _File: nepse-trading/trading_api.py_

- [ ] **T09** `POST /api/v1/positions/execute`
      - Validate body (symbol, shares, price, telegram_id, timestamp)
      - Write paper_portfolio row (OPEN) + paper_trade_log row in one transaction
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

- [ ] **T16** `briefing.py` — replace 3 direct paper table reads
      - `_get_approved_users()` → `trading_client.fetch_approved_users()`
      - `_get_paper_capital()` + `_get_paper_portfolio()` → `trading_client.fetch_briefing_data(telegram_id)`
      On TradingServiceError: log + skip paper section, don't crash
      _File: nepse-decisionEngine/workflows/briefing.py_

- [ ] **T17** `execution_monitor.py` live book read
      Replace `portfolio` table direct query with `engine_client.fetch_live_positions()`
      On EngineServiceError: continue guarding paper positions, log live book unavailable
      _File: nepse-trading/modules/execution_monitor.py_

- [ ] **T18** Engine signal notifier → POST to trading webhook
      Replace `send_buy_signal` Telegram direct send with POST to `/api/v1/webhook/signal`
      Trading service handles the fan-out
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

- [ ] **T26** Engine `GET /api/v1/symbols/validate?q={query}`
      Replace direct `share_sectors` SQL in `trading_core.lookup_symbols()` with a REST call.
      Exact match first, ILIKE fallback, returns up to 8 results on partial match.
      _File: nepse-decisionEngine/dashboard_api.py_

- [ ] **T27** Engine `GET /api/v1/market-watch/{symbol}/ltp`
      Expose latest `atrad_market_watch` row per symbol for execution_monitor.
      Returns: `{ ltp, vwap, bid_ask_ratio, dpr_proximity, low_dpr, high_dpr }`
      On no row today: return 404 (monitor falls back to last known price).
      _File: nepse-decisionEngine/dashboard_api.py_

- [ ] **T28** Trading `GET /api/v1/system-account/trades`
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

## Blocked / deferred

- [ ] **T23** Auto-execute via ATrade API — deferred until ATrade credential flow is tested manually first
- [ ] **T24** Meroshare auto-application for roadshows — deferred, needs Meroshare API research
- [ ] **T25** Web session with proper JWT — current cookie approach is good enough for personal use
