import type { RpcInput, RpcOutput } from "@getpaseo/plugin";
import { execFile, spawn } from "node:child_process";
import { promisify } from "node:util";
import { memoryCorrectRpc, memoryDropRpc, memoryViewRpc, thoughtDiscardRpc } from "../shared/view";
import type { MutationResult } from "../shared/view";

const execFileAsync = promisify(execFile);

/** Overridable so a development checkout can be pointed at without reinstalling. */
const CLI = process.env.PI_MEMORY_CLI ?? "memory";

/**
 * The whole server half: run the CLI and hand back its JSON.
 *
 * There is no second implementation of anything here. The panel shows exactly
 * what `memory view` returns, so the numbers on screen are the numbers the
 * nightly pass works from, and there is no schema knowledge in
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

/**
 * The three writes. Each is a thin shim over a CLI verb with `--actor human`, so
 * an edit made in the panel is attributed to a person in the `events` table and
 * not confused with something the dreamer decided. Nothing mutates the store in
 * this process.
 */
export async function correctMemory({
  id,
  text,
}: RpcInput<typeof memoryCorrectRpc>): Promise<RpcOutput<typeof memoryCorrectRpc>> {
  return runMutation(["correct", id, "--text", "-", "--actor", "human", "--json"], text);
}

export async function dropMemory({
  id,
  reason,
  force,
}: RpcInput<typeof memoryDropRpc>): Promise<RpcOutput<typeof memoryDropRpc>> {
  const args = ["drop", id, "--actor", "human", "--json"];
  if (reason) args.push("--reason", reason);
  if (force) args.push("--force");
  return runMutation(args);
}

export async function discardThoughts({
  ids,
}: RpcInput<typeof thoughtDiscardRpc>): Promise<RpcOutput<typeof thoughtDiscardRpc>> {
  return runMutation(["discard", "--thought", ids.join(","), "--actor", "human", "--json"]);
}

/**
 * Run a mutating verb and normalise its result.
 *
 * The CLI prints its JSON result whether it succeeded or refused, and only then
 * exits non-zero; so the payload on stdout is authoritative and the exit code is
 * not consulted. A verb that refused on an invariant (`core_is_permanent`) comes
 * back as `ok: false` with the reason intact, which is what the panel renders.
 */
async function runMutation(args: string[], stdin?: string): Promise<MutationResult> {
  const { stdout, stderr } = await run(CLI, args, stdin);
  const parsed = parseLastJson(stdout);
  if (!parsed) {
    return { ok: false, error: stderr.trim() || "the memory CLI returned no result", id: null, detail: null };
  }
  const id = typeof parsed.id === "string" ? parsed.id : null;
  return {
    ok: Boolean(parsed.ok),
    error: typeof parsed.error === "string" ? parsed.error : null,
    id,
    detail: typeof parsed.hint === "string" ? parsed.hint : null,
  };
}

/** spawn rather than execFile: correct passes the new text on stdin, so a long
 * entry never has to survive an argument list or a shell. */
function run(
  cmd: string,
  args: string[],
  stdin?: string,
): Promise<{ code: number | null; stdout: string; stderr: string }> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { stdio: ["pipe", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (d) => (stdout += d));
    child.stderr.setEncoding("utf8").on("data", (d) => (stderr += d));
    child.on("error", (err) => resolve({ code: null, stdout, stderr: stderr + String(err) }));
    child.on("close", (code) => resolve({ code, stdout, stderr }));
    if (child.stdin) {
      child.stdin.end(stdin ?? "");
    }
  });
}

function parseLastJson(stdout: string): Record<string, unknown> | null {
  const text = stdout.trim();
  if (!text) return null;
  // `memory --json` pretty-prints with indent=2, so the whole buffer is the
  // document — parsing the last line would read a bare "}" and fail. The
  // first-{..last-} slice is the fallback for any stray prose before the JSON.
  const candidates = [text];
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start >= 0 && end > start) candidates.push(text.slice(start, end + 1));
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === "object") return parsed as Record<string, unknown>;
    } catch {
      // fall through to the next candidate
    }
  }
  return null;
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
  const level = { entries: 0, tokens: 0, target_tokens: 0, share: 0, over_target: 0 };
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
      decayable_tokens: 0,
      total_tokens: 0,
      over_total: false,
      next_to_squeeze: 1,
      core: { entries: 0, tokens: 0, cap_tokens: 0, over: false },
      levels: { "1": level, "2": level, "3": level },
    },
    breakdown: {
      block_tokens: 0,
      preamble_tokens: 0,
      core_tokens: 0,
      level_tokens: { "1": 0, "2": 0, "3": 0 },
      decayable_tokens: 0,
      truncated: false,
    },
    rendered: { block: "", tokens: 0, preamble_tokens: 0 },
    memories: [],
    thoughts: [],
    events: [],
    recall_failures: [],
  };
}
