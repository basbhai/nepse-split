# Daily 3:30 PM Heartbeat — Wait Condition Validator

## Scheduled Execution Time
**15:30:00 NST (3:30 PM)**

## Execution Context
- Market hours: CLOSED (3 PM NST close)
- All EOD data finalized
- Ready for next-day decisions

## Workflow

### 1. OBSERVE — Fetch Conditions
```
Action: Fetch PENDING WAIT records from market_log
Query: SELECT id, symbol, entry_price, target, stop_loss, 
       wait_condition_parsed, confidence, created_at
       FROM market_log
       WHERE action='WAIT' AND outcome='PENDING_CLAUDE'
       ORDER BY created_at DESC
```

### 2. REASON — Generate/Retrieve Queries
For each WAIT condition:
```
a. Check if wait_condition_query is cached
b. If NOT cached:
   - Call Llama Scout to generate SQL query
   - Validate query (no destructive keywords)
   - Save to market_log.wait_condition_query
   - Cost: ~$0.005
c. If cached:
   - Use existing query (cost: $0)
```

### 3. EXECUTE — Collect Data
For each WAIT condition:
```
a. Execute query via POST /reporter/run
b. Collect results (price, volume, indicators)
c. Store in memory for analysis
d. Log execution trace
```

### 4. DECIDE — Analyze All Conditions
```
Single Claude call:
- Input: All WAIT conditions + fetched data + market context
- Model: claude-3-5-sonnet-20241022
- Output: [{"id": 288, "symbol": "KHPL", "condition_met": true, ...}]
- Cost: ~$0.02
```

### 5. ACT — Update & Alert
For each condition_met=true:
```
a. UPDATE market_log SET outcome='BOUGHT', remark=<Claude remark>
b. POST Discord webhook → Send alert
c. Log execution trace
```

### 6. PERSIST — Update Memory
```
a. Update HEARTBEAT.md with execution results
b. Create logs/wait_agent_YYYY_MM_DD.md execution trace
c. Store query cache in market_log.wait_condition_query
```

## Success Criteria
- [x] Fetched all PENDING WAIT records
- [x] Generated/executed queries successfully
- [x] Claude analyzed conditions
- [x] Updated market_log for conditions met
- [x] Sent Discord notifications
- [x] Logged execution trace

## Cost Per Execution
- Query generation (1st new WAIT): $0.005
- Claude analysis (all WAITs): $0.020
- Total per heartbeat: ~$0.025

## Monitoring
- Check logs: GET /dashboard/logs/list
- View details: GET /dashboard/logs/file?path=agents/wait_agent_*.md
- Verify updates: Check market_log outcomes

## Next Execution
- Scheduled: 15:30:00 NST (next day)
- Timer: /etc/systemd/system/openclaw-wait-agent.timer
