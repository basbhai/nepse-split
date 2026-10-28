"""
register_api.py — nepse-trading
User-facing web UI: registration, user login, credentials, admin management.
Port 8768.

Auth model:
  - /register        — public (anyone can submit)
  - /user-login      — public form; issues nepse_user_session cookie on success
  - /credentials     — requires nepse_user_session (approved user)
  - /admin           — requires valid admin nepse_session (validated via engine)
"""
import os
import time
import secrets
import hashlib
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from pydantic import BaseModel

from db.connection import _db
from sheets import run_raw_sql
from db.credentials import encrypt_password

log = logging.getLogger(__name__)

ENGINE_SERVICE_URL = os.getenv("ENGINE_SERVICE_URL", "http://localhost:8766")
ENGINE_API_SECRET  = os.getenv("ENGINE_API_SECRET", "")
NST = ZoneInfo("Asia/Kathmandu")

USER_SESSION_COOKIE = "nepse_user_session"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

_user_sessions: dict[str, dict] = {}


def _nst_now() -> str:
    return datetime.now(tz=NST).strftime("%Y-%m-%d %H:%M:%S")


@asynccontextmanager
async def lifespan(app):
    with _db() as cur:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS pin_hash TEXT")
    yield


app = FastAPI(title="NEPSE Trading — Register & Credentials", version="1.0.0", lifespan=lifespan)

_WEB_UI_DIR = os.path.join(os.path.dirname(__file__), "web_ui")


# ── Session helpers ────────────────────────────────────────────────────────────

def _get_user_session(request: Request) -> Optional[dict]:
    sid = request.cookies.get(USER_SESSION_COOKIE, "")
    if not sid:
        return None
    sess = _user_sessions.get(sid)
    if sess is None or sess["expires_at"] < time.time():
        _user_sessions.pop(sid, None)
        return None
    return sess


def _validate_admin_session(request: Request) -> bool:
    """Calls engine /api/v1/auth/validate-session to verify admin nepse_session cookie."""
    sid = request.cookies.get("nepse_session", "")
    if not sid or not ENGINE_API_SECRET:
        return False
    try:
        r = httpx.get(
            f"{ENGINE_SERVICE_URL}/api/v1/auth/validate-session",
            headers={"X-Service-Key": ENGINE_API_SECRET, "X-Session-Cookie": sid},
            timeout=3,
        )
        data = r.json()
        return r.status_code == 200 and data.get("valid") and data.get("type") == "admin"
    except Exception as e:
        log.warning("admin session validation failed: %s", e)
        return False


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "register_api"}


# ── Registration ───────────────────────────────────────────────────────────────

@app.get("/register", response_class=HTMLResponse)
def register_page():
    return FileResponse(os.path.join(_WEB_UI_DIR, "register.html"))


@app.get("/", response_class=HTMLResponse)
def index():
    return RedirectResponse(url="/register", status_code=302)


class _RegisterBody(BaseModel):
    full_name:   str
    telegram_id: Optional[str] = None
    discord_id:  Optional[str] = None


@app.post("/register")
def register_user(body: _RegisterBody):
    full_name   = (body.full_name or "").strip()
    telegram_id = (body.telegram_id or "").strip() or None
    discord_id  = (body.discord_id  or "").strip() or None

    if not full_name:
        return JSONResponse({"detail": "full_name is required"}, status_code=400)
    if not telegram_id and not discord_id:
        return JSONResponse({"detail": "At least one of telegram_id or discord_id is required"}, status_code=400)

    now = _nst_now()
    try:
        with _db() as cur:
            # Check if already registered via any provided platform ID
            for platform, pid in [("telegram", telegram_id), ("discord", discord_id)]:
                if not pid:
                    continue
                cur.execute(
                    "SELECT user_id FROM user_channels WHERE platform=%s AND platform_user_id=%s",
                    (platform, pid)
                )
                row = cur.fetchone()
                if row:
                    return {"status": "already_registered", "user_id": row["user_id"]}

            # Create users row
            cur.execute(
                "INSERT INTO users (full_name, status, created_at) VALUES (%s, 'PENDING', %s) RETURNING id",
                (full_name, now)
            )
            user_id = str(cur.fetchone()["id"])

            # Create user_channels rows
            if telegram_id:
                cur.execute(
                    """INSERT INTO user_channels
                           (user_id, platform, platform_user_id, is_primary, linked_at)
                       VALUES (%s, 'telegram', %s, 'true', %s)
                       ON CONFLICT (platform, platform_user_id) DO NOTHING""",
                    (user_id, telegram_id, now)
                )
            if discord_id:
                cur.execute(
                    """INSERT INTO user_channels
                           (user_id, platform, platform_user_id, is_primary, linked_at)
                       VALUES (%s, 'discord', %s, %s, %s)
                       ON CONFLICT (platform, platform_user_id) DO NOTHING""",
                    (user_id, discord_id, "false" if telegram_id else "true", now)
                )

        return {"status": "registered", "user_id": user_id}
    except Exception as e:
        log.exception("register_user failed: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


# ── User login ─────────────────────────────────────────────────────────────────

_USER_LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>NEPSE — User Login</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  :root{--bg:#0d0f0e;--surface:#141714;--border:#2a2e2a;--green:#4ade80;--green-dim:#22c55e44;
        --red:#f87171;--text:#e2e8e2;--text-dim:#6b7a6b;--mono:'IBM Plex Mono',monospace}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;
       display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:4px;
        padding:32px;width:320px;display:flex;flex-direction:column;gap:16px}
  h1{font-size:13px;letter-spacing:.12em;color:var(--text-dim);text-transform:uppercase;
     border-left:3px solid var(--green);padding-left:10px}
  label{font-size:10px;letter-spacing:.1em;color:var(--text-dim);text-transform:uppercase;
        display:block;margin-bottom:5px}
  input{width:100%;background:#1a1d1a;border:1px solid var(--border);color:var(--text);
        font-family:var(--mono);font-size:13px;padding:8px 12px;border-radius:3px;outline:none;
        transition:border-color .15s}
  input:focus{border-color:var(--green)}
  button{font-family:var(--mono);font-size:11px;letter-spacing:.08em;text-transform:uppercase;
         padding:10px 24px;border-radius:3px;border:1px solid var(--green);cursor:pointer;
         background:transparent;color:var(--green);width:100%;transition:background .15s}
  button:hover{background:var(--green-dim)}
  #err{font-size:11px;color:var(--red);min-height:14px}
  .help{font-size:11px;color:var(--text-dim);line-height:1.5}
</style></head>
<body>
  <div class="card">
    <h1>NEPSE Trading — Login</h1>
    <div>
      <label>Telegram ID</label>
      <input type="text" id="tid" placeholder="e.g. 123456789" autofocus autocomplete="username"/>
    </div>
    <div>
      <label>Web PIN</label>
      <input type="password" id="pin" placeholder="4-8 digit PIN" autocomplete="current-password"/>
    </div>
    <div id="err"></div>
    <button onclick="doLogin()">Log in</button>
    <p class="help">Set your PIN in Telegram: <strong>/setpin 1234</strong><br>Don't have an account? <a href="/register" style="color:var(--green)">Register</a></p>
  </div>
  <script>
    document.addEventListener('keydown', e => { if (e.key === 'Enter') doLogin(); });
    async function doLogin() {
      const err = document.getElementById('err');
      err.textContent = '';
      const tid = document.getElementById('tid').value.trim();
      const pin = document.getElementById('pin').value.trim();
      if (!tid || !pin) { err.textContent = 'Telegram ID and PIN are required.'; return; }
      try {
        const r = await fetch('/user-login', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({telegram_id: tid, pin}),
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) { window.location.href = d.redirect || '/credentials'; }
        else { err.textContent = d.detail || 'Login failed'; }
      } catch(ex) { err.textContent = ex.message; }
    }
  </script>
</body></html>"""


@app.get("/user-login", response_class=HTMLResponse)
def user_login_page():
    return HTMLResponse(_USER_LOGIN_PAGE)


class _UserLoginBody(BaseModel):
    telegram_id: str
    pin: str


@app.post("/user-login")
def user_login_submit(body: _UserLoginBody, request: Request):
    tid = (body.telegram_id or "").strip()
    pin = (body.pin or "").strip()

    if not tid or not pin:
        return JSONResponse({"detail": "telegram_id and pin are required"}, status_code=400)
    if not pin.isdigit() or not (4 <= len(pin) <= 8):
        return JSONResponse({"detail": "Invalid PIN format"}, status_code=400)

    try:
        rows = run_raw_sql(
            """SELECT u.id, u.status, u.pin_hash
               FROM users u
               JOIN user_channels uc ON u.id::text = uc.user_id
               WHERE uc.platform = 'telegram' AND uc.platform_user_id = %s
               LIMIT 1""",
            (tid,)
        )
    except Exception as e:
        log.exception("user_login DB error: %s", e)
        return JSONResponse({"detail": "Server error"}, status_code=500)

    if not rows:
        return JSONResponse({"detail": "No account found for that Telegram ID"}, status_code=401)

    user = rows[0]
    if user["status"] != "APPROVED":
        return JSONResponse({"detail": "Account pending approval"}, status_code=403)
    if not user["pin_hash"]:
        return JSONResponse({"detail": "No web PIN set — use /setpin in Telegram first"}, status_code=401)

    expected = hashlib.sha256(pin.encode()).hexdigest()
    if not hmac.compare_digest(expected, user["pin_hash"]):
        return JSONResponse({"detail": "Incorrect PIN"}, status_code=401)

    sid = secrets.token_urlsafe(32)
    _user_sessions[sid] = {
        "expires_at": time.time() + SESSION_TTL_SECONDS,
        "user_id": str(user["id"]),
        "telegram_id": tid,
    }
    resp = JSONResponse({"status": "ok", "redirect": "/credentials"})
    resp.set_cookie(
        USER_SESSION_COOKIE, sid, max_age=SESSION_TTL_SECONDS,
        httponly=True, samesite="lax",
    )
    return resp


@app.post("/user-logout")
def user_logout(request: Request):
    sid = request.cookies.get(USER_SESSION_COOKIE, "")
    _user_sessions.pop(sid, None)
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(USER_SESSION_COOKIE)
    return resp


# ── Credentials UI ─────────────────────────────────────────────────────────────

@app.get("/credentials", response_class=HTMLResponse)
def credentials_page(request: Request):
    sess = _get_user_session(request)
    if not sess:
        return RedirectResponse(url="/user-login", status_code=302)
    return FileResponse(os.path.join(_WEB_UI_DIR, "credentials.html"))


@app.get("/api/credentials/data")
def credentials_data(request: Request):
    """Return all credentials for the logged-in user."""
    sess = _get_user_session(request)
    if not sess:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    uid = sess["user_id"]
    meroshare = run_raw_sql(
        "SELECT id, label, dp_id, demat_number, client_code, username, is_active, is_primary "
        "FROM user_meroshare WHERE user_id = %s ORDER BY id", (uid,)
    )
    brokers = run_raw_sql(
        "SELECT id, label, broker_name, broker_code, tms_base_url, username, account_type, "
        "       is_active, is_primary, auto_execute_enabled, max_auto_buy_npr "
        "FROM user_broker WHERE user_id = %s ORDER BY id", (uid,)
    )
    broker_ids = [str(b["id"]) for b in brokers]
    links = []
    if broker_ids:
        placeholders = ", ".join(["%s"] * len(broker_ids))
        links = run_raw_sql(
            f"SELECT id, broker_id, link_type, label, url, is_active "
            f"FROM user_broker_links WHERE broker_id IN ({placeholders}) ORDER BY id",
            tuple(broker_ids)
        )
    return {"meroshare": meroshare, "brokers": brokers, "links": links}


class _MeroshareBody(BaseModel):
    label:         Optional[str] = None
    dp_id:         str
    demat_number:  str
    client_code:   str
    username:      str
    password:      Optional[str] = None
    is_primary:    bool = False


@app.post("/credentials/meroshare")
def upsert_meroshare(body: _MeroshareBody, request: Request):
    sess = _get_user_session(request)
    if not sess:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    uid = sess["user_id"]

    existing = run_raw_sql(
        "SELECT id, password_enc FROM user_meroshare WHERE user_id=%s AND dp_id=%s",
        (uid, body.dp_id)
    )
    password_enc = existing[0]["password_enc"] if existing else None
    if body.password:
        try:
            password_enc = encrypt_password(body.password)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)

    if not password_enc:
        return JSONResponse({"detail": "password is required for new accounts"}, status_code=400)

    now = _nst_now()
    try:
        with _db() as cur:
            if existing:
                cur.execute(
                    """UPDATE user_meroshare SET label=%s, demat_number=%s, client_code=%s,
                       username=%s, password_enc=%s, is_primary=%s
                       WHERE id=%s""",
                    (body.label, body.demat_number, body.client_code, body.username,
                     password_enc, str(body.is_primary).lower(), existing[0]["id"])
                )
            else:
                cur.execute(
                    """INSERT INTO user_meroshare
                       (user_id, label, dp_id, demat_number, client_code, username,
                        password_enc, is_active, is_primary, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,'true',%s,%s)""",
                    (uid, body.label, body.dp_id, body.demat_number, body.client_code,
                     body.username, password_enc, str(body.is_primary).lower(), now)
                )
        return {"status": "ok"}
    except Exception as e:
        log.exception("upsert_meroshare failed: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


class _BrokerBody(BaseModel):
    label:                Optional[str] = None
    broker_name:          str
    broker_code:          str
    tms_base_url:         str
    username:             str
    password:             Optional[str] = None
    account_type:         str = "both"
    is_primary:           bool = False
    auto_execute_enabled: bool = False
    max_auto_buy_npr:     str = "0"


@app.post("/credentials/broker")
def upsert_broker(body: _BrokerBody, request: Request):
    sess = _get_user_session(request)
    if not sess:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    uid = sess["user_id"]

    existing = run_raw_sql(
        "SELECT id, password_enc FROM user_broker WHERE user_id=%s AND broker_code=%s AND username=%s",
        (uid, body.broker_code, body.username)
    )
    password_enc = existing[0]["password_enc"] if existing else None
    if body.password:
        try:
            password_enc = encrypt_password(body.password)
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)

    if not password_enc:
        return JSONResponse({"detail": "password is required for new accounts"}, status_code=400)

    now = _nst_now()
    try:
        with _db() as cur:
            if existing:
                cur.execute(
                    """UPDATE user_broker SET label=%s, broker_name=%s, tms_base_url=%s,
                       password_enc=%s, account_type=%s, is_primary=%s,
                       auto_execute_enabled=%s, max_auto_buy_npr=%s
                       WHERE id=%s""",
                    (body.label, body.broker_name, body.tms_base_url, password_enc,
                     body.account_type, str(body.is_primary).lower(),
                     str(body.auto_execute_enabled).lower(), body.max_auto_buy_npr,
                     existing[0]["id"])
                )
                return {"status": "ok", "broker_id": existing[0]["id"]}
            else:
                cur.execute(
                    """INSERT INTO user_broker
                       (user_id, label, broker_name, broker_code, tms_base_url, username,
                        password_enc, account_type, is_active, is_primary,
                        auto_execute_enabled, max_auto_buy_npr, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'true',%s,%s,%s,%s) RETURNING id""",
                    (uid, body.label, body.broker_name, body.broker_code, body.tms_base_url,
                     body.username, password_enc, body.account_type,
                     str(body.is_primary).lower(), str(body.auto_execute_enabled).lower(),
                     body.max_auto_buy_npr, now)
                )
                broker_id = cur.fetchone()["id"]
                return {"status": "ok", "broker_id": broker_id}
    except Exception as e:
        log.exception("upsert_broker failed: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


class _BrokerLinkBody(BaseModel):
    link_type: str
    label:     str
    url:       str
    is_active: bool = True


@app.post("/credentials/broker/{broker_id}/links")
def add_broker_link(broker_id: int, body: _BrokerLinkBody, request: Request):
    sess = _get_user_session(request)
    if not sess:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    uid = sess["user_id"]

    owner = run_raw_sql(
        "SELECT id FROM user_broker WHERE id=%s AND user_id=%s",
        (str(broker_id), uid)
    )
    if not owner:
        return JSONResponse({"detail": "Broker not found"}, status_code=404)

    now = _nst_now()
    try:
        with _db() as cur:
            cur.execute(
                """INSERT INTO user_broker_links
                   (user_id, broker_id, link_type, label, url, is_active, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (uid, str(broker_id), body.link_type, body.label, body.url,
                 str(body.is_active).lower(), now)
            )
            link_id = cur.fetchone()["id"]
        return {"status": "ok", "link_id": link_id}
    except Exception as e:
        log.exception("add_broker_link failed: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.delete("/credentials/broker/{broker_id}")
def delete_broker(broker_id: int, request: Request):
    sess = _get_user_session(request)
    if not sess:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    uid = sess["user_id"]

    owner = run_raw_sql(
        "SELECT id FROM user_broker WHERE id=%s AND user_id=%s",
        (str(broker_id), uid)
    )
    if not owner:
        return JSONResponse({"detail": "Broker not found"}, status_code=404)

    try:
        with _db() as cur:
            cur.execute("DELETE FROM user_broker_links WHERE broker_id=%s", (str(broker_id),))
            cur.execute("DELETE FROM user_broker WHERE id=%s", (str(broker_id),))
        return {"status": "ok"}
    except Exception as e:
        log.exception("delete_broker failed: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.delete("/credentials/meroshare/{ms_id}")
def delete_meroshare(ms_id: int, request: Request):
    sess = _get_user_session(request)
    if not sess:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    uid = sess["user_id"]

    owner = run_raw_sql(
        "SELECT id FROM user_meroshare WHERE id=%s AND user_id=%s",
        (str(ms_id), uid)
    )
    if not owner:
        return JSONResponse({"detail": "Account not found"}, status_code=404)

    try:
        with _db() as cur:
            cur.execute("DELETE FROM user_meroshare WHERE id=%s", (str(ms_id),))
        return {"status": "ok"}
    except Exception as e:
        log.exception("delete_meroshare failed: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.delete("/credentials/broker/{broker_id}/links/{link_id}")
def delete_broker_link(broker_id: int, link_id: int, request: Request):
    sess = _get_user_session(request)
    if not sess:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    uid = sess["user_id"]

    owner = run_raw_sql(
        "SELECT bl.id FROM user_broker_links bl "
        "JOIN user_broker b ON bl.broker_id = b.id::text "
        "WHERE bl.id=%s AND b.user_id=%s",
        (str(link_id), uid)
    )
    if not owner:
        return JSONResponse({"detail": "Link not found"}, status_code=404)

    try:
        with _db() as cur:
            cur.execute("DELETE FROM user_broker_links WHERE id=%s", (str(link_id),))
        return {"status": "ok"}
    except Exception as e:
        log.exception("delete_broker_link failed: %s", e)
        return JSONResponse({"detail": str(e)}, status_code=500)


# ── Admin UI ───────────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not _validate_admin_session(request):
        return RedirectResponse(url=f"{ENGINE_SERVICE_URL}/login", status_code=302)
    return FileResponse(os.path.join(_WEB_UI_DIR, "admin.html"))


@app.get("/api/admin/data")
def admin_data(request: Request):
    if not _validate_admin_session(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    pending = run_raw_sql(
        "SELECT u.id, u.full_name, u.status, u.created_at, "
        "       uc.platform, uc.platform_user_id, uc.username "
        "FROM users u "
        "LEFT JOIN user_channels uc ON u.id::text = uc.user_id "
        "WHERE u.status = 'PENDING' ORDER BY u.id"
    )
    all_users = run_raw_sql(
        "SELECT u.id, u.full_name, u.status, u.created_at, u.approved_at, "
        "       uc.platform, uc.platform_user_id, uc.username "
        "FROM users u "
        "LEFT JOIN user_channels uc ON u.id::text = uc.user_id "
        "ORDER BY u.id"
    )
    return {"pending": pending, "users": all_users}


@app.post("/admin/approve/{user_id}")
def admin_approve(user_id: int, request: Request):
    if not _validate_admin_session(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    # Look up telegram_id for this user_id, then call trading_core.approve_user
    rows = run_raw_sql(
        "SELECT platform_user_id FROM user_channels "
        "WHERE user_id=%s AND platform='telegram' LIMIT 1",
        (str(user_id),)
    )
    if not rows:
        return JSONResponse({"detail": "No telegram channel found for user"}, status_code=404)
    telegram_id = int(rows[0]["platform_user_id"])
    from modules.trading_core import approve_user
    ok = approve_user(telegram_id, approved_by=0)
    if not ok:
        # User may not be in paper_users yet — approve directly in canonical tables
        now = _nst_now()
        with _db() as cur:
            cur.execute(
                "UPDATE users SET status='APPROVED', approved_at=%s WHERE id=%s",
                (now, user_id)
            )
            cur.execute(
                """INSERT INTO user_signal_preferences
                   (user_id, notify_telegram, notify_discord, auto_execute, created_at, updated_at)
                   VALUES (%s, 'true', 'false', 'false', %s, %s)
                   ON CONFLICT (user_id) DO NOTHING""",
                (str(user_id), now, now)
            )
    return {"status": "ok"}


@app.post("/admin/deny/{user_id}")
def admin_deny(user_id: int, request: Request):
    if not _validate_admin_session(request):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    rows = run_raw_sql(
        "SELECT platform_user_id FROM user_channels "
        "WHERE user_id=%s AND platform='telegram' LIMIT 1",
        (str(user_id),)
    )
    now = _nst_now()
    with _db() as cur:
        cur.execute(
            "UPDATE users SET status='BLOCKED' WHERE id=%s", (user_id,)
        )
    if rows:
        telegram_id = int(rows[0]["platform_user_id"])
        from modules.trading_core import deny_user
        deny_user(telegram_id)
    return {"status": "ok"}
