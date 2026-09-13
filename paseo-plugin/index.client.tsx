import type { PluginClientContext } from "@getpaseo/plugin/client";
import { MemorySurface } from "./client/view";

// A sidebar surface rather than a workspace panel: the memory store is global to
// the machine, not scoped to whichever workspace is open, so it belongs next to
// the other whole-app views rather than inside a project.
export default function contribute(client: PluginClientContext) {
  client.addSurface("memory", MemorySurface);
  client.addSidebarItem({
    id: "memory",
    title: "Memory",
    icon: "Brain",
    surface: "memory",
  });
  return () => {};
}
