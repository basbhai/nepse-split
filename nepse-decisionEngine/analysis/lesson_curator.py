#!/usr/bin/env python3
"""
analysis/lesson_curator.py — checks whether each ACTIVE lesson actually worked, and says KEEP / UPDATE / RETIRE / SKIP.
Deterministic, no LLM. Preview by default; DB writes only with --apply.

How a lesson is judged
  * market_log.lessons_applied records which lessons fired on each BUY/WAIT/AVOID decision.
  * Outcome = the stock's 10-trading-day return minus NEPSE (decision_context_study dataset, first decision of each chain,
    bonus/right-suspect windows dropped).
  * Within each decision day, compare decisions where the lesson fired vs ones where it did not (this removes market-wide timing).
    Permutation test (labels shuffled within each day) gives the p-value.
  * Expected direction comes from the lesson's action: REDUCE_*/SOFT_BLOCK/BLOCK_ENTRY/WAIT_FOR_*/REQUIRE_* expect LOWER alpha
    on decisions where it fired; INCREASE_* expect HIGHER; ADD_TO_REASONING/MONITOR are informational (not testable).
Verdicts
  KEEP    >= MIN_DAYS informative days, effect in the claimed direction, p < P_MAX     -> bump validation, close any open finding
  RETIRE  >= MIN_DAYS informative days, effect OPPOSITE to the claim, p < P_MAX        -> lesson set inactive (never deleted)
  UPDATE  >= MIN_DAYS informative days, no demonstrable effect                         -> open finding + source_weight x0.75
  SKIP    not enough evidence / informational / never applied / no contrast            -> no change (re-checked next run)
  Lessons with source='user' are PROTECTED: judged and reported, never changed.
Usage: python -m analysis.lesson_curator [--apply] [--min-days 20] [--perms 5000]
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "output" / "study"
MIN_DAYS, P_MAX, PERMS, WEIGHT_FACTOR, WEIGHT_FLOOR = 20, 0.10, 5000, 0.75, 0.25


def expected_sign(action: str) -> int:
    a = (action or "").upper()
    if a.startswith(("REDUCE_", "SOFT_BLOCK", "BLOCK_ENTRY", "WAIT_FOR", "REQUIRE_")):
        return -1
    return 1 if a.startswith("INCREASE_") else 0


def within_day_test(g: pd.DataFrame, perms: int = PERMS, seed: int = 0) -> dict | None:
    """g columns: day, applied(bool), alpha. Weighted mean of (applied - not applied) alpha per informative day,
    p from shuffling the applied labels inside each day."""
    days = [(x.alpha.to_numpy(float), x.applied.to_numpy(bool)) for _, x in g.groupby("day") if x.applied.any() and (~x.applied).any()]
    if not days:
        return None
    w = np.array([a.sum() * (~a).sum() / len(a) for _, a in days], float)

    def stat(labels):
        d = np.array([v[l].mean() - v[~l].mean() for (v, _), l in zip(days, labels)])
        return float((w * d).sum() / w.sum())

    obs = stat([a for _, a in days])
    rng = np.random.default_rng(seed)
    null = np.array([stat([rng.permutation(a) for _, a in days]) for _ in range(perms)])
    p = float((np.sum(np.abs(null) >= abs(obs)) + 1) / (perms + 1))
    inn = np.concatenate([v[a] for v, a in days]); out = np.concatenate([v[~a] for v, a in days])
    return {"days": len(days), "effect": obs, "p": p, "mean_applied": float(inn.mean()), "mean_rest": float(out.mean()), "n_applied": int(len(inn))}


def decide(lesson: dict, g: pd.DataFrame, min_days: int = MIN_DAYS, perms: int = PERMS) -> dict:
    exp = expected_sign(lesson["action"])
    v = {"id": int(lesson["id"]), "action": lesson["action"], "source": lesson.get("source"), "expected": exp,
         "n_applied_all": int(g["applied"].sum()), "days": 0, "effect": None, "p": None}
    if exp == 0:
        return v | {"verdict": "SKIP", "reason": "informational action (no testable direction)"}
    if v["n_applied_all"] == 0:
        return v | {"verdict": "SKIP", "reason": "never applied with a matured outcome in the attribution window"}
    t = within_day_test(g, perms)
    if t is None:
        return v | {"verdict": "SKIP", "reason": "no day has both applied and non-applied decisions (it fires on ~every decision, so no contrast)"}
    v |= {"days": t["days"], "effect": round(t["effect"], 2), "p": round(t["p"], 4), "mean_applied": round(t["mean_applied"], 2), "mean_rest": round(t["mean_rest"], 2)}
    if t["days"] < min_days:
        return v | {"verdict": "SKIP", "reason": f"insufficient evidence: {t['days']} informative days < {min_days}"}
    aligned = np.sign(t["effect"]) == exp
    if t["p"] < P_MAX:
        return v | ({"verdict": "KEEP", "reason": "effect in the claimed direction"} if aligned else {"verdict": "RETIRE", "reason": "effect opposite to the claim"})
    return v | {"verdict": "UPDATE", "reason": f"no demonstrable effect over {t['days']} days (p={v['p']})"}


def load_pool() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(pool of matured decisions with day/alpha/id, exploded lessons_applied)."""
    from analysis.decision_context_study import build
    from db.connection import _db
    df, _, _ = build()
    with _db() as cur:
        cur.execute("SELECT id, lessons_applied FROM market_log WHERE lessons_applied IS NOT NULL AND jsonb_typeof(lessons_applied)='array'")
        la = [(r["id"], int(e["lesson_id"])) for r in cur.fetchall() for e in (r["lessons_applied"] or []) if e.get("lesson_id") is not None]
    app = pd.DataFrame(la, columns=["id", "lesson_id"]).drop_duplicates()
    ids_with_attr = set(app["id"]) | set(_ids_attributed())
    pool = df[df["id"].isin(ids_with_attr) & df["first_in_chain"] & df["alpha_nepse_10"].notna() & ~df["fwd_suspect_10"].fillna(True).astype(bool)]
    pool = pool.rename(columns={"entry_day": "day", "alpha_nepse_10": "alpha"})[["id", "day", "alpha"]].copy()
    return pool, app


def _ids_attributed() -> list[int]:
    """Decisions with an attribution record but an empty list (lessons evaluated, none fired) count as 'not applied'."""
    from db.connection import _db
    with _db() as cur:
        cur.execute("SELECT id FROM market_log WHERE lessons_applied IS NOT NULL AND jsonb_typeof(lessons_applied)='array'")
        return [r["id"] for r in cur.fetchall()]


def active_lessons() -> list[dict]:
    from db.connection import _db
    with _db() as cur:
        cur.execute("SELECT id, action, source, source_weight, confidence_level, substr(finding,1,120) AS finding FROM learning_hub WHERE active='true' ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def apply_verdicts(verdicts: list[dict]) -> dict:
    from analysis import findings_log as FL
    from db.connection import _db
    today, tally = datetime.now().strftime("%Y-%m-%d"), {"KEEP": 0, "RETIRE": 0, "UPDATE": 0, "SKIP": 0, "PROTECTED": 0}
    for v in verdicts:
        if v.get("protected"):
            tally["PROTECTED"] += 1; continue
        tally[v["verdict"]] += 1
        key = f"lesson_{v['id']}_no_effect"
        with _db() as cur:
            if v["verdict"] == "RETIRE":
                cur.execute("UPDATE learning_hub SET active='false' WHERE id=%s", (v["id"],))
                FL.add(f"lesson_{v['id']}_retired", f"Curator retired lesson {v['id']}: {v['reason']} (effect {v['effect']}, p={v['p']}, {v['days']} days)", "lesson_curator", "LOW")
                FL.close(f"lesson_{v['id']}_retired", "FIXED", "retired by curator")
            elif v["verdict"] == "KEEP":
                cur.execute("UPDATE learning_hub SET last_validated=%s, validation_count=CAST(COALESCE(NULLIF(validation_count,''),'0') AS int)+1 WHERE id=%s", (today, v["id"]))
                FL.close(key, "FIXED", "effect now demonstrated")
            elif v["verdict"] == "UPDATE":
                try:
                    sw = float(v.get("source_weight"))
                    cur.execute("UPDATE learning_hub SET source_weight=%s WHERE id=%s", (str(round(max(WEIGHT_FLOOR, sw * WEIGHT_FACTOR), 3)), v["id"]))
                except (TypeError, ValueError):
                    pass
                FL.add(key, f"Lesson {v['id']} shows no demonstrable effect: {v['reason']}. Review or rewrite it.", "lesson_curator", "MED")
    return tally


def report(verdicts: list[dict], applied: dict | None) -> str:
    md = [f"# Lesson curator — {datetime.now():%Y-%m-%d %H:%M}", "",
          f"Mode: {'APPLIED ' + json.dumps(applied) if applied else 'PREVIEW (nothing written)'}. Bar: {MIN_DAYS} informative days, p<{P_MAX}.", "",
          "| lesson | action | verdict | days | applied/rest alpha | effect | p | reason |", "|---|---|---|---|---|---|---|---|"]
    for v in sorted(verdicts, key=lambda v: ({"RETIRE": 0, "UPDATE": 1, "KEEP": 2, "SKIP": 3}[v["verdict"]], v["id"])):
        md.append(f"| {v['id']} | {v['action']} | {'PROTECTED ' if v.get('protected') else ''}{v['verdict']} | {v['days']} | {v.get('mean_applied', '—')} / {v.get('mean_rest', '—')} | {v['effect'] if v['effect'] is not None else '—'} | {v['p'] if v['p'] is not None else '—'} | {v['reason']} |")
    return "\n".join(md)


def run(apply: bool = False, min_days: int = MIN_DAYS, perms: int = PERMS) -> list[dict]:
    pool, app = load_pool()
    verdicts = []
    for les in active_lessons():
        g = pool.merge(app[app["lesson_id"] == int(les["id"])].assign(applied=True)[["id", "applied"]], on="id", how="left")
        g["applied"] = g["applied"].fillna(False).astype(bool)
        v = decide(les, g, min_days, perms) | {"source_weight": les.get("source_weight")}
        v["protected"] = (les.get("source") or "") == "user"
        verdicts.append(v)
    applied = apply_verdicts(verdicts) if apply else None
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    (OUT / f"lesson_curator_{stamp}.json").write_text(json.dumps({"applied": applied, "verdicts": verdicts}, indent=2, default=str))
    (OUT / "lesson_curator_latest.md").write_text(report(verdicts, applied))
    return verdicts


def digest(max_items: int = 12) -> str:
    """Prompt-ready summary of the latest curator run (empty string if it has never run). Non-SKIP verdicts only."""
    try:
        d = json.loads(sorted(OUT.glob("lesson_curator_2*.json"))[-1].read_text())
    except Exception:
        return ""
    vs = d["verdicts"]
    hot = [v for v in vs if v["verdict"] != "SKIP"]
    lines = [f"Latest curator run: {len(vs)} active lessons checked, {len(hot)} with enough evidence, {len(vs) - len(hot)} skipped (not enough evidence yet)."]
    for v in hot[:max_items]:
        lines.append(f"- lesson {v['id']} ({v['action']}): {v['verdict']} — {v['reason']}; effect {v['effect']}, p={v['p']}, {v['days']} days")
    return "\n".join(lines)


def selftest() -> None:
    rng = np.random.default_rng(1)
    def mk(shift, days=30, per=6):
        rows = []
        for d in range(days):
            lab = rng.permutation([True] * (per // 2) + [False] * (per // 2))
            for a in lab: rows.append((f"d{d}", bool(a), rng.normal(0, 3) + (shift if a else 0)))
        return pd.DataFrame(rows, columns=["day", "applied", "alpha"])
    L = lambda a: {"id": 1, "action": a}
    assert decide(L("REDUCE_CONFIDENCE_BY_15"), mk(-2.5), perms=1000)["verdict"] == "KEEP"          # fired -> worse, as claimed
    assert decide(L("REDUCE_CONFIDENCE_BY_15"), mk(+2.5), perms=1000)["verdict"] == "RETIRE"        # fired -> better, opposite
    assert decide(L("INCREASE_CONFIDENCE_BY_10"), mk(+2.5), perms=1000)["verdict"] == "KEEP"
    assert decide(L("REDUCE_CONFIDENCE_BY_15"), mk(0.0), perms=1000)["verdict"] in ("UPDATE", "KEEP", "RETIRE")  # noise: must never be a hard claim at p<.1 often
    assert decide(L("REDUCE_CONFIDENCE_BY_15"), mk(-2.5, days=8), perms=500)["verdict"] == "SKIP"    # too few days
    assert decide(L("ADD_TO_REASONING"), mk(-2.5), perms=500)["verdict"] == "SKIP"                    # informational
    g = mk(-2.5); g["applied"] = True
    assert decide(L("REDUCE_CONFIDENCE_BY_15"), g, perms=500)["verdict"] == "SKIP"                    # fires on everything
    g = mk(-2.5); g["applied"] = False
    assert decide(L("REDUCE_CONFIDENCE_BY_15"), g, perms=500)["verdict"] == "SKIP"                    # never fired
    nulls = sum(decide(L("REDUCE_CONFIDENCE_BY_15"), mk(0.0), perms=400)["verdict"] == "UPDATE" for _ in range(8))
    assert nulls >= 5, nulls                                                                          # pure noise is mostly "no effect", not KEEP/RETIRE
    print("lesson_curator selftest ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true"); ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--min-days", type=int, default=MIN_DAYS); ap.add_argument("--perms", type=int, default=PERMS)
    a = ap.parse_args()
    if a.selftest:
        selftest()
    else:
        vs = run(a.apply, a.min_days, a.perms)
        print((OUT / "lesson_curator_latest.md").read_text())
