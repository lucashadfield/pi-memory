---
name: memory-analytics
description: Measures how the memory system is actually being used — capture and recall rates, per-entry utility, what the consolidation pass did, and whether the store's invariants hold — from the database and session transcripts. Appends a dated snapshot to the instance's analytics/metrics.jsonl and a written report to analytics/report.md. Use when asked to measure the memory system, check recall rates, or decide how to tune the memory hyperparameters.
---

# Memory analytics

You measure the memory system. You do not change it.

The point of this pass is to replace argument with rates. Every hyperparameter —
the wording of the recall tool description, the core threshold, the number of
decay levels, the budget, the compress-versus-drop ordering — was set by
reasoning rather than evidence, and this is the evidence.

```
$PI_MEMORY_DIR/analytics/
  metrics.jsonl     one headline record per run, append-only
  report.md         written reports, newest section first
```

Those live in the **instance** directory, not in the repository: they are
measurements of one user's store. The miner itself is code and lives in the repo
at `analytics/mine.py`.

## Read-only, absolutely

**Never write to the store.** Not through the CLI's write verbs, not with SQL,
not by hand. You may write only inside the instance's `analytics/`.

If you notice something wrong with the store, say so in the report. Do not fix
it. An analytics pass that edits the thing it measures destroys the measurement.

## Run

```bash
python3 analytics/mine.py                # human report
python3 analytics/mine.py --snapshot     # one-line headline JSON
python3 analytics/mine.py --json         # full panel

cd "$PI_MEMORY_DIR/analytics" 2>/dev/null || mkdir -p "$PI_MEMORY_DIR/analytics"
python3 ~/pi-memory/analytics/mine.py --snapshot >> "$PI_MEMORY_DIR/analytics/metrics.jsonl"
```

Read the previous record first, so you report deltas rather than levels.

## Where the numbers come from

No instrumentation is needed, and this is now true in a stronger sense than it
used to be. Every number is a query:

- **Capture and recall** are rows in `events` with `actor='live'`, with the
  session id attached. They are facts, not parses of a transcript.
- **Exposure** is the `exposures` table: one row per memory per session it was
  injected into. This used to be *inferred* — "the number of sessions started
  since the entry first appeared" — which assumed injection succeeded and could
  not see a truncation. It is measured now.
- **Utility** is therefore `recalls / exposures`, which is a rate with a real
  denominator rather than a proxy for one.
- **What the consolidation pass did** is the `events` table filtered to
  `actor='dream'`. The old miner recovered this by diffing consecutive git
  commits of `memories.md` and attributing them by commit subject, which was
  inference dressed as record.
- **The denominator** — sessions, turns, cost — still comes from session
  transcripts under `~/.pi/agent/sessions`, because turns are not memory events.

History before the migration was backfilled from session transcripts
(`python3 lib/importer.py --backfill-events`) so the series has no artefactual
cliff at the cutover. Recall outcomes in the backfilled range are classified by
matching tool-result text, which is exactly the fragility the CLI removes
going forward.

## Read the invariants first

```bash
memory verify
```

That is the lint. It replaces the old table of diff-inferred flags (`EXPAND`,
`DEMOTE`, `grew`, `NO RECORD`) with direct constraint checks: a memory whose text
is unrecoverable, an invalid level, a core memory that is not active, a completed
thought with no recorded disposition, an exposure pointing at nothing.

`growth` deserves a note: a memory getting longer is legitimate when a new
observation carried detail the entry had lost — that is the one sanctioned way an
injected entry regains words, and it resets the entry to full resolution. Any
other growth is a violation. `memory show <id>` prints every version and every
event for one memory, which is how you tell the two apart.

## Interpret

| signal | knob it bears on |
|---|---|
| `recall.per_100_turns`, `recall.sessions` | how `recall` is described and pushed in prompting |
| `recall.unknown`, `recall.duplicate` | whether the model understands the tool, as distinct from whether it uses it |
| `capture.per_100_turns`, `capture.concentration` | how `remember` is described; whether capture is representative or bursty |
| `utility.mean`, `utility.dead_with_exposure` | whether the store earns its context cost |
| `headline.max_uses` | whether the core threshold is reachable at all |
| `occupancy.levels[*].over_target`, `next_to_squeeze` | whether the level ladder runs, and whether the target split is the right shape |
| `occupancy.decayable_tokens` vs `total_tokens` | how big the bank should be |
| `utility.by_level` | **the gaming tell** — see below |
| `dreamer` verb mix | how the consolidation prompt is written |

### The gaming tell

`recall` pays off: an agent that sees a terse entry has an incentive to recall it
speculatively just to read what is behind it. That inflates `↺` and corrupts the
only real signal in the system.

The measurement is **recall rate by level, controlled for exposure** —
`utility.by_level`. Under honest use L3 should be comparable to L1, or lower,
since a full entry is easier to judge relevant. A marked excess at L3 is
browsing, not use. Report it every run.

The countermeasures, in order of preference: sharpen the tool description, then
reconsider a separate non-voting `expand(id)` verb, which was rejected at design
time on surface-area grounds and is the one thing this evidence would overturn.

Distinguish **precision** from **volume** every time. Zero invented ids with a
near-zero call rate means the model understands the tool and does not reach for
it: a prompting problem, not a comprehension problem, with the opposite fix.

Guard against reading noise as signal. Under about 20 exposures an entry's rate
is not interpretable. Say when N is too small rather than ranking on it.

### The forgetting check

The sharpest metric available, and it needs no labelling: **re-observation of
dropped content**. Thoughts are kept forever, and a dropped memory's `events` row
records the drop, so a thought arriving that restates a dropped memory is direct
evidence the drop was wrong.

```bash
memory log --since <date> --json | python3 -c "
import json,sys
print([e for e in json.load(sys.stdin)['events'] if e['verb']=='drop'])"
memory thoughts --state all --json | head -50   # then read for restatements
```

Run the same match against memories still *present*. A memory that was in context
and got re-observed anyway is the opposite failure — injected but not read, not
believed, or not findable — and it is worth more attention than a bad drop,
because it means injection itself is not working.

There is no automated matcher for this. Do it by reading, and only over the drops
since the last run.

## Write the report

Prepend a dated section to `$PI_MEMORY_DIR/analytics/report.md`. Never rewrite an
earlier section: the value of this file is the trend, and a rewritten past
destroys it.

```markdown
## `@DD/MM/YYYY`

**Headline.** One or two sentences: what changed since the last run and whether
anything needs a decision.

| metric | now | last run | Δ |
|---|---|---|---|

**Invariants.** From `memory verify` — or "hold".

**What this says about tuning.** Only knobs where the data actually moved. Name
the knob, the number, the direction. If a number has not moved enough to justify
a change, say that instead of inventing a recommendation.

**Not measurable yet.** What you wanted to know and could not.
```

Keep it short. The numbers are in `metrics.jsonl` and the panel is always
recomputable, so the report's only job is judgement and the things a future
reader would not reconstruct.

## Known limits — state them, don't paper over them

- `cost_total` is whole-session spend, not the cost attributable to memory.
- Exposure before the migration does not exist; the rate is only meaningful for
  sessions after it, and `mean_utility` will be thin until exposures accumulate.
- The consolidation pass's own token cost is not recorded per run.
- Duplicate recalls are recorded as `recall_duplicate` events but do not count as
  uses; the rate math treats them correctly, the volume math should too.
- There is still no hold-out: nobody has run the counterfactual that would say
  whether memory helps rather than whether it is used.

## Restraint

- Measure, don't tune. Recommending a change is in scope; making one is not.
- A number that has not moved is a finding. Report it and stop.
- Never fabricate a metric the miner does not produce. If you want a new one, add
  it to `mine.py` and say in the report that the series starts today.
