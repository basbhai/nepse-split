"""
Shared quality gate for NEW learning_hub lessons (weekly review + monthly council).

Pure rules, no AI call. check() returns None if the lesson may be written,
otherwise a short rejection reason. Existing lessons are never touched here.
Self-check: python -m analysis.lesson_quality
"""
import re

MIN_EVENTS = 8          # independent events (separate days/situations), not trade rows
DUP_JACCARD = 0.7       # condition-token overlap that counts as a duplicate

_META = re.compile(r"\blessons?\s*#?\d+", re.I)
_TOK = re.compile(r"[a-z_]+|\d+(?:\.\d+)?", re.I)


def _tokens(cond: str) -> set:
    return {t.lower() for t in _TOK.findall(cond or "")}


def load_active() -> list[dict]:
    from sheets import run_raw_sql
    return run_raw_sql("SELECT id, condition FROM learning_hub WHERE active = 'true'") or []


def check(lesson: dict, active: list[dict]) -> str | None:
    """active: [{'id', 'condition'}] lessons that will stay live (exclude ones being superseded)."""
    if lesson.get("action") == "MONITOR":
        return "MONITOR lessons cannot change a decision"
    directive = (lesson.get("directive") or "").strip()
    if not 15 <= len(directive) <= 200:
        return "directive missing or not 15-200 chars"
    try:
        events = int(lesson.get("independent_events") or 0)
    except (TypeError, ValueError):
        events = 0
    if events < MIN_EVENTS:
        return f"only {events} independent events, need {MIN_EVENTS}"
    if len((lesson.get("baseline_comparison") or "").strip()) < 10:
        return "no baseline comparison"
    if any(_META.search(lesson.get(k) or "") for k in ("finding", "condition", "directive")):
        return "lesson about another lesson"
    mine = _tokens(lesson.get("condition"))
    for a in active:
        theirs = _tokens(a.get("condition"))
        if mine and theirs and len(mine & theirs) / len(mine | theirs) >= DUP_JACCARD:
            return f"near-duplicate of active lesson {a.get('id')}"
    return None


def annotate(lesson: dict) -> None:
    """Keep the evidence claim with the lesson (no schema change: appended to gpt_reasoning)."""
    lesson["gpt_reasoning"] = (
        (lesson.get("gpt_reasoning") or "")
        + f" [EVIDENCE: {lesson.get('independent_events')} independent events | "
        f"BASELINE: {lesson.get('baseline_comparison')}]"
    )


if __name__ == "__main__":
    good = {"action": "REDUCE_CONFIDENCE_BY_15", "directive": "Reduce confidence on hydropower names signalled only by MACD.",
            "independent_events": 9, "baseline_comparison": "all-trades win rate 33%, this 12%",
            "finding": "hydro MACD entries lost", "condition": "sector = 'HYDRO' AND primary_signal = 'MACD'"}
    assert check(good, []) is None
    assert "MONITOR" in check({**good, "action": "MONITOR"}, [])
    assert "directive" in check({**good, "directive": ""}, [])
    assert "independent" in check({**good, "independent_events": 3}, [])
    assert "baseline" in check({**good, "baseline_comparison": ""}, [])
    assert "another lesson" in check({**good, "finding": "lesson 95 was applied too often"}, [])
    assert "near-duplicate" in check(good, [{"id": 95, "condition": "sector = 'HYDRO' AND primary_signal = 'MACD'"}])
    assert check(good, [{"id": 1, "condition": "pe_ratio > 80"}]) is None
    print("lesson_quality self-check OK")
