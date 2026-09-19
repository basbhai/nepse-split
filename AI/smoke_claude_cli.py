#!/usr/bin/env python3
"""
AI/smoke_claude_cli.py
======================
Phase 3 smoke test for the `claude -p` analyst backend — real CLI calls, NO DB
writes (`log_call=False`), against real historical `claude_analyst` prompts
pulled from `ai_call_log`.

    python AI/smoke_claude_cli.py            # 10 varied historical prompts, 1 call each
    python AI/smoke_claude_cli.py --variance SYMBOL_OR_ID   # same prompt x10

Gate (see nepse-project/TODO.md Phase 3):
    JSON validity 100% on the sample; p95 latency x max_flags comfortably under
    the systemd headroom; 0 exceptions.
"""
import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from AI.claude_cli import ask_claude_cli
from sheets import run_raw_sql

REQUIRED_KEYS = [
    "action", "confidence", "entry_price", "stop_loss", "target",
    "allocation_npr", "shares", "breakeven", "risk_reward",
    "suggested_hold_days", "primary_signal", "reasoning",
    "lesson_applied", "lessons_applied", "wait_condition", "herding_note",
]
VALID_ACTIONS = {"BUY", "WAIT", "AVOID"}


def _fetch(limit):
    return run_raw_sql(
        "SELECT id, symbol, system_prompt, user_prompt, response_raw "
        "FROM ai_call_log WHERE context='claude_analyst' AND success='true' "
        "ORDER BY id DESC LIMIT %s", (limit,)
    ) or []


def _fetch_one(key):
    where = "id = %s" if str(key).isdigit() else "symbol = %s"
    rows = run_raw_sql(
        f"SELECT id, symbol, system_prompt, user_prompt, response_raw "
        f"FROM ai_call_log WHERE context='claude_analyst' AND success='true' "
        f"AND {where} ORDER BY id DESC LIMIT 1", (key,)
    ) or []
    return rows[0] if rows else None


def _validate(parsed):
    problems = []
    if not isinstance(parsed, dict):
        return ["not a dict"]
    missing = [k for k in REQUIRED_KEYS if k not in parsed]
    if missing:
        problems.append(f"missing keys: {missing}")
    if parsed.get("action") not in VALID_ACTIONS:
        problems.append(f"bad action: {parsed.get('action')!r}")
    try:
        e = float(parsed.get("entry_price") or 0)
        t = float(parsed.get("target") or 0)
        if e > 0 and t > 0 and t < e and parsed.get("action") == "BUY":
            problems.append(f"BUY with target {t} < entry {e}")
    except (TypeError, ValueError):
        problems.append("entry/target not numeric")
    if not isinstance(parsed.get("lessons_applied", []), list):
        problems.append("lessons_applied not a list")
    return problems


def run_sample(limit):
    rows = _fetch(limit)
    print(f"\n=== Phase 3 smoke — {len(rows)} historical prompts, 1 CLI call each ===\n")
    lat, valid, exc = [], 0, 0
    for r in rows:
        t0 = time.monotonic()
        try:
            out = ask_claude_cli(r["user_prompt"], system=r["system_prompt"],
                                 context="claude_analyst_smoke",
                                 symbol=r["symbol"], log_call=False)
        except Exception as e:                                   # pragma: no cover
            exc += 1
            print(f"  id {r['id']:>3} {r['symbol']:<8} EXCEPTION {e}")
            continue
        dt = time.monotonic() - t0
        lat.append(dt)
        if out is None:
            print(f"  id {r['id']:>3} {r['symbol']:<8} {dt:6.1f}s  -> None (CLI failure)")
            continue
        problems = _validate(out)
        try:
            was = json.loads(r["response_raw"]).get("action")
        except Exception:
            was = "?"
        if problems:
            print(f"  id {r['id']:>3} {r['symbol']:<8} {dt:6.1f}s  action={out.get('action'):<5} "
                  f"conf={out.get('confidence')}  ISSUES: {problems}")
        else:
            valid += 1
            flag = "  " if out.get("action") == was else " *"
            print(f"  id {r['id']:>3} {r['symbol']:<8} {dt:6.1f}s  action={out.get('action'):<5} "
                  f"conf={out.get('confidence'):<3} rr={out.get('risk_reward')}  "
                  f"(openrouter said {was}){flag}")

    print(f"\n  JSON valid: {valid}/{len(rows)}   CLI failures: {len(rows)-valid-exc}   exceptions: {exc}")
    if lat:
        lat.sort()
        p95 = lat[int(len(lat) * 0.95) - 1] if len(lat) > 1 else lat[0]
        print(f"  latency  avg {statistics.mean(lat):5.1f}s   p50 {statistics.median(lat):5.1f}s   "
              f"p95 {p95:5.1f}s   max {max(lat):5.1f}s")
        print(f"  p95 x 3 flags = {p95*3:.0f}s  (systemd TimeoutStartSec now 600s; "
              f"OpenRouter cycle baseline ~124s)")


def run_variance(key, n=10):
    row = _fetch_one(key)
    if not row:
        print(f"no historical claude_analyst prompt for {key!r}")
        return
    print(f"\n=== Variance — id {row['id']} {row['symbol']} x{n} (no temperature knob) ===\n")
    actions, confs, lat = [], [], []
    for i in range(n):
        t0 = time.monotonic()
        out = ask_claude_cli(row["user_prompt"], system=row["system_prompt"],
                             context="claude_analyst_smoke", symbol=row["symbol"],
                             log_call=False)
        dt = time.monotonic() - t0
        lat.append(dt)
        if out is None:
            print(f"  run {i+1:>2} {dt:6.1f}s  -> None")
            continue
        actions.append(out.get("action"))
        try:
            confs.append(int(out.get("confidence")))
        except (TypeError, ValueError):
            pass
        print(f"  run {i+1:>2} {dt:6.1f}s  action={out.get('action'):<5} conf={out.get('confidence')}")
    print()
    from collections import Counter
    print(f"  actions: {dict(Counter(actions))}")
    if confs:
        print(f"  confidence: min {min(confs)} max {max(confs)} "
              f"mean {statistics.mean(confs):.1f} stdev {statistics.pstdev(confs):.1f}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variance", metavar="SYMBOL_OR_ID")
    ap.add_argument("--n", type=int, default=10)
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()
    if a.variance:
        run_variance(a.variance, a.n)
    else:
        run_sample(a.limit)
