# Production vs Split — Gap Analysis
*SSH audit of shanvi@192.168.1.200:~/nepse-engine · 2026-10-27*

---

## Production State

| Item | Value |
|---|---|
| Host | 192.168.1.200 |
| User | shanvi |
| Path | ~/nepse-engine |
| DB name | **nepse_engine** (not `nepse_decision_engine`) |
| Python | 3.12.3 |
| Latest commit | `ab2f881` 2026-10-26 — Capture real ATrad fills nightly |
| Running services | nepse-bot, nepse-dashboard, nepse-discord-bot, nepse-monitor |

---

## DB Tables: Production vs Split Schema

### In production `nepse_engine` but NOT in split schema

| Table | Status | Notes |
|---|---|---|
| `trade_fills` | 🔴 **MISSING from split** | New Oct-26 feature — real ATrad fill capture. Schema: order_id, exchange_order_id, symbol, side, qty, price, value, trade_ts, trade_date, entry_origin, origin_ref_id, origin_days |
| `portfolio` | 🔴 **MISSING from split** | Live portfolio tracking (not paper). Columns: symbol, entry_date, entry_price, shares, total_cost, current_price, current_value, pnl_npr, pnl_pct, peak_price, stop_type, stop_level, trail_active, trail_stop, status, exit_date, exit_price |
| `corporate_events` | ⚪ Orphaned | No live writer/reader — safe to ignore |
| `dividend_announcements` | ⚪ Orphaned | No live writer/reader — safe to ignore |
| `dividend_pattern_study` | ⚪ Orphaned | No live writer/reader — safe to ignore |
| `international_prices` | ⚪ Orphaned | No live writer/reader — safe to ignore |
| `macro_stat_results` | ⚪ Orphaned | No live writer/reader — safe to ignore |
| `sector_momentum` | ⚪ Orphaned | No live writer/reader — safe to ignore |

### In split schema but NOT in production (new additions)

| Table | Status | Notes |
|---|---|---|
| `wait_parse_log` | 🟡 New | LLM parse attempts for wait_condition — not yet in prod DB |
| `stealth_signals` | 🟡 New | Quiet broker accumulation lifecycle — appears in prod actually ✓ |

---

## Python Files: Production vs Split

### Production modules NOT in `nepse-decisionEngine/modules/`

| File | Purpose | Priority |
|---|---|---|
| `atrad_mcp_server.py` | MCP server for ATrad API | LOW — MCP tooling |
| `backup_sync.py` | Local→Neon backup push | LOW — broken service (wrong venv path) |
| `broker_flow_scraper.py` | Broker flow data scraper | **HIGH** — feeds broker_flow table daily |
| `candle_detector.py` | EOD candle pattern detection | **HIGH** — called by summary_workflow |
| `claude_bot_guard_hook.py` | Claude Code hook guard | LOW — dev tooling |
| `docs_mcp_server.py` | MCP server for docs | LOW — dev tooling |
| `email_mcp_server.py` | MCP server for email | LOW — dev tooling |
| `event_detector.py` | Event/corporate action detection | MED |
| `floorsheet_scraper.py` | Floorsheet raw data pull | **HIGH** — called by summary_workflow |
| `floorsheet_signals.py` | Aggregate floorsheet to signals | **HIGH** — called by summary_workflow |
| `fundamentals_scraper.py` | Quarterly fundamentals fetch | MED |
| `geo_sentiment.py` | VIX/Crude/Nifty geo risk | **HIGH** — injected into every AI analysis |
| `hidden_accum_scanner.py` | Hidden accumulation scanner | MED — stealth_signals feature |
| `history_bootstrap.py` | Historical OHLCV bootstrap | MED — one-shot on setup |
| `indicators.py` | Technical indicator calculator | **HIGH** — RSI/MACD/EMA/BB/OBV computed here |
| `interest_scraper.py` | FD/interest rate scraper | LOW |
| `intraday_indicator_refresh.py` | Mid-session indicator refresh | MED |
| `nepal_pulse.py` | Nepal macro event data | **HIGH** — macro context for AI |
| `scraper.py` | Main TMS/NEPSE data scraper | **HIGH** — EOD OHLCV |
| `sharehub_scraper.py` | Sharehub broker data | MED |
| `share_sectors_sync.py` | Share→sector mapping sync | MED |
| `spring_loading_screener.py` | Spring loading pattern screen | MED |
| `tms_scraper.py` | Legacy TMS49 scraper | MED |
| `trade_origin.py` | Trade origin tracking | MED — new Oct-03 feature |
| `wait_condition_writer.py` | WAIT condition parser/writer | **HIGH** — writes wait_condition_parsed to market_log |

### Production root files NOT in `nepse-decisionEngine/`

| File | Purpose | Priority |
|---|---|---|
| `filter_engine.py` | v1/v2/v3 engine coordinator | **CRITICAL** — this IS the trading loop |
| `filter_v1.py` | Filter engine v1 | **CRITICAL** |
| `filter_v2.py` | Filter engine v2 | **CRITICAL** |
| `filter_v3.py` | Filter engine v3 | **CRITICAL** |
| `gemini_filter.py` | Gemini pre-screen filter | **CRITICAL** — called before Claude analysis |

### Production `analysis/` files NOT in split

| File | Purpose | Priority |
|---|---|---|
| `accuracy_review.py` | Monthly DeepSeek accuracy analysis | MED |
| `gate_miss_tracker.py` | Gate miss outcome tracker | MED |
| `learning_hub_audit.py` | Learning hub quality audit | MED |
| `spring_loading_precision.py` | Spring loading precision analysis | LOW |
| `test_enricher.py` | Test enricher for analysis | LOW |

---

## Critical Gaps for Shadow Live

These must be resolved before the engine produces signals:

| # | Gap | Fix |
|---|---|---|
| 1 | `filter_engine.py`, `filter_v1/v2/v3.py`, `gemini_filter.py` missing from split | Copy from production → `nepse-decisionEngine/` |
| 2 | `trade_fills` table missing from split schema | Add to `nepse-decisionEngine/schema.prisma` + codegen + migration |
| 3 | `portfolio` table missing from split schema | Add to `nepse-decisionEngine/schema.prisma` (live portfolio — engine-owned) |
| 4 | All HIGH-priority modules missing (indicators.py, scraper.py, geo_sentiment.py, etc.) | Copy from production |
| 5 | Production DB is `nepse_engine` but split expects `nepse_decision_engine` | Either rename DB or update split `.env` |

---

## Production env vars NOT in split `.env.example`

| Key | Notes |
|---|---|
| `GEMINI_API_KEY_1/2/3` | Multi-key round-robin — missing from split example |
| `ATRAD_SIM_ENABLED` | ATrad simulation flag |
| `NEPSE_API_KEY` | NEPSE public API key |
| `PORTFOLIO_API_KEY` | Portfolio API key |
| `PORTFOLIO_API_URL` | Portfolio API URL |
| `CLAUDE_DISABLE_REASONING` | Toggle for Claude extended thinking |
| `TMS_ROADSHOW_WATCH_ID` | Watch account ID |
| `LOCAL_DATABASE_URL` | Pinned local DB (separate from DATABASE_URL) |

---

## Database Migration Sequence (production cutover)

```
PRE-MIGRATION (on dev machine):
1. Add trade_fills + portfolio to nepse-decisionEngine/schema.prisma
2. Rerun codegen → db/schema.py
3. Run migrations against nepse_decision_engine (local dev)
4. Add seed_settings() to nepse-trading/db/migrations.py
5. Test full round-trip: signal push → webhook → paper execute

CUTOVER (maintenance window, ~3:45 PM NST after EOD):
1. Stop all services: nepse-bot, nepse-discord-bot, nepse-monitor, nepse-dashboard
2. pg_dump paper_users, paper_capital, paper_portfolio, paper_trade_log, trade_journal
3. Create nepse_trading database on production
4. Restore 5 tables → nepse_trading; run setval() for all sequences
5. Run nepse-trading/db/migrations.py → creates all 13 trading tables
6. Run nepse-decisionEngine/db/migrations.py against nepse_engine (rename or alias)
7. Set DATABASE_URL in each service's .env
8. Start trading service: python main.py (ports 8767+8768)
9. Start engine: uvicorn web_ui.dashboard_api:app --port 8766
10. Smoke test: POST /api/v1/webhook/signal with test payload
11. Verify paper_capital totals match pre-migration snapshot

POST-MIGRATION:
- Monitor execution_monitor LTP REST calls
- Monitor auditor.py trade_journal writes
- After 1 week: plan paper_users drop (after bot query audit)
```
