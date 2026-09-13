/**
 * pi-memory — injection, capture, and recall acknowledgement.
 *
 * This extension is a presentation layer and nothing else. It owns the prompt
 * text the model sees and the shape of the two tools; every fact it needs, and
 * every write it makes, goes through the `memory` CLI. There is no store logic
 * here, no file format, no lock, and no token arithmetic — all of that lives in
 * ~/pi-memory/lib, speaks SQLite, and is tested in tests/test_memory.py.
 *
 * The reason for that split is not tidiness. Invariants the dreamer used to be
 * told in prose (a new memory enters at L1, core is never demoted, one
 * compression per memory per night) are now refused by the CLI, so they cannot
 * be forgotten by a model or corrupted by a race. And the injected block is
 * composed by one function in one language, so what the agent sees and what the
 * analytics measures cannot drift apart.
 *
 * The CLI is the single point of failure for injection, so a failure here is
 * reported rather than swallowed: a session that silently loses its memory looks
 * exactly like a session that had none to begin with, and that ambiguity has
 * hidden real faults before.
 *
 * Where the wording lives, because there are two injected surfaces and they must
 * not diverge:
 *
 *   prompts/inject.md   the policy — when and why to call these tools. One file,
 *                       editable without touching code, substituted with the
 *                       entries at {{memories}}. Authoritative: it is what every
 *                       session of every user gets.
 *   this file           mechanism — what each tool does and how to call it,
 *                       attached to the tool definitions where the model looks
 *                       when deciding to use one.
 *
 * Change policy in inject.md. Only change these descriptions when what the tool
 * does changes.
 *
 *   memory render --session <id>   → the block injected at session start
 *   memory remember --text ...     → a raw observation, queued for the dream
 *   memory recall <id> --session … → full-resolution text, plus a vote
 */

import { execFile } from "node:child_process";
import { promisify } from "node:util";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const execFileAsync = promisify(execFile);

/** Overridable so a development checkout can be pointed at without reinstalling. */
const CLI = process.env.PI_MEMORY_CLI ?? "memory";
const RENDER_TIMEOUT_MS = 15_000;
const TOOL_TIMEOUT_MS = 10_000;

type CliResult = { ok: boolean; error?: string; [k: string]: unknown };

/**
 * Run a CLI verb and parse its JSON. The contract is that the CLI prints a JSON
 * object with an `ok` field and exits non-zero on refusal, which is a far more
 * stable thing to depend on than matching message text.
 */
async function cli(args: string[], timeout = TOOL_TIMEOUT_MS): Promise<CliResult> {
  try {
    const { stdout } = await execFileAsync(CLI, [...args, "--json"], {
      timeout,
      maxBuffer: 16 * 1024 * 1024,
      env: process.env,
    });
    return JSON.parse(stdout) as CliResult;
  } catch (err) {
    // A refusal exits non-zero but still prints a well-formed result on stdout.
    const e = err as { stdout?: string; code?: string | number; message?: string };
    if (e.stdout?.trim()) {
      try {
        return JSON.parse(e.stdout) as CliResult;
      } catch {
        /* fall through to the transport error below */
      }
    }
    return {
      ok: false,
      error: e.code === "ENOENT" ? "cli_not_found" : "cli_failed",
      message: e.message ?? String(err),
    };
  }
}

const REFUSAL_TEXT: Record<string, string> = {
  cli_not_found:
    "The memory CLI is not on PATH, so memory is unavailable this session. " +
    "Install it with `install.sh`, or set PI_MEMORY_CLI to its full path.",
  cli_failed: "The memory CLI failed. Memory is unavailable for this call.",
  unknown_id: "Unknown memory id — ignored.",
};

function describe(result: CliResult, fallback: string): string {
  if (result.error && REFUSAL_TEXT[result.error]) return REFUSAL_TEXT[result.error];
  if (result.error) return `${fallback} (${result.error})`;
  return fallback;
}

export default function (pi: ExtensionAPI) {
  let injectedThisSession = false;
  let noted = 0;
  let recalled = 0;

  const status = (ctx: ExtensionContext) => {
    const bits: string[] = [];
    if (noted) bits.push(`${noted} noted`);
    if (recalled) bits.push(`${recalled} recalled`);
    if (bits.length) ctx.ui.setStatus?.("memory", `memory: ${bits.join(", ")}`);
  };

  // -- read side: injection ------------------------------------------------

  pi.on("before_agent_start", async (event, ctx) => {
    if (injectedThisSession) return;
    injectedThisSession = true;

    const sessionId = ctx.sessionManager.getSessionId?.() ?? "";
    const result = await cli(["render", "--session", sessionId], RENDER_TIMEOUT_MS);

    if (!result.ok || typeof result.block !== "string" || !result.block.trim()) {
      const why = describe(result, "Memory could not be loaded.");
      ctx.ui.setStatus?.("memory", "memory: UNAVAILABLE");
      ctx.ui.notify?.(why, "error");
      return;
    }

    return { systemPrompt: `${event.systemPrompt}\n\n${result.block}` };
  });

  // -- write side: capture -------------------------------------------------

  pi.registerTool({
    name: "remember",
    label: "Remember",
    description: [
      "Record something you learned that will still be useful in a future session.",
      "",
      "Write it out in full — several sentences if needed, with the specifics:",
      "commands, paths, numbers, the reason behind a preference, what you got wrong.",
      "A nightly pass merges and files these, and a memory can never be made more",
      "detailed later than the thought it came from, so detail omitted here is lost.",
      "",
      "The highest-value memories surface after the fact. Once a question is",
      "answered or a task completed, ask what cost you the most steps — grepping to",
      "locate a file, rediscovering a convention, re-deriving a decision — and",
      "record the fact that would have skipped them. Write it at the level of the",
      "recurring task, not the one-off instance, with the specifics attached: a",
      "memory that only matches one instance is never recalled, so it earns",
      "nothing.",
      "",
      "Do not judge whether it is important enough to keep — that is decided later,",
      "with hindsight. Record freely.",
    ].join("\n"),
    promptSnippet: "Record a durable observation about the user or project for future sessions",
    promptGuidelines: [
      "Call remember when you learn something durable: a correction from the user, a project convention, a build or test command, a constraint, a preference, or a mistake worth not repeating. Prefer over-recording; a nightly pass filters.",
      "Do not call remember for transient task state, or to restate something already in the Memory section unless you are correcting it.",
      "After answering a question or completing a task, reflect on the path: if a single fact — a file location, a convention, a decision — would have saved you a pile of steps, record it, generalised enough to apply to the next instance of the task.",
    ],
    parameters: Type.Object({
      thought: Type.String({
        description:
          "The observation, written in full with specifics, generalised to the recurring task so a future instance of it matches. No date or session id needed — those are added automatically.",
      }),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx: ExtensionContext) {
      const thought = params.thought.trim();
      if (!thought) {
        return { content: [{ type: "text", text: "Nothing to record." }], isError: true, details: {} };
      }
      const result = await cli([
        "remember",
        "--text",
        thought,
        "--session",
        ctx.sessionManager.getSessionId?.() ?? "",
        "--transcript",
        ctx.sessionManager.getSessionFile?.() ?? "",
        "--project",
        ctx.cwd ?? "",
      ]);
      if (!result.ok) {
        return {
          content: [{ type: "text", text: describe(result, "Could not record that.") }],
          isError: true,
          details: result,
        };
      }
      noted++;
      status(ctx);
      return {
        content: [{ type: "text", text: "Noted. It'll be consolidated on the next dream." }],
        details: result,
      };
    },
  });

  // -- write side: use acknowledgement -------------------------------------

  pi.registerTool({
    name: "recall",
    label: "Recall",
    description: [
      "Records use of a memory from the Memory section and returns its full-resolution",
      "text.",
      "",
      "Use it proactively, at the start of a turn: before other work, scan the",
      "injected Memory section for anything that could make the task faster or more",
      "accurate — a compressed entry whose keywords look relevant, a path or command",
      "that matches the terrain, a preference that constrains what you are about to",
      "do — and recall those first. You can also call it mid-turn when a memory",
      "proves useful.",
      "",
      "Recalling returns the entry's full text. A terse entry has been compressed by",
      "disuse, not written that way: the full version is still on disk, so the call",
      "recovers the exact command, path, number or reason that was summarised away.",
      "",
      "It also adds one to that memory's cumulative evidence count, a vote that it",
      "earned its place. The count never goes down, so votes accumulate into",
      "permanence: the weakest entries are compressed when the store runs out of",
      "room, and an entry that reaches 50 becomes core and is never forgotten.",
      "",
      "The test is relevance to what you are about to do — which is why the check",
      "happens at the top of the turn. Do not recall entries at random just to see",
      "what they say, and do not invent ids: a speculative vote is worse than a",
      "missing one because it keeps dead weight alive forever.",
    ].join("\n"),
    promptSnippet: "Fetch a memory's full text by id — scan the Memory section at the start of each turn",
    promptGuidelines: [
      "At the start of a turn, before doing other work, scan the injected Memory section and recall any entry that could make the task faster or more accurate. Recalling returns the full-resolution text (for a compressed entry, the detail that was summarised away) and votes for the entry.",
      "Call recall again mid-turn when a memory proves useful. Do not recall entries at random, and do not invent ids — relevance to what you are about to do is the test.",
    ],
    parameters: Type.Object({
      memory_id: Type.String({
        description: "Id of the memory to recall, e.g. \"m3f\". Without brackets.",
      }),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx: ExtensionContext) {
      const id = params.memory_id.trim().replace(/^\[|\]$/g, "");
      if (!id) {
        return { content: [{ type: "text", text: "No id given." }], isError: true, details: {} };
      }
      const result = await cli([
        "recall",
        id,
        "--session",
        ctx.sessionManager.getSessionId?.() ?? "",
      ]);
      if (!result.ok) {
        return {
          content: [{ type: "text", text: describe(result, `Could not recall ${id}.`) }],
          isError: true,
          details: result,
        };
      }
      if (result.already_counted) {
        return {
          content: [{ type: "text", text: `${id} already counted this session.` }],
          details: result,
        };
      }
      recalled++;
      status(ctx);
      return { content: [{ type: "text", text: String(result.text ?? "") }], details: result };
    },
  });

  // -- inspection ----------------------------------------------------------

  pi.registerCommand("memory", {
    description: "Show memory status (entries, levels, occupancy, pending thoughts)",
    handler: async (_args, ctx) => {
      const result = await cli(["status"]);
      if (!result.ok) {
        ctx.ui.notify(describe(result, "Memory status unavailable."), "error");
        return;
      }
      // The CLI's human output is the report; ask for it without --json.
      try {
        const { stdout } = await execFileAsync(CLI, ["status"], { timeout: TOOL_TIMEOUT_MS });
        ctx.ui.notify(stdout.trim());
      } catch {
        ctx.ui.notify(JSON.stringify(result, null, 2));
      }
    },
  });
}
