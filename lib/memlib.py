#!/usr/bin/env python3
"""
pi-memory core: schema access, invariants, and the verbs everything speaks.

Nothing outside this module (and the schema file) is allowed to write SQL. The
CLI in bin/memory is the only writer of the database; the pi extension, the
dreamer, and the analytics all go through it.

The two properties this module exists to guarantee:

  * Nothing is lost. Dropping sets a state flag; every text a memory ever had
    stays in `versions`. Compression is paging: recall reads the fullest row.
  * The audit trail cannot diverge. Every mutation writes its `events` row in
    the same transaction as the state change, so there is no separate "report
    what you did" step to forget or lie about.

Invariants that used to be prose the dreamer had to remember are enforced here,
so a violation is an error return rather than a silent corruption.
"""

import json
import math
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

REPO_DIR = Path(__file__).resolve().parent.parent


def instance_dir() -> Path:
    """Where the database lives. Never inside the repo, so a clone carries no
    personal data and an instance can be moved or backed up on its own."""
    env = os.environ.get("PI_MEMORY_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".local" / "share" / "pi-memory"


def db_path() -> Path:
    env = os.environ.get("PI_MEMORY_DB")
    if env:
        return Path(env).expanduser().resolve()
    return instance_dir() / "memory.db"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "chars_per_token": "3.6",
    "total_tokens": "5000",
    "core_tokens": "1000",
    "inject_max_tokens": "11111",
    "level_1_share": "0.5",
    "level_2_share": "0.35",
    "level_3_share": "0.15",
    "level_1_words": "50",
    "level_2_words": "15",
    "level_3_words": "8",
}

# The only conversion in the system. Deliberately a heuristic rather than a real
# tokenizer: it only decides when the dreamer compresses, and it used to have to
# be computed identically in TypeScript, Python and JavaScript. Now it is here.
def estimate_tokens(chars: float, chars_per_token: float | None = None) -> int:
    cpt = float(chars_per_token if chars_per_token is not None else DEFAULT_CONFIG["chars_per_token"])
    return math.ceil(chars / cpt)


def tokens_to_chars(tokens: int, chars_per_token: float | None = None) -> int:
    """A character allowance that provably does not exceed the token ceiling.

    Floor, not ceil: this is used to slice the injected block against a hard
    budget, and rounding up would let the block through one token over. The
    inverse direction (estimate_tokens) rounds up, because it is a cost.
    """
    cpt = float(chars_per_token if chars_per_token is not None else DEFAULT_CONFIG["chars_per_token"])
    return int(tokens * cpt)


# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------

def connect(path: Path | None = None, create: bool = True) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    if not create and not p.exists():
        raise FileNotFoundError(f"no database at {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    _init_schema(conn)
    return conn


def _init_schema(conn: sqlite3.Connection) -> None:
    schema = (REPO_DIR / "lib" / "schema.sql").read_text()
    conn.executescript(schema)
    for k, v in DEFAULT_CONFIG.items():
        conn.execute("INSERT OR IGNORE INTO config(key, value) VALUES (?, ?)", (k, v))
    conn.commit()


def cfg(conn: sqlite3.Connection, key: str, cast=float):
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if row is None:
        return cast(DEFAULT_CONFIG[key]) if key in DEFAULT_CONFIG else None
    return cast(row["value"])


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def now_ts() -> int:
    return int(time.time())


def today() -> str:
    return datetime.now().strftime("%d/%m/%Y")


def today_start_ts() -> int:
    return int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def _event(conn, verb, memory_id=None, detail=None, actor="live", session_id=None, ts=None):
    """Write the audit row. Always called inside the caller's transaction, so the
    trail and the state change commit together or not at all."""
    conn.execute(
        "INSERT INTO events(ts, actor, verb, memory_id, session_id, detail) VALUES (?,?,?,?,?,?)",
        (ts or now_ts(), actor, verb, memory_id, session_id,
         json.dumps(detail) if detail is not None else None),
    )


ID_RE = re.compile(r"^m[0-9a-z]{2,5}$")
ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def new_id(conn: sqlite3.Connection, length: int = 3) -> str:
    """A permanent, never-reused handle. Agents pass these back to `recall`, so a
    renumbering or a reuse silently misattributes use.

    Random, not sequential. The first version enumerated the alphabet, which
    produced m000, m001, ... — valid but guessable, and an invented handle that
    happens to hit a real memory casts a vote for it. That is the exact failure
    `recall` is supposed to surface, so the ids should not be predictable.
    """
    existing = {r["id"] for r in conn.execute("SELECT id FROM memories")}
    import secrets
    for _ in range(200):
        cand = "m" + "".join(secrets.choice(ALPHABET) for _ in range(length))
        if cand not in existing:
            return cand
    return new_id(conn, length + 1)


def valid_id(mid: str) -> bool:
    return bool(ID_RE.match(mid or ""))


def live_memory(conn, mid):
    return conn.execute("SELECT * FROM memories WHERE id = ? AND state = 'active'", (mid,)).fetchone()


def memory_any(conn, mid):
    return conn.execute("SELECT * FROM memories WHERE id = ?", (mid,)).fetchone()


def fullest_version(conn, mid, limit=1):
    """The fullest text on file for a memory: L1 if it exists, else the longest
    row. Two early entries were graduated straight in at L2 and have no L1."""
    rows = conn.execute(
        "SELECT * FROM versions WHERE memory_id = ? ORDER BY level ASC, ts ASC", (mid,)
    ).fetchall()
    if not rows:
        return None
    l1 = [r for r in rows if r["level"] == 1]
    if l1:
        return sorted(l1, key=lambda r: (len(r["text"]), r["ts"]))[-1]
    return sorted(rows, key=lambda r: len(r["text"]))[-1]


def version_at_level(conn, mid, level):
    rows = conn.execute(
        "SELECT * FROM versions WHERE memory_id = ? AND level = ? ORDER BY ts ASC", (mid, level)
    ).fetchall()
    return rows[-1] if rows else None


def _write_version(conn, mid, level, text, noted=None, source=None, author="dream"):
    conn.execute(
        "INSERT OR REPLACE INTO versions(memory_id, level, text, ts, noted, source, author)"
        " VALUES (?,?,?,?,?,?,?)",
        (mid, level, text, now_ts(), noted, source, author),
    )


def _link_thought(conn, thought_id, disposition, memory_id=None):
    """Record what a thought became. The old system kept this only as prose in a
    nightly report, so the thought -> entry mapping was one of the things the
    analytics listed as not derivable. It is a column now."""
    if not thought_id:
        return
    conn.execute(
        "UPDATE thoughts SET state = 'done', disposition = ?, memory_id = ?, processed_at = ?"
        " WHERE id = ?",
        (disposition, memory_id, now_ts(), thought_id),
    )


def pending_thoughts(conn, state="pending", limit=None):
    q = "SELECT * FROM thoughts"
    args = []
    if state != "all":
        q += " WHERE state = ?"
        args.append(state)
    q += " ORDER BY ts ASC, id ASC"
    if limit:
        q += " LIMIT ?"
        args.append(limit)
    return conn.execute(q, args).fetchall()


# --------------------------------------------------------------------------
# render: the injected block
# --------------------------------------------------------------------------

def _preamble_path() -> Path:
    return REPO_DIR / "prompts" / "inject.md"


# The slot the entries are substituted into. The block injected at session start
# is one editable file, prompts/inject.md, so the policy text and the store's
# layout can be changed without touching code — and the same file is what the
# viewer's Prompt tab shows.
MEMORIES_SLOT = "{{memories}}"


def substitute(text: str, values: dict) -> str:
    """Fill {{placeholders}}. Shared by the injected block and the consolidation
    instructions, so there is one substitution convention in the system."""
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def entry_line(conn, row) -> str:
    v = version_at_level(conn, row["id"], row["level"])
    text = v["text"] if v else "(no text on file)"
    return f"- [{row['id']}] {text}"


def render(conn, session_id=None, truncate=True):
    """Build the block injected at the start of a session.

    Returns (block, ids, tokens). The block is prompts/inject.md with every live
    memory at its current level substituted into {{memories}}, in `position`
    order. Selection is still "everything" by design: the store is small and the
    whole point of the level ladder is that unused entries are cheaper, not that
    they are withheld. Ranking, if it ever comes, is a separate change with its
    own measurement.
    """
    template = _preamble_path().read_text().strip()
    rows = conn.execute(
        "SELECT * FROM memories WHERE state = 'active' ORDER BY position ASC, id ASC"
    ).fetchall()
    lines = [entry_line(conn, r) for r in rows]

    def compose(entry_lines, truncated=False):
        body = "\n".join(entry_lines)
        if truncated:
            body = f"{body}\n[truncated]" if body else "[truncated]"
        if MEMORIES_SLOT in template:
            return substitute(template, {"memories": body}).strip()
        # A template with no slot would drop the entire store from every session
        # without erroring, so append instead of losing it. `memory verify`
        # reports this as a fault, because that is what it is.
        return (template + (f"\n\n{body}" if body else "")).strip()

    if not truncate:
        return compose(lines), [r["id"] for r in rows], estimate_tokens(
            len(compose(lines)), cfg(conn, "chars_per_token"))

    max_chars = tokens_to_chars(int(cfg(conn, "inject_max_tokens", float)),
                                cfg(conn, "chars_per_token"))
    if len(compose(lines)) <= max_chars:
        block = compose(lines)
    else:
        # Cut at an entry boundary, never mid-entry. The old renderer sliced the
        # string at a character offset, which could hand the agent half a memory.
        overhead = len(template) - len(MEMORIES_SLOT) + len("\n[truncated]")
        budget = max(0, max_chars - overhead)
        kept, total = [], 0
        for ln in lines:
            if total + len(ln) + 1 > budget:
                break
            kept.append(ln)
            total += len(ln) + 1
        lines = kept
        block = compose(kept, truncated=True)

    ids = [r["id"] for r in rows][: len(lines)]
    return block, ids, estimate_tokens(len(block), cfg(conn, "chars_per_token"))


def record_exposures(conn, session_id, ids, block_tokens, levels=None):
    """The instrumentation the old system never had. Exposure used to be inferred
    from the number of sessions started since an entry appeared, on the
    assumption that injection succeeded and nothing was truncated."""
    if not session_id:
        return
    ts = now_ts()
    for mid in ids:
        lvl = (levels or {}).get(mid)
        if lvl is None:
            row = memory_any(conn, mid)
            lvl = row["level"] if row else 1
        conn.execute(
            "INSERT OR IGNORE INTO exposures(session_id, memory_id, ts, level, block_tokens)"
            " VALUES (?,?,?,?,?)",
            (session_id, mid, ts, lvl, block_tokens),
        )
    conn.commit()


# --------------------------------------------------------------------------
# occupancy
# --------------------------------------------------------------------------

def occupancy(conn):
    """Cost against the budget is the *injected* size of each entry, which is the
    id, a space, and the text at the entry's current level — not the size of the
    line as it sits on disk.

    This differs from the pre-database accounting, which measured the markdown
    line including the `noted ... ↺n · Ln` metadata span that was stripped before
    injection. That span is real on disk and costs the model nothing, so the old
    figure over-counted the store by roughly 10% (579 tokens at the 12/09
    migration) and gave the store headroom it did not know it had. The basis
    change is deliberate and is recorded in analytics/report.md.
    """
    cpt = cfg(conn, "chars_per_token")
    total_tokens = int(cfg(conn, "total_tokens", float))
    core_cap = int(cfg(conn, "core_tokens", float))

    rows = conn.execute("SELECT * FROM memories WHERE state = 'active'").fetchall()
    levels = {1: {"entries": 0, "chars": 0}, 2: {"entries": 0, "chars": 0}, 3: {"entries": 0, "chars": 0}}
    core = {"entries": 0, "chars": 0}
    for r in rows:
        v = version_at_level(conn, r["id"], r["level"])
        n = len(f"- [{r['id']}] {v['text'] if v else ''}")
        if r["core"]:
            core["entries"] += 1
            core["chars"] += n
        else:
            lv = r["level"] if r["level"] in levels else 1
            levels[lv]["entries"] += 1
            levels[lv]["chars"] += n

    decayable_chars = sum(l["chars"] for l in levels.values())
    decayable = estimate_tokens(decayable_chars, cpt)
    out = {
        "decayable_chars": decayable_chars,
        "decayable_tokens": decayable,
        "total_tokens": total_tokens,
        "over_total": decayable > total_tokens,
        "core": {
            "entries": core["entries"],
            "chars": core["chars"],
            "tokens": estimate_tokens(core["chars"], cpt),
            "cap_tokens": core_cap,
            "over": estimate_tokens(core["chars"], cpt) > core_cap,
        },
        "levels": {},
    }
    for lv in (1, 2, 3):
        share = cfg(conn, f"level_{lv}_share")
        target = round(total_tokens * share)
        tok = estimate_tokens(levels[lv]["chars"], cpt)
        out["levels"][lv] = {
            "entries": levels[lv]["entries"],
            "chars": levels[lv]["chars"],
            "tokens": tok,
            "target_tokens": target,
            "share": share,
            "over_target": tok - target,
        }
    out["next_to_squeeze"] = max(
        (1, 2, 3), key=lambda lv: (out["levels"][lv]["tokens"] - out["levels"][lv]["target_tokens"])
    )
    return out


# --------------------------------------------------------------------------
# invariants
# --------------------------------------------------------------------------

def verify(conn):
    """Checks the constraints the old system enforced by convention, and the
    ones it could only lint for after the fact by diffing git snapshots."""
    problems = []

    for r in conn.execute("SELECT * FROM memories"):
        mid, state, level = r["id"], r["state"], r["level"]
        if state not in ("active", "dropped", "merged"):
            problems.append(f"{mid}: invalid state {state!r}")
        if state == "merged" and not r["merged_into"]:
            problems.append(f"{mid}: merged with no merged_into target")
        if level not in (1, 2, 3):
            problems.append(f"{mid}: invalid level {level}")
        if r["core"] and state != "active":
            problems.append(f"{mid}: core memory is not active ({state})")
        n_versions = conn.execute(
            "SELECT COUNT(*) c FROM versions WHERE memory_id = ?", (mid,)
        ).fetchone()["c"]
        if n_versions == 0:
            problems.append(f"{mid}: no version on file — its text is unrecoverable")
        elif state == "active" and not version_at_level(conn, mid, level):
            problems.append(f"{mid}: at L{level} with no version row at that level")

    # A dropped or merged memory must never appear in the live view.
    live_ids = {r["id"] for r in conn.execute("SELECT id FROM live_memories")}
    for r in conn.execute("SELECT id, state FROM memories WHERE state != 'active'"):
        if r["id"] in live_ids:
            problems.append(f"{r['id']}: non-active memory is in live_memories")

    # Used to be caught only by reading the dreamer's prose report.
    for t in conn.execute("SELECT * FROM thoughts WHERE state = 'done'"):
        if not t["disposition"]:
            problems.append(f"thought {t['id']}: done with no disposition recorded")

    # Exposure rows must point at a memory that exists, live or not.
    for e in conn.execute(
        "SELECT DISTINCT e.memory_id FROM exposures e"
        " LEFT JOIN memories m ON m.id = e.memory_id WHERE m.id IS NULL"
    ):
        problems.append(f"exposure for unknown memory {e['memory_id']}")

    # The injected block is built from prompts/inject.md. If the slot the entries
    # are substituted into is missing, every session silently loses the entire
    # store — a worse failure than any data problem under this function, and an
    # invisible one, so it is checked here.
    try:
        template = _preamble_path().read_text()
        if MEMORIES_SLOT not in template:
            problems.append(
                f"{_preamble_path().name}: missing {MEMORIES_SLOT} — memories would not"
                f" be injected at all")
    except OSError as exc:
        problems.append(f"{_preamble_path()}: unreadable ({exc})")

    # The two tools' wording lives in tools.json. A malformed file is not fatal —
    # the extension falls back to built-in text — but it means the agent is being
    # told how to record memories by a fallback nobody chose, which is the kind of
    # quiet degradation that only shows up months later in the quality of what got
    # recorded. Checked here so there is an offline way to catch it.
    tools_path = REPO_DIR / "extensions" / "pi-memory" / "tools.json"
    try:
        tools = json.loads(tools_path.read_text())
        for name in ("remember", "recall"):
            entry = tools.get(name) or {}
            if not entry.get("promptSnippet"):
                problems.append(f"tools.json: {name} has no promptSnippet")
            if not isinstance(entry.get("description"), list) or not entry["description"]:
                problems.append(f"tools.json: {name} has no description")
            if not isinstance(entry.get("parameters"), dict) or not entry["parameters"]:
                problems.append(f"tools.json: {name} has no parameter descriptions")
    except OSError as exc:
        problems.append(f"tools.json: unreadable ({exc})")
    except ValueError as exc:
        problems.append(f"tools.json: does not parse ({exc})")

    return problems


# --------------------------------------------------------------------------
# verbs — capture
# --------------------------------------------------------------------------

def remember(conn, text, session_id=None, transcript=None, project=None, actor="live", ts=None):
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text"}
    ts = ts or now_ts()
    day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    cur = conn.execute(
        "INSERT INTO thoughts(ts, day, session_id, transcript, project, text, state)"
        " VALUES (?,?,?,?,?,?,'pending')",
        (ts, day, session_id, transcript, project, text),
    )
    _event(conn, "remember", detail={"thought": cur.lastrowid, "chars": len(text)},
           actor=actor, session_id=session_id, ts=ts)
    conn.commit()
    return {"ok": True, "thought": cur.lastrowid, "chars": len(text)}


# --------------------------------------------------------------------------
# verbs — use acknowledgement
# --------------------------------------------------------------------------

def recall(conn, mid, session_id=None, actor="live"):
    mid = (mid or "").strip().strip("[]")
    if not mid:
        return {"ok": False, "error": "no_id"}

    row = live_memory(conn, mid)
    if row is None:
        # An unknown id means the model invented a handle, or something
        # renumbered against instructions. Surfacing it is how that gets noticed.
        # It is also recorded as an event: the analytics used to recover this
        # count, and the duplicate count below, by matching on the text of the
        # tool result, which broke silently whenever that wording changed.
        _event(conn, "recall_missing", mid, {"invented": True}, actor=actor, session_id=session_id)
        conn.commit()
        return {"ok": False, "error": "unknown_id", "id": mid}

    # Per-session dedupe, enforced here rather than in the caller's memory so it
    # holds across processes. A speculative vote is worse than a missing one:
    # it keeps dead weight alive forever.
    if session_id:
        dup = conn.execute(
            "SELECT 1 FROM events WHERE verb = 'recall' AND memory_id = ? AND session_id = ?",
            (mid, session_id),
        ).fetchone()
        if dup:
            _event(conn, "recall_duplicate", mid, None, actor=actor, session_id=session_id)
            conn.commit()
            return {"ok": True, "id": mid, "already_counted": True, "level": row["level"]}

    conn.execute(
        "UPDATE memories SET uses = uses + 1, last_recalled = ? WHERE id = ?", (today(), mid)
    )
    _event(conn, "recall", mid, {"level": row["level"]}, actor=actor, session_id=session_id)
    conn.commit()

    full = fullest_version(conn, mid)
    shown = version_at_level(conn, mid, row["level"])
    shown_text = shown["text"] if shown else (full["text"] if full else mid)
    full_text = full["text"] if full else None
    level = row["level"]
    expanded = bool(full_text) and level > 1 and full_text.strip() != shown_text.strip()

    if expanded:
        text = (f"Recorded use of [{mid}]. Expanded from L{level} to full resolution:\n\n{full_text}")
    elif full is None:
        text = f"Recorded use of [{mid}] — {shown_text}\n\n(No fuller version on file.)"
    elif level > 1:
        text = f"Recorded use of [{mid}] — {shown_text}\n\n(No fuller version on file.)"
    else:
        text = f"Recorded use of [{mid}] — {full_text or shown_text}"

    return {
        "ok": True, "id": mid, "level": level, "expanded": expanded,
        "uses": row["uses"] + 1, "text": text,
    }


# --------------------------------------------------------------------------
# verbs — dreamer: graduate, reinforce, correct
# --------------------------------------------------------------------------

def _next_position(conn):
    row = conn.execute("SELECT COALESCE(MAX(position), -1) p FROM memories").fetchone()
    return row["p"] + 1


def discard(conn, thought_ids, reason=None, actor="dream"):
    """Disposition thoughts that are not worth keeping, without creating a memory.

    This exists because the first version had no such path: `drop` acts on a
    memory, so a thought whose disposition was "drop" could not be recorded at
    all. The consolidation pass spent much of a validation run trying to work out
    what to call, which is the cost of a missing verb being paid in tokens. A
    thought is discarded; a memory is dropped.
    """
    if isinstance(thought_ids, (int, str)):
        thought_ids = [thought_ids]
    done, missing, already = [], [], []
    for tid in thought_ids:
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            missing.append(tid)
            continue
        row = conn.execute("SELECT * FROM thoughts WHERE id = ?", (tid,)).fetchone()
        if row is None:
            missing.append(tid)
            continue
        if row["state"] == "done":
            already.append(tid)
            continue
        conn.execute("UPDATE thoughts SET state = 'done', disposition = 'drop',"
                     " processed_at = ? WHERE id = ?", (now_ts(), tid))
        _event(conn, "discard", None, {"thought": tid, "reason": reason}, actor=actor)
        done.append(tid)
    conn.commit()
    if missing:
        return {"ok": False, "error": "unknown_thought", "unknown": missing, "discarded": done}
    return {"ok": True, "discarded": done, "already": already}


def graduate(conn, text, source=None, actor="dream", memory_id=None, ts=None, noted=None,
             thought=None):
    """Move a thought into the store. Always L1: graduating straight in at a
    compressed level destroys the full text before anything has recorded it, and
    it has happened before (mayc, mayk, 23/08). There is deliberately no way to
    ask for another level."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text"}
    if memory_id:
        if not valid_id(memory_id):
            return {"ok": False, "error": "bad_id", "id": memory_id}
        if memory_any(conn, memory_id):
            return {"ok": False, "error": "id_in_use", "id": memory_id}
        mid = memory_id
    else:
        mid = new_id(conn)
    ts = ts or now_ts()
    conn.execute(
        "INSERT INTO memories(id, state, level, uses, noted, last_recalled, core, position,"
        " first_seen, source) VALUES (?, 'active', 1, 1, ?, NULL, 0, ?, ?, ?)",
        (mid, noted or today(), _next_position(conn), ts, source),
    )
    _write_version(conn, mid, 1, text, noted=noted or today(), source=source, author=actor)
    _link_thought(conn, thought, "graduate", mid)
    _event(conn, "graduate", mid, {"chars": len(text), "source": source,
                                   "thought": thought}, actor=actor, ts=ts)
    conn.commit()
    return {"ok": True, "id": mid, "level": 1, "chars": len(text)}


def reinforce(conn, mid, text=None, source=None, actor="dream", reset_full=False, noted=None,
              thought=None):
    """A memory observed again. +1 evidence, merge in the new specifics, and not
    one word more.

    reset_full is the single sanctioned way an injected entry regains detail: a
    new thought arrived carrying something the entry had lost. It resets the
    level to L1 because that detail came from a real observation.
    """
    row = memory_any(conn, mid)
    if row is None:
        return {"ok": False, "error": "unknown_id", "id": mid}
    if row["state"] != "active":
        return {"ok": False, "error": "not_active", "id": mid, "state": row["state"]}

    new_level = 1 if reset_full else row["level"]
    text = (text or "").strip()
    if not text:
        cur = version_at_level(conn, mid, row["level"])
        text = cur["text"] if cur else ""
    noted = noted or today()

    conn.execute(
        "UPDATE memories SET uses = uses + 1, noted = ?, level = ? WHERE id = ?",
        (noted, new_level, mid),
    )
    _write_version(conn, mid, new_level, text, noted=noted, source=source, author=actor)
    _link_thought(conn, thought, "reinforce", mid)
    _event(conn, "reinforce", mid, {"level": new_level, "reset_full": bool(reset_full),
                                    "thought": thought}, actor=actor)
    conn.commit()
    return {"ok": True, "id": mid, "level": new_level, "uses": row["uses"] + 1}


def correct(conn, mid, text, source=None, actor="dream", thought=None):
    """The new observation wins. If the old version was worth knowing, the caller
    keeps one clause saying what changed — a reversal is worth knowing."""
    row = memory_any(conn, mid)
    if row is None:
        return {"ok": False, "error": "unknown_id", "id": mid}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text"}
    conn.execute("UPDATE memories SET noted = ? WHERE id = ?", (today(), mid))
    _write_version(conn, mid, row["level"], text, noted=today(), source=source, author=actor)
    _link_thought(conn, thought, "correct", mid)
    _event(conn, "correct", mid, {"level": row["level"], "thought": thought}, actor=actor)
    conn.commit()
    return {"ok": True, "id": mid, "level": row["level"]}


# --------------------------------------------------------------------------
# verbs — decay: compress, drop, merge, promote
# --------------------------------------------------------------------------

def compress(conn, mid, source=None, actor="dream"):
    """One level down, exactly. Refuses core, refuses a second compression of the
    same memory in one night, and refuses to step past L3 — L3 to gone is a drop,
    which is a decision the caller states explicitly rather than a side effect."""
    row = memory_any(conn, mid)
    if row is None:
        return {"ok": False, "error": "unknown_id", "id": mid}
    if row["state"] != "active":
        return {"ok": False, "error": "not_active", "id": mid, "state": row["state"]}
    if row["core"]:
        return {"ok": False, "error": "core_is_permanent", "id": mid}
    if row["level"] >= 3:
        return {"ok": False, "error": "at_l3_use_drop", "id": mid}

    already = conn.execute(
        "SELECT 1 FROM events WHERE verb = 'compress' AND memory_id = ? AND ts >= ?",
        (mid, today_start_ts()),
    ).fetchone()
    if already:
        return {"ok": False, "error": "already_compressed_today", "id": mid}

    new_level = row["level"] + 1
    text = (source or "").strip() or None
    if not text:
        return {"ok": False, "error": "compressed_text_required", "id": mid,
                "hint": "pass the rewritten shorter form as the argument"}
    conn.execute("UPDATE memories SET level = ? WHERE id = ?", (new_level, mid))
    _write_version(conn, mid, new_level, text, noted=row["noted"], source=source, author=actor)
    _event(conn, "compress", mid, {"from": row["level"], "to": new_level}, actor=actor)
    conn.commit()
    return {"ok": True, "id": mid, "from": row["level"], "to": new_level}


def drop(conn, mid, reason=None, actor="dream", thought=None):
    """Forget it from the injected block. The versions stay as a dead archive and
    the thought that produced them stays in `thoughts`. Dropping is cheap, and
    reversible in principle, which is what makes it safe to be aggressive."""
    row = memory_any(conn, mid)
    if row is None:
        return {"ok": False, "error": "unknown_id", "id": mid}
    if row["core"]:
        return {"ok": False, "error": "core_is_permanent", "id": mid}
    if row["state"] != "active":
        return {"ok": False, "error": "not_active", "id": mid, "state": row["state"]}
    conn.execute("UPDATE memories SET state = 'dropped' WHERE id = ?", (mid,))
    _link_thought(conn, thought, "drop", mid)
    _event(conn, "drop", mid, {"reason": reason, "level": row["level"], "uses": row["uses"],
                               "thought": thought}, actor=actor)
    conn.commit()
    return {"ok": True, "id": mid}


def merge(conn, survivor, absorbed, text=None, actor="dream"):
    """Fold several memories into one better-stated entry, summing their evidence.
    The absorbed ids keep their versions and are never reused."""
    srow = memory_any(conn, survivor)
    if srow is None:
        return {"ok": False, "error": "unknown_id", "id": survivor}
    if srow["state"] != "active":
        return {"ok": False, "error": "not_active", "id": survivor}
    absorbed = [a for a in absorbed if a != survivor]
    total = srow["uses"]
    rows = []
    for a in absorbed:
        r = memory_any(conn, a)
        if r is None:
            return {"ok": False, "error": "unknown_id", "id": a}
        total += r["uses"]
        rows.append(r)
    if text:
        _write_version(conn, survivor, 1, text, noted=today(), author=actor)
        conn.execute("UPDATE memories SET level = 1 WHERE id = ?", (survivor,))
    conn.execute("UPDATE memories SET uses = ? WHERE id = ?", (total, survivor))
    for r in rows:
        conn.execute("UPDATE memories SET state = 'merged', merged_into = ? WHERE id = ?",
                     (survivor, r["id"]))
    _event(conn, "merge", survivor, {"absorbed": [r["id"] for r in rows],
                                     "uses": total}, actor=actor)
    conn.commit()
    return {"ok": True, "survivor": survivor, "absorbed": [r["id"] for r in rows], "uses": total}


def promote(conn, mid, actor="dream", force=False):
    """Core is the permanent layer: who the user is, how they want to be spoken
    to. Two ways in. Stipulated by the user, or earned at 50 pieces of evidence.
    Never demoted. The merge hazard is the caller's to manage: merging into a core
    memory makes the merged content permanent too, without it earning anything."""
    row = memory_any(conn, mid)
    if row is None:
        return {"ok": False, "error": "unknown_id", "id": mid}
    if row["core"]:
        return {"ok": True, "id": mid, "already_core": True}
    if not force and row["uses"] < 50:
        return {"ok": False, "error": "not_earned", "id": mid, "uses": row["uses"],
                "threshold": 50, "hint": "pass --stipulated only for a user-marked memory"}
    conn.execute("UPDATE memories SET core = 1 WHERE id = ?", (mid,))
    _event(conn, "promote", mid, {"uses": row["uses"], "stipulated": bool(force)}, actor=actor)
    conn.commit()
    return {"ok": True, "id": mid, "uses": row["uses"]}


# --------------------------------------------------------------------------
# log
# --------------------------------------------------------------------------

def read_log(conn, since_ts=None, limit=50):
    """Newest first, by id rather than timestamp: several events routinely share
    a second, and ordering by ts made the log non-deterministic."""
    q = "SELECT * FROM events"
    args = []
    if since_ts:
        q += " WHERE ts >= ?"
        args.append(since_ts)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return conn.execute(q, args).fetchall()


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def export(conn):
    """Everything an analytics pass or a visualisation needs, in one call. This
    is the seam a rebuilt viz consumes, so nothing has to reach into the schema."""
    occ = occupancy(conn)
    memories = []
    for r in conn.execute("SELECT * FROM memories ORDER BY position ASC"):
        versions = [dict(v) for v in conn.execute(
            "SELECT level, text, ts, noted, author FROM versions WHERE memory_id = ?"
            " ORDER BY level ASC, ts ASC", (r["id"],))]
        memories.append({
            "id": r["id"], "state": r["state"], "level": r["level"], "uses": r["uses"],
            "noted": r["noted"], "last_recalled": r["last_recalled"], "core": bool(r["core"]),
            "position": r["position"], "first_seen": r["first_seen"],
            "merged_into": r["merged_into"], "source": r["source"],
            "versions": versions,
        })
    return {
        "generated_at": now_ts(),
        "instance_dir": str(instance_dir()),
        "db": str(db_path()),
        "occupancy": occ,
        "memories": memories,
        "thoughts": [dict(t) for t in conn.execute("SELECT * FROM thoughts ORDER BY ts ASC")],
        "events": [dict(e) for e in conn.execute("SELECT * FROM events ORDER BY ts ASC")],
        "exposures": [dict(e) for e in conn.execute("SELECT * FROM exposures")],
        "config": {r["key"]: r["value"] for r in conn.execute("SELECT * FROM config")},
    }


def headline(conn):
    occ = occupancy(conn)
    counts = {"active": 0, "dropped": 0, "merged": 0}
    for r in conn.execute("SELECT state, COUNT(*) c FROM memories GROUP BY state"):
        counts[r["state"]] = r["c"]
    pending = conn.execute("SELECT COUNT(*) c FROM thoughts WHERE state = 'pending'").fetchone()["c"]
    total_uses = conn.execute("SELECT COALESCE(SUM(uses),0) s FROM live_memories").fetchone()["s"]
    never = conn.execute(
        "SELECT COUNT(*) c FROM live_memories WHERE last_recalled IS NULL").fetchone()["c"]
    max_uses = conn.execute("SELECT COALESCE(MAX(uses),0) m FROM live_memories").fetchone()["m"]
    live = counts["active"]
    return {
        "entries": live, "core": occ["core"]["entries"],
        "decayable_tokens": occ["decayable_tokens"], "total_tokens": occ["total_tokens"],
        "core_tokens": occ["core"]["tokens"], "core_cap_tokens": occ["core"]["cap_tokens"],
        "levels": {str(l): occ["levels"][l]["entries"] for l in (1, 2, 3)},
        "dropped": counts["dropped"], "merged": counts["merged"],
        "pending_thoughts": pending, "total_uses": total_uses,
        "never_recalled": never, "max_uses": max_uses,
        "next_to_squeeze": occ["next_to_squeeze"],
        "over_total": occ["over_total"], "core_over": occ["core"]["over"],
    }
