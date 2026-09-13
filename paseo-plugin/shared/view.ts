import { defineRpc } from "@getpaseo/plugin";
import { z } from "zod";

/**
 * The memory viewer's whole contract with the daemon: one read, three writes.
 *
 * The read comes from `memory view --json`, a purpose-built read verb: a viewer
 * wants the live entries with all
 * of their versions, the raw thoughts awaiting consolidation, the exact block
 * that injection produces, and where that block's tokens go — not the entire
 * event history.
 *
 * Nothing here converts characters to tokens. `memlib.estimate_tokens` is the
 * single estimator (`ceil(chars / chars_per_token)`, 3.6 by default), and every
 * token count the panel shows is the CLI's arithmetic, not a second copy of it
 * in TypeScript.
 *
 * The reads deliberately record no exposure rows and cast no votes, so opening
 * this panel cannot move any recall or exposure count in the store. The writes
 * are the three things a human operator does by hand: correct
 * an entry's text, drop it from injection, and discard a pending thought.
 */

const versionSchema = z.object({
  level: z.number(),
  text: z.string(),
  ts: z.number(),
  author: z.string().nullable(),
  noted: z.string().nullable(),
  tokens: z.number(),
});

const memorySchema = z.object({
  id: z.string(),
  state: z.string(), // active | dropped | merged
  level: z.number(), // the level currently injected
  uses: z.number(),
  core: z.boolean(),
  noted: z.string().nullable(),
  last_recalled: z.string().nullable(),
  position: z.number(),
  first_seen: z.number(),
  merged_into: z.string().nullable(),
  injected_tokens: z.number(),
  versions: z.array(versionSchema),
});

const thoughtSchema = z.object({
  id: z.number(),
  ts: z.number(),
  day: z.string(),
  session_id: z.string().nullable(),
  project: z.string().nullable(),
  text: z.string(),
  tokens: z.number(),
});

const eventSchema = z.object({
  ts: z.number(),
  actor: z.string(),
  verb: z.string(),
  memory_id: z.string().nullable(),
  session_id: z.string().nullable(),
  detail: z.string().nullable(),
});

const headlineSchema = z.object({
  entries: z.number(),
  core: z.number(),
  dropped: z.number(),
  merged: z.number(),
  decayable_tokens: z.number(),
  total_tokens: z.number(),
  core_tokens: z.number(),
  core_cap_tokens: z.number(),
  levels: z.record(z.string(), z.number()),
  pending_thoughts: z.number(),
  total_uses: z.number(),
  never_recalled: z.number(),
  max_uses: z.number(),
  next_to_squeeze: z.number(),
  over_total: z.boolean(),
  core_over: z.boolean(),
});

const occupancyLevelSchema = z.object({
  entries: z.number(),
  tokens: z.number(),
  target_tokens: z.number(),
  share: z.number(),
  over_target: z.number(),
});

const occupancySchema = z.object({
  decayable_tokens: z.number(),
  total_tokens: z.number(),
  over_total: z.boolean(),
  next_to_squeeze: z.number(),
  core: z.object({
    entries: z.number(),
    tokens: z.number(),
    cap_tokens: z.number(),
    over: z.boolean(),
  }),
  levels: z.record(z.string(), occupancyLevelSchema),
});

/**
 * Where the injected block's tokens are: the fixed preamble, core, and each
 * decayable level, priced per entry. The preamble is the remainder — block total
 * minus the priced entries — so the parts sum to `block_tokens`, the one number
 * the panel quotes as the cost of a session.
 */
const breakdownSchema = z.object({
  block_tokens: z.number(),
  preamble_tokens: z.number(),
  core_tokens: z.number(),
  level_tokens: z.record(z.string(), z.number()),
  decayable_tokens: z.number(),
  truncated: z.boolean(),
});

export const memoryViewRpc = defineRpc({
  name: "memory.view",
  input: z.object({ events: z.number().int().min(0).max(500) }),
  output: z.object({
    ok: z.boolean(),
    error: z.string().nullable(),
    instance: z.object({ dir: z.string(), db: z.string() }),
    headline: headlineSchema,
    occupancy: occupancySchema,
    breakdown: breakdownSchema,
    rendered: z.object({ block: z.string(), tokens: z.number(), preamble_tokens: z.number() }),
    memories: z.array(memorySchema),
    thoughts: z.array(thoughtSchema),
    events: z.array(eventSchema),
    recall_failures: z.array(eventSchema),
  }),
});

/**
 * The result of a write. Every mutating verb returns the same shape — the CLI is
 * an `ok`/`error` contract on every path — so the panel has one thing to render
 * when a mutation is refused by an invariant rather than one path per verb.
 */
const mutationOutput = z.object({
  ok: z.boolean(),
  error: z.string().nullable(),
  id: z.string().nullable(),
  detail: z.string().nullable(),
});

/** Edit an entry's text at the level it is injected at. `memory correct`. */
export const memoryCorrectRpc = defineRpc({
  name: "memory.correct",
  input: z.object({ id: z.string().min(1), text: z.string().min(1) }),
  output: mutationOutput,
});

/** Forget an entry from the injected block. Soft: the text is kept. `memory drop`.
 * `force` is the human override for core memories — the dreamer has no way to pass it. */
export const memoryDropRpc = defineRpc({
  name: "memory.drop",
  input: z.object({ id: z.string().min(1), reason: z.string().optional(), force: z.boolean().optional() }),
  output: mutationOutput,
});

/** Disposition pending thoughts without turning them into memories. `memory discard`. */
export const thoughtDiscardRpc = defineRpc({
  name: "memory.discard",
  input: z.object({ ids: z.array(z.number().int()).min(1) }),
  output: mutationOutput,
});

export type MemoryView = z.infer<typeof memoryViewRpc.output>;
export type Memory = z.infer<typeof memorySchema>;
export type MemoryVersion = z.infer<typeof versionSchema>;
export type Thought = z.infer<typeof thoughtSchema>;
export type MemoryEvent = z.infer<typeof eventSchema>;
export type Breakdown = z.infer<typeof breakdownSchema>;
export type MutationResult = z.infer<typeof mutationOutput>;

/** Newest version at each level — what a level chip would show. */
export function versionsByLevel(m: Memory): Record<number, MemoryVersion> {
  const out: Record<number, MemoryVersion> = {};
  for (const v of m.versions) {
    const held = out[v.level];
    if (!held || v.ts >= held.ts) out[v.level] = v;
  }
  return out;
}

/** How many records exist at each level, including superseded ones. */
export function versionCounts(m: Memory): Record<number, number> {
  const out: Record<number, number> = {};
  for (const v of m.versions) out[v.level] = (out[v.level] ?? 0) + 1;
  return out;
}
