import type { PluginServerContext } from "@getpaseo/plugin/server";
import { correctMemory, discardThoughts, dropMemory, viewMemory } from "./server/view";
import { memoryCorrectRpc, memoryDropRpc, memoryViewRpc, thoughtDiscardRpc } from "./shared/view";

// One read and three writes. Everything the panel knows comes from `memory view
// --json`, and every write is a CLI verb run with `--actor human`, so there is no
// second implementation of the store here.
export default function contribute(server: PluginServerContext) {
  server.handle(memoryViewRpc, viewMemory);
  server.handle(memoryCorrectRpc, correctMemory);
  server.handle(memoryDropRpc, dropMemory);
  server.handle(thoughtDiscardRpc, discardThoughts);
  return () => {};
}
