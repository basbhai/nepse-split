#!/usr/bin/env python3
"""
analysis/daily_check.py — once-a-day system check = deterministic checks (exact, repeatable) + an optional bounded `claude -p`
agent (web search + interpretation). Everything lands in findings_log, which the weekly review and the council already read.

  Layer 1 (always, no AI): SQL checks for stale feeds, copied price rows, open positions without a stop, regressions of the
          bugs fixed on 2026-10-03 (alpha==return, live trades flagged paper, unjournaled live closes) ... Failing check ->
          finding opened; passing check -> its open finding auto-closed.
  Layer 2 (--llm only): ONE `claude -p` call using the Claude subscription (same binary/model/scrubbed env as the analyst).
          Tools: WebSearch + WebFetch ONLY (no database, no shell, no file access). It receives the evidence in its prompt and
          returns JSON: finding triage suggestions, new findings, market-moving news.
  Guardrails in code (the agent never writes anything itself):
          - it can never close a finding (suggestions are stored in status_note; only Layer 1 or a human closes)
          - new findings: prefix 'agent_', severity capped at MED, max 5/day; HIGH news becomes a 'news_*' finding that expires in 7 days
          - any failure (CLI missing, timeout, bad JSON) is logged as a finding; nothing else changes, nothing raises
          - full transcript (prompt, thinking, tool calls, result) saved to output/daily_checks/<date>/transcript.jsonl
Default = preview (prints, writes nothing to the DB). --apply writes findings. --llm enables the agent call.
Usage: python -m analysis.daily_check [--llm] [--apply] [--seed]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "output" / "daily_checks"
STALE_DAYS, AGENT_TIMEOUT_SEC, MAX_NEW, NEWS_TTL_DAYS = 5, 600, 5, 7


def _db():
    from db.connection import _db as d
    return d()


def _scalar(sql: str, params=()):
    with _db() as cur:
        cur.execute(sql, params)
        r = cur.fetchone()
        return list(r.values())[0] if r else None


# ── Layer 1: deterministic checks. Each returns (topic_key, ok, severity, text) ─────────────────────────────────────
def _stale(table: str, where: str = "") -> tuple:
    last = _scalar(f"SELECT max(date) FROM {table} {where}")
    age = (datetime.now() - datetime.strptime(str(last)[:10], "%Y-%m-%d")).days if last else 999
    return (f"feed_stale_{table}", age <= STALE_DAYS, "MED", f"{table} last updated {last} ({age} days ago, limit {STALE_DAYS}).")


def check_copied_days():
    cut = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    with _db() as cur:
        cur.execute("SELECT DISTINCT date FROM price_history WHERE date >= %s", (cut,)); ph = {r["date"][:10] for r in cur.fetchall()}
        cur.execute("SELECT date, current_value FROM nepse_indices WHERE index_id='58' AND date >= %s ORDER BY date", (cut,)); ix = cur.fetchall()
    trading = sum(1 for a, b in zip(ix, ix[1:]) if a["current_value"] != b["current_value"]) + 1
    ratio = len(ph) / max(trading, 1)
    return ("price_history_copied_rows", ratio <= 1.15, "LOW", f"price_history has {len(ph)} distinct days vs {trading} real trading days in the last 60 days (x{ratio:.2f}); weekend/holiday rows are copies.")


def check_open_stops():
    with _db() as cur:
        cur.execute("SELECT symbol FROM portfolio WHERE status='OPEN' AND coalesce(stop_level,'')=''"); s = [r["symbol"] for r in cur.fetchall()]
    return ("open_positions_missing_stop", not s, "MED", f"Open live positions with no stop_level recorded in portfolio: {', '.join(s)}. Confirm the execution monitor holds stops for them.")


def check_alpha_regression():
    cut = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%d")
    n = _scalar("SELECT count(*) FROM trade_journal WHERE result IN ('WIN','LOSS') AND exit_date >= %s AND alpha_vs_nepse = return_pct AND return_pct <> '0.0'", (cut,))
    return ("journal_alpha_equals_return", n == 0, "HIGH", f"{n} recent journal rows have alpha_vs_nepse == return_pct (the NEPSE return is not being netted; entry index missing).")


def check_paper_flag():
    n = _scalar("SELECT count(*) FROM trade_journal t WHERE t.paper_mode='true' AND EXISTS (SELECT 1 FROM portfolio p WHERE p.symbol=t.symbol AND p.exit_date=t.exit_date)")
    return ("journal_live_flagged_paper", n == 0, "MED", f"{n} journal rows matching a live portfolio position are flagged paper_mode='true'.")


def check_unjournaled():
    n = _scalar("SELECT count(*) FROM portfolio p WHERE p.status='CLOSED' AND p.exit_date >= '2026-08-01' AND NOT EXISTS (SELECT 1 FROM trade_journal t WHERE t.symbol=p.symbol AND t.exit_date=p.exit_date)")
    return ("live_close_unjournaled", n == 0, "MED", f"{n} closed live positions since 2026-08-01 have no trade_journal row.")


def check_fills():
    fills, openp = _scalar("SELECT count(*) FROM trade_fills"), _scalar("SELECT count(*) FROM portfolio WHERE status='OPEN'")
    return ("trade_fills_empty", not (fills == 0 and openp > 0), "LOW", f"trade_fills is empty while {openp} live positions are open: entry origin is inferred, not from real broker fills.")


def check_curator_age():
    f = sorted((ROOT / "output" / "study").glob("lesson_curator_2*.json"))
    age = (datetime.now() - datetime.fromtimestamp(f[-1].stat().st_mtime)).days if f else 999
    return ("lesson_curator_stale", age <= 9, "LOW", f"Last lesson-curator report is {age} days old (weekly workflow should refresh it).")


def run_checks() -> list[tuple]:
    out = []
    for fn in (lambda: _stale("international_prices"), lambda: _stale("nepse_indices", "WHERE index_id='58'"), lambda: _stale("price_history"),
               check_copied_days, check_open_stops, check_alpha_regression, check_paper_flag, check_unjournaled, check_fills, check_curator_age):
        try:
            out.append(fn())
        except Exception as e:                                   # a broken check must not hide the others
            out.append((f"check_error_{getattr(fn, '__name__', 'x')}", False, "LOW", f"check failed to run: {e}"))
    return out


def reconcile(results: list[tuple], apply: bool) -> dict:
    from analysis import findings_log as FL
    open_keys = {r["topic_key"] for r in FL.rows()}
    tally = {"opened": 0, "kept_open": 0, "closed": 0, "ok": 0}
    for key, ok, sev, text in results:
        if not ok:
            tally["kept_open" if key in open_keys else "opened"] += 1
            if apply:
                FL.add(key, text, "daily_check", sev)
        elif key in open_keys:
            tally["closed"] += 1
            if apply:
                FL.close(key, "FIXED", "check passes")
        else:
            tally["ok"] += 1
    return tally


# ── Layer 2: bounded claude -p agent (web search only) ─────────────────────────────────────────────────────────────
SYSTEM = ("You are a careful market-operations analyst for a NEPSE (Nepal Stock Exchange) trading system. You have web search only. "
          "Be conservative: report only what you can support with a source. Respond with ONE JSON object and nothing else.")


def build_prompt(results: list[tuple]) -> str:
    from analysis import findings_log as FL
    parts = [f"Date: {datetime.now():%Y-%m-%d} (Nepal). Mon-Fri trading.", "", "=== TODAY'S AUTOMATIC CHECKS ==="]
    parts += [f"- {'PASS' if ok else 'FAIL'} [{sev}] {key}: {text}" for key, ok, sev, text in results]
    parts += ["", "=== OPEN FINDINGS ===", FL.digest(30) or "none"]
    try:
        from analysis.lesson_curator import digest as cd
        parts += ["", "=== LESSON CURATOR ===", cd() or "never run"]
    except Exception:
        pass
    with _db() as cur:
        cur.execute("SELECT symbol, entry_price, stop_level FROM portfolio WHERE status='OPEN'"); pos = [dict(r) for r in cur.fetchall()]
    parts += ["", "=== OPEN LIVE POSITIONS ===", json.dumps(pos)]
    parts += ["", """=== TASK ===
1. For each open finding, say whether today's evidence changes it: suggestion = keep_open | propose_close | escalate, with one line of evidence. You can only SUGGEST; you cannot close anything.
2. Add at most 5 genuinely NEW problems visible in the checks above (not duplicates of open findings).
3. Use web search for events TODAY that could move NEPSE or the open positions: bandh/strikes, NRB or SEBON announcements, corporate actions (bonus/right/dividend/book closure) for the open symbols. Include a source URL for each item.
Return exactly:
{"finding_updates":[{"topic_key":"...","suggestion":"keep_open|propose_close|escalate","evidence":"..."}],
 "new_findings":[{"topic_key":"snake_case","finding":"...","severity":"LOW|MED|HIGH"}],
 "news":[{"headline":"...","affects":"market|sector:X|symbol:Y","severity":"LOW|MED|HIGH","url":"..."}]}"""]
    return "\n".join(parts)


def call_agent(prompt: str, day_dir: Path, timeout: int = AGENT_TIMEOUT_SEC) -> tuple[dict | None, str]:
    from AI import claude_cli as C
    binary = os.getenv("CLAUDE_CLI_BIN", C.CLAUDE_CLI_BIN)
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose", "--model", C.CLAUDE_CLI_MODEL, "--system-prompt", SYSTEM,
            "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch", "WebFetch", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    try:
        p = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd="/tmp", env=C._child_env(), timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"agent timeout after {timeout}s"
    except Exception as e:
        return None, f"agent spawn error: {e}"
    (day_dir / "transcript.jsonl").write_text(p.stdout or "")
    if p.returncode != 0:
        return None, f"agent exit {p.returncode}: {(p.stderr or p.stdout or '')[:200]}"
    final = None
    for line in (p.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "result":
            final = ev
    if not final or final.get("is_error"):
        return None, f"agent returned no usable result: {str(final)[:200]}"
    m = re.search(r"\{.*\}", re.sub(r"```(?:json)?", "", final.get("result") or ""), re.S)
    try:
        return json.loads(m.group(0)), ""
    except Exception:
        return None, "agent result was not valid JSON"


def clean_agent(d: dict) -> dict:
    """Enforce the schema and every cap; drop anything unexpected."""
    sev = lambda s, cap=None: ("MED" if s == "HIGH" and cap == "MED" else s) if s in ("LOW", "MED", "HIGH") else "LOW"
    ups = [{"topic_key": str(u.get("topic_key", ""))[:80], "suggestion": u.get("suggestion"), "evidence": str(u.get("evidence", ""))[:300]}
           for u in d.get("finding_updates", []) if isinstance(u, dict) and u.get("suggestion") in ("keep_open", "propose_close", "escalate")]
    new = []
    for n in d.get("new_findings", [])[:MAX_NEW]:
        if isinstance(n, dict) and n.get("finding"):
            k = re.sub(r"[^a-z0-9_]", "_", str(n.get("topic_key", "x")).lower())[:60]
            new.append({"topic_key": k if k.startswith("agent_") else "agent_" + k, "finding": str(n["finding"])[:400], "severity": sev(n.get("severity"), "MED")})
    news = [{"headline": str(x.get("headline", ""))[:200], "affects": str(x.get("affects", ""))[:60], "severity": sev(x.get("severity")), "url": str(x.get("url", ""))[:300]}
            for x in d.get("news", []) if isinstance(x, dict) and x.get("headline")]
    return {"finding_updates": ups, "new_findings": new, "news": news}


def apply_agent(c: dict, apply: bool) -> dict:
    from analysis import findings_log as FL
    tally = {"notes": 0, "new": 0, "news_findings": 0, "expired": 0}
    open_keys = {r["topic_key"] for r in FL.rows()}
    day = datetime.now().strftime("%Y%m%d")
    if apply:
        for u in c["finding_updates"]:
            if u["topic_key"] in open_keys and u["suggestion"] != "keep_open":
                with _db() as cur:
                    cur.execute("UPDATE findings_log SET status_note=%s WHERE topic_key=%s AND status='OPEN'", (f"agent suggests {u['suggestion']}: {u['evidence']}"[:400], u["topic_key"]))
                tally["notes"] += 1
        for n in c["new_findings"]:
            FL.add(n["topic_key"], n["finding"], "daily_agent", n["severity"]); tally["new"] += 1
        for i, x in enumerate([x for x in c["news"] if x["severity"] == "HIGH"][:5]):
            FL.add(f"news_{day}_{i}", f"{x['headline']} (affects {x['affects']}) {x['url']}", "daily_agent_news", "HIGH"); tally["news_findings"] += 1
        cutoff = (datetime.now() - timedelta(days=NEWS_TTL_DAYS)).strftime("%Y-%m-%d")
        for r in FL.rows():
            if r["topic_key"].startswith("news_") and r["first_seen"] < cutoff:
                FL.close(r["topic_key"], "WONT_FIX", "news item expired"); tally["expired"] += 1
    return tally


SESSION_FINDINGS = [  # (key, status, severity, text) — carried over from the 2026-10-03 review
    ("v2_geometry_negative_after_fees", "OPEN", "HIGH", "v2 setups: 88% target hit rate is ~break-even after 1.24% fee because targets are tiny vs stops. Council rule: target gap >= 0.136*stop + 1.41%. Shadow-test it on every new setup."),
    ("rr_gate_experiment_unevaluated", "OPEN", "HIGH", "RISK_REWARD_GATE_ENABLED=false since 2026-08-17 'to observe'; avg BUY risk/reward fell 1.83 -> 0.44 (confounded with the v2 engine launch). Never compared; decide keep/restore."),
    ("council_reads_paper_book", "OPEN", "MED", "Council loads paper_portfolio when PAPER_MODE=true, so live positions (IGI, MEN, SHL) are never reviewed."),
    ("dxy_signal_not_validated", "OPEN", "MED", "DXY 7-day-lag signal does not reproduce (32 tests 2020-2026, none q<0.05; ~0 after 2023). Remove 'validated' wording from prompts; international_prices feed is also stale."),
    ("nepal_score_entry_blank", "OPEN", "MED", "nepal_score_entry/geo_score_entry blank on historical journal rows, so nepal_delta is wrong and loss_cause cannot be re-derived reliably."),
    ("nepal_score_positive_on_negative_breadth", "OPEN", "MED", "nepal_score prints +4/+5 on sessions where decliners lead; the divergence has repeated since 08-13 without a fix."),
    ("lessons_mostly_untestable", "OPEN", "MED", "12 of 16 active lessons use ADD_TO_REASONING/MONITOR, so no outcome test is possible; only 4 are testable and none has 20 informative days yet."),
    ("exit_index_uses_latest_row", "OPEN", "LOW", "_compute_deltas takes the latest index row at write time, not the exit-date close; same-day exits written before the close are off by a day."),
    ("bonus_adjusted_sector_index_untested", "OPEN", "LOW", "Commercial Banks / Life Insurance reversal (rho -0.25..-0.36) may be a bonus-share artifact; rerun on adjusted indexes with 10,000 permutations."),
    ("journal_alpha_bug_2026_10_03", "FIXED", "HIGH", "alpha_vs_nepse equalled return_pct on all 30 rows; backfilled and fixed at write time."),
    ("lessons_74_95_double_count", "FIXED", "MED", "74 and 95 double-penalised the same hydro MACD trades; 95 retired."),
    ("council_silent_empty_stages", "FIXED", "HIGH", "May/Jun/Aug councils ran with empty stages; fail-fast + logging + resume added."),
]


def seed(apply: bool) -> int:
    from analysis import findings_log as FL
    have = {r["topic_key"] for r in FL.rows(True)}
    todo = [s for s in SESSION_FINDINGS if s[0] not in have]
    if apply:
        for key, status, sev, text in todo:
            FL.add(key, text, "session_2026-10-03", sev)
            if status != "OPEN":
                FL.close(key, status, "resolved 2026-10-03")
    return len(todo)


def run(llm: bool = False, apply: bool = False, do_seed: bool = False, echo: bool = True) -> dict:
    """Callable entry point (the summary workflow uses this). Returns a small result dict; never raises on agent failure."""
    say = print if echo else (lambda *a, **k: None)
    day_dir = OUT / datetime.now().strftime("%Y%m%d"); day_dir.mkdir(parents=True, exist_ok=True)
    res = {}
    if do_seed:
        res["seeded"] = seed(apply); say(f"seed: {res['seeded']} findings {'written' if apply else 'would be written (preview)'}")
    results = run_checks()
    (day_dir / "checks.json").write_text(json.dumps(results, indent=1, default=str))
    for key, ok, sev, text in results:
        say(f"{'PASS' if ok else 'FAIL'} [{sev}] {key}: {text if not ok else ''}")
    res["layer1"] = reconcile(results, apply); say("layer 1:", res["layer1"], "(applied)" if apply else "(preview)")
    if llm:
        from analysis import findings_log as FL
        prompt = build_prompt(results); (day_dir / "prompt.txt").write_text(prompt)
        parsed, err = call_agent(prompt, day_dir)
        if parsed is None:
            res["agent"] = {"error": err}; say("agent FAILED:", err)
            if apply:
                FL.add("daily_agent_failed", f"Daily agent run failed: {err}", "daily_check", "LOW")
        else:
            clean = clean_agent(parsed); (day_dir / "agent_result.json").write_text(json.dumps(clean, indent=1))
            res["agent"] = apply_agent(clean, apply)
            say("layer 2:", {k: len(v) for k, v in clean.items()}, res["agent"], "(applied)" if apply else "(preview)")
            if apply and "daily_agent_failed" in {r["topic_key"] for r in FL.rows()}:
                FL.close("daily_agent_failed", "FIXED", "agent ran OK")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true"); ap.add_argument("--apply", action="store_true"); ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    run(a.llm, a.apply, a.seed)


if __name__ == "__main__":
    main()
