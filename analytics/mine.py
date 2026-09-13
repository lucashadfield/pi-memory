#!/usr/bin/env python3
"""
Memory analytics: how the store is actually being used.

    python3 analytics/mine.py            human report
    python3 analytics/mine.py --snapshot one-line JSON for the trend file
    python3 analytics/mine.py --json     the full panel

Read-only. This never writes to the store; the only file it appends to is
metrics.jsonl, and only when asked with --snapshot.

Where the numbers come from now, and why it is better than before:

  store, per-entry state ... the database directly (one query, no git snapshots)
  dreamer actions .......... the `events` table, which is a record of what
                            happened; the old miner reconstructed it by diffing
                            consecutive git commits and attributing them by
                            commit subject.
  exposure ................. the `exposures` table. Previously inferred:
                            "the number of sessions started since an entry first
                            appeared", which assumed injection succeeded and
                            could not see a truncation.
  capture and recall ....... events with actor='live'. Previously parsed out of
                            session transcripts and classified by matching on the
                            text of the tool result, which broke silently
                            whenever that wording changed.
  the denominator .......... session transcripts, for sessions, turns and cost.
                            Everything is reported as a rate per 100 turns.

Invariant violations are not linted here any more. They are `memory verify`.
"""

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))
import memlib  # noqa: E402

ANALYTICS = REPO / "analytics"
SESSIONS = Path.home() / ".pi" / "agent" / "sessions"
OUT = REPO / "analytics"


def read_sessions(root=SESSIONS):
    """Sessions, turns and cost, for the denominator. The memory events are no
    longer read from here — they are in the database, which is authoritative and
    does not depend on parsing anyone's prose."""
    out = []
    if not root.exists():
        return out
    for f in sorted(root.rglob("*.jsonl")):
        s = {"file": str(f), "cwd": None, "start": None, "turns": 0, "cost": 0.0,
             "tokens": 0, "user_msgs": 0}
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.split("\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "session":
                s["cwd"] = d.get("cwd")
                s["start"] = d.get("timestamp")
                continue
            if d.get("type") != "message":
                continue
            m = d.get("message", {})
            role = m.get("role")
            if role == "user":
                s["user_msgs"] += 1
            elif role == "assistant":
                s["turns"] += 1
                u = m.get("usage") or {}
                s["tokens"] += u.get("totalTokens", 0)
                s["cost"] += (u.get("cost") or {}).get("total", 0) or 0
        out.append(s)
    return out


def per_entry(conn):
    """Utility per memory: how often it was in context, and how often that
    exposure turned into a recall. Exposure is now a measured fact."""
    rows = conn.execute("""
        SELECT m.id, m.level, m.uses, m.core, m.state,
               (SELECT COUNT(*) FROM exposures e WHERE e.memory_id = m.id) AS shown,
               (SELECT COUNT(*) FROM events v
                 WHERE v.memory_id = m.id AND v.verb = 'recall') AS recalled
        FROM memories m
        WHERE m.state = 'active'
        ORDER BY m.position ASC
    """).fetchall()
    out = []
    for r in rows:
        v = memlib.version_at_level(conn, r["id"], r["level"])
        shown, recalled = r["shown"], r["recalled"]
        out.append({
            "id": r["id"], "level": r["level"], "uses": r["uses"], "core": bool(r["core"]),
            "shown": shown, "recalled": recalled,
            "rate": (recalled / shown) if shown else None,
            "chars": len(v["text"]) if v else 0,
            "text": (v["text"] if v else "")[:70],
        })
    return out


def dreamer_actions(conn):
    """What the consolidation pass actually did, from the record rather than from
    an inference over commit diffs."""
    return dict(Counter(
        r["verb"] for r in conn.execute("SELECT verb FROM events WHERE actor = 'dream'")
    ))


def panel(conn, sessions):
    h = memlib.headline(conn)
    occ = memlib.occupancy(conn)
    turns = sum(s["turns"] for s in sessions)
    cost = sum(s["cost"] for s in sessions)
    n_sessions = len([s for s in sessions if s["turns"] > 0])

    ev = conn.execute("""
        SELECT verb, COUNT(*) n, COUNT(DISTINCT session_id) sess
        FROM events WHERE actor = 'live' GROUP BY verb
    """).fetchall()
    live = {r["verb"]: {"calls": r["n"], "sessions": r["sess"]} for r in ev}

    def per100(n):
        return round(100 * n / turns, 2) if turns else 0.0

    remember = live.get("remember", {"calls": 0, "sessions": 0})
    recall = live.get("recall", {"calls": 0, "sessions": 0})
    missing = live.get("recall_missing", {"calls": 0, "sessions": 0})
    dupe = live.get("recall_duplicate", {"calls": 0, "sessions": 0})

    # capture concentration: the share of thoughts from the busiest session
    by_session = Counter(r["session_id"] for r in conn.execute(
        "SELECT session_id FROM events WHERE verb = 'remember' AND session_id IS NOT NULL"))
    concentration = (max(by_session.values()) / sum(by_session.values())) if by_session else 0.0

    entries = per_entry(conn)
    exposed = [e for e in entries if e["shown"] >= 5]
    util = [e["rate"] for e in exposed if e["rate"] is not None]

    # the gaming tell: are compressed entries being opened far more often than
    # full ones? Under honest use the two should be comparable, or L1 higher.
    by_level = defaultdict(list)
    for e in exposed:
        if e["rate"] is not None:
            by_level[e["level"]].append(e["rate"])
    level_rate = {str(k): round(statistics.mean(v), 4) for k, v in sorted(by_level.items()) if v}

    return {
        "date": datetime.now().strftime("%d/%m/%Y"),
        "generated_at": int(datetime.now(timezone.utc).timestamp()),
        "sessions": n_sessions,
        "turns": turns,
        "cost_total": round(cost, 2),
        "headline": h,
        "occupancy": occ,
        "capture": {"remember_calls": remember["calls"], "sessions": remember["sessions"],
                    "per_100_turns": per100(remember["calls"]),
                    "concentration": round(concentration, 2)},
        "recall": {"calls": recall["calls"], "sessions": recall["sessions"],
                   "per_100_turns": per100(recall["calls"]),
                   "unknown": missing["calls"], "duplicate": dupe["calls"]},
        "utility": {"mean": round(statistics.mean(util), 4) if util else None,
                    "exposed_entries": len(exposed), "never_recalled": h["never_recalled"],
                    "dead_with_exposure": sum(1 for e in exposed if e["recalled"] == 0),
                    "by_level": level_rate},
        "dreamer": dreamer_actions(conn),
        "entries": entries,
        "violations": memlib.verify(conn),
    }


def report(d):
    h = d["headline"]
    print("═══ CAPTURE ═══")
    c, r = d["capture"], d["recall"]
    print(f"sessions (>=1 turn)   {d['sessions']}   turns {d['turns']}   cost ${d['cost_total']}")
    print(f"remember              {c['remember_calls']} calls in {c['sessions']} sessions"
          f" ({_pct(c['sessions'], d['sessions'])}, {c['per_100_turns']} per 100 turns)")
    print(f"recall                {r['calls']} calls in {r['sessions']} sessions"
          f" ({_pct(r['sessions'], d['sessions'])}, {r['per_100_turns']} per 100 turns)")
    print(f"recall quality        {r['unknown']} unknown id, {r['duplicate']} duplicate-in-session")
    print(f"capture concentration top session holds {c['concentration']:.0%} of all thoughts")

    print("\n═══ STORE ═══")
    print(f"entries               {h['entries']}  ({h['core']} core, {h['dropped']} dropped,"
          f" {h['merged']} merged)")
    print(f"decayable             {h['decayable_tokens']}/{h['total_tokens']} tok"
          f"  {'OVER' if h['over_total'] else 'ok'}")
    for lv in (1, 2, 3):
        o = d["occupancy"]["levels"][lv]
        print(f"  L{lv}                   {o['entries']:2d} entries  {o['tokens']:5d}/"
              f"{o['target_tokens']} tok  {o['over_target']:+d} vs target")
    print(f"core                  {h['core_tokens']}/{h['core_cap_tokens']} tok (exempt)"
          f"  {'OVER' if h['core_over'] else 'ok'}")
    print(f"next tier to squeeze  L{h['next_to_squeeze']}  (only matters once over budget)")
    print(f"pending thoughts      {h['pending_thoughts']}")

    print("\n═══ DREAMER (from the events table) ═══")
    for k in sorted(d["dreamer"]):
        print(f"  {k:20s} {d['dreamer'][k]}")

    print("\n═══ UTILITY (recalls / sessions injected) ═══")
    u = d["utility"]
    print(f"mean among exposed    {u['mean']}   over {u['exposed_entries']} entries")
    print(f"never recalled        {h['never_recalled']}/{h['entries']}")
    print(f"dead with exposure    {u['dead_with_exposure']} entries shown >=5x, never used")
    print(f"recall rate by level  {u['by_level']}   <- the gaming tell: L3 should not"
          f" exceed L1")
    print(f"max ↺                 {h['max_uses']}  (core threshold 50)")
    print(f"\n{'id':6s} {'shown':>5s} {'recall':>6s} {'rate':>6s}  L  text")
    for e in sorted(d["entries"], key=lambda x: -(x["rate"] or 0)):
        rate = f"{e['rate']:.2f}" if e["rate"] is not None else "  --"
        print(f"{e['id']:6s} {e['shown']:5d} {e['recalled']:6d} {rate:>6s}"
              f"  {e['level']}  {e['text']}")

    print("\n═══ INVARIANTS (memory verify) ═══")
    if d["violations"]:
        for v in d["violations"]:
            print(f"  ! {v}")
    else:
        print("  none")


def _pct(a, b):
    return f"{100*a/b:.0f}% of sessions" if b else "n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="full panel as JSON")
    ap.add_argument("--snapshot", action="store_true", help="append a headline record")
    ap.add_argument("--db", help="override the database path")
    a = ap.parse_args()

    conn = memlib.connect(Path(a.db) if a.db else None, create=False)
    sessions = read_sessions()
    d = panel(conn, sessions)

    if a.snapshot:
        rec = {
            "date": d["date"], "sessions": d["sessions"], "turns": d["turns"],
            "entries": d["headline"]["entries"], "core": d["headline"]["core"],
            "decayable_tokens": d["headline"]["decayable_tokens"],
            "total_tokens": d["headline"]["total_tokens"],
            "core_tokens": d["headline"]["core_tokens"],
            "levels": d["headline"]["levels"],
            "remember": d["capture"]["remember_calls"],
            "remember_per_100_turns": d["capture"]["per_100_turns"],
            "recall": d["recall"]["calls"],
            "recall_per_100_turns": d["recall"]["per_100_turns"],
            "recall_unknown": d["recall"]["unknown"],
            "recall_dupe": d["recall"]["duplicate"],
            "never_recalled": d["headline"]["never_recalled"],
            "max_uses": d["headline"]["max_uses"],
            "mean_utility_exposed": d["utility"]["mean"],
            "dead_with_exposure": d["utility"]["dead_with_exposure"],
            "violations": d["violations"],
            "pending_thoughts": d["headline"]["pending_thoughts"],
        }
        print(json.dumps(rec))
    elif a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        report(d)


if __name__ == "__main__":
    main()
