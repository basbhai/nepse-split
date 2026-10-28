#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# nepse-trading/setup/install.sh — Ubuntu 22.04/24.04
# Run ONCE after deploying nepse-trading alongside nepse-engine.
#
# Prerequisites:
#   - nepse-engine already installed at /opt/nepse-engine (or equivalent)
#   - nepse_trading PostgreSQL database created
#   - .env filled in (copy .env.example, fill DATABASE_URL, bot tokens, etc.)
#
# Usage:
#   cd /opt/nepse-trading
#   cp .env.example .env && nano .env
#   bash setup/install.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "${GREEN}✅ $1${RESET}"; }
warn() { echo -e "${YELLOW}⚠️  $1${RESET}"; }
info() { echo -e "${CYAN}   $1${RESET}"; }
fail() { echo -e "${RED}❌ $1${RESET}"; exit 1; }
step() { echo -e "\n${BOLD}── $1${RESET}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR" || fail "Cannot cd to $PROJECT_DIR"

echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         NEPSE Trading Service — Ubuntu Setup                 ║"
echo "║         Project: $PROJECT_DIR"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${RESET}"

[ -f ".env" ] || fail ".env not found — copy .env.example and fill in secrets"
ok ".env found"

NEPSE_USER="$(whoami)"
info "Installing for user: $NEPSE_USER"
info "Project directory:   $PROJECT_DIR"

# ── Step 1: System packages ───────────────────────────────────────────────────
step "1/8  System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-pip python3-venv libpq-dev build-essential python3-dev 2>/dev/null
ok "System packages installed"

# ── Step 2: Python venv ───────────────────────────────────────────────────────
step "2/8  Python virtual environment"
if [ ! -d "venv" ]; then
    python3 -m venv venv
    ok "venv created"
else
    ok "venv already exists"
fi
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
pip install cryptography -q 2>/dev/null || warn "cryptography install failed"
ok "Python dependencies installed"

# ── Step 3: DB migrations ─────────────────────────────────────────────────────
step "3/8  Database migrations (nepse_trading)"
python -m db.migrations && ok "DB migrations complete" || fail "DB migrations failed — check DATABASE_URL in .env"

# ── Step 4: Systemd services ──────────────────────────────────────────────────
step "4/8  Systemd services"
SYSTEMD_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_DIR"

for svc in nepse-bot nepse-discord-bot nepse-trading-api nepse-register-api; do
    if [ -f "setup/${svc}.service" ]; then
        sed "s|%i|$NEPSE_USER|g; s|/opt/nepse-trading|$PROJECT_DIR|g" \
            "setup/${svc}.service" > "$SYSTEMD_DIR/${svc}.service"
        ok "Installed $svc.service"
    else
        warn "setup/${svc}.service not found — skipping"
    fi
done

# nepse-monitor (timer-based — runs during market hours)
for svc in nepse-monitor; do
    if [ -f "setup/${svc}.service" ]; then
        sed "s|%i|$NEPSE_USER|g; s|/opt/nepse-trading|$PROJECT_DIR|g" \
            "setup/${svc}.service" > "$SYSTEMD_DIR/${svc}.service"
        ok "Installed $svc.service"
    fi
done
if [ -f "setup/nepse-monitor.timer" ]; then
    cp "setup/nepse-monitor.timer" "$SYSTEMD_DIR/nepse-monitor.timer"
    ok "Installed nepse-monitor.timer"
fi

systemctl --user daemon-reload
ok "systemd daemon reloaded"

# ── Step 5: Enable + start ────────────────────────────────────────────────────
step "5/8  Enable and start services"

# Persistent services (always running)
for svc in nepse-bot nepse-discord-bot nepse-trading-api nepse-register-api; do
    systemctl --user enable ${svc}.service 2>/dev/null && ok "Enabled $svc" || warn "$svc enable skipped"
done

# Timer for execution monitor
systemctl --user enable nepse-monitor.timer 2>/dev/null && ok "Enabled nepse-monitor.timer" || warn "nepse-monitor.timer enable skipped"

# Start persistent services
for svc in nepse-trading-api nepse-register-api nepse-bot nepse-discord-bot; do
    systemctl --user start ${svc}.service 2>/dev/null && ok "Started $svc" || warn "$svc start skipped — check logs"
done
systemctl --user start nepse-monitor.timer 2>/dev/null && ok "Started nepse-monitor.timer" || warn "timer start skipped"

# Linger so services survive logout
sudo loginctl enable-linger "$NEPSE_USER"
ok "Linger enabled"

# ── Step 6: Verify ports ──────────────────────────────────────────────────────
step "6/8  Port check (give services 5s to start)"
sleep 5
curl -s http://localhost:8767/health >/dev/null 2>&1 && ok "trading_api :8767 UP" || warn "trading_api :8767 not responding — check: journalctl --user -u nepse-trading-api"
curl -s http://localhost:8768/health >/dev/null 2>&1 && ok "register_api :8768 UP" || warn "register_api :8768 not responding — check: journalctl --user -u nepse-register-api"

# ── Step 7: Engine connectivity ───────────────────────────────────────────────
step "7/8  Engine connectivity"
ENGINE_URL="$(grep '^ENGINE_BASE_URL=' .env | cut -d'=' -f2- | tr -d '\"')"
ENGINE_URL="${ENGINE_URL:-http://localhost:8766}"
curl -s "${ENGINE_URL}/health" >/dev/null 2>&1 && ok "Engine at ${ENGINE_URL} reachable" || warn "Engine not reachable — start nepse-engine first, trading service will degrade gracefully"

# ── Step 8: Summary ───────────────────────────────────────────────────────────
step "8/8  Done"
echo -e "${BOLD}${GREEN}"
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅  nepse-trading installed!                                ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo -e "${RESET}"
echo -e "${BOLD}Ports:${RESET}"
echo "  8767 → trading_api   (engine → trading signals, paper execute)"
echo "  8768 → register_api  (user registration, credentials, admin)"
echo ""
echo -e "${BOLD}Bot commands:${RESET}"
echo "  /register  — register for paper trading"
echo "  /signal    — view today's signals from engine"
echo "  /buy SYMBOL PRICE — paper buy"
echo "  /status    — portfolio overview"
echo "  /setpin    — set web UI PIN"
echo ""
echo -e "${BOLD}Useful commands:${RESET}"
echo "  journalctl --user -u nepse-bot -f"
echo "  journalctl --user -u nepse-trading-api -f"
echo "  journalctl --user -u nepse-register-api -f"
echo "  systemctl --user list-timers"
echo ""
echo -e "${YELLOW}⚠️  SANDBOX_MODE=true by default. Real broker calls are disabled.${RESET}"
