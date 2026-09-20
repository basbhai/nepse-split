# THREAT_MODEL.md — nepse-trading
## Security Rules, Auth Flows, and Sensitive Data Protection

---

## 1. Assets to Protect

| Asset | Sensitivity | Where it lives |
|---|---|---|
| User paper capital and trades | High — financial simulation data | paper_capital, paper_portfolio, paper_trade_log |
| Telegram user identities | High — PII (telegram_id, username, full_name) | paper_users |
| Discord user identities | High — PII (discord_id) | paper_users |
| Bot tokens (TELEGRAM_BOT_TOKEN, DISCORD_BOT_TOKEN) | Critical — full bot control | .env only |
| ENGINE_API_SECRET | Critical — cross-service impersonation if leaked | .env only |
| DASHBOARD_AUTH_SECRET | High — dashboard public access | .env only |
| DATABASE_URL | Critical — full DB read/write access | .env only |
| Admin telegram_id (TELEGRAM_CHAT_ID) | High — admin command escalation | .env only |
| ATrad order session (TMS_ROADSHOW_USER/PASS) | Critical — live order placement capability | .env only, engine side |

---

## 2. Trust Boundaries

```
[Internet / Tailscale Funnel]
         |
         | HTTPS (Tailscale TLS)
         v
[trading_api.py port 8767]  <-- X-Service-Key for engine calls
[register_api.py port 8768] <-- public (by design, no auth on /register)
         |
         | local Unix socket / loopback
         v
[PostgreSQL localhost:5432]  <-- no network exposure, local only

[telegram_bot.py]  <-- Bot token auth (Telegram servers handle this)
[discord_bot.py]   <-- Bot token auth (Discord servers handle this)

[nepse-decisionEngine port 8766]  <-- same machine, loopback only
         |
         | X-Service-Key (loopback)
         v
[trading service REST endpoints]
```

Threats from outside the machine arrive only via:
1. Tailscale Funnel (HTTPS, public hostname) -> trading_api
2. Telegram servers -> telegram_bot (via Bot API polling)
3. Discord servers -> discord_bot (via gateway websocket)

The Postgres instance and engine<->trading REST calls never leave the machine.

---

## 3. Authentication Flows

### 3.1 Telegram Bot Auth

Telegram handles token validation at the API gateway level.
All updates received by python-telegram-bot are already verified as
originating from Telegram's servers.

Admin command escalation guard:
```python
# In telegram_bot.py — all admin commands check this
ADMIN_TID = int(os.getenv("TELEGRAM_CHAT_ID"))

def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_TID

# Applied to: /approve, /deny, /users, /reset, /claude, /play
async def cmd_approve(update, context):
    if not is_admin(update):
        await update.message.reply_text("Admin only.")
        return
```

User auth check for all trading commands:
```python
# trading_core.is_approved() called before every /buy, /sell, /status, etc.
def is_approved(telegram_id: int) -> bool:
    user = get_user(telegram_id)
    return user is not None and user.get("status") == "APPROVED"
```

### 3.2 Discord Bot Auth

Discord handles token validation at the gateway level.
Slash commands are verified by Discord before delivery.

Admin command escalation guard:
```python
DISCORD_ADMIN_ID = int(os.getenv("DISCORD_ADMIN_ID"))

def is_discord_admin(interaction: discord.Interaction) -> bool:
    return interaction.user.id == DISCORD_ADMIN_ID

# Applied to: /claude, /play, and admin management commands
```

User auth for Discord commands:
```python
# All trading commands resolve discord_id to telegram_id first
tid = resolve_telegram_id(str(interaction.user.id), "DISCORD")
if tid is None:
    await interaction.followup.send("Not registered. Visit /register to sign up.")
    return
if not is_approved(int(tid)):
    await interaction.followup.send("Access pending approval.")
    return
```

### 3.3 Service-to-Service Auth (engine <-> trading)

All cross-service REST calls use a shared bearer secret.
Verified via constant-time comparison (prevents timing attacks).

```python
ENGINE_API_SECRET = os.getenv("ENGINE_API_SECRET", "")

# In trading_api.py middleware
async def verify_service_key(request: Request, call_next):
    if request.url.path in ("/health",):
        return await call_next(request)
    key = request.headers.get("X-Service-Key", "")
    if not ENGINE_API_SECRET or not hmac.compare_digest(key, ENGINE_API_SECRET):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)
```

The /register endpoint is exempt from service-key auth (public by design).
It accepts any submission and creates a PENDING row — no capability until
an admin explicitly approves.

### 3.4 Dashboard Registration Auth

The /register endpoint is intentionally public. It creates a PENDING row only.
No capability is granted until admin approves via /approve command.

Rate limiting: not yet implemented. Risk is low because:
- PENDING rows have no capability
- Spam registrations are visible to admin via /users
- Admin must explicitly approve each one

Future hardening: add per-IP rate limit (FastAPI slowapi) if spam becomes a problem.

---

## 4. Data Isolation

### 4.1 User-to-user isolation

Every DB query in trading_core.py is scoped by telegram_id.
There is no query that returns data across multiple users except:
- `get_all_approved_ids()` — returns telegram_id list only, no financial data
- `_get_approved_users()` in briefing — returns username/telegram_id/discord_id only

The full isolation invariant:
```python
# EVERY financial data query must include this predicate
WHERE telegram_id = %s   # and %s is always the calling user's telegram_id
```

This is enforced by convention in trading_core.py. There is no DB-level
row security (Postgres RLS) — the application is the only enforcement layer.

Violation risk: a bug in trading_core.py could expose cross-user data.
Mitigation: all user data functions in trading_core.py take telegram_id as
their first argument and pass it directly into every SQL query they issue.

### 4.2 System account isolation

The system account (telegram_id='SYSTEM_BOT') is never returned in user-facing
queries. All user listing functions filter:
```sql
WHERE status = 'APPROVED' AND telegram_id != 'SYSTEM_BOT'
```

---

## 5. Sensitive Environment Variables

All secrets live in `.env`. Never committed to git (`.env` in `.gitignore`).
Never logged (log statements must not include these values).

| Variable | Used by | Risk if leaked |
|---|---|---|
| TELEGRAM_BOT_TOKEN | telegram_bot.py | Full bot control, can send as the bot |
| DISCORD_BOT_TOKEN | discord_bot.py | Full bot control |
| ENGINE_API_SECRET | trading_api.py, engine_client.py | Cross-service impersonation |
| DATABASE_URL | db/connection.py | Full DB read/write |
| DASHBOARD_AUTH_SECRET | trading_api.py | Dashboard public access |
| TELEGRAM_CHAT_ID | trading_core.py | Admin command escalation |
| DISCORD_ADMIN_ID | discord_bot.py | Admin command escalation |

Rotation procedure: change in .env, restart affected service.
No token invalidation is required for ENGINE_API_SECRET (it is a shared
symmetric secret, not an issued token).

---

## 6. /claude and /play Command Security

These admin-only commands run Claude Code and Playwright respectively on the
server. They are the highest-privilege operations in the system.

Guardrails:
- `is_admin()` / `is_discord_admin()` checked before any prompt is processed
- `CLAUDE_BIN` runs headless with `--allowedTools` whitelist (explicit tool grant)
- `GUARD_SETTINGS_PATH` points to `claude_bot_guard_settings.json` which wires
  in `modules/claude_bot_guard_hook.py` as a pre-tool hook
- The guard hook blocks shell redirects that write `.env` or `.py` files
  outside `CLAUDE_AGENT_DIR` (belt-and-suspenders, not airtight)
- `/play` is more restricted than `/claude`: no Bash, no Write/Edit tools
- Responses are DM'd to admin, not posted in public channels

Known limitations:
- The guard hook is regex-based and can be bypassed by creative prompts
- Treat any `/claude` invocation as equivalent to giving admin a shell on the box
- Never grant `/claude` or `/play` access to non-admin users under any circumstances

---

## 7. SQL Injection Prevention

All DB queries use parameterized queries via psycopg2's `%s` parameter style.
No string interpolation in SQL. This is enforced in sheets.py and _db() usage.

```python
# CORRECT — always
cur.execute("SELECT * FROM paper_users WHERE telegram_id = %s", (str(telegram_id),))

# NEVER — direct string interpolation
cur.execute(f"SELECT * FROM paper_users WHERE telegram_id = '{telegram_id}'")
```

User-supplied symbol input is sanitized via `lookup_symbols()` before any
SQL query. The function does an ILIKE match against the share_sectors table
and returns only matched symbols — never passes raw user input into a trade.

---

## 8. Fail-Safe Rules

These rules must be maintained after the split. A violation in any one of
them can cause silent data corruption or capital miscounting.

| Rule | Failure mode if broken |
|---|---|
| execute_buy and execute_sell are atomic (_db() transaction) | Capital deducted but position not written, or vice versa |
| get_capital_status() must raise on DB error, not return fake capital | Engine or bot thinks capital is free when DB is down |
| paper_trade_log is append-only (write_row only, never update_row) | Audit trail corrupted, PnL reconstruction fails |
| market_log is engine-owned — trading service never writes to it | Learning hub receives corrupted data |
| resolve_telegram_id() returns None on any failure — never raises | Bot crashes mid-command, state machine breaks |
| User data queries always include telegram_id predicate | Cross-user data exposure |
| Admin identity check uses constant-time comparison (hmac.compare_digest) | Timing attack escalation to admin |
| SANDBOX_MODE tags all writes test_mode='true' | Test data bleeds into production stats |

---

## 9. Known Risks (Accepted for Now)

| Risk | Acceptance reason | Future mitigation |
|---|---|---|
| No Postgres RLS — application is only isolation layer | Small trusted user base, single-developer project | Add RLS policies when user base grows |
| /register has no rate limit | PENDING rows have no capability; spam is harmless until approved | Add per-IP rate limit if spam occurs |
| Shared DATABASE_URL between engine and trading service | Both run on the same trusted machine | Separate DB users with table-level grants in future |
| Guard hook for /claude is regex-based, not airtight | Admin-only command, trusted operator | Acceptable for current scale |
| No audit log of admin actions (/approve, /deny, /reset) | Single-admin system | Add admin_audit_log table when team grows |
| No token rotation policy for ENGINE_API_SECRET | Both services on same machine, low exposure surface | Add rotation procedure if services move to separate hosts |
