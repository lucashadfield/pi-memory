import type { PluginServerContext } from "@getpaseo/plugin/server";
import { viewMemory } from "./server/view";
import { memoryViewRpc } from "./shared/view";

// The whole server half is one handler. Everything the panel knows comes from
// `memory view --json`, so there is no second implementation of the store here.
export default function contribute(server: PluginServerContext) {
  server.handle(memoryViewRpc, viewMemory);
  return () => {};
}
