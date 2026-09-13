-- pi-memory schema.
--
-- Design rules, in order of importance:
--
--   1. Nothing is ever deleted. Dropping a memory sets state='dropped'; the
--      versions table keeps every text it ever had. This is the property the
--      old design claimed via an append-only sidecar plus a git history, here
--      made structural.
--   2. Every mutation writes an `events` row in the same transaction as the
--      state change. The audit trail cannot diverge from the data because it
--      is written by the same statement boundary.
--   3. Invariants the dreamer used to be told in prose are constraints here:
--      a new memory is always L1, core never loses its flag, a compression can
--      only move one level per night. See lib/memlib.py.
--   4. Consumers never write SQL. They call bin/memory.
--
-- Timestamps are unix seconds. Dates shown to humans are DD/MM/YYYY strings,
-- matching the format the system has always used, so imports and renders are
-- byte-comparable with the legacy files.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- Instance configuration: budgets and the estimator's constant. Replaces
-- budget.json, and is read by the CLI rather than duplicated in TypeScript,
-- Python and JavaScript.
CREATE TABLE IF NOT EXISTS config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Raw observations, exactly as a live agent recorded them. Never deleted,
-- never edited. `state` moves pending -> done once a dream has dispositioned
-- the thought, and `memory_id` records what it became, which is the mapping
-- the analytics previously could not recover at all.
CREATE TABLE IF NOT EXISTS thoughts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           INTEGER NOT NULL,
  day          TEXT    NOT NULL,
  session_id   TEXT,
  transcript   TEXT,
  project      TEXT,
  text         TEXT    NOT NULL,
  state        TEXT    NOT NULL DEFAULT 'pending',   -- pending | done
  disposition  TEXT,                                 -- graduate|reinforce|correct|drop|merge
  memory_id    TEXT,
  processed_at INTEGER,
  dream_run    INTEGER
);
CREATE INDEX IF NOT EXISTS thoughts_pending ON thoughts(state, ts);

-- Live memory state, one row per memory. `position` preserves the order
-- entries appeared in the injected block so a render is reproducible.
CREATE TABLE IF NOT EXISTS memories (
  id            TEXT PRIMARY KEY,
  state         TEXT    NOT NULL DEFAULT 'active',  -- active | dropped | merged
  level         INTEGER NOT NULL DEFAULT 1,
  uses          INTEGER NOT NULL DEFAULT 0,         -- cumulative evidence (the ↺ column)
  noted         TEXT,                               -- DD/MM/YYYY, last observation
  last_recalled TEXT,                               -- DD/MM/YYYY or NULL
  core          INTEGER NOT NULL DEFAULT 0,
  position      INTEGER NOT NULL DEFAULT 0,
  first_seen    INTEGER NOT NULL,
  merged_into   TEXT,
  source        TEXT                                -- provenance of the original thought
);
CREATE INDEX IF NOT EXISTS memories_state ON memories(state, position);

-- Append-only. One row per (memory, level) text ever written. This is the old
-- store.jsonl as a table, and it is why compression is paging rather than
-- destruction: recall reads the fullest row from here.
CREATE TABLE IF NOT EXISTS versions (
  memory_id TEXT    NOT NULL,
  level     INTEGER NOT NULL,
  text      TEXT    NOT NULL,
  ts        INTEGER NOT NULL,
  noted     TEXT,
  source    TEXT,
  author    TEXT,                                   -- dream | human | import | live
  PRIMARY KEY (memory_id, level, ts)
);
CREATE INDEX IF NOT EXISTS versions_lookup ON versions(memory_id, level);

-- One row per mutation. Replaces the analytics' inference of dreamer behaviour
-- from git diffs: these are facts, not reconstructions.
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         INTEGER NOT NULL,
  actor      TEXT    NOT NULL,                      -- live | dream | human | import
  verb       TEXT    NOT NULL,
  memory_id  TEXT,
  session_id TEXT,                                  -- present for live-agent verbs
  detail     TEXT                                   -- JSON
);
CREATE INDEX IF NOT EXISTS events_recent ON events(ts);
CREATE INDEX IF NOT EXISTS events_memory ON events(memory_id, verb);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, verb, memory_id);

-- One row per memory per session in which it was injected. This is the
-- instrumentation the old system never had: exposure was inferred from session
-- counts on the assumption that injection succeeded. Deduplicated so a session
-- appears once per memory.
CREATE TABLE IF NOT EXISTS exposures (
  session_id   TEXT    NOT NULL,
  memory_id    TEXT    NOT NULL,
  ts           INTEGER NOT NULL,
  level        INTEGER NOT NULL,
  block_tokens INTEGER,
  PRIMARY KEY (session_id, memory_id)
);
CREATE INDEX IF NOT EXISTS exposures_memory ON exposures(memory_id);

-- The only definition of "a memory that exists". Nothing should query
-- `memories` directly for a live set, because one forgotten WHERE clause would
-- put dropped memories back into every prompt.
CREATE VIEW IF NOT EXISTS live_memories AS
  SELECT * FROM memories WHERE state = 'active';
