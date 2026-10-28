#!/usr/bin/env python3
"""
analysis/findings_log.py — a persistent list of problems the pipeline has noticed, so the weekly review and the council
stop re-reporting them and nothing flagged is forgotten.

Each finding has a stable `topic_key` (re-adding the same key bumps `times_seen`, it never duplicates) and a status:
OPEN | FIXED | WONT_FIX. `digest()` returns a short text block that the weekly review prompt and the council context include.

CLI:
  python -m analysis.findings_log list [--all]
  python -m analysis.findings_log add <topic_key> "<finding>" [--severity HIGH|MED|LOW] [--source manual]
  python -m analysis.findings_log close <topic_key> FIXED|WONT_FIX "<note>"
  python -m analysis.findings_log selftest
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
STATUSES = ("OPEN", "FIXED", "WONT_FIX")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def add(topic_key: str, finding: str, source: str = "manual", severity: str = "MED") -> str:
    """Insert a finding, or bump an existing one (a FIXED/WONT_FIX finding seen again is reopened). Returns 'new'|'bumped'|'reopened'."""
    from db.connection import _db
    with _db() as cur:
        cur.execute("SELECT status, times_seen FROM findings_log WHERE topic_key=%s", (topic_key,))
        r = cur.fetchone()
        if not r:
            cur.execute("INSERT INTO findings_log (topic_key, source, finding, severity, status, first_seen, last_seen, times_seen) "
                        "VALUES (%s,%s,%s,%s,'OPEN',%s,%s,'1')", (topic_key, source, finding, severity, _today(), _today()))
            return "new"
        reopened = r["status"] != "OPEN"
        cur.execute("UPDATE findings_log SET finding=%s, last_seen=%s, times_seen=%s, status='OPEN', "
                    "closed_at=CASE WHEN %s THEN NULL ELSE closed_at END, "
                    "status_note=CASE WHEN %s THEN 'reopened: seen again' ELSE status_note END WHERE topic_key=%s",
                    (finding, _today(), str(int(r["times_seen"] or 1) + 1), reopened, reopened, topic_key))
        return "reopened" if reopened else "bumped"


def close(topic_key: str, status: str, note: str = "") -> bool:
    assert status in STATUSES and status != "OPEN", status
    from db.connection import _db
    with _db() as cur:
        cur.execute("UPDATE findings_log SET status=%s, status_note=%s, closed_at=%s WHERE topic_key=%s", (status, note, _today(), topic_key))
        return cur.rowcount == 1


def rows(include_closed: bool = False) -> list[dict]:
    from db.connection import _db
    with _db() as cur:
        cur.execute("SELECT * FROM findings_log " + ("" if include_closed else "WHERE status='OPEN' ") +
                    "ORDER BY CASE severity WHEN 'HIGH' THEN 0 WHEN 'MED' THEN 1 ELSE 2 END, CAST(times_seen AS int) DESC, id")
        return [dict(r) for r in cur.fetchall()]


def digest(max_items: int = 12) -> str:
    """Prompt-ready text. Empty string when there are no open findings (callers then add nothing)."""
    try:
        o = rows()
    except Exception:
        return ""
    if not o:
        return ""
    lines = [f"{len(o)} known open issue(s). Do not re-report these as new findings; say only whether the evidence changed."]
    for r in o[:max_items]:
        lines.append(f"- [{r['severity']}] {r['topic_key']} (seen {r['times_seen']}x since {r['first_seen']}): {r['finding'][:220]}")
    if len(o) > max_items:
        lines.append(f"- ... and {len(o) - max_items} more")
    return "\n".join(lines)


def selftest() -> None:
    k = "QA_SELFTEST_FINDING"
    from db.connection import _db
    try:
        assert add(k, "first text", "qa", "LOW") == "new"
        assert add(k, "second text", "qa") == "bumped"
        r = [x for x in rows() if x["topic_key"] == k][0]
        assert r["times_seen"] == "2" and r["finding"] == "second text" and r["severity"] == "LOW", r
        assert k in digest(50)
        assert close(k, "FIXED", "done") and k not in digest(50)
        assert [x for x in rows(True) if x["topic_key"] == k][0]["status"] == "FIXED"
        assert add(k, "back again") == "reopened" and k in digest(50)
        print("findings_log selftest ok")
    finally:
        with _db() as cur:
            cur.execute("DELETE FROM findings_log WHERE topic_key=%s", (k,))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest")
    l = sub.add_parser("list"); l.add_argument("--all", action="store_true")
    a = sub.add_parser("add"); a.add_argument("topic_key"); a.add_argument("finding"); a.add_argument("--severity", default="MED"); a.add_argument("--source", default="manual")
    c = sub.add_parser("close"); c.add_argument("topic_key"); c.add_argument("status"); c.add_argument("note", nargs="?", default="")
    x = ap.parse_args()
    if x.cmd == "selftest": selftest()
    elif x.cmd == "list":
        for r in rows(x.all): print(f"#{r['id']:<3}{r['status']:<9}{r['severity']:<5}{r['topic_key']:<34} seen {r['times_seen']}x  {r['finding'][:90]}")
    elif x.cmd == "add": print(add(x.topic_key, x.finding, x.source, x.severity))
    else: print("closed" if close(x.topic_key, x.status.upper(), x.note) else "no such topic_key")
