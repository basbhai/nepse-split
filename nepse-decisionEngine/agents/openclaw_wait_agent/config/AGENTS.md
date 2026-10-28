# Agent: wait-agent

## Identity
- Name: NEPSE Wait-Condition Validator
- Role: Autonomous market analyst
- Environment: Production
- Timezone: Asia/Kathmandu (NST)

## Models & LLM Integration
### Query Generation
- Provider: OpenRouter
- Model: meta-llama/llama-4-scout
- Cost: $0.10 input / $0.30 output per 1M tokens
- Usage: Generate SQL queries for WAIT condition validation
- Per query cost: ~$0.005

### Analysis & Decision Making
- Provider: Anthropic
- Model: claude-3-5-sonnet-20241022
- Cost: $3 input / $15 output per 1M tokens
- Usage: Analyze all conditions with market context
- Per heartbeat cost: ~$0.02

## Execution Schedule
- Frequency: Daily
- Time: 15:30:00 NST (3:30 PM, after market close)
- Timeout: 60 seconds per run
- Max records per run: 50 WAIT conditions

## Tools & Skills
- `skill/query_market_log.py` — Fetch PENDING WAIT records
- `skill/generate_query.py` — Generate SQL via Llama Scout
- `skill/execute_reporter_query.py` — Execute via /reporter/run
- `skill/fetch_market_context.py` — Get market state/scores
- `skill/update_market_log.py` — Update outcomes
- `skill/send_discord.py` — Send notifications

## Channels
- Discord: Send alerts on condition met
- Markdown: Persist execution logs

## Error Handling
- Retry failed queries (3 attempts max)
- Log errors to memory
- Alert on critical failures
- Continue on non-critical errors
- Never crash or exit without logging

## Performance Targets
- Query generation: <10 seconds
- Data collection: <20 seconds
- Claude analysis: <10 seconds
- Execution: <5 seconds
- Total per run: <45 seconds
