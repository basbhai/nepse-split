# Pre-Deployment Checklist

## Before Running Agent

### ✅ Prerequisites
- [ ] OpenClaw CLI installed (`openclaw --version` works)
- [ ] Anthropic API key obtained (https://console.anthropic.com)
- [ ] OpenRouter API key obtained (https://openrouter.ai)
- [ ] Discord webhook URL created
- [ ] Network access to 192.168.1.200:8766 (reporter API)

### ✅ Configuration
- [ ] `.env` file created with all keys filled in
- [ ] `config/SOUL.md` reviewed
- [ ] `config/AGENTS.md` reviewed
- [ ] `config/HEARTBEAT.md` reviewed
- [ ] `config/openclaw.json` reviewed
- [ ] `openclaw-wait-agent.service` ready
- [ ] `openclaw-wait-agent.timer` ready

### ✅ Database
- [ ] market_log table has `wait_condition_query` column
- [ ] Can connect to reporter API
- [ ] SELECT query to market_log works

### ✅ Testing
- [ ] API keys work (test via curl or OpenRouter/Anthropic dashboard)
- [ ] Reporter API healthy (`curl http://192.168.1.200:8766/health`)
- [ ] Discord webhook works (test with curl)
- [ ] Dry run executes without errors (`openclaw run --agent wait-agent --config config/openclaw.json --dry-run`)

### ✅ System Setup
- [ ] Systemd service file in place
- [ ] Systemd timer file in place
- [ ] `systemctl daemon-reload` executed
- [ ] Timer enabled (`sudo systemctl enable openclaw-wait-agent.timer`)
- [ ] Timer started (`sudo systemctl start openclaw-wait-agent.timer`)
- [ ] Timer shows in `systemctl list-timers`

### ✅ Monitoring
- [ ] Can view logs (`journalctl -u openclaw-wait-agent`)
- [ ] Can access dashboard (`http://localhost:8766/dashboard/logs/list`)
- [ ] Discord channel ready for notifications

## First Execution

- [ ] Wait until 3:30 PM NST
- [ ] Agent should run automatically
- [ ] Check Discord for alert (if condition met)
- [ ] Verify market_log updated (`curl ... /reporter/run`)
- [ ] Check logs for execution trace

## Deployment Approval

| Item | Status | Notes |
|------|--------|-------|
| All prerequisites met | [ ] | |
| All configuration reviewed | [ ] | |
| Database updated | [ ] | |
| All tests passed | [ ] | |
| Systemd installed | [ ] | |
| First execution verified | [ ] | |

**Ready to deploy**: ______________________ (Date)

## Rollback Plan

If issues occur:
1. Stop timer: `sudo systemctl stop openclaw-wait-agent.timer`
2. Check logs: `journalctl -u openclaw-wait-agent -n 100`
3. Fix issue
4. Re-enable: `sudo systemctl start openclaw-wait-agent.timer`

## Support Contacts

- OpenClaw docs: https://clawdocs.org/
- Anthropic support: https://support.anthropic.com
- OpenRouter support: https://openrouter.ai/support
