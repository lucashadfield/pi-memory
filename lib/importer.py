#!/usr/bin/env python3
"""
One-time import of a legacy instance into the database.

Reads the three legacy stores and writes the schema:

    memories.md    the injected store, with inline metadata  -> memories
    store.jsonl    append-only sidecar, full text per level  -> versions
    thoughts.md    raw queue + thoughts/YYYY-MM-DD.md        -> thoughts
    budget.json    the budgets                               -> config

The legacy files are never modified and never deleted. They stay as the archive
and the rollback, which is what makes the cutover safe: if anything here is
wrong, the original is still on disk, untouched, with its git history intact.

The check that matters is that `memory render` reproduces the block the old
extension produced, and that `memory export` reproduces the numbers the
analytics reported. Both are asserted by tests/test_import_parity.py.
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memlib  # noqa: E402

ENTRY_RE = re.compile(r"^\s*-\s*\[(m[0-9a-z]{2,5})\]\s*(.*)$")
METADATA_RE = re.compile(r"\s*`noted [^`]*`\s*$")
NOTED_RE = re.compile(r"noted\s+(\d{2}/\d{2}/\d{4})")
RECALLED_RE = re.compile(r"last recalled\s+(\S+)")
USES_RE = re.compile(r"[↺↻](\d+)")
LEVEL_RE = re.compile(r"·\s*L(\d)")
CORE_RE = re.compile(r"·\s*core")
THOUGHT_HEADER_RE = re.compile(r"^##\s+`@(\d{2}/\d{2}/\d{4})`\s*(\d{2}:\d{2})?", re.M)


def parse_memories_md(raw: str):
    """Yield one dict per entry, in file order, with the metadata decoded."""
    out = []
    for line in raw.split("\n"):
        m = ENTRY_RE.match(line)
        if not m:
            continue
        mid, rest = m.group(1), m.group(2)
        meta = ""
        meta_match = METADATA_RE.search(rest)
        if meta_match:
            meta = meta_match.group(0)
            rest = rest[: meta_match.start()]
        noted = NOTED_RE.search(meta)
        recalled = RECALLED_RE.search(meta)
        uses = USES_RE.search(meta)
        level = LEVEL_RE.search(meta)
        out.append({
            "id": mid,
            "text": rest.strip(),
            "noted": noted.group(1) if noted else None,
            "last_recalled": (None if (not recalled or recalled.group(1) == "never")
                              else recalled.group(1)),
            "uses": int(uses.group(1)) if uses else 0,
            "level": int(level.group(1)) if level else 1,
            "core": bool(CORE_RE.search(meta)),
        })
    return out


def parse_store_jsonl(raw: str):
    """Fold the append-only sidecar to one record per (id, level), last write
    winning, exactly as the extension's foldStore did. Malformed lines are
    skipped rather than fatal: a torn append must not break the import."""
    folded = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not rec.get("id") or not isinstance(rec.get("text"), str):
            continue
        lvl = int(rec.get("level") or 1)
        folded.setdefault(rec["id"], {})[lvl] = rec
    return folded


def parse_thoughts(raw: str, source: str, processed: bool = False):
    """Thoughts are written by the extension as a header block then free text.
    The header carries the provenance the old system only kept as a comment.

    `processed` marks thoughts from an archive that already carries a
    `consolidated` footer. Without this the import would hand the dreamer months
    of already-dispositioned history to re-graduate.
    """
    out = []
    matches = list(THOUGHT_HEADER_RE.finditer(raw))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        body = raw[start:end]
        date, hhmm = m.group(1), m.group(2)
        session = transcript = project = None
        for line in body.split("\n")[:6]:
            if line.startswith("- session:"):
                session = line.split(":", 1)[1].strip()
            elif line.startswith("- transcript:"):
                transcript = line.split(":", 1)[1].strip()
            elif line.startswith("- project:"):
                project = line.split(":", 1)[1].strip()
        text = "\n".join(
            l for l in body.split("\n")
            if not l.startswith(("- session:", "- transcript:", "- project:"))
        ).strip()
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
        if not text:
            continue
        # The header date is DD/MM/YYYY; build a timestamp for the start of that day.
        try:
            dt = datetime.strptime(date + (f" {hhmm}" if hhmm else " 00:00"), "%d/%m/%Y %H:%M")
            ts = int(dt.timestamp())
        except ValueError:
            ts = memlib.now_ts()
        out.append({"ts": ts, "day": dt.strftime("%Y-%m-%d"), "session_id": session,
                    "transcript": transcript, "project": project, "text": text,
                    "source": source, "processed": processed})
    return out


def import_legacy(conn, legacy_dir: Path, force=False):
    stats = {"memories": 0, "versions": 0, "thoughts": 0, "core": 0, "skipped": []}

    existing = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()["c"]
    if existing and not force:
        return {"ok": False, "error": "not_empty", "existing_memories": existing,
                "hint": "pass --force to wipe and rebuild from the legacy store"}
    if force and existing:
        # Wipe rather than append: an append would duplicate every thought, and a
        # half-imported store is worse than either state. `--force` therefore means
        # "rebuild from scratch", which is the operation actually wanted when
        # re-running an import after the legacy files have moved on.
        for table in ("exposures", "events", "versions", "memories", "thoughts"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        stats["rebuilt"] = True

    # ---- budget.json -> config -------------------------------------------
    budget_path = legacy_dir / "budget.json"
    if budget_path.exists():
        try:
            b = json.loads(budget_path.read_text())
            mapping = {
                "chars_per_token": b.get("chars_per_token"),
                "total_tokens": b.get("total_tokens"),
                "core_tokens": b.get("core_tokens"),
                "inject_max_tokens": b.get("inject_max_tokens"),
            }
            for lv, spec in (b.get("levels") or {}).items():
                mapping[f"level_{lv}_share"] = spec.get("share")
                mapping[f"level_{lv}_words"] = spec.get("words")
            for k, v in mapping.items():
                if v is not None:
                    conn.execute("INSERT OR REPLACE INTO config(key, value) VALUES (?,?)",
                                 (k, str(v)))
        except (json.JSONDecodeError, OSError) as exc:
            stats["skipped"].append(f"budget.json: {exc}")

    # ---- store.jsonl -> versions -----------------------------------------
    sidecar = {}
    store_path = legacy_dir / "store.jsonl"
    if store_path.exists():
        sidecar = parse_store_jsonl(store_path.read_text())

    # ---- thoughts ---------------------------------------------------------
    # A dated archive carrying a `consolidated` footer was already processed by a
    # previous dream, so its thoughts are history, not a queue. Only the live
    # queue (and any un-footered archive, which is a genuine backlog) is pending.
    def _insert(t, processed):
        conn.execute(
            "INSERT INTO thoughts(ts, day, session_id, transcript, project, text, state,"
            " disposition, processed_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (t["ts"], t["day"], t["session_id"], t["transcript"], t["project"], t["text"],
             "done" if processed else "pending",
             "consolidated-pre-migration" if processed else None,
             memlib.now_ts() if processed else None),
        )

    thoughts_dir = legacy_dir / "thoughts"
    for path in sorted(thoughts_dir.glob("*.md")) if thoughts_dir.exists() else []:
        raw = path.read_text()
        done = "consolidated" in raw
        for t in parse_thoughts(raw, path.name, processed=done):
            _insert(t, done)
            stats["thoughts"] += 1
            if done:
                stats["thoughts_archived"] = stats.get("thoughts_archived", 0) + 1
    queue = legacy_dir / "thoughts.md"
    if queue.exists() and queue.read_text().strip():
        for t in parse_thoughts(queue.read_text(), "thoughts.md"):
            _insert(t, False)
            stats["thoughts"] += 1

    # ---- memories.md -> memories + versions ------------------------------
    mem_path = legacy_dir / "memories.md"
    if not mem_path.exists():
        return {"ok": False, "error": "no_memories_md", "path": str(mem_path)}

    entries = parse_memories_md(mem_path.read_text())
    for pos, e in enumerate(entries):
        mid = e["id"]
        recs = sidecar.get(mid, {})
        # Every level the sidecar holds becomes a version row. This is the step
        # that makes compression non-destructive in the new store: the detail
        # that was only in store.jsonl is now first-class.
        for lvl, rec in sorted(recs.items()):
            conn.execute(
                "INSERT OR REPLACE INTO versions(memory_id, level, text, ts, noted, source, author)"
                " VALUES (?,?,?,?,?,?, 'import')",
                (mid, lvl, rec["text"], int(rec.get("ts") or memlib.now_ts()),
                 rec.get("noted"), rec.get("source")),
            )
            stats["versions"] += 1
        if e["level"] not in recs:
            stats["skipped"].append(f"{mid}: L{e['level']} text not in store.jsonl, using memories.md")
        # The injected text from memories.md is authoritative for the entry's
        # current level: on conflict with the sidecar, memories.md won.
        conn.execute(
            "INSERT OR REPLACE INTO versions(memory_id, level, text, ts, noted, source, author)"
            " VALUES (?,?,?,?,?,?, 'import')",
            (mid, e["level"], e["text"], memlib.now_ts(), e["noted"], "memories.md"),
        )
        first_seen = min([int(r.get("ts") or memlib.now_ts()) for r in recs.values()]
                         or [memlib.now_ts()])
        conn.execute(
            "INSERT OR REPLACE INTO memories(id, state, level, uses, noted, last_recalled,"
            " core, position, first_seen, source)"
            " VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, 'import')",
            (mid, e["level"], e["uses"], e["noted"], e["last_recalled"],
             1 if e["core"] else 0, pos, first_seen),
        )
        stats["memories"] += 1
        stats["core"] += 1 if e["core"] else 0

    # ---- sidecar ids that are no longer in the live store -----------------
    # These were dropped by an earlier dream. They are unreachable by recall
    # either way, but keeping them as state='dropped' makes this database a
    # complete archive rather than one that silently lost the ids it was told
    # to forget. The legacy store.jsonl also remains on disk.
    live_ids = {e["id"] for e in entries}
    dropped = 0
    for mid, recs in sidecar.items():
        if mid in live_ids:
            continue
        for lvl, rec in sorted(recs.items()):
            conn.execute(
                "INSERT OR REPLACE INTO versions(memory_id, level, text, ts, noted, source, author)"
                " VALUES (?,?,?,?,?,?, 'import')",
                (mid, lvl, rec["text"], int(rec.get("ts") or memlib.now_ts()),
                 rec.get("noted"), rec.get("source")),
            )
            stats["versions"] += 1
        first_seen = min([int(r.get("ts") or memlib.now_ts()) for r in recs.values()]
                         or [memlib.now_ts()])
        conn.execute(
            "INSERT OR REPLACE INTO memories(id, state, level, uses, noted, last_recalled,"
            " core, position, first_seen, source)"
            " VALUES (?, 'dropped', 3, 0, NULL, NULL, 0, ?, ?, 'import')",
            (mid, len(entries) + dropped, first_seen),
        )
        dropped += 1
    stats["dropped"] = dropped

    memlib._event(conn, "import", detail={"from": str(legacy_dir), **stats}, actor="import",
                  ts=memlib.now_ts())
    conn.commit()
    return {"ok": True, **stats}


def backfill_events(conn, sessions_root: Path):
    """Reconstruct the capture and recall history from session transcripts.

    The database only starts recording live activity at migration, so without
    this the analytics would show a cliff at the cutover that is an artefact of
    the migration rather than a change in behaviour. The transcripts are the only
    remaining record of every remember and recall call, so they are the source.

    Two deliberate limits:
      * events only — `uses` on each memory is NOT touched, because it was
        imported from the legacy store and re-deriving it here would double-count.
      * recall outcome is classified by matching the tool result text for an
        unknown id. That is the string-matching the CLI exists to eliminate, but
        this is a one-time read of historical data that has no other source.
    """
    import glob
    inserted = {"remember": 0, "recall": 0, "recall_missing": 0, "recall_duplicate": 0}
    seen_sessions = set()

    for path in sorted(glob.glob(str(sessions_root / "**" / "*.jsonl"), recursive=True)):
        sid = ts = None
        calls = {}   # callId -> (verb, args, ts)
        results = {}  # callId -> text
        try:
            with open(path, errors="replace") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") == "session":
                        sid = d.get("id")
                    if d.get("type") != "message":
                        continue
                    m = d.get("message", {})
                    stamp = m.get("timestamp") or d.get("timestamp")
                    epoch = _epoch(stamp) or ts or memlib.now_ts()
                    content = m.get("content")
                    if not isinstance(content, list):
                        continue
                    for c in content:
                        if c.get("type") == "toolCall" and c.get("name") in ("remember", "recall"):
                            calls[c.get("id")] = (c["name"], c.get("arguments", {}), epoch)
                        elif c.get("type") == "toolResult":
                            results[c.get("toolCallId")] = json.dumps(c.get("content", ""))
        except OSError:
            continue

        for call_id, (verb, callargs, epoch) in calls.items():
            if not sid:
                continue
            result_text = results.get(call_id, "")
            if verb == "remember":
                conn.execute(
                    "INSERT INTO events(ts, actor, verb, memory_id, session_id, detail)"
                    " VALUES (?,?,?,?,?,?)",
                    (epoch, "live", "remember", None, sid,
                     json.dumps({"backfilled": True})),
                )
                inserted["remember"] += 1
            else:
                mid = str(callargs.get("memory_id", "")).strip().strip("[]")
                if "Unknown memory id" in result_text:
                    v = "recall_missing"
                elif "already counted" in result_text:
                    v = "recall_duplicate"
                else:
                    v = "recall"
                conn.execute(
                    "INSERT INTO events(ts, actor, verb, memory_id, session_id, detail)"
                    " VALUES (?,?,?,?,?,?)",
                    (epoch, "live", v, mid or None, sid, json.dumps({"backfilled": True})),
                )
                inserted[v] += 1
            seen_sessions.add(sid)

    conn.commit()
    return {"ok": True, "sessions": len(seen_sessions), **inserted}


def _epoch(stamp):
    if not stamp:
        return None
    if isinstance(stamp, (int, float)):
        return int(stamp)
    s = str(stamp).replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return int(datetime.fromisoformat(s).timestamp())
        except (ValueError, TypeError):
            continue
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_dir")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--backfill-events", action="store_true",
                    help="rebuild capture/recall history from session transcripts")
    a = ap.parse_args()
    c = memlib.connect()
    if a.backfill_events:
        root = Path(os.environ.get("PI_SESSION_DIR", Path.home() / ".pi" / "agent" / "sessions"))
        print(json.dumps(backfill_events(c, root), indent=2))
    else:
        print(json.dumps(import_legacy(c, Path(a.from_dir).expanduser().resolve(), a.force),
                         indent=2))
