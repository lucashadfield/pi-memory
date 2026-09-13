import type { RpcInput, RpcOutput } from "@getpaseo/plugin";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { memoryViewRpc } from "../shared/view";

const execFileAsync = promisify(execFile);

/** Overridable so a development checkout can be pointed at without reinstalling. */
const CLI = process.env.PI_MEMORY_CLI ?? "memory";

/**
 * The whole server half: run the CLI and hand back its JSON.
 *
 * There is no second implementation of anything here. The panel shows exactly
 * what `memory view` returns, so the numbers on screen are the numbers the
 * analytics and the nightly pass work from, and there is no schema knowledge in
 * this plugin beyond the shape of the payload.
 *
 * Failures are returned rather than thrown: a missing CLI, a broken store, or a
 * plugin running before the memory system is installed should render as an
 * explanatory panel, not as a crash inside the daemon.
 */
export async function viewMemory({
  events,
}: RpcInput<typeof memoryViewRpc>): Promise<RpcOutput<typeof memoryViewRpc>> {
  try {
    const { stdout } = await execFileAsync(CLI, ["view", "--json", "--events", String(events)], {
      timeout: 20_000,
      maxBuffer: 32 * 1024 * 1024,
    });
    // A trust boundary: the payload is the CLI's output, and its shape is
    // enforced by the schemas on both sides of the RPC.
    const parsed = JSON.parse(stdout) as RpcOutput<typeof memoryViewRpc>;
    return { ...parsed, ok: true, error: null };
  } catch (err) {
    return { ...empty(), ok: false, error: describe(err) };
  }
}

function describe(err: unknown): string {
  const e = err as { code?: string | number; stderr?: string; message?: string };
  if (e?.code === "ENOENT") {
    return (
      `The memory CLI was not found on PATH (looked for "${CLI}"). ` +
      `Install the memory system, or set PI_MEMORY_CLI to the full path of bin/memory.`
    );
  }
  const stderr = (e?.stderr ?? "").trim();
  if (stderr) return stderr.split("\n").slice(-3).join("\n");
  return e?.message ?? String(err);
}

/** A well-formed payload with nothing in it, so the schema always validates. */
function empty(): Omit<RpcOutput<typeof memoryViewRpc>, "ok" | "error"> {
  const level = { entries: 0, chars: 0, tokens: 0, target_tokens: 0, share: 0, over_target: 0 };
  return {
    instance: { dir: "", db: "" },
    headline: {
      entries: 0,
      core: 0,
      dropped: 0,
      merged: 0,
      decayable_tokens: 0,
      total_tokens: 0,
      core_tokens: 0,
      core_cap_tokens: 0,
      levels: {} as Record<string, number>,
      pending_thoughts: 0,
      total_uses: 0,
      never_recalled: 0,
      max_uses: 0,
      next_to_squeeze: 1,
      over_total: false,
      core_over: false,
    },
    occupancy: {
      decayable_chars: 0,
      decayable_tokens: 0,
      total_tokens: 0,
      over_total: false,
      next_to_squeeze: 1,
      core: { entries: 0, chars: 0, tokens: 0, cap_tokens: 0, over: false },
      levels: { "1": level, "2": level, "3": level },
    },
    rendered: { block: "", tokens: 0 },
    memories: [],
    thoughts: [],
    events: [],
    recall_failures: [],
  };
}
