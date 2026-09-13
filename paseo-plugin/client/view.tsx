import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Pressable, ScrollView, Text, TextInput, View } from "react-native";
import type { TextStyle } from "react-native";
import type { PluginSurfaceProps } from "@getpaseo/plugin/client";
import { useRpc } from "@getpaseo/plugin/client";
import { memoryViewRpc, versionCounts, versionsByLevel } from "../shared/view";
import type { Memory, MemoryView, Thought } from "../shared/view";

/**
 * A window onto ~/.pi/memory.
 *
 * Everything on screen comes from one `memory view` call. The panel deliberately
 * renders rather than reimplements: the levels shown, the token counts and the
 * injected block are all the CLI's answers, so a disagreement between this panel
 * and `memory status` would be a bug in one place, not a discrepancy between two.
 *
 * Three views, because those are the three things the store is:
 *   Memories — what survives, at whatever level it has decayed to, with the
 *              ability to walk an entry down its own compression ladder.
 *   Thoughts — the raw observations a live agent recorded and no dream has
 *              dispositioned yet.
 *   Prompt   — the exact text appended to the system prompt, verbatim.
 *
 * The level lens is the point of the thing. "injected" is what the agent really
 * sees; picking L1/L2/L3 shows what the whole store would look like at that
 * resolution, which is the fastest way to judge whether compression is losing
 * something that matters.
 */

type Tab = "memories" | "thoughts" | "prompt";

function fmtTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")} ${String(
    d.getHours(),
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function MemorySurface({ theme, layout }: PluginSurfaceProps) {
  const fetchView = useRpc(memoryViewRpc);
  const query = useQuery<MemoryView>({
    queryKey: ["memory.view"],
    queryFn: () => fetchView({ events: 60 }) as Promise<MemoryView>,
  });

  const [tab, setTab] = useState<Tab>("memories");
  const [lens, setLens] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showForgotten, setShowForgotten] = useState(false);
  const [filter, setFilter] = useState("");

  const s = makeStyles(theme, layout.compact);
  const data = query.data;

  return (
    <View style={s.screen}>
      <View style={s.header}>
        <Text style={s.title}>Memory</Text>
        {data ? (
          <Text style={s.subtitle}>
            {data.headline.entries} entries · {fmtTokens(data.headline.decayable_tokens)}/
            {fmtTokens(data.headline.total_tokens)} tok
            {data.headline.over_total ? " · over budget" : ""}
            {data.headline.core_over ? " · core over cap" : ""}
            {" · "}
            {data.headline.pending_thoughts} pending
          </Text>
        ) : (
          <Text style={s.subtitle}>{query.isLoading ? "loading…" : "unavailable"}</Text>
        )}
      </View>

      <View style={s.tabBar}>
        {(["memories", "thoughts", "prompt"] as Tab[]).map((t) => (
          <Pressable
            key={t}
            accessibilityRole="button"
            accessibilityLabel={`Show ${t}`}
            onPress={() => setTab(t)}
            style={[s.tab, tab === t && s.tabActive]}
          >
            <Text style={[s.tabText, tab === t && s.tabTextActive]}>
              {t === "memories" ? "Memories" : t === "thoughts" ? "Thoughts" : "Prompt"}
              {t === "thoughts" && data?.headline.pending_thoughts
                ? ` (${data.headline.pending_thoughts})`
                : ""}
            </Text>
          </Pressable>
        ))}
        <View style={s.spacer} />
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Reload"
          onPress={() => query.refetch()}
          style={s.button}
        >
          <Text style={s.buttonText}>{query.isFetching ? "…" : "Reload"}</Text>
        </Pressable>
      </View>

      {!data?.ok && (
        <View style={s.notice}>
          <Text style={s.noticeText}>
            {data?.error ?? "The memory store could not be read."}
          </Text>
        </View>
      )}

      {data?.ok && tab === "memories" && (
        <>
          <View style={s.controls}>
            <Text style={s.controlLabel}>level</Text>
            {[null, 1, 2, 3].map((l) => (
              <Pressable
                key={String(l)}
                accessibilityRole="button"
                accessibilityLabel={l === null ? "Show injected levels" : `Show level ${l}`}
                onPress={() => setLens(l)}
                style={[s.chip, lens === l && s.chipActive]}
              >
                <Text style={[s.chipText, lens === l && s.chipTextActive]}>
                  {l === null ? "injected" : `L${l}`}
                </Text>
              </Pressable>
            ))}
            <View style={s.spacer} />
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Toggle forgotten entries"
              onPress={() => setShowForgotten((v) => !v)}
              style={[s.chip, showForgotten && s.chipActive]}
            >
              <Text style={[s.chipText, showForgotten && s.chipTextActive]}>
                forgotten ({data.headline.dropped + data.headline.merged})
              </Text>
            </Pressable>
          </View>
          <TextInput
            value={filter}
            onChangeText={setFilter}
            placeholder="filter by id or text…"
            placeholderTextColor={theme.colors.foregroundMuted}
            style={s.input}
          />
          <ScrollView style={s.scroll} contentContainerStyle={s.scrollContent}>
            {data.memories
              .filter((m) => showForgotten || m.state === "active")
              .filter((m) => matches(m, filter))
              .map((m) => (
                <MemoryRow
                  key={m.id}
                  memory={m}
                  lens={lens}
                  styles={s}
                  open={expanded === m.id}
                  onToggle={() => setExpanded(expanded === m.id ? null : m.id)}
                />
              ))}
          </ScrollView>
        </>
      )}

      {data?.ok && tab === "thoughts" && (
        <ScrollView style={s.scroll} contentContainerStyle={s.scrollContent}>
          {data.thoughts.length === 0 && (
            <Text style={s.muted}>Nothing pending. The last dream dispositioned everything.</Text>
          )}
          {data.thoughts.map((t) => (
            <ThoughtRow key={t.id} thought={t} styles={s} />
          ))}
        </ScrollView>
      )}

      {data?.ok && tab === "prompt" && (
        <ScrollView style={s.scroll} contentContainerStyle={s.scrollContent}>
          <Text style={s.muted}>
            Exactly what is appended to the system prompt, from `memory render`.{" "}
            {fmtTokens(data.rendered.tokens)} tokens, {data.rendered.block.length} characters.
            This is the text every session on this machine starts with.
          </Text>
          <View style={s.promptBox}>
            <Text selectable style={s.promptText}>
              {data.rendered.block}
            </Text>
          </View>
        </ScrollView>
      )}

      <View style={s.footer}>
        <Text style={s.footerText}>
          {lens === null ? "showing injected levels" : `showing L${lens} where it exists`}
          {" · "}
          {[
            `L1 ${data?.occupancy.levels["1"]?.entries ?? 0}`,
            `L2 ${data?.occupancy.levels["2"]?.entries ?? 0}`,
            `L3 ${data?.occupancy.levels["3"]?.entries ?? 0}`,
            `core ${data?.headline.core ?? 0}`,
          ].join(" / ")}
          {" · "}
          next squeeze L{data?.headline.next_to_squeeze ?? "—"}
        </Text>
      </View>
    </View>
  );
}

function matches(m: Memory, filter: string): boolean {
  const q = filter.trim().toLowerCase();
  if (!q) return true;
  if (m.id.toLowerCase().includes(q)) return true;
  return m.versions.some((v) => v.text.toLowerCase().includes(q));
}

function MemoryRow({
  memory,
  lens,
  styles: s,
  open,
  onToggle,
}: {
  memory: Memory;
  lens: number | null;
  styles: Styles;
  open: boolean;
  onToggle: () => void;
}) {
  const byLevel = useMemo(() => versionsByLevel(memory), [memory]);
  const counts = useMemo(() => versionCounts(memory), [memory]);
  const [picked, setPicked] = useState<number | null>(null);

  const shown = picked ?? lens ?? memory.level;
  const version = byLevel[shown] ?? byLevel[memory.level];
  const isInjected = shown === memory.level;
  const available = Object.keys(byLevel)
    .map(Number)
    .sort();

  const faded = memory.state !== "active";

  return (
    <View style={[s.card, faded && s.cardFaded]}>
      <Pressable accessibilityRole="button" accessibilityLabel={`Toggle ${memory.id}`} onPress={onToggle}>
        <View style={s.cardHead}>
          <Text style={s.id}>[{memory.id}]</Text>
          <View style={[s.badge, isInjected && s.badgeInjected]}>
            <Text style={s.badgeText}>L{shown}</Text>
          </View>
          {memory.core && <Text style={s.core}>core</Text>}
          {memory.state !== "active" && <Text style={s.forgotten}>{memory.state}</Text>}
          <Text style={s.uses}>↺{memory.uses}</Text>
          {memory.last_recalled === null && <Text style={s.mutedSmall}>never recalled</Text>}
          <View style={s.spacer} />
          <Text style={s.mutedSmall}>{memory.injected_chars}c injected</Text>
        </View>
        {!open && (
          <Text numberOfLines={2} style={s.preview}>
            {version?.text ?? "(no text)"}
          </Text>
        )}
      </Pressable>

      {open && (
        <View>
          <View style={s.controls}>
            {[1, 2, 3].map((l) => {
              const has = Boolean(byLevel[l]);
              const active = shown === l;
              return (
                <Pressable
                  key={l}
                  disabled={!has}
                  accessibilityRole="button"
                  accessibilityLabel={`Show level ${l} of ${memory.id}`}
                  onPress={() => setPicked(l)}
                  style={[s.chip, active && s.chipActive, !has && s.chipDisabled]}
                >
                  <Text style={[s.chipText, active && s.chipTextActive]}>
                    L{l}
                    {has ? (counts[l] > 1 ? ` (${counts[l]})` : "") : " —"}
                  </Text>
                </Pressable>
              );
            })}
            <View style={s.spacer} />
            {isInjected ? (
              <Text style={s.injectedNote}>this is what is injected</Text>
            ) : (
              <Text style={s.warnNote}>
                preview — injected is L{memory.level}
              </Text>
            )}
          </View>
          <Text selectable style={s.body}>
            {version?.text ?? "(no version at this level)"}
          </Text>
          <Text style={s.meta}>
            {version ? `${version.text.length}c` : "—"}
            {version?.author ? ` · written by ${version.author}` : ""}
            {version ? ` · ${fmtTime(version.ts)}` : ""}
            {" · "}noted {memory.noted ?? "—"}
            {" · last recalled "}
            {memory.last_recalled ?? "never"}
            {memory.merged_into ? ` · merged into ${memory.merged_into}` : ""}
          </Text>
        </View>
      )}
    </View>
  );
}

function ThoughtRow({
  thought,
  styles: s,
}: {
  thought: Thought;
  styles: Styles;
}) {
  const [open, setOpen] = useState(false);
  const long = thought.text.length > 320;
  return (
    <View style={s.card}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Toggle thought ${thought.id}`}
        onPress={() => setOpen((v) => !v)}
      >
        <View style={s.cardHead}>
          <Text style={s.id}>#{thought.id}</Text>
          <Text style={s.mutedSmall}>{fmtTime(thought.ts)}</Text>
          <Text style={s.project} numberOfLines={1}>
            {thought.project ?? "unknown project"}
          </Text>
          <View style={s.spacer} />
          <Text style={s.mutedSmall}>{thought.text.length}c</Text>
        </View>
      </Pressable>
      <Text selectable style={s.body} numberOfLines={open || !long ? undefined : 4}>
        {thought.text}
      </Text>
      {long && (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Toggle full thought"
          onPress={() => setOpen((v) => !v)}
        >
          <Text style={s.link}>{open ? "show less" : "show all"}</Text>
        </Pressable>
      )}
    </View>
  );
}

// ---------------------------------------------------------------------------
// styles
// ---------------------------------------------------------------------------

/**
 * Every style in one flat map, typed as TextStyle.
 *
 * TextStyle extends ViewStyle in React Native, so one record serves both a View
 * and a Text without a cast at each call site. Splitting them by destination
 * would mean a cast on every `style=` prop, which is worse than one widening.
 */
type Styles = Record<string, TextStyle>;

function makeStyles(theme: PluginSurfaceProps["theme"], compact: boolean): Styles {
  const pad = compact ? 10 : 16;
  const c = theme.colors;

  const view: Record<string, TextStyle> = {
    screen: { flex: 1, backgroundColor: c.surface0 },
    header: { paddingHorizontal: pad, paddingTop: pad, paddingBottom: 6 },
    tabBar: { flexDirection: "row", alignItems: "center", gap: 6, paddingHorizontal: pad, paddingBottom: 8 },
    controls: { flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap", paddingVertical: 6 },
    controlLabel: { color: c.foregroundMuted, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5 },
    tab: { paddingVertical: 6, paddingHorizontal: 10, borderRadius: 6, backgroundColor: c.surface1 },
    tabActive: { backgroundColor: c.accent },
    chip: {
      paddingVertical: 4,
      paddingHorizontal: 8,
      borderRadius: 5,
      borderWidth: 1,
      borderColor: c.border,
      backgroundColor: c.surface1,
    },
    chipActive: { backgroundColor: c.accent, borderColor: c.accent },
    chipDisabled: { opacity: 0.4 },
    button: { paddingVertical: 6, paddingHorizontal: 10, borderRadius: 6, backgroundColor: c.surface2 },
    spacer: { flex: 1 },
    scroll: { flex: 1 },
    scrollContent: { paddingHorizontal: pad, paddingBottom: 24, gap: 8 },
    card: {
      borderWidth: 1,
      borderColor: c.border,
      borderRadius: 8,
      padding: 10,
      backgroundColor: c.surface1,
      gap: 6,
    },
    cardFaded: { opacity: 0.55 },
    cardHead: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
    badge: {
      paddingHorizontal: 6,
      paddingVertical: 1,
      borderRadius: 4,
      backgroundColor: c.surface2,
      borderWidth: 1,
      borderColor: c.border,
    },
    badgeInjected: { borderColor: c.accent },
    promptBox: {
      borderWidth: 1,
      borderColor: c.border,
      borderRadius: 8,
      padding: 12,
      backgroundColor: c.surface1,
    },
    notice: {
      margin: pad,
      padding: 12,
      borderRadius: 8,
      borderWidth: 1,
      borderColor: c.statusDanger,
      backgroundColor: c.surface1,
    },
    input: {
      marginHorizontal: pad,
      marginBottom: 6,
      paddingVertical: 6,
      paddingHorizontal: 10,
      borderRadius: 6,
      borderWidth: 1,
      borderColor: c.border,
      backgroundColor: c.surface1,
      color: c.foreground,
      fontSize: 13,
    },
    footer: {
      borderTopWidth: 1,
      borderTopColor: c.border,
      paddingHorizontal: pad,
      paddingVertical: 6,
    },
  };

  const text: Record<string, TextStyle> = {
    title: { color: c.foreground, fontSize: 16, fontWeight: "600" },
    subtitle: { color: c.foregroundMuted, fontSize: 12, marginTop: 2 },
    tabText: { color: c.foreground, fontSize: 12 },
    tabTextActive: { color: c.accentForeground, fontWeight: "600" },
    buttonText: { color: c.foreground, fontSize: 12 },
    chipText: { color: c.foreground, fontSize: 12 },
    chipTextActive: { color: c.accentForeground, fontWeight: "600" },
    id: { color: c.foreground, fontSize: 13, fontWeight: "600" },
    core: { color: c.statusWarning, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4 },
    forgotten: { color: c.statusDanger, fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4 },
    uses: { color: c.foregroundMuted, fontSize: 11 },
    badgeText: { color: c.foreground, fontSize: 10 },
    preview: { color: c.foregroundMuted, fontSize: 12, marginTop: 4 },
    body: { color: c.foreground, fontSize: 13, lineHeight: 19, marginTop: 4 },
    promptText: { color: c.foreground, fontSize: 12, lineHeight: 17, fontFamily: "monospace" },
    meta: { color: c.foregroundMuted, fontSize: 11, marginTop: 4 },
    muted: { color: c.foregroundMuted, fontSize: 12 },
    mutedSmall: { color: c.foregroundMuted, fontSize: 10 },
    project: { color: c.foregroundMuted, fontSize: 10, maxWidth: 220 },
    injectedNote: { color: c.statusSuccess, fontSize: 10 },
    warnNote: { color: c.statusWarning, fontSize: 10 },
    link: { color: c.accent, fontSize: 11, marginTop: 4 },
    noticeText: { color: c.foreground, fontSize: 12 },
    footerText: { color: c.foregroundMuted, fontSize: 10 },
  };

  return Object.assign({} as Styles, view, text);
}
