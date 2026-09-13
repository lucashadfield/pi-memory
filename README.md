# pi-memory

A durable memory store for pi agents.

Agents capture raw observations all day; a nightly consolidation pass turns them
into a small, compressed store that is injected into every future session. What
an agent learns in one repository it therefore has in the next one.

This repository is the **system**. It contains no personal data: the memories
live in a database under `$PI_MEMORY_DIR`, which nobody commits.

```
  agents ──remember──► thoughts / observations        (raw, unfiltered, kept forever)
                          │
                    nightly consolidation             (an LLM, judging)
                          │
                    memories at L1/L2/L3              (what gets injected)
                          │
  sessions ◄──render──────┘    and ──recall──► full text on demand
```

## The ideas it is built on

**Nothing is lost.** A memory decays from full text to a gist to keywords, and
each of those versions is a row in `versions`. Compression is *paging*, not
deletion: `recall` hands back the full-resolution text at the moment an agent
needs it. Dropping a memory sets a flag; its versions and the observation that
produced it stay in the archive. This is what makes it safe to be aggressive
about what survives.

**The audit trail cannot diverge from the data.** Every mutation writes its
`events` row in the same transaction as the state change. There is no separate
"report what you did" step for the consolidation pass to forget, and nothing is
reconstructed after the fact by diffing files.

**The invariants are code, not prompt text.** A new memory always enters at full
resolution — there is no flag to ask for a shorter one, because that destroys
detail before anything has recorded it. Core memories cannot be compressed,
dropped or demoted. A memory compresses at most one level per night, and the
last step from shortest to gone must be stated as an explicit drop. These used
to be paragraphs an LLM was asked to remember.

**Use is the only score.** `↺n` counts how often a memory was recalled, or
re-observed by a session that had not already contributed. It never goes down.
It decides what survives when the store exceeds its budget, and at 50 the memory
becomes permanent. Enriching the injected entry never happens (recall already
returns the full text), so there is no way to farm attention.

## Install

```bash
git clone <this repo> ~/pi-memory
cd ~/pi-memory
./install.sh                 # CLI on PATH, empty instance created
./install.sh --import DIR    # ...or load an existing memories.md + store.jsonl
```

Then install the pi side, which provides the `remember`/`recall` tools and the
injection hook:

```bash
pi install ~/pi-memory
```

And create the nightly consolidation job:

```bash
paseo schedule create 'Run `memory dream` and follow the instructions it prints.' \
  --name dream --cron '0 3 * * *' --timezone Australia/Sydney --provider pi --cwd ~/pi-memory
```

The schedule holds one line that points at `prompts/dream.md` in this repository.
The consolidation procedure is therefore versioned with the code and reviewable
in a diff, instead of living in a scheduler's configuration blob.

## The CLI

`bin/memory` is the entire interface. Nothing else writes SQL, and the pi
extension, the consolidation pass, the analytics and you all speak these verbs.

```
read
  memory status                    what the store looks like now
  memory list [--level N]          entries, at the level they are injected
  memory show <id>                 every version of one memory, and its history
  memory render [--session ID]     the block injected at session start
  memory thoughts                  raw observations awaiting consolidation
  memory log [--since DATE]        what changed, and who changed it
  memory budget                    occupancy against the configured budget
  memory verify                    check the invariants
  memory export                    the whole store as JSON
  memory dream                     print the consolidation instructions

write
  memory remember --text ...       record an observation
  memory recall <id>               fetch the full text and vote for it
  memory graduate --text ...       a new memory, always at L1
  memory reinforce <id>            observed again: +1, merge in the specifics
  memory correct <id>              the new observation wins
  memory compress <id> --text ...  one level shorter
  memory merge --survivor ...      fold memories into one, summing evidence
  memory drop <id>                 forget it; the text is kept
  memory promote <id>              make permanent
```

Exit codes are stable so callers never match on message text: `0` ok, `2` usage,
`3` unknown id, `4` refused by an invariant, `5` busy. With `--json` every verb
prints an object with an `ok` field.

## The viewer

`paseo-plugin/` is a Paseo plugin that puts the whole store in a window inside
the app, rather than in a browser tab or a terminal. It is a pure reader: it
renders what one `memory view` call returns, so the panel cannot disagree with
`memory status`, and it records no exposure and casts no vote, because a viewer
that moved the recall rate would corrupt the measurement it exists to display.

```bash
cd paseo-plugin && npm install          # dependencies for the plugin bundler
paseo plugin install ~/pi-memory/paseo-plugin
```

Then look for **Memory** in the sidebar. Three tabs:

- **Memories** — every entry with its id, level, evidence count and injected
  size. Expand one to walk its compression ladder: the chips show which levels
  actually exist (and how many historical records sit at each), and the header
  marks whether the level you are looking at is the one being injected.
  A **level lens** at the top re-renders the whole list at L1, L2 or L3, which is
  the fastest way to judge whether compression is losing something that matters.
  Forgotten entries are hidden behind a toggle.
- **Thoughts** — the raw observations no dream has dispositioned yet, with their
  project and timestamp, which is what tomorrow's consolidation will work from.
- **Prompt** — the exact text appended to the system prompt, verbatim, selectable.

The plugin is not part of the pi package manifest in `package.json`, so pi will
not try to load it; Paseo discovers it from its own config.

## Where things live

```
$PI_MEMORY_DIR/memory.db      the store (default ~/.local/share/pi-memory)
$PI_MEMORY_DIR/logs/          consolidation run logs
```

`PI_MEMORY_DIR` and `PI_MEMORY_DB` override the location. The database is a
single SQLite file: back it up by copying it.

| table | holds |
|---|---|
| `thoughts` | every raw observation, and what it became |
| `memories` | live state: level, evidence, dates, the core flag |
| `versions` | every text a memory ever had, at every level |
| `events` | every mutation, with actor and session |
| `exposures` | which memories were injected into which sessions |
| `live_memories` | a view — the only definition of "a memory that exists" |

## Analysis
```bash
python3 analytics/mine.py             # capture, recall, utility, occupancy
python3 analytics/mine.py --snapshot  # one-line record for the trend
python3 -m unittest discover -s tests # the invariants
```

Because exposure is recorded rather than inferred, `mean_utility` and the
recall rate by level are real measurements. The analytics' job is to replace
argument with rates about how the store is actually used, and to say plainly
when a number has not moved.

## Design notes

- **Everything is measured against the injected text.** The budget counts the
  bytes an agent actually sees, not the size of a row. Earlier accounting
  included per-entry metadata that was stripped before injection, which
  over-counted the store by about a tenth.
- **The schema is the source of truth, and the CLI is the only writer.** A
  second representation of the same state is a second thing that can be stale.
- **Nothing depends on a login environment.** The CLI is installed to
  `/usr/local/bin` because scheduled jobs run under systemd with the minimal
  PATH, and `install.sh` verifies resolution there rather than assuming it.

## Licence

MIT.
