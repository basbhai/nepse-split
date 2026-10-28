#!/usr/bin/env python3
"""
analysis/test_daily_check.py — runnable check (python -m analysis.test_daily_check). NO real AI call: a fake `claude` script stands in.
Covers: layer-1 reconcile (open/auto-close), agent argv safety, guardrails (cannot close, caps, prefixes, severity cap, news expiry),
and every failure mode (exit!=0, non-JSON, is_error, timeout). All mock rows are removed in `finally`.
"""
import json
import os
import stat
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis import daily_check as D, findings_log as FL  # noqa: E402
from db.connection import _db  # noqa: E402

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
open(os.environ["FAKE_ARGV"], "w").write(json.dumps(sys.argv[1:]))
sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "good")
if mode == "exit1": sys.stderr.write("boom"); sys.exit(1)
if mode == "sleep": time.sleep(5)
print(json.dumps({"type": "system", "subtype": "init"}))
print(json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "weighing evidence"}, {"type": "tool_use", "name": "WebSearch", "input": {"query": "nepal bandh"}}]}}))
good = {"finding_updates": [{"topic_key": "QA_DC_A", "suggestion": "propose_close", "evidence": "check passes now"},
                            {"topic_key": "QA_DC_B", "suggestion": "close", "evidence": "invalid suggestion must be dropped"},
                            {"topic_key": "QA_DC_C", "suggestion": "keep_open", "evidence": "no change"}],
        "new_findings": [{"topic_key": "qa Weird Key! %d" % i, "finding": "n%d" % i, "severity": "HIGH"} for i in range(7)],
        "news": [{"headline": "QA strike announced", "affects": "market", "severity": "HIGH", "url": "http://x"},
                 {"headline": "QA minor note", "affects": "sector:Hydro", "severity": "LOW", "url": "http://y"}]}
text = {"good": "```json\n" + json.dumps(good) + "\n```", "nonjson": "I could not do that", "iserror": "x"}.get(mode, "")
print(json.dumps({"type": "result", "is_error": mode == "iserror", "result": text, "total_cost_usd": 0.0, "num_turns": 2}))
'''

res = []
def check(name, cond, detail=""):
    res.append(bool(cond)); print(("PASS " if cond else "FAIL ") + name, detail)

def rows(prefix=None):
    return {r["topic_key"]: r for r in FL.rows(True) if prefix is None or r["topic_key"].startswith(prefix)}

with _db() as _c:
    _c.execute("SELECT coalesce(max(id), 0) m FROM findings_log"); START_ID = _c.fetchone()["m"]


def cleanup():
    """Remove everything created during this run (by id), so real pre-existing findings are never touched."""
    with _db() as cur:
        cur.execute("DELETE FROM findings_log WHERE id > %s", (START_ID,))


tmp = Path(tempfile.mkdtemp()); D.OUT = tmp / "out"
fake = tmp / "fake_claude.py"; fake.write_text(FAKE); fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
os.environ["CLAUDE_CLI_BIN"] = str(fake); os.environ["FAKE_ARGV"] = str(tmp / "argv.json")
try:
    cleanup()
    # ---- layer 1 -------------------------------------------------------------------------------------------------
    live = D.run_checks()
    check("all 10 checks ran without crashing", len(live) == 10 and not any(k.startswith("check_error") for k, *_ in live), str([k for k, ok, *_ in live if not ok]))
    bad = [("QA_DC_A", False, "MED", "a"), ("QA_DC_B", False, "LOW", "b"), ("QA_DC_C", False, "LOW", "c")]
    check("preview writes nothing", D.reconcile(bad, apply=False)["opened"] == 3 and not rows("QA_DC"))
    check("apply opens findings", D.reconcile(bad, apply=True)["opened"] == 3 and len(rows("QA_DC")) == 3)
    check("second run keeps them open (no duplicates)", D.reconcile(bad, apply=True)["kept_open"] == 3 and rows("QA_DC")["QA_DC_A"]["times_seen"] == "2")
    D.reconcile([("QA_DC_C", True, "LOW", "")], apply=True)
    check("passing check auto-closes its finding", rows("QA_DC")["QA_DC_C"]["status"] == "FIXED")
    D.reconcile([("QA_DC_C", False, "LOW", "c")], apply=True)
    check("a regression reopens it", rows("QA_DC")["QA_DC_C"]["status"] == "OPEN")

    # ---- layer 2: agent -------------------------------------------------------------------------------------------
    prompt = D.build_prompt(live)
    check("prompt carries checks + open findings", "TODAY'S AUTOMATIC CHECKS" in prompt and "QA_DC_A" in prompt and "OPEN LIVE POSITIONS" in prompt)
    os.environ["FAKE_MODE"] = "good"; tmp.joinpath("out").mkdir(exist_ok=True)
    parsed, err = D.call_agent(prompt, tmp / "out")
    argv = json.loads((tmp / "argv.json").read_text())
    check("agent parsed from fenced JSON", parsed is not None and not err, err)
    check("agent restricted to WebSearch/WebFetch (no Bash/Edit/Write/MCP)", argv[argv.index("--tools") + 1] == "WebSearch,WebFetch" and "Bash" not in " ".join(argv) and '{"mcpServers":{}}' in argv)
    check("transcript saved (thinking + tool call visible)", "weighing evidence" in (tmp / "out" / "transcript.jsonl").read_text())
    clean = D.clean_agent(parsed)
    check("invalid suggestion ('close') dropped", {u["suggestion"] for u in clean["finding_updates"]} == {"propose_close", "keep_open"})
    check("new findings capped at 5, prefixed agent_, HIGH->MED", len(clean["new_findings"]) == 5 and all(n["topic_key"].startswith("agent_") and n["severity"] == "MED" for n in clean["new_findings"]))
    t = D.apply_agent(clean, apply=True)
    r = rows()
    check("agent can NOT close: finding stays OPEN, suggestion stored as note", r["QA_DC_A"]["status"] == "OPEN" and "agent suggests propose_close" in (r["QA_DC_A"]["status_note"] or ""))
    check("keep_open leaves no note", not (r["QA_DC_C"]["status_note"] or "").startswith("agent suggests"))
    check("5 agent findings written", t["new"] == 5 and sum(1 for k in r if k.startswith("agent_")) >= 5)
    day = datetime.now().strftime("%Y%m%d")
    check("HIGH news -> finding; LOW news -> no finding", f"news_{day}_0" in r and f"news_{day}_1" not in r and t["news_findings"] == 1)
    with _db() as cur:
        cur.execute("UPDATE findings_log SET first_seen=%s WHERE topic_key=%s", ((datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"), f"news_{day}_0"))
    D.apply_agent({"finding_updates": [], "new_findings": [], "news": []}, apply=True)
    check("news finding expires after 7 days", rows()[f"news_{day}_0"]["status"] == "WONT_FIX")
    n_before = len(rows())
    D.apply_agent(clean, apply=False)
    check("apply_agent preview writes nothing", len(rows()) == n_before)

    # ---- failure modes: never raise, record one finding, change nothing else -----------------------------------------
    for mode, expect in (("exit1", "exit 1"), ("nonjson", "not valid JSON"), ("iserror", "no usable result")):
        os.environ["FAKE_MODE"] = mode
        p, e = D.call_agent(prompt, tmp / "out")
        check(f"failure mode '{mode}' returns None + reason", p is None and expect in e, e)
    os.environ["FAKE_MODE"] = "sleep"
    p, e = D.call_agent(prompt, tmp / "out", timeout=1)
    check("timeout handled", p is None and "timeout" in e, e)
    os.environ["FAKE_MODE"] = "exit1"
    before = {k: v["status"] for k, v in rows().items() if k != "daily_agent_failed"}
    out = D.run(llm=True, apply=True, echo=False)
    check("run(): agent failure -> daily_agent_failed finding, nothing else changed", "error" in out["agent"] and rows()["daily_agent_failed"]["status"] == "OPEN" and
          all(rows()[k]["status"] == s for k, s in before.items() if k in rows()))
    os.environ["FAKE_MODE"] = "good"
    out = D.run(llm=True, apply=True, echo=False)
    check("run(): later success closes daily_agent_failed", rows()["daily_agent_failed"]["status"] == "FIXED" and out["agent"]["new"] >= 1)
    n = len(rows()); D.run(llm=True, apply=False, echo=False)
    check("run(apply=False) writes nothing", len(rows()) == n)
    check("seed: preview counts, apply is idempotent", D.seed(False) >= 1 and D.seed(True) >= 1 and D.seed(True) == 0)
finally:
    cleanup()
    with _db() as cur:
        cur.execute("SELECT count(*) c FROM findings_log WHERE id > %s", (START_ID,)); left = cur.fetchone()["c"]
    check("every row created by this test removed", left == 0, f"left={left}")
    print(f"{sum(res)}/{len(res)} checks passed"); sys.exit(0 if all(res) else 1)
