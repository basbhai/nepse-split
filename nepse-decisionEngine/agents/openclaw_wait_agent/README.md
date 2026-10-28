# OpenClaw Wait-Agent — NEPSE Wait Condition Validator

Autonomous agent that validates WAIT conditions daily at 3:30 PM NST.

## Quick Start

### 1. Prerequisites
- OpenClaw CLI installed: `curl -fsSL https://openclaw.ai/install.sh | bash`
- Anthropic API key (Claude 3.5 Sonnet)
- OpenRouter API key (Llama 4 Scout)
- Discord webhook for alerts

### 2. Setup

**Copy environment template:**
```bash
cp .env.example .env
```

**Edit .env with your keys:**
```bash
nano .env
```

Required values:
- `ANTHROPIC_API_KEY` — Your Claude API key
- `OPENROUTER_API_KEY` — Your OpenRouter key
- `DISCORD_WEBHOOK_WAIT_AGENT` — Your Discord webhook URL

### 3. Install as systemd service

**Copy to systemd directory:**
```bash
sudo cp openclaw-wait-agent.service /etc/systemd/system/
sudo cp openclaw-wait-agent.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

**Enable and start timer:**
```bash
sudo systemctl enable openclaw-wait-agent.timer
sudo systemctl start openclaw-wait-agent.timer
```

**Verify timer is active:**
```bash
sudo systemctl status openclaw-wait-agent.timer
systemctl list-timers openclaw-wait-agent.timer
```

### 4. Manual Test

**Run once to test:**
```bash
source .env
openclaw run --agent wait-agent --config config/openclaw.json --dry-run
```

**Run for real:**
```bash
source .env
openclaw run --agent wait-agent --config config/openclaw.json
```

## Configuration Files

| File | Purpose |
|------|---------|
| `config/SOUL.md` | Agent personality & values |
| `config/AGENTS.md` | Agent configuration & models |
| `config/HEARTBEAT.md` | 3:30 PM trigger instructions |
| `config/openclaw.json` | MCP servers, channels, LLM config |
| `.env` | API keys and secrets |

## How It Works

**Every day at 3:30 PM NST:**

1. **OBSERVE**: Fetch PENDING WAIT records from market_log
2. **REASON**: Generate SQL queries to validate conditions (Llama Scout)
3. **EXECUTE**: Run queries via /reporter/run API
4. **DECIDE**: Analyze all conditions (Claude 3.5 Sonnet)
5. **ACT**: Update market_log, send Discord alerts
6. **PERSIST**: Log execution to memory

## Monitoring

**Check if timer is running:**
```bash
sudo systemctl status openclaw-wait-agent.timer
journalctl -u openclaw-wait-agent -f  # Follow logs
```

**View agent logs:**
```bash
cat ~/nepse-engine/logs/agents/wait_agent_executions.log
```

**Via dashboard:**
```
GET http://localhost:8766/dashboard/logs/list
GET http://localhost:8766/dashboard/logs/file?path=agents/wait_agent_*.log
```

## Cost Estimate

Per daily execution (3:30 PM):
- Query generation (1st new WAIT): $0.005
- Claude analysis (all WAITs): $0.020
- **Total per day**: ~$0.025
- **Per month**: ~$0.75
- **Per year**: ~$9

Well under budget.

## Troubleshooting

**Timer not running?**
```bash
sudo systemctl restart openclaw-wait-agent.timer
sudo systemctl status openclaw-wait-agent.timer
```

**Missing environment variables?**
```bash
source .env
echo $ANTHROPIC_API_KEY  # Should print your key
```

**Test API connectivity:**
```bash
curl -s http://192.168.1.200:8766/health
```

**Check agent logs:**
```bash
journalctl -u openclaw-wait-agent -n 50  # Last 50 lines
```

## Database Schema Update Required

Before running, ensure market_log has the query cache column:

```sql
ALTER TABLE market_log ADD COLUMN IF NOT EXISTS wait_condition_query TEXT;
```

## Disable Agent

**Stop timer:**
```bash
sudo systemctl stop openclaw-wait-agent.timer
```

**Disable permanently:**
```bash
sudo systemctl disable openclaw-wait-agent.timer
```

## Support

For OpenClaw documentation: https://clawdocs.org/
For agent issues: Check `journalctl -u openclaw-wait-agent`
