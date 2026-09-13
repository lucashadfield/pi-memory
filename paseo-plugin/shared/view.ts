import { defineRpc } from "@getpaseo/plugin";
import { z } from "zod";

/**
 * The memory viewer's whole contract with the daemon: one read, one shape.
 *
 * The data comes from `memory view --json`, which is a purpose-built read verb
 * rather than the full `memory export`: a viewer wants the live entries with all
 * of their versions, the raw thoughts awaiting consolidation, and the exact block
 * that injection produces — not the entire event history.
 *
 * A viewer must never affect the evidence. `memory view` deliberately records no
 * exposure rows and casts no votes, so opening this panel cannot move the recall
 * rate that the analytics use to judge the store.
 */

const versionSchema = z.object({
  level: z.number(),
  text: z.string(),
  ts: z.number(),
  author: z.string().nullable(),
  noted: z.string().nullable(),
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
  injected_chars: z.number(),
  versions: z.array(versionSchema),
});

const thoughtSchema = z.object({
  id: z.number(),
  ts: z.number(),
  day: z.string(),
  session_id: z.string().nullable(),
  project: z.string().nullable(),
  text: z.string(),
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
  chars: z.number(),
  tokens: z.number(),
  target_tokens: z.number(),
  share: z.number(),
  over_target: z.number(),
});

const occupancySchema = z.object({
  decayable_chars: z.number(),
  decayable_tokens: z.number(),
  total_tokens: z.number(),
  over_total: z.boolean(),
  next_to_squeeze: z.number(),
  core: z.object({
    entries: z.number(),
    chars: z.number(),
    tokens: z.number(),
    cap_tokens: z.number(),
    over: z.boolean(),
  }),
  levels: z.record(z.string(), occupancyLevelSchema),
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
    rendered: z.object({ block: z.string(), tokens: z.number() }),
    memories: z.array(memorySchema),
    thoughts: z.array(thoughtSchema),
    events: z.array(eventSchema),
    recall_failures: z.array(eventSchema),
  }),
});

export type MemoryView = z.infer<typeof memoryViewRpc.output>;
export type Memory = z.infer<typeof memorySchema>;
export type MemoryVersion = z.infer<typeof versionSchema>;
export type Thought = z.infer<typeof thoughtSchema>;
export type MemoryEvent = z.infer<typeof eventSchema>;

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
