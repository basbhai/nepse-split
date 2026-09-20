# PRD.md — nepse-trading
## Product Requirements Document

**Project:** nepse-trading (split from nepse-decisionEngine)
**Version:** 1.0 — Initial split scope
**Status:** Pre-build, branching in progress
**Owner:** Hembro
**Last updated:** 2026-09-20

---

## 1. Context and Purpose

nepse-decisionEngine is an autonomous NEPSE trading signal system currently running in paper mode.
It has grown to contain two distinct concerns that need to be separated:

1. Signal generation, decision-making, and learning (stays in nepse-decisionEngine)
2. Multi-user paper trading, portfolio tracking, and bot interaction (this repo)

This document covers only the trading service. The engine's PRD is the existing
Technical Reference document.

---

## 2. Problem Statement

As of September 2026:

- The Telegram bot, Discord bot, execution monitor, and all user-facing paper trading
  logic live inside the same process as the signal engine.
- A bot crash or user interaction bug can affect signal generation and learning.
- The paper trading system has grown to serve multiple users with isolated capital,
  requiring its own operational lifecycle independent of the engine.
- The engine's `briefing.py` and `filter_common.py` directly query paper_portfolio,
  coupling two systems that should be independent.

The split resolves this by giving user-facing trading its own repo, deployment, and
service boundary. The engine and the trading service communicate only via REST.

---

## 3. Users

**Admin (Hembro):**
- Approves/denies user registrations
- Resets user paper data
- Runs /claude and /play commands (AI-assisted analysis and browser automation)
- Receives EOD performance summaries
- Is the only user whose trades feed back into the engine's learning hub
  (via the system account)

**Approved paper traders (small group, invite-only):**
- Register via dashboard, get approved by admin
- Receive BUY signal push alerts
- Record their own paper trades via /buy and /sell
- View their own capital, positions, and PnL
- Each user has fully isolated capital (NPR 100,000 starting), portfolio, and trade log

**System account (machine user, not a real person):**
- Auto-executes every BUY signal the engine emits
- Is the sole source of training data fed back to the engine's learning hub
- Visible only to admin; excluded from user-facing stats

---

## 4. Core Features

### 4.1 User Registration and Access Control
- Anyone can submit a registration via the dashboard `/register` page
  or via Telegram `/register` command
- Registration creates a PENDING row in paper_users
- Admin approves or denies via `/approve <telegram_id>` or `/deny <telegram_id>`
- Approved users get a seeded paper_capital row (NPR 100,000 starting capital)
- Discord account linking: user submits their discord_id via /register form;
  this links their discord_id to their canonical telegram_id
- Duplicate registration (same discord_id submitted twice) must UPDATE, not INSERT
- Status values: PENDING, APPROVED, BLOCKED

### 4.2 Paper Trading (Telegram)
- `/buy SYMBOL SHARES PRICE` — open or average into a position with confirmation prompt
- `/sell SYMBOL SHARES PRICE` — close or partial sell with confirmation prompt
- `/cancel` — cancel a pending confirmation
- Both commands blocked outside NEPSE market hours (11:00 AM – 3:00 PM NST, Mon-Fri,
  non-holiday) unless SANDBOX_MODE is active
- Averaging into an existing position recomputes WACC correctly
- Max positions per user is configurable (currently 15 for paper)
- Full fee math on every trade: brokerage (slab-based), SEBON (0.015%), DP (NPR 25),
  CGT (7.5% long-term / 10% short-term on profit only)
- All writes tagged test_mode='true' in sandbox, allowing clean purge before official trading

### 4.3 Paper Trading (Discord)
- Full parity with Telegram for read commands: /capital, /status, /pnl, /mode, /help
- Write commands (/buy, /sell) also available on Discord
- /buy and /sell require a button confirmation (ephemeral embed, 60-second timeout)
- Auth via discord_id -> telegram_id resolution (resolve_telegram_id in trading_core)
- /claude (admin only): run a one-off Claude Code prompt, response DM'd to admin
- /play (admin only): run a browser automation task via Playwright

### 4.4 Signal Delivery
- On each engine BUY signal, the engine calls the trading service's
  POST /api/v1/webhook/signal with the signal details
- The trading service: (1) fans out push alerts to all approved users per their
  signal_preferences (Telegram and/or Discord), (2) auto-executes for the system
  account only via trading_core.execute_buy, (3) logs every delivery to
  signal_deliveries — the UNIQUE constraint prevents double-push on restart
- /signal command shows today's engine BUY signals fetched via GET /api/v1/signals/today
- If the engine REST endpoint is unavailable, /signal returns a graceful
  "engine unavailable" message, never crashes the bot

### 4.5 Portfolio and Capital Views
- /status: open positions with symbol, shares, WACC, current unrealized context
- /capital: starting capital, current capital, total realized PnL, fees paid, CGT paid,
  win/loss counts, win rate
- /pnl: closed trades with symbol, exit date, shares, exit price, net PnL, result
- All views are per-user isolated; no user sees another user's data

### 4.6 Execution Monitor
- Runs as a separate systemd service, 30-second cycle during market hours
- Monitors all OPEN positions in paper_portfolio
- Computes GIS (Guardianship Intelligence Score) per position using ATrad live data
- Hard stop: -5% from entry (net of fees) triggers immediate paper close
- Trailing stop: activates at +6% net gain, floor at peak - 3%
- For live book positions (portfolio table): calls engine REST for live position data;
  alerts admin on trigger but does NOT auto-place ATrad orders unless
  LIVE_AUTO_CLOSE_ENABLED=true and the 55% WR gate has been passed

### 4.7 Morning Briefing Data
- The engine's briefing.py calls the trading service's
  GET /api/v1/briefing-data/{telegram_id} and GET /api/v1/users/approved
  instead of querying paper tables directly
- The trading service returns capital, open positions, and closed-today data per user
- If the trading service is unavailable, briefing logs the error and skips
  the paper section rather than crashing

### 4.8 Admin Controls
- /approve, /deny, /users, /reset (Telegram and Discord)
- /reset wipes one user's paper_trade_log, paper_portfolio, paper_capital
  and reseeds capital at NPR 100,000 (atomic, in one DB transaction)
- Dashboard /register page for Discord linking

---

## 5. Out of Scope (for this split)

- Live ATrad order placement (gated behind 55% WR threshold, not yet met)
- IPO application (MeroShare API does not support it)
- Any write to engine-owned tables (market_log, portfolio, learning_hub, etc.)
- User-initiated backtest runs
- Multi-account support (one primary account per user)

---

## 6. Success Criteria

- Both bots (Telegram + Discord) run independently of the engine process
- A bot restart does not affect the engine's signal generation cycle
- An engine restart shows "engine unavailable" gracefully in the bots, not a crash
- System account auto-executes every engine BUY within 30 seconds of the engine
  POST to /api/v1/positions/execute
- No user data leaks across users (isolation verified by querying with a
  different telegram_id and confirming empty results)
- Sandbox data is fully purgeable via test_mode='true' DELETE before official trading

---

## 7. Non-Functional Requirements

- Both bots must reconnect and resume on network drops without manual restart
- All DB errors in bot command handlers must return a user-friendly message,
  never an unhandled exception traceback
- The trading API (port 8767) must respond within 500ms for all read endpoints
- Service-to-service calls include a 5-second timeout with a single retry
- All inter-service calls authenticated with X-Service-Key header (shared secret)
