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
 *   prompts/inject.md.j2   the policy — when and why to call these tools. One file,
 *                       editable without touching code, substituted with the
 *                       entries at {{memories}}. Authoritative: it is what every
 *                       session of every user gets.
 *   this file           mechanism — what each tool does and how to call it,
 *                       attached to the tool definitions where the model looks
 *                       when deciding to use one.
 *
 * Change policy in inject.md.j2. Only change these descriptions when what the tool
 * does changes.
 *
 *   memory render --session <id>   → the block injected at session start
 *   memory remember --text ...     → a raw observation, queued for the dream
 *   memory recall <id> --session … → full-resolution text, plus a vote
 */

import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { promisify } from "node:util";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const execFileAsync = promisify(execFile);

// ---------------------------------------------------------------------------
// tool text
// ---------------------------------------------------------------------------
//
// The wording of both tools lives in tools.json next to this file, so it can be
// reviewed and changed without editing code. It is adjacent rather than in
// prompts/ because this module is its only reader, and sitting beside index.ts
// resolves however pi loads the extension.
//
// These definitions say what each tool is and how to call it. They deliberately
// do not say when to call it: that policy is prompts/inject.md.j2, injected into
// every session, and it is the only place it should live.

type ToolText = {
  promptSnippet: string;
  description: string[];
  parameters: Record<string, string>;
};
type ToolsFile = { remember: ToolText; recall: ToolText };

const FALLBACK_TEXT: ToolsFile = {
  remember: {
    promptSnippet: "Record a durable observation for a future session",
    description: [
      "Record something durable that will still be useful in a future session.",
      "",
      "Include the specifics: the command, the path, the number, the reason behind a",
      "preference. A memory can never be made more detailed later than the thought it",
      "came from, so detail left out here is lost. One observation per call.",
      "",
      "(tools.json failed to load; this is the built-in fallback. Run `memory verify`.)",
    ],
    parameters: { thought: "The observation, written in full with the specifics." },
  },
  recall: {
    promptSnippet: "Fetch a memory's full text at full resolution",
    description: [
      "Read one memory at full resolution, and record that it was used.",
      "",
      "The list shows each entry at whatever level it has decayed to, often a handful",
      "of keywords. Every entry's full original text is kept, and this returns it.",
      "",
      "(tools.json failed to load; this is the built-in fallback. Run `memory verify`.)",
    ],
    parameters: { memory_id: "Id of the memory to recall, e.g. \"m3f\"." },
  },
};

function loadToolText(): ToolsFile {
  try {
    const parsed = JSON.parse(
      readFileSync(new URL("./tools.json", import.meta.url), "utf8"),
    ) as ToolsFile;
    for (const name of ["remember", "recall"] as const) {
      const t = parsed[name];
      if (!t?.promptSnippet || !Array.isArray(t.description) || !t.parameters) {
        throw new Error(`${name} needs promptSnippet, description[] and parameters{}`);
      }
    }
    return parsed;
  } catch (err) {
    // Degrade rather than refuse to load: a typo in a description file should not
    // take the whole memory system down with it. The failure is loud in pi's log
    // and in `memory verify`, and the fallback text says so in the tool itself.
    console.error(`[pi-memory] tools.json unusable: ${(err as Error).message}`);
    return FALLBACK_TEXT;
  }
}

const TOOL_TEXT = loadToolText();
const asDescription = (t: ToolText) => t.description.join("\n");

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
    description: asDescription(TOOL_TEXT.remember),
    promptSnippet: TOOL_TEXT.remember.promptSnippet,
    parameters: Type.Object({
      thought: Type.String({
        description: TOOL_TEXT.remember.parameters.thought,
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
    description: asDescription(TOOL_TEXT.recall),
    promptSnippet: TOOL_TEXT.recall.promptSnippet,
    parameters: Type.Object({
      memory_id: Type.String({
        description: TOOL_TEXT.recall.parameters.memory_id,
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
