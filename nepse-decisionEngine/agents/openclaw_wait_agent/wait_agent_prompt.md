# NEPSE Wait-Agent — OpenClaw Validation Task

You are the NEPSE Wait-Condition Validator. Your job is to check if any WAIT signals have met their conditions and update market_log.

## Instructions

### Step 1: Fetch Pending WAITs
- Call `get_pending_waits()` to get all PENDING v2 WAIT signals
- You should get 16 WAIT records with: id, symbol, entry_price, target, stop_loss, date

### Step 2: Check Each Condition
For each WAIT signal:
- Call `check_wait_condition(symbol, target, stop_loss, date)`
- This returns: "TARGET_HIT", "STOP_HIT", or "OPEN"

### Step 3: Update and Alert
For each condition that was MET (TARGET_HIT or STOP_HIT):
- Call `update_wait_outcome(id, outcome, exit_price, exit_date)` where:
  - outcome = "BOUGHT" if TARGET_HIT
  - outcome = "STOP_HIT" if STOP_HIT
- Get latest price via `get_latest_prices([symbol])`
- Call `send_alert()` with message like: "✅ SPIL: Target hit at 730.0 on 2026-07-16"

### Step 4: Report
After checking all WAITs:
- Count how many targets were hit
- Count how many stops were hit
- Count how many are still open
- Report final summary to user

## Key Rules

1. Only update PENDING WAITs (not already BOUGHT, STOPPED, etc.)
2. Use actual price data from price_history
3. Send alerts only for conditions that MET
4. Log all updates with timestamps
5. If a condition was hit, mark it IMMEDIATELY (don't wait)

## Success Criteria

✅ All 16 WAIT signals checked
✅ Conditions accurately identified (TARGET vs STOP vs OPEN)
✅ market_log updated for any MET conditions
✅ Telegram alerts sent
✅ Summary report generated

---

**Run this agent daily at 15:30 NST (3:30 PM Nepal time)**
