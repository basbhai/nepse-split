# DATA_MODEL.md — nepse-trading
## Database Tables, Relations, and Ownership

---

## 1. Ownership Principle

The trading service OWNS (read + write):

```
GROUP 1 — Identity
  users
  user_channels

GROUP 2 — User Services
  user_meroshare
  user_broker
  user_broker_links
  user_signal_preferences

GROUP 3 — Subscriptions
  subscription_plans
  user_subscriptions
  subscription_payments

GROUP 4 — Auth (auth server owns, same DB)
  auth_tokens
  auth_otp
  auth_permissions

GROUP 5 — Paper Trading (migrated from engine)
  paper_capital
  paper_portfolio
  paper_trade_log

GROUP 6 — Operational Logs
  signal_deliveries
  auto_execute_log
  credential_verify_log
  admin_audit_log
```

The trading service READ-ONLY accesses two engine-owned tables:

```
share_sectors      — symbol validation only (lookup_symbols function)
atrad_market_watch — execution_monitor reads LTP for GIS computation
```

The trading service NEVER WRITES to any engine-owned table directly.
All feedback to the engine goes through the REST webhook.

Engine-owned tables the trading service must NOT touch:
```
market_log, portfolio, trade_journal, learning_hub, gate_misses,
indicators, settings, financials, and all others
```

paper_users is RETIRED — replaced by users + user_channels.
Kept alive only during migration, then dropped.

---

## 2. GROUP 1 — Identity

### users

Root identity. One row per real person. Everything else references this.

```sql
CREATE TABLE users (
    id              SERIAL PRIMARY KEY,
    full_name       TEXT NOT NULL,
    email           TEXT,
    phone           TEXT,
    kyc_name        TEXT,           -- name as it appears in CDSC/broker KYC (may differ)
    status          TEXT NOT NULL DEFAULT 'PENDING',
                                    -- PENDING | APPROVED | BLOCKED
    role            TEXT NOT NULL DEFAULT 'USER',
                                    -- USER | ADMIN
    notes           TEXT,           -- admin notes
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    approved_at     TIMESTAMPTZ,
    approved_by     INTEGER,        -- FK → users.id (self-ref, nullable)
    last_active_at  TIMESTAMPTZ
);

CREATE INDEX ix_users_status ON users (status);
CREATE INDEX ix_users_role   ON users (role);
CREATE INDEX ix_users_email  ON users (email);
```

Key rules:
- `users.id` is the canonical join key across all other tables
- `status='APPROVED'` is required before any /buy or /sell is accepted
- `role='ADMIN'` gates /approve, /deny, /reset, /users, /claude, /play
- `approved_by` is nullable — first admin row has no approver

### user_channels

How each user connects. One user can have Telegram + Discord + web all linked.
Every bot command resolves `platform_user_id` to `users.id` via this table.
Replaces `telegram_id` and `discord_id` columns on paper_users.

```sql
CREATE TABLE user_channels (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER NOT NULL,      -- FK → users.id
    platform         TEXT NOT NULL,
                                            -- telegram | discord | web
    platform_user_id TEXT NOT NULL,         -- Telegram chat ID, Discord snowflake, web username
    username         TEXT,                  -- @handle (platform-specific display name)
    is_primary       TEXT DEFAULT 'false',
    linked_at        TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT ux_user_channels UNIQUE (platform, platform_user_id)
);

CREATE INDEX ix_user_channels_user_id ON user_channels (user_id);
CREATE INDEX ix_user_channels_lookup  ON user_channels (platform, platform_user_id);
```

Resolution pattern (replaces resolve_telegram_id()):
```sql
-- Given a Discord snowflake, find user_id
SELECT user_id FROM user_channels
WHERE platform = 'discord' AND platform_user_id = %s;

-- Given a Telegram ID, find user_id
SELECT user_id FROM user_channels
WHERE platform = 'telegram' AND platform_user_id = %s;
```

---

## 3. GROUP 2 — User Services (one-to-many on user_id)

### user_meroshare

Meroshare / CDSC credentials per user. One user can have multiple DEMAT accounts
(joint accounts, family members). All fields map directly to what meroshare.py
uses in the actual CDSC API calls.

```sql
CREATE TABLE user_meroshare (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER NOT NULL,      -- FK → users.id
    label            TEXT,                  -- user-defined: 'My DEMAT', 'Joint Account'
    dp_id            TEXT NOT NULL,         -- MEROSHARE_DP_ID e.g. '13200'
    demat_number     TEXT NOT NULL,         -- MEROSHARE_DEMAT e.g. '1301180000232764'
    client_code      TEXT NOT NULL,         -- MEROSHARE_CLIENT_CODE e.g. '11800'
    username         TEXT NOT NULL,         -- MEROSHARE_USERNAME
    password_enc     TEXT NOT NULL,         -- MEROSHARE_PASSWORD — Fernet encrypted
    is_active        TEXT DEFAULT 'true',
    is_primary       TEXT DEFAULT 'false',
    last_verified_at TIMESTAMPTZ,           -- last successful Meroshare API login
    last_synced_at   TIMESTAMPTZ,           -- last successful portfolio sync
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_user_meroshare_user_id ON user_meroshare (user_id);
```

Encryption: `cryptography.fernet.Fernet`. Key stored in `.env` as
`CREDENTIAL_ENCRYPTION_KEY`. Generated once, never goes in the DB.
Lost key = all credentials need re-entry.

### user_broker

ATrade / TMS broker credentials per user. One user can have accounts at multiple
brokers. All fields map directly to what atrad_scraper.py uses.

```sql
CREATE TABLE user_broker (
    id                   SERIAL PRIMARY KEY,
    user_id              INTEGER NOT NULL,  -- FK → users.id
    label                TEXT,              -- 'Roadshow - Order', 'Merolagani'
    broker_name          TEXT NOT NULL,     -- 'Roadshow Securities'
    broker_code          TEXT NOT NULL,     -- e.g. 'BS92009'
    tms_base_url         TEXT NOT NULL,     -- e.g. 'https://tms.roadshowsecurities.com.np/atsweb'
    username             TEXT NOT NULL,     -- TMS_ROADSHOW_USER
    password_enc         TEXT NOT NULL,     -- TMS_ROADSHOW_PASS — Fernet encrypted
    acnt_id              TEXT,              -- TMS_ROADSHOW_ACNT_ID (for order placement)
    watch_id             TEXT,              -- TMS_ROADSHOW_WATCH_ID (market watch ID)
    client_code          TEXT,              -- broker client account number
    account_type         TEXT DEFAULT 'both',
                                           -- order | data | both
                                           -- 'order': place_order/place_sell_order only
                                           -- 'data': market watch and quotes only
                                           -- 'both': single account does everything
    is_active            TEXT DEFAULT 'true',
    is_primary           TEXT DEFAULT 'false',
    auto_execute_enabled TEXT DEFAULT 'false',
                                           -- system auto-places live orders on this account
    max_auto_buy_npr     TEXT DEFAULT '0', -- per-signal cap; 0 = disabled
    last_verified_at     TIMESTAMPTZ,
    notes                TEXT,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_user_broker_user_id     ON user_broker (user_id);
CREATE INDEX ix_user_broker_account_type ON user_broker (account_type);
```

Note on account_type: atrad_scraper.py splits one broker into ORDER and DATA
sessions to prevent market watch polling from sharing a session with live orders.
A user wanting this split adds two rows with the same broker_code: one 'order',
one 'data'. A single 'both' row gets legacy single-session behavior.

### user_broker_links

Custom action links per broker account. Roadshow links, rights offering links,
kitta transfer links. Surfaced in the dashboard and bot on signal push.

```sql
CREATE TABLE user_broker_links (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL,    -- FK → users.id
    broker_id  INTEGER NOT NULL,    -- FK → user_broker.id
    link_type  TEXT NOT NULL,
               -- ROADSHOW | RIGHTS | KITTA_TRANSFER | ALLOTMENT_CHECK | OTHER
    label      TEXT NOT NULL,       -- 'Roadshow - GBIME', 'Rights - NLG'
    url        TEXT NOT NULL,
    is_active  TEXT DEFAULT 'true',
    notes      TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_user_broker_links_user_id   ON user_broker_links (user_id);
CREATE INDEX ix_user_broker_links_broker_id ON user_broker_links (broker_id);
```

### user_signal_preferences

Signal routing and auto-execute config. One row per user (1:1).

```sql
CREATE TABLE user_signal_preferences (
    id                      SERIAL PRIMARY KEY,
    user_id                 INTEGER NOT NULL UNIQUE,  -- FK → users.id
    notify_telegram         TEXT DEFAULT 'true',
    notify_discord          TEXT DEFAULT 'false',
    channel_priority        TEXT DEFAULT 'telegram',
                            -- telegram | discord
                            -- primary channel; falls back to the other if both enabled
    auto_execute            TEXT DEFAULT 'false',
    auto_execute_broker_id  INTEGER,
                            -- FK → user_broker.id (nullable = auto-execute off)
    signal_filter           TEXT DEFAULT 'ALL',
                            -- ALL | BUY_ONLY | WAIT_ONLY
    min_score_threshold     TEXT DEFAULT '0',
                            -- skip push if composite_score below this
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_user_signal_prefs_user_id ON user_signal_preferences (user_id);
```

---

## 4. GROUP 3 — Subscriptions

### subscription_plans

Admin-managed tier definitions. Changing limits here propagates everywhere
without a code change. Never deleted — set is_active='false' to retire.

```sql
CREATE TABLE subscription_plans (
    id                      SERIAL PRIMARY KEY,
    name                    TEXT NOT NULL UNIQUE,
                            -- FREE | BASIC | PRO
    display_name            TEXT NOT NULL,
    price_npr_monthly       TEXT NOT NULL DEFAULT '0',
    price_npr_yearly        TEXT NOT NULL DEFAULT '0',
                            -- yearly price (typically 2 months free)
    max_broker_accounts     INTEGER NOT NULL DEFAULT 1,
    max_meroshare_accounts  INTEGER NOT NULL DEFAULT 1,
    max_paper_positions     INTEGER NOT NULL DEFAULT 3,
    signal_push_enabled     TEXT NOT NULL DEFAULT 'false',
                            -- BUY alert push to all channels
    briefing_enabled        TEXT NOT NULL DEFAULT 'false',
                            -- morning briefing per user
    auto_execute_enabled    TEXT NOT NULL DEFAULT 'false',
                            -- live broker auto-execute
    ai_analysis_enabled     TEXT NOT NULL DEFAULT 'false',
                            -- /claude command
    full_analytics_enabled  TEXT NOT NULL DEFAULT 'false',
                            -- full PnL history and analytics dashboard
    is_active               TEXT NOT NULL DEFAULT 'true',
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Seed rows (run once at setup)
INSERT INTO subscription_plans (
    name, display_name, price_npr_monthly, price_npr_yearly,
    max_broker_accounts, max_meroshare_accounts, max_paper_positions,
    signal_push_enabled, briefing_enabled, auto_execute_enabled,
    ai_analysis_enabled, full_analytics_enabled
) VALUES
    ('FREE',  'Free',  '0',    '0',
     1, 1,  3,  'false', 'false', 'false', 'false', 'false'),
    ('BASIC', 'Basic', '500',  '5000',
     2, 2, 10,  'true',  'true',  'false', 'false', 'false'),
    ('PRO',   'Pro',   '1500', '15000',
     5, 5, 15,  'true',  'true',  'true',  'true',  'true');
```

Feature gate summary:

| Feature                  | FREE | BASIC | PRO |
|--------------------------|------|-------|-----|
| /status /capital /pnl   | Yes  | Yes   | Yes |
| /signal (view)           | Yes  | Yes   | Yes |
| Paper /buy /sell         | Yes  | Yes   | Yes |
| Max paper positions      | 3    | 10    | 15  |
| Max broker accounts      | 1    | 2     | 5   |
| Max Meroshare accounts   | 1    | 2     | 5   |
| BUY signal push alerts   | No   | Yes   | Yes |
| Morning briefing         | No   | Yes   | Yes |
| Auto-execute (live)      | No   | No    | Yes |
| /claude AI analysis      | No   | No    | Yes |
| Full analytics           | No   | No    | Yes |

### user_subscriptions

Active subscription per user. One current row per user; historical rows kept
for audit. Only one row per user should have status='ACTIVE' at any time.

```sql
CREATE TABLE user_subscriptions (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL,       -- FK → users.id
    plan_id         INTEGER NOT NULL,       -- FK → subscription_plans.id
    billing_cycle   TEXT NOT NULL,          -- monthly | yearly
    status          TEXT NOT NULL DEFAULT 'ACTIVE',
                                            -- ACTIVE | EXPIRED | CANCELLED | GRACE
                                            -- GRACE: payment overdue, 7-day grace period
    started_at      TIMESTAMPTZ NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,   -- next renewal date
    cancelled_at    TIMESTAMPTZ,
    cancel_reason   TEXT,
    auto_renew      TEXT DEFAULT 'true',
    grace_until     TIMESTAMPTZ,            -- set when payment fails; access continues until this
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_user_subscriptions_user_id    ON user_subscriptions (user_id);
CREATE INDEX ix_user_subscriptions_status     ON user_subscriptions (status);
CREATE INDEX ix_user_subscriptions_expires_at ON user_subscriptions (expires_at);
```

Status transitions:
```
ACTIVE  → GRACE     (payment due, not received within 24h of expires_at)
GRACE   → EXPIRED   (grace_until passed with no payment)
GRACE   → ACTIVE    (payment received during grace period)
ACTIVE  → CANCELLED (user cancels; access continues until expires_at)
CANCELLED → EXPIRED (expires_at passed)
EXPIRED → ACTIVE    (user resubscribes — new row inserted, old stays for history)
```

Access rule: a user has full plan access if their subscription status is ACTIVE
or GRACE. EXPIRED and CANCELLED (past expires_at) fall back to FREE tier limits.
FREE users always have a synthetic FREE subscription row created on approval.

### subscription_payments

Payment record per billing event. Append-only.

```sql
CREATE TABLE subscription_payments (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL,   -- FK → users.id
    subscription_id     INTEGER NOT NULL,   -- FK → user_subscriptions.id
    plan_id             INTEGER NOT NULL,   -- FK → subscription_plans.id
    amount_npr          TEXT NOT NULL,
    billing_cycle       TEXT NOT NULL,      -- monthly | yearly
    payment_method      TEXT,               -- esewa | khalti | bank_transfer | manual
    payment_reference   TEXT,              -- transaction ID from payment gateway
    status              TEXT NOT NULL,      -- PENDING | COMPLETED | FAILED | REFUNDED
    paid_at             TIMESTAMPTZ,
    period_start        TIMESTAMPTZ,        -- what period this payment covers
    period_end          TIMESTAMPTZ,
    notes               TEXT,               -- admin notes e.g. 'manual override'
    recorded_by         INTEGER,            -- FK → users.id (admin if manual)
    inserted_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_subscription_payments_user_id         ON subscription_payments (user_id);
CREATE INDEX ix_subscription_payments_subscription_id ON subscription_payments (subscription_id);
CREATE INDEX ix_subscription_payments_status          ON subscription_payments (status);
```

---

## 5. GROUP 4 — Auth (auth server owns, same DB)

Auth server is the only writer to these tables. No other service writes here.

### auth_tokens

```sql
CREATE TABLE auth_tokens (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL,       -- FK → users.id
    token_hash   TEXT NOT NULL UNIQUE,   -- SHA-256 of raw token (never store raw)
    token_type   TEXT NOT NULL,          -- access | refresh | service
    scope        TEXT NOT NULL,          -- user | admin | service
    issued_to    TEXT,                   -- web | telegram_bot | discord_bot | engine
    issued_at    TIMESTAMPTZ DEFAULT NOW(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked      TEXT DEFAULT 'false',
    revoked_at   TIMESTAMPTZ,
    ip_address   TEXT
);

CREATE INDEX ix_auth_tokens_user_id    ON auth_tokens (user_id);
CREATE INDEX ix_auth_tokens_hash       ON auth_tokens (token_hash);
CREATE INDEX ix_auth_tokens_expires_at ON auth_tokens (expires_at);
```

Token lifetimes:
- access: 15 minutes
- refresh: 30 days, rotated on each use
- service: 365 days (engine-to-trading and trading-to-engine calls)

### auth_otp

```sql
CREATE TABLE auth_otp (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL,       -- FK → users.id
    otp_hash    TEXT NOT NULL,          -- bcrypt hash of 6-digit code
    channel     TEXT NOT NULL,          -- telegram | discord
    purpose     TEXT NOT NULL,          -- login | credential_add | admin_action
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,   -- 10 minutes
    used        TEXT DEFAULT 'false',
    used_at     TIMESTAMPTZ,
    attempts    INTEGER DEFAULT 0       -- max 3; row locked after 3 failures
);

CREATE INDEX ix_auth_otp_user_id ON auth_otp (user_id);
```

### auth_permissions

Granular permissions beyond role. Lets admin grant a specific feature to a
specific user without a full plan upgrade.

```sql
CREATE TABLE auth_permissions (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL,   -- FK → users.id
    permission  TEXT NOT NULL,
                -- signals:read | trades:write | admin:users
                -- | credentials:write | auto_execute:enable | ai_analysis:enable
    granted_by  INTEGER NOT NULL,   -- FK → users.id (admin who granted)
    granted_at  TIMESTAMPTZ DEFAULT NOW(),
    expires_at  TIMESTAMPTZ,        -- NULL = permanent
    revoked     TEXT DEFAULT 'false'
);

CREATE INDEX ix_auth_permissions_user_id    ON auth_permissions (user_id);
CREATE INDEX ix_auth_permissions_permission ON auth_permissions (permission);
```

---

## 6. GROUP 5 — Paper Trading (migrated from engine)

These four tables keep all existing columns unchanged. The only structural
change is `user_id INTEGER` added as the new join key, replacing `telegram_id`
as the primary reference. `telegram_id` stays as a legacy column during
migration and is dropped after all bot code is migrated.

### paper_capital

```sql
CREATE TABLE paper_capital (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER,            -- FK → users.id (added, backfilled)
    telegram_id         TEXT NOT NULL,      -- legacy, kept during migration
    starting_capital    TEXT DEFAULT '100000',
    current_capital     TEXT DEFAULT '100000',
    total_realised_pnl  TEXT DEFAULT '0',
    total_fees_paid     TEXT DEFAULT '0',
    total_cgt_paid      TEXT DEFAULT '0',
    total_trades        TEXT DEFAULT '0',
    total_wins          TEXT DEFAULT '0',
    total_losses        TEXT DEFAULT '0',
    last_updated        TEXT,
    test_mode           TEXT DEFAULT 'false',
    inserted_at         TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT ux_paper_capital_telegram_id UNIQUE (telegram_id)
);

CREATE INDEX ix_paper_capital_user_id     ON paper_capital (user_id);
CREATE INDEX ix_paper_capital_telegram_id ON paper_capital (telegram_id);
```

Key rules:
- All monetary values stored as TEXT (project-wide convention — no NUMERIC)
- Use `Decimal(str(row["current_capital"]))` in Python, never float()
- current_capital updated atomically inside execute_buy / execute_sell transactions
- total_wins + total_losses incremented on SELL, not on BUY
- test_mode='true' rows written in SANDBOX_MODE and purged before official trading
- /reset deletes this row and reinserts with starting_capital='100000'
- Subscription plan's max_paper_positions checked at /buy time via user_subscriptions

### paper_portfolio

Open and closed positions. One row per (user_id, symbol) pair while OPEN.
Multiple CLOSED rows can exist for the same symbol (one per trade cycle).

```sql
CREATE TABLE paper_portfolio (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER,                -- FK → users.id (added, backfilled)
    telegram_id     TEXT NOT NULL,          -- legacy, kept during migration
    symbol          TEXT NOT NULL,
    status          TEXT DEFAULT 'OPEN',    -- OPEN | CLOSED
    total_shares    TEXT,
    wacc            TEXT,                   -- weighted average cost (Decimal, 4 dp)
    total_cost      TEXT,                   -- total capital deployed including fees
    first_buy_date  TEXT,                   -- YYYY-MM-DD, used for CGT hold period
    last_buy_date   TEXT,
    buy_count       TEXT DEFAULT '1',       -- number of averaging buys
    exit_date       TEXT,
    exit_price      TEXT,
    exit_shares     TEXT,
    gross_pnl       TEXT,
    sell_fees       TEXT,
    cgt_paid        TEXT,
    net_pnl         TEXT,
    result          TEXT,                   -- WIN | LOSS | BREAKEVEN
    created_at      TEXT,
    updated_at      TEXT,
    test_mode       TEXT DEFAULT 'false',
    audited         TEXT DEFAULT 'false',   -- 'true' after auditor processes this row
    peak_price      TEXT,                   -- for trailing stop (execution_monitor)
    trail_active    TEXT DEFAULT 'false',   -- 'true' when trailing stop is armed
    inserted_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_paper_portfolio_user_id        ON paper_portfolio (user_id);
CREATE INDEX ix_paper_portfolio_user_id_status ON paper_portfolio (user_id, status);
CREATE INDEX ix_paper_portfolio_telegram_id    ON paper_portfolio (telegram_id);
CREATE INDEX ix_paper_portfolio_symbol         ON paper_portfolio (symbol);
CREATE INDEX ix_paper_portfolio_test_mode      ON paper_portfolio (test_mode);
```

Key rules:
- OPEN rows: one per (user_id, symbol) — enforced by logic, not DB constraint
- Averaging: UPDATE existing OPEN row (recompute WACC, add shares and cost)
- Partial sell: CLOSE existing row, create new OPEN row for remaining shares
- peak_price and trail_active updated every 30s by execution_monitor
- audited='false' rows are the queue for the engine's auditor (shared Postgres read)
- Never use upsert — WACC averaging requires explicit OPEN row detection

### paper_trade_log

Immutable event log. Append-only. One row per BUY or SELL event.

```sql
CREATE TABLE paper_trade_log (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER,                -- FK → users.id (added, backfilled)
    telegram_id     TEXT NOT NULL,          -- legacy, kept during migration
    portfolio_id    INTEGER,                -- FK → paper_portfolio.id (nullable for old rows)
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,          -- BUY | SELL
    shares          TEXT,
    price           TEXT,
    gross_amount    TEXT,
    brokerage       TEXT,
    sebon           TEXT,
    dp_fee          TEXT,
    cgt             TEXT,
    total_fees      TEXT,
    net_amount      TEXT,
    capital_before  TEXT,
    capital_after   TEXT,
    wacc_before     TEXT,
    wacc_after      TEXT,
    note            TEXT,
    created_at      TEXT,
    test_mode       TEXT DEFAULT 'false',
    close_type      TEXT,                   -- MANUAL | HARD_STOP | TRAIL_STOP | PARTIAL
    inserted_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_paper_trade_log_user_id      ON paper_trade_log (user_id);
CREATE INDEX ix_paper_trade_log_telegram_id  ON paper_trade_log (telegram_id);
CREATE INDEX ix_paper_trade_log_portfolio_id ON paper_trade_log (portfolio_id);
CREATE INDEX ix_paper_trade_log_symbol       ON paper_trade_log (symbol);
CREATE INDEX ix_paper_trade_log_test_mode    ON paper_trade_log (test_mode);
```

Key rules:
- Append-only: use write_row(), never upsert_row() or update_row() on this table
- Written atomically inside execute_buy / execute_sell alongside the portfolio update
- close_type distinguishes manual sells from execution_monitor auto-closes
- System account rows (telegram_id='SYSTEM_BOT') are the learning hub training source

---

## 7. GROUP 6 — Operational Logs

### signal_deliveries

Delivery record per user per signal. Dedup constraint prevents double-push.

```sql
CREATE TABLE signal_deliveries (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL,      -- FK → users.id
    signal_date  TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    channel      TEXT NOT NULL,         -- telegram | discord
    status       TEXT NOT NULL,         -- SENT | FAILED | SKIPPED
    sent_at      TIMESTAMPTZ,
    error_msg    TEXT,
    inserted_at  TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT ux_signal_deliveries UNIQUE (signal_date, symbol, user_id, channel)
);

CREATE INDEX ix_signal_deliveries_user_id     ON signal_deliveries (user_id);
CREATE INDEX ix_signal_deliveries_signal_date ON signal_deliveries (signal_date);
```

Dedup pattern before every send:
```sql
INSERT INTO signal_deliveries (signal_date, symbol, user_id, channel, status, sent_at)
VALUES (%s, %s, %s, %s, 'SENT', NOW())
ON CONFLICT (signal_date, symbol, user_id, channel) DO NOTHING;
-- 0 rows affected = already sent, skip
```

Only users with signal_push_enabled='true' on their active subscription plan
receive push alerts. FREE tier users do NOT receive push alerts.

### auto_execute_log

Real broker order record per signal per user. Separate from paper_trade_log.

```sql
CREATE TABLE auto_execute_log (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL,   -- FK → users.id
    broker_id       INTEGER NOT NULL,   -- FK → user_broker.id
    signal_date     TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    shares          TEXT,
    price           TEXT,
    order_id        TEXT,               -- ATrad order ID returned
    status          TEXT NOT NULL,      -- SUBMITTED | FILLED | FAILED | SKIPPED
    error_msg       TEXT,
    broker_response TEXT,               -- raw JSON from ATrad
    submitted_at    TIMESTAMPTZ DEFAULT NOW(),
    filled_at       TIMESTAMPTZ
);

CREATE INDEX ix_auto_execute_log_user_id     ON auto_execute_log (user_id);
CREATE INDEX ix_auto_execute_log_symbol      ON auto_execute_log (symbol);
CREATE INDEX ix_auto_execute_log_signal_date ON auto_execute_log (signal_date);
```

Only users with auto_execute_enabled='true' on their active subscription plan
(PRO only) and auto_execute='true' on their user_signal_preferences are eligible.

### credential_verify_log

Health log per credential. Surfaces "your credentials stopped working" before
a signal fires and auto-execute silently fails.

```sql
CREATE TABLE credential_verify_log (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL,
    credential_type TEXT NOT NULL,      -- meroshare | broker
    credential_id   INTEGER NOT NULL,   -- FK → user_meroshare.id or user_broker.id
    status          TEXT NOT NULL,      -- OK | FAILED | EXPIRED
    error_msg       TEXT,
    verified_at     TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_cred_verify_user_id ON credential_verify_log (user_id);
CREATE INDEX ix_cred_verify_status  ON credential_verify_log (status);
```

### admin_audit_log

Immutable record of every admin action. Append-only.

```sql
CREATE TABLE admin_audit_log (
    id           SERIAL PRIMARY KEY,
    admin_id     INTEGER NOT NULL,      -- FK → users.id
    action       TEXT NOT NULL,
                 -- APPROVE | DENY | RESET | BLOCK | GRANT_PERMISSION
                 -- | REVOKE_PERMISSION | UPGRADE_PLAN | DOWNGRADE_PLAN
                 -- | MANUAL_PAYMENT | REFUND | VERIFY_CREDENTIALS
    target_id    INTEGER,               -- FK → users.id (affected user)
    detail       TEXT,                  -- reason, what changed
    performed_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ix_admin_audit_admin_id  ON admin_audit_log (admin_id);
CREATE INDEX ix_admin_audit_target_id ON admin_audit_log (target_id);
```

---

## 8. Read-only Tables (engine-owned, trading service reads)

### share_sectors (read-only)

Used by trading_core.lookup_symbols() to validate symbol input.

```sql
-- Exact match
SELECT symbol FROM share_sectors WHERE UPPER(symbol) = UPPER(%s);

-- Partial match fallback for typos
SELECT symbol FROM share_sectors WHERE symbol ILIKE %s LIMIT 8;
```

### atrad_market_watch (read-only)

Used by execution_monitor to get live LTP for GIS computation without
making an ATrad API call on every 30-second cycle.

```sql
SELECT ltp, vwap, bid_price, ask_price, bid_qty, ask_qty,
       low_dpr, high_dpr, dpr_proximity
FROM atrad_market_watch
WHERE symbol = %s
ORDER BY id DESC LIMIT 1;
```

---

## 9. Complete FK Map

```
users
  ├── users.approved_by                          → users.id  (self-ref, nullable)
  │
  ├── user_channels.user_id                      → users.id
  │
  ├── user_meroshare.user_id                     → users.id
  │
  ├── user_broker.user_id                        → users.id
  ├── user_broker_links.user_id                  → users.id
  ├── user_broker_links.broker_id                → user_broker.id
  │
  ├── user_signal_preferences.user_id            → users.id  (UNIQUE, 1:1)
  ├── user_signal_preferences
  │     .auto_execute_broker_id                  → user_broker.id  (nullable)
  │
  ├── user_subscriptions.user_id                 → users.id
  ├── user_subscriptions.plan_id                 → subscription_plans.id
  ├── subscription_payments.user_id              → users.id
  ├── subscription_payments.subscription_id      → user_subscriptions.id
  ├── subscription_payments.plan_id              → subscription_plans.id
  ├── subscription_payments.recorded_by          → users.id  (nullable)
  │
  ├── auth_tokens.user_id                        → users.id
  ├── auth_otp.user_id                           → users.id
  ├── auth_permissions.user_id                   → users.id
  ├── auth_permissions.granted_by                → users.id
  │
  ├── paper_capital.user_id                      → users.id
  ├── paper_portfolio.user_id                    → users.id
  ├── paper_trade_log.user_id                    → users.id
  ├── paper_trade_log.portfolio_id               → paper_portfolio.id  (nullable)
  │
  ├── signal_deliveries.user_id                  → users.id
  ├── auto_execute_log.user_id                   → users.id
  ├── auto_execute_log.broker_id                 → user_broker.id
  ├── credential_verify_log.user_id              → users.id
  └── admin_audit_log.admin_id / target_id       → users.id
```

No FK constraints enforced at the DB level (project convention).
All FK integrity enforced in application code (trading_core.py).

---

## 10. System Account

The system account auto-executes every engine BUY. Its paper_trade_log rows
are the sole learning hub training source. Excluded from user-facing stats.

```sql
-- Insert into users
INSERT INTO users (full_name, status, role, approved_at)
VALUES ('System Auto-Trader', 'APPROVED', 'USER', NOW());

-- Insert into user_channels (canonical telegram_id for compatibility)
INSERT INTO user_channels (user_id, platform, platform_user_id, username, is_primary)
VALUES (<system_user_id>, 'telegram', 'SYSTEM_BOT', 'system', 'true');

-- Insert FREE plan subscription (system account never needs paid features)
INSERT INTO user_subscriptions (user_id, plan_id, billing_cycle, status, started_at, expires_at)
VALUES (<system_user_id>, <free_plan_id>, 'monthly', 'ACTIVE', NOW(), '2099-12-31');
```

---

## 11. Subscription Access Check Pattern

Every feature-gated operation checks the active plan at runtime:

```python
def get_active_plan(user_id: int) -> dict:
    """Returns the subscription_plans row for the user's active subscription.
    Falls back to FREE plan if no ACTIVE/GRACE subscription exists."""
    rows = run_raw_sql("""
        SELECT sp.*
        FROM user_subscriptions us
        JOIN subscription_plans sp ON sp.id = us.plan_id
        WHERE us.user_id = %s
          AND us.status IN ('ACTIVE', 'GRACE')
        ORDER BY us.started_at DESC
        LIMIT 1
    """, (user_id,))
    if rows:
        return rows[0]
    # Fallback: return FREE plan limits directly
    rows = run_raw_sql(
        "SELECT * FROM subscription_plans WHERE name = 'FREE' LIMIT 1"
    )
    return rows[0] if rows else {}

# Usage in /buy gate
plan = get_active_plan(user_id)
open_count = count_open_positions(user_id)
max_positions = int(plan.get("max_paper_positions", 3))
if open_count >= max_positions:
    return f"Position limit reached ({open_count}/{max_positions}). Upgrade to open more."

# Usage in signal push gate
plan = get_active_plan(user_id)
if plan.get("signal_push_enabled") != "true":
    return  # skip — FREE tier does not receive push alerts
```

---

## 12. Migration Order

```
Phase 1 — Create new identity tables
  1. CREATE users
  2. CREATE user_channels
  3. Backfill users from paper_users (one row per paper_users row)
  4. Backfill user_channels from paper_users (platform='telegram')

Phase 2 — Create service and subscription tables
  5. CREATE user_meroshare
  6. CREATE user_broker
  7. CREATE user_broker_links
  8. CREATE user_signal_preferences
  9. CREATE subscription_plans + seed FREE/BASIC/PRO rows
  10. CREATE user_subscriptions
      Backfill: all existing APPROVED users get FREE plan subscription
  11. CREATE subscription_payments

Phase 3 — Create auth tables
  12. CREATE auth_tokens
  13. CREATE auth_otp
  14. CREATE auth_permissions

Phase 4 — Migrate paper tables
  15. ALTER paper_capital    ADD COLUMN user_id INTEGER
  16. ALTER paper_portfolio  ADD COLUMN user_id INTEGER
  17. ALTER paper_trade_log  ADD COLUMN user_id INTEGER
  18. ALTER paper_trade_log  ADD COLUMN portfolio_id INTEGER
  19. Backfill user_id on all three via user_channels join on telegram_id

Phase 5 — Create operational log tables
  20. CREATE signal_deliveries
  21. CREATE auto_execute_log
  22. CREATE credential_verify_log
  23. CREATE admin_audit_log

Phase 6 — Switch code and clean up
  24. Update all bot queries to use user_id; keep telegram_id as fallback
  25. Verify zero queries still depend on paper_users
  26. DROP paper_users
```

---

## 13. Monetary Value Convention

ALL monetary columns stored as TEXT, not NUMERIC or FLOAT.

```python
from decimal import Decimal, ROUND_HALF_UP

# Read
capital = Decimal(str(row["current_capital"]))

# Write
str(capital.quantize(Decimal("0.01"), ROUND_HALF_UP))

# Never
float(row["current_capital"])   # loses precision
int(row["current_capital"])     # truncates cents
```

---

## 14. Boolean Convention

All boolean columns stored as TEXT 'true' or 'false' (lowercase).

```python
is_active = str(row.get("is_active", "false")).lower() == "true"
str(True).lower()   # -> "true"
str(False).lower()  # -> "false"
```

---

## 15. Date and Time Convention

- All date columns: TEXT, format "YYYY-MM-DD"
- All datetime columns: TEXT, format "YYYY-MM-DD HH:MM:SS" (NST)
- `inserted_at` / `created_at` on system tables: TIMESTAMPTZ DEFAULT NOW() (UTC)

```python
from config import NST
from datetime import datetime

datetime.now(tz=NST).strftime("%Y-%m-%d %H:%M:%S")   # datetime columns
datetime.now(tz=NST).strftime("%Y-%m-%d")              # date columns
```

---

## 16. Schema Management

```bash
# After editing schema.prisma:
python -m db.codegen      # regenerates db/schema.py
python -m db.migrations   # applies CREATE TABLE / ALTER TABLE ADD COLUMN

# Never write manual ALTER TABLE — always go through codegen + migrations
```

---

## 17. Test Data Purge

All sandbox writes tagged test_mode='true'. Run before official trading begins:

```sql
DELETE FROM paper_trade_log WHERE test_mode = 'true';
DELETE FROM paper_portfolio  WHERE test_mode = 'true';
UPDATE paper_capital SET
    current_capital    = '100000',
    total_realised_pnl = '0',
    total_fees_paid    = '0',
    total_cgt_paid     = '0',
    total_trades       = '0',
    total_wins         = '0',
    total_losses       = '0',
    test_mode          = 'false'
WHERE test_mode = 'true';
```
