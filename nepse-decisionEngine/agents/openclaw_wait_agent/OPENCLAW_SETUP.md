# OpenClaw Wait-Agent Setup for NEPSE Engine

## What You Have

1. **nepse_mcp_server.py** — MCP server that exposes nepse-engine as tools
2. **wait_agent_prompt.md** — Agent instructions for validating WAITs
3. **.env** — Configuration with API keys
4. **openclaw** CLI — Already installed and configured

## How to Run

### Option 1: Direct Agent Call (Manual)

```bash
cd ~/nepse-engine/agents/openclaw_wait_agent
source .env

# Run agent with custom instructions
openclaw agent --message "$(cat wait_agent_prompt.md)" --deliver
```

This will:
- Use OpenRouter + Claude
- Execute the wait validation task
- Fetch pending WAITs
- Check conditions
- Update market_log
- Send alerts

### Option 2: Scheduled Daily (15:30 NST)

First, create a cron job:

```bash
crontab -e

# Add this line:
30 15 * * * cd ~/nepse-engine/agents/openclaw_wait_agent && source .env && openclaw agent --message "$(cat wait_agent_prompt.md)" --deliver > /tmp/wait_agent_$(date +\%Y\%m\%d_\%H\%M\%S).log 2>&1
```

Or use OpenClaw's native cron:

```bash
openclaw cron add "30 15 * * *" "cd ~/nepse-engine/agents/openclaw_wait_agent && source .env && openclaw agent --message \$(cat wait_agent_prompt.md) --deliver"
```

### Option 3: Test First (Dry Run)

```bash
cd ~/nepse-engine/agents/openclaw_wait_agent
source .env

# Just chat with agent, don't commit changes
openclaw agent --message "List pending WAITs without updating anything" --deliver
```

## Tools Available to Agent

The agent has access to these functions via the MCP server:

- `get_pending_waits()` — Fetch all PENDING v2 WAITs
- `check_wait_condition(symbol, target, stop, since_date)` — Check if condition met
- `get_latest_prices(symbols)` — Get current prices
- `update_wait_outcome(id, outcome, exit_price, exit_date)` — Update market_log
- `send_alert(message)` — Send Telegram notification

## Monitoring

### View logs:
```bash
tail -f /tmp/wait_agent_*.log
```

### Check if cron job runs:
```bash
grep CRON /var/log/syslog | tail -20
```

### Manually trigger:
```bash
cd ~/nepse-engine/agents/openclaw_wait_agent && source .env && openclaw agent --message "$(cat wait_agent_prompt.md)" --deliver
```

## Troubleshooting

### Agent can't access database
- Check `.env` has `REPORTER_API_URL=http://localhost:8766`
- Test with: `curl http://localhost:8766/health`

### Agent can't send Telegram alerts
- Check `.env` has `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`
- Test with manual message

### Agent running but not updating market_log
- Check logs for errors
- Verify agent has permission to UPDATE market_log
- Test MCP server directly: `python3 nepse_mcp_server.py`

## Next Steps

1. **Test manually first:**
   ```bash
   cd ~/nepse-engine/agents/openclaw_wait_agent
   source .env
   openclaw agent --message "$(cat wait_agent_prompt.md)" --deliver
   ```

2. **If working, schedule it:**
   ```bash
   openclaw cron add "30 15 * * *" "cd ~/nepse-engine/agents/openclaw_wait_agent && source .env && openclaw agent --message \$(cat wait_agent_prompt.md) --deliver"
   ```

3. **Monitor first execution:**
   ```bash
   watch -n 5 'openclaw cron list'
   ```

Done! Agent runs daily at 3:30 PM.
