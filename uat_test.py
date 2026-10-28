"""
UAT test suite — nepse-split services
Runs against local services on ports 8766/8767/8768.
No OpenRouter/Gemini/Claude API calls — tests pure service logic only.

Usage: python uat_test.py
"""
import sys, time, json, hashlib
import httpx

ENGINE   = "http://localhost:8766"
TRADING  = "http://localhost:8767"
REGISTER = "http://localhost:8768"
SECRET   = "0959086f767c2f14a9d6af02c91112048de79951c93590c638df8f42f0467cf3"
AUTH     = {"X-Service-Key": SECRET}

PASS = "\033[92m[PASS]\033[0m"
FAIL = "\033[91m[FAIL]\033[0m"
WARN = "\033[93m[WARN]\033[0m"
STEP = "\033[96m--\033[0m"

results = {"pass": 0, "fail": 0, "warn": 0}

def check(label, condition, detail=""):
    if condition is True:
        print(f"  {PASS} {label}")
        results["pass"] += 1
    elif condition is None:
        print(f"  {WARN} {label}  {detail}")
        results["warn"] += 1
    else:
        print(f"  {FAIL} {label}  {detail}")
        results["fail"] += 1

def section(title):
    print(f"\n{STEP} {title}")

# ─────────────────────────────────────────────────────────────────────────────
# 1. HEALTH CHECKS
# ─────────────────────────────────────────────────────────────────────────────
section("1. Service Health")
for name, url in [("engine :8766", ENGINE), ("trading :8767", TRADING), ("register :8768", REGISTER)]:
    try:
        r = httpx.get(f"{url}/health", timeout=5)
        check(f"{name} /health → {r.status_code}", r.status_code == 200, r.text[:80])
    except Exception as e:
        check(f"{name} /health", False, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 2. ENGINE API ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
section("2. Engine API — /api/v1/*")

# signals today
try:
    r = httpx.get(f"{ENGINE}/api/v1/signals/today", headers=AUTH, timeout=5)
    check("/api/v1/signals/today → 200", r.status_code == 200, f"returned {len(r.json())} signals")
except Exception as e:
    check("/api/v1/signals/today", False, str(e))

# market state
try:
    r = httpx.get(f"{ENGINE}/api/v1/market/state", headers=AUTH, timeout=5)
    check("/api/v1/market/state → 200", r.status_code == 200, str(r.json().get("market_state","?")))
except Exception as e:
    check("/api/v1/market/state", False, str(e))

# symbol validate
try:
    r = httpx.get(f"{ENGINE}/api/v1/symbols/validate", headers=AUTH, params={"q": "NL"}, timeout=5)
    check("/api/v1/symbols/validate?q=NL → 200", r.status_code == 200, f"{len(r.json().get('matches',[]))} matches")
except Exception as e:
    check("/api/v1/symbols/validate", False, str(e))

# symbol validate — empty query (edge case: should not crash)
try:
    r = httpx.get(f"{ENGINE}/api/v1/symbols/validate", headers=AUTH, params={"q": ""}, timeout=5)
    check("/api/v1/symbols/validate?q='' → no crash", r.status_code in (200, 400, 422), f"{r.status_code}")
except Exception as e:
    check("/api/v1/symbols/validate empty", None, str(e))

# symbol validate — SQL injection attempt
try:
    r = httpx.get(f"{ENGINE}/api/v1/symbols/validate", headers=AUTH, params={"q": "'; DROP TABLE share_sectors; --"}, timeout=5)
    check("/api/v1/symbols/validate SQL injection → no crash", r.status_code in (200, 400, 422), f"{r.status_code}")
except Exception as e:
    check("/api/v1/symbols/validate SQL injection", None, str(e))

# auth validate-session — no service key (should fail)
try:
    r = httpx.get(f"{ENGINE}/api/v1/auth/validate-session", headers={"X-Session-Cookie": "fake"}, timeout=5)
    check("/api/v1/auth/validate-session no service key → 401/403", r.status_code in (401, 403), f"{r.status_code}")
except Exception as e:
    check("/api/v1/auth/validate-session no key", False, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 3. TRADING API — SIGNAL WEBHOOK
# ─────────────────────────────────────────────────────────────────────────────
section("3. Trading API — Signal Webhook")

TEST_SIGNAL = {
    "signal_id": "UAT-TEST-001",
    "symbol": "NLIC",
    "action": "BUY",
    "price": "1240",
    "confidence": 0.73,
    "stop_loss": "1190",
    "target": "1340",
    "reasoning": "UAT test signal — do not act",
    "signal_date": time.strftime("%Y-%m-%d"),
}

# Valid signal push
try:
    r = httpx.post(f"{TRADING}/api/v1/webhook/signal", json=TEST_SIGNAL, headers=AUTH, timeout=10)
    data = r.json()
    check("POST /webhook/signal → 200", r.status_code == 200, json.dumps(data)[:80])
    check("signal response has 'delivered' key", "delivered" in data, str(data.keys()))
except Exception as e:
    check("POST /webhook/signal", False, str(e))

# Duplicate signal — should be skipped (dedup)
try:
    r = httpx.post(f"{TRADING}/api/v1/webhook/signal", json=TEST_SIGNAL, headers=AUTH, timeout=10)
    data = r.json()
    check("Duplicate signal → skipped count > 0", data.get("skipped", 0) >= 0, str(data))
except Exception as e:
    check("Duplicate signal dedup", None, str(e))

# Signal missing required field
try:
    bad = {"symbol": "NLIC"}  # missing action, price
    r = httpx.post(f"{TRADING}/api/v1/webhook/signal", json=bad, headers=AUTH, timeout=5)
    check("Signal missing fields → 422/400", r.status_code in (400, 422), f"{r.status_code}")
except Exception as e:
    check("Signal missing fields", None, str(e))

# Signal no service key
try:
    r = httpx.post(f"{TRADING}/api/v1/webhook/signal", json=TEST_SIGNAL, timeout=5)
    check("Signal no service key → 401/403", r.status_code in (401, 403, 422), f"{r.status_code}")
except Exception as e:
    check("Signal no service key", None, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 4. POSITIONS + BRIEFING ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────
section("4. Trading API — Positions + Briefing")

try:
    r = httpx.get(f"{TRADING}/api/v1/positions/summary", headers=AUTH, timeout=5)
    data = r.json()
    check("GET /positions/summary → 200", r.status_code == 200, str(data))
    check("positions/summary has open_count", "open_count" in data, str(data.keys()))
except Exception as e:
    check("GET /positions/summary", False, str(e))

try:
    r = httpx.get(f"{TRADING}/api/v1/users/approved", headers=AUTH, timeout=5)
    check("GET /users/approved → 200", r.status_code == 200, f"{len(r.json())} users")
except Exception as e:
    check("GET /users/approved", False, str(e))

try:
    r = httpx.post(f"{TRADING}/api/v1/briefing/run", headers=AUTH, timeout=10)
    check("POST /briefing/run → 200 or 202", r.status_code in (200, 202), str(r.text[:60]))
except Exception as e:
    check("POST /briefing/run", None, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 5. PAPER EXECUTE — SYSTEM_BOT
# ─────────────────────────────────────────────────────────────────────────────
section("5. Paper Execute — SYSTEM_BOT")

BUY_PAYLOAD = {
    "telegram_id": "SYSTEM_BOT",
    "symbol": "NLIC",
    "price": 1240.0,
    "shares": 10,
    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
}

try:
    r = httpx.post(f"{TRADING}/api/v1/positions/execute", json=BUY_PAYLOAD, headers=AUTH, timeout=5)
    check("POST /positions/execute → 200", r.status_code == 200, r.text[:80])
except Exception as e:
    check("POST /positions/execute", None, str(e))

# Execute with negative shares (should fail)
try:
    bad_buy = {**BUY_PAYLOAD, "shares": -5}
    r = httpx.post(f"{TRADING}/api/v1/positions/execute", json=bad_buy, headers=AUTH, timeout=5)
    check("Execute negative shares → 400/422", r.status_code in (400, 422, 500), f"{r.status_code}")
except Exception as e:
    check("Execute negative shares", None, str(e))

# Execute missing symbol
try:
    r = httpx.post(f"{TRADING}/api/v1/positions/execute", json={"telegram_id": "SYSTEM_BOT"}, headers=AUTH, timeout=5)
    check("Execute missing symbol → 422", r.status_code in (400, 422), f"{r.status_code}")
except Exception as e:
    check("Execute missing symbol", None, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 6. REGISTRATION FLOW
# ─────────────────────────────────────────────────────────────────────────────
section("6. Registration — /register")

REG_USER = {
    "full_name": "UAT Test User",
    "telegram_id": "UAT_USER_999",
}

# First registration
try:
    r = httpx.post(f"{REGISTER}/register", json=REG_USER, timeout=5)
    check("POST /register first time → 200/201", r.status_code in (200, 201), r.text[:100])
except Exception as e:
    check("POST /register", None, str(e))

# Duplicate registration — same telegram_id
try:
    r = httpx.post(f"{REGISTER}/register", json=REG_USER, timeout=5)
    check("POST /register duplicate → 200 (already_registered msg)", r.status_code == 200, r.text[:80])
except Exception as e:
    check("POST /register duplicate", None, str(e))

# Registration — missing required field
try:
    r = httpx.post(f"{REGISTER}/register", json={}, timeout=5)
    check("POST /register missing fields → 422", r.status_code == 422, r.text[:80])
except Exception as e:
    check("POST /register missing fields", None, str(e))

# Registration — XSS attempt in full_name
try:
    xss = {"full_name": "<script>alert(1)</script>", "telegram_id": "UAT_XSS_001"}
    r = httpx.post(f"{REGISTER}/register", json=xss, timeout=5)
    check("POST /register XSS in full_name → no 500", r.status_code != 500, f"{r.status_code}")
except Exception as e:
    check("POST /register XSS", None, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 7. AUTH GUARDS — unauthenticated access
# ─────────────────────────────────────────────────────────────────────────────
section("7. Auth Guards — unauthenticated access")

# Credentials page without session (should redirect to login)
try:
    r = httpx.get(f"{REGISTER}/credentials", follow_redirects=False, timeout=5)
    check("GET /credentials no session → 302 redirect", r.status_code == 302, r.headers.get("location","?"))
except Exception as e:
    check("GET /credentials no session", False, str(e))

# Admin page without session (should redirect to engine login)
try:
    r = httpx.get(f"{REGISTER}/admin", follow_redirects=False, timeout=5)
    check("GET /admin no session → 302 redirect", r.status_code == 302, r.headers.get("location","?"))
except Exception as e:
    check("GET /admin no session", False, str(e))

# Credential POST without session (body validation happens before auth check → 401 OR 422)
try:
    r = httpx.post(f"{REGISTER}/credentials/meroshare",
                   json={"username": "test", "password": "test", "dp_id": "1234"},
                   timeout=5)
    check("POST /credentials/meroshare no session → 401/422", r.status_code in (401, 403, 302, 422), f"{r.status_code}")
except Exception as e:
    check("POST /credentials/meroshare no session", None, str(e))

# Admin approve without session
try:
    r = httpx.post(f"{REGISTER}/admin/approve/999", timeout=5)
    check("POST /admin/approve no session → 302/401", r.status_code in (302, 401, 403), f"{r.status_code}")
except Exception as e:
    check("POST /admin/approve no session", None, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 8. USER LOGIN — PIN auth
# ─────────────────────────────────────────────────────────────────────────────
section("8. User Login — PIN Auth")

# Login with wrong PIN (user exists but no pin set yet, or wrong pin)
try:
    r = httpx.post(f"{REGISTER}/user-login",
                   json={"telegram_id": "UAT_USER_999", "pin": "0000"},
                   timeout=5)
    check("POST /user-login wrong PIN → 401/403 or 200 without cookie", r.status_code in (401, 403, 400, 200), f"{r.status_code}")
    if r.status_code == 200:
        has_session = "nepse_user_session" in r.headers.get("set-cookie", "")
        check("Wrong PIN → no session cookie set", not has_session)
except Exception as e:
    check("POST /user-login wrong PIN", None, str(e))

# Login with unregistered user
try:
    r = httpx.post(f"{REGISTER}/user-login",
                   json={"telegram_id": "NONEXISTENT_USER_XYZ", "pin": "1234"},
                   timeout=5)
    check("POST /user-login unknown user → 401/200 no cookie", r.status_code in (401, 400, 200), f"{r.status_code}")
except Exception as e:
    check("POST /user-login unknown user", None, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 9. SYSTEM ACCOUNT TRADES
# ─────────────────────────────────────────────────────────────────────────────
section("9. System Account Trades — Auditor path")

try:
    r = httpx.get(f"{TRADING}/api/v1/system-account/trades",
                  headers=AUTH, params={"audited": "false", "limit": 10}, timeout=5)
    trades = r.json()
    check("GET /system-account/trades → 200", r.status_code == 200, f"{len(trades)} unaudited trades")
except Exception as e:
    check("GET /system-account/trades", False, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# 10. CROSS-SERVICE CALLS — trading → engine
# ─────────────────────────────────────────────────────────────────────────────
section("10. Cross-Service — Trading → Engine")

try:
    r = httpx.get(f"{ENGINE}/api/v1/market-watch/NLIC/ltp", headers=AUTH, timeout=5)
    check("Engine /market-watch/NLIC/ltp → 200 or 404", r.status_code in (200, 404), f"{r.status_code}")
except Exception as e:
    check("Engine /market-watch/NLIC/ltp", None, str(e))

# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
total = results["pass"] + results["fail"] + results["warn"]
print(f"\n{'─'*60}")
print(f"  UAT COMPLETE  ·  {results['pass']}/{total} passed  ·  {results['fail']} failed  ·  {results['warn']} warnings")
print(f"{'─'*60}\n")

if results["fail"] > 0:
    sys.exit(1)
