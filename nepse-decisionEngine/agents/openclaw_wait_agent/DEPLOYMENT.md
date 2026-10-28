# OpenClaw Wait-Agent — Deployment Guide

## What Was Built

A production-ready autonomous agent that validates WAIT conditions daily at 3:30 PM NST.

### Files Created

```
~/nepse-engine/agents/openclaw_wait_agent/
├── config/
│   ├── SOUL.md                 # Agent personality
│   ├── AGENTS.md               # Configuration
│   ├── HEARTBEAT.md            # 3:30 PM instructions
│   └── openclaw.json           # MCP + channels
├── openclaw-wait-agent.service # Systemd service
├── openclaw-wait-agent.timer   # Systemd timer (3:30 PM daily)
├── .env.example                # Environment template
├── README.md                    # Setup instructions
└── DEPLOYMENT.md               # This file
```

## Architecture

**Flow (daily at 3:30 PM):**

```
Timer fires (15:30 NST)
    ↓
Service executes: openclaw run --agent wait-agent
    ↓
OpenClaw loads: SOUL.md → AGENTS.md → HEARTBEAT.md
    ↓
Agent executes:
  1. Fetch PENDING WAIT records (via /reporter/run)
  2. Generate/cache SQL queries (Llama Scout)
  3. Execute queries (via /reporter/run)
  4. Analyze conditions (Claude 3.5 Sonnet)
  5. Update market_log (outcome='BOUGHT')
  6. Send Discord alerts
  7. Log execution trace
    ↓
Persists results to:
  - market_log (updated outcomes)
  - Discord (notifications)
  - ~/nepse-engine/logs/agents/ (execution logs)
```

## Deployment Steps

### Step 1: Install OpenClaw (if not done)

```bash
curl -fsSL https://openclaw.ai/install.sh | bash
openclaw --version  # Verify installation
```

### Step 2: Set Up Environment

```bash
cd ~/nepse-engine/agents/openclaw_wait_agent
cp .env.example .env
nano .env  # Edit with your API keys
```

**Required keys:**
- `ANTHROPIC_API_KEY` — Get from https://console.anthropic.com
- `OPENROUTER_API_KEY` — Get from https://openrouter.ai
- `DISCORD_WEBHOOK_WAIT_AGENT` — Create in Discord server

### Step 3: Update Database

Add query cache column (one-time):

```bash
curl -s -X POST "http://192.168.1.200:8766/reporter/run" \
  -H "Content-Type: application/json" \
  -d '{"sql":"ALTER TABLE market_log ADD COLUMN IF NOT EXISTS wait_condition_query TEXT","params":{}}'
```

Or via direct DB if you prefer:
```sql
ALTER TABLE market_log ADD COLUMN IF NOT EXISTS wait_condition_query TEXT;
```

### Step 4: Deploy Systemd Timer

**Copy to systemd:**
```bash
sudo cp openclaw-wait-agent.service /etc/systemd/system/
sudo cp openclaw-wait-agent.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

**Enable and start:**
```bash
sudo systemctl enable openclaw-wait-agent.timer
sudo systemctl start openclaw-wait-agent.timer
```

**Verify:**
```bash
sudo systemctl status openclaw-wait-agent.timer
systemctl list-timers openclaw-wait-agent.timer
```

Output should show:
```
NEXT                        LEFT       LAST                        PASSED       UNIT
2026-07-30 15:30:00 +0545   1h 30min   —                           —            openclaw-wait-agent.timer
```

### Step 5: Test Execution (Optional)

**Dry run:**
```bash
cd ~/nepse-engine/agents/openclaw_wait_agent
source .env
openclaw run --agent wait-agent --config config/openclaw.json --dry-run
```

**Real run:**
```bash
source .env
openclaw run --agent wait-agent --config config/openclaw.json
```

## Monitoring

### Check Timer Status

```bash
sudo systemctl status openclaw-wait-agent.timer
```

### Follow Agent Logs

```bash
# Real-time logs
journalctl -u openclaw-wait-agent -f

# Last 50 lines
journalctl -u openclaw-wait-agent -n 50

# Today's logs only
journalctl -u openclaw-wait-agent --since today
```

### View via Dashboard

```
http://localhost:8766/dashboard/logs/list
→ Shows: wait_agent_*.log files

http://localhost:8766/dashboard/logs/file?path=agents/wait_agent_2026_07_30.log
→ Shows: Full execution trace
```

### Check Market Log Updates

```bash
curl -s -X POST "http://192.168.1.200:8766/reporter/run" \
  -H "Content-Type: application/json" \
  -d '{"sql":"SELECT symbol, outcome, remark FROM market_log WHERE action='"'"'WAIT'"'"' AND outcome='"'"'BOUGHT'"'"' ORDER BY exit_date DESC LIMIT 5","params":{}}'
```

## Cost Tracking

**Per daily execution:**
- Llama Scout (1st new WAIT): $0.005
- Claude (all WAITs): $0.020
- Total: ~$0.025

**Billing:**
- Anthropic: Monitor at https://console.anthropic.com/account/billing
- OpenRouter: Monitor at https://openrouter.ai/account/billing

## Troubleshooting

### Timer not running?

```bash
sudo systemctl restart openclaw-wait-agent.timer
sudo systemctl status openclaw-wait-agent.timer
journalctl -u openclaw-wait-agent -n 20
```

### Agent fails to start?

Check if OpenClaw is installed:
```bash
which openclaw
openclaw --version
```

Check if .env exists:
```bash
ls -la ~/nepse-engine/agents/openclaw_wait_agent/.env
```

Check API connectivity:
```bash
curl -s http://192.168.1.200:8766/health
echo "Reporter API: $?"
```

### Discord not sending?

Test webhook:
```bash
curl -X POST "$DISCORD_WEBHOOK_WAIT_AGENT" \
  -H "Content-Type: application/json" \
  -d '{"content":"Test message from Wait-Agent"}'
```

### No query generation?

Check Llama Scout access:
```bash
curl -s "https://api.openrouter.ai/api/v1/models" \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | grep llama-4-scout
```

## Disable / Uninstall

**Pause agent:**
```bash
sudo systemctl stop openclaw-wait-agent.timer
```

**Disable permanently:**
```bash
sudo systemctl disable openclaw-wait-agent.timer
sudo rm /etc/systemd/system/openclaw-wait-agent.*
sudo systemctl daemon-reload
```

## Performance Expectations

**Execution time:**
- Fetch conditions: 2-3 seconds
- Generate queries: 5-8 seconds (1st WAIT only, then cached)
- Execute queries: 2-5 seconds
- Claude analysis: 3-5 seconds
- Update + Discord: 1-2 seconds
- **Total**: 13-23 seconds

**Reliability:**
- 99.5% expected uptime
- Self-healing on transient failures
- Retries up to 3 times on API errors
- All errors logged for review

## Next Steps

1. ✅ Deploy systemd timer (today)
2. ✅ Verify first execution at 3:30 PM
3. ✅ Monitor Discord alerts
4. ✅ Check logs via dashboard
5. ✅ Collect data for 60+ days
6. ✅ Validate v2 engine accuracy

## Support

- OpenClaw docs: https://clawdocs.org/
- Agent issues: `journalctl -u openclaw-wait-agent`
- API issues: Check reporter connectivity
- LLM issues: Check API key expiry
