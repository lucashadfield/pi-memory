import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Pressable, ScrollView, Text, TextInput, View } from "react-native";
import type { TextStyle } from "react-native";
import type { PluginSurfaceProps } from "@getpaseo/plugin/client";
import { useRpc } from "@getpaseo/plugin/client";
import {
  memoryCorrectRpc,
  memoryDropRpc,
  thoughtDiscardRpc,
  memoryViewRpc,
  versionCounts,
  versionsByLevel,
} from "../shared/view";
import type { Memory, MemoryView, MutationResult, Thought } from "../shared/view";

/**
 * A window onto ~/.pi/memory.
 *
 * Everything on screen comes from one `memory view` call, and every token count
 * on screen was computed by the CLI. The panel does arithmetic on those numbers
 * (sums, widths, deltas) and nothing else: it never converts characters to
 * tokens itself, because the estimator has exactly one home and two copies of it
 * would eventually disagree.
 *
 * Four views: what the store is, what is waiting to be consolidated, where the
 * injected tokens actually go, and the injected text itself. The summary strip
 * at the top is the second of those made numeric: a bar whose segments are the
 * fixed preamble, core, and each decayable level, so "core is over its cap" and
 * "L2 is over its share" are visible at a glance rather than summed into one
 * ambiguous total.
 *
 * The write surface is deliberately small and human: edit an entry's text,
 * drop it from injection, or discard a pending thought. Dropping is soft — the
 * entry leaves the injected block and stays reachable behind the forgotten
 * toggle, versions and originating thought intact.
 */

type Tab = "memories" | "thoughts" | "prompt";

/** Grouped thousands, because the breakdown is meant to be read precisely. */
function fmtTokens(n: number): string {
  return String(Math.max(0, Math.round(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")} ${String(
    d.getHours(),
  ).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function fmtRelative(ms: number, now: number): string {
  const s = Math.max(0, Math.round((now - ms) / 1000));
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} hr ago`;
  const d = Math.round(h / 24);
  return `${d} day${d === 1 ? "" : "s"} ago`;
}

export function MemorySurface({ theme, layout }: PluginSurfaceProps) {
  const fetchView = useRpc(memoryViewRpc);
  const correct = useRpc(memoryCorrectRpc);
  const drop = useRpc(memoryDropRpc);
  const discard = useRpc(thoughtDiscardRpc);

  const query = useQuery<MemoryView>({
    queryKey: ["memory.view"],
    queryFn: async () => {
      try {
        return (await fetchView({ events: 60 })) as MemoryView;
      } catch {
        // A schema change to the panel's contract fails here, as a zod parse
        // error on a payload the daemon's stale plugin process produced. Name
        // the fix rather than reporting a generic read failure.
        throw new Error(
          "This panel and the daemon's plugin process are out of sync (the plugin was edited). Run `paseo daemon restart`.",
        );
      }
    },
  });

  const [tab, setTab] = useState<Tab>("memories");
  const [lens, setLens] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [showForgotten, setShowForgotten] = useState(false);
  const [showCore, setShowCore] = useState(false);
  const [filter, setFilter] = useState("");
  const [now, setNow] = useState(() => Date.now());

  // The loaded stamp is only honest if it ages while the panel sits open.
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);

  const s = makeStyles(theme, layout.compact);
  const data = query.data;

  async function editMemory(id: string, text: string): Promise<MutationResult> {
    const result = (await correct({ id, text })) as MutationResult;
    if (result.ok) await query.refetch();
    return result;
  }

  // force is the core override and nothing else: a plain drop must not be
  // recorded as forced in the event log.
  async function dropMemory(id: string, core: boolean): Promise<MutationResult> {
    const result = (await drop({ id, force: core })) as MutationResult;
    if (result.ok) await query.refetch();
    return result;
  }

  async function discardThought(id: number): Promise<MutationResult> {
    const result = (await discard({ ids: [id] })) as MutationResult;
    if (result.ok) await query.refetch();
    return result;
  }

  const visible = (data?.memories ?? [])
    .filter((m) => showForgotten || m.state === "active")
    .filter((m) => !showCore || m.core)
    .filter((m) => matches(m, filter));

  return (
    <View style={s.screen}>
      <View style={s.header}>
        <View style={s.headerTop}>
          <Text style={s.title}>Memory</Text>
          <View style={s.spacer} />
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Reload the store"
            onPress={() => query.refetch()}
            style={s.button}
          >
            <Text style={s.buttonText}>{query.isFetching ? "…" : "Reload"}</Text>
          </Pressable>
        </View>
        <Text style={s.subtitle}>
          {data
            ? `${data.headline.entries} entries · ${data.headline.core} core · ${
                data.headline.pending_thoughts
              } thoughts pending · loaded ${
                query.dataUpdatedAt ? fmtRelative(query.dataUpdatedAt, now) : "just now"
              }`
            : query.isLoading
              ? "loading…"
              : "unavailable"}
        </Text>
      </View>

      {data?.ok && <BudgetPanel data={data} theme={theme} styles={s} />}

      {!data?.ok && (
        <View style={s.notice}>
          <Text style={s.noticeText}>
            {query.error?.message ?? data?.error ?? "The memory store could not be read."}
          </Text>
        </View>
      )}

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
                ? ` ${data.headline.pending_thoughts}`
                : ""}
            </Text>
          </Pressable>
        ))}
      </View>

      {data?.ok && tab === "memories" && (
        <>
          <View style={s.toolbar}>
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
              accessibilityLabel="Toggle core entries"
              onPress={() => setShowCore((v) => !v)}
              style={[s.chip, showCore && s.chipActive]}
            >
              <Text style={[s.chipText, showCore && s.chipTextActive]}>core {data.headline.core}</Text>
            </Pressable>
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Toggle forgotten entries"
              onPress={() => setShowForgotten((v) => !v)}
              style={[s.chip, showForgotten && s.chipActive]}
            >
              <Text style={[s.chipText, showForgotten && s.chipTextActive]}>
                forgotten {data.headline.dropped + data.headline.merged}
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
            {visible.length === 0 && <Text style={s.muted}>Nothing matches.</Text>}
            {visible.map((m) => (
              <MemoryRow
                key={m.id}
                memory={m}
                lens={lens}
                styles={s}
                open={expanded === m.id}
                onToggle={() => setExpanded(expanded === m.id ? null : m.id)}
                onEdit={editMemory}
                onDrop={dropMemory}
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
            <ThoughtRow key={t.id} thought={t} styles={s} onDiscard={discardThought} />
          ))}
        </ScrollView>
      )}

      {data?.ok && tab === "prompt" && (
        <ScrollView style={s.scroll} contentContainerStyle={s.scrollContent}>
          <Text style={s.muted}>
            Exactly what is appended to the system prompt, from `memory render`.{" "}
            {fmtTokens(data.rendered.tokens)} tokens every session on this machine starts with.
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
          next squeeze L{data?.headline.next_to_squeeze ?? "—"}
          {" · "}
          {data?.instance.db ?? ""}
        </Text>
      </View>
    </View>
  );
}

// ---------------------------------------------------------------------------
// summary
// ---------------------------------------------------------------------------

/**
 * The token breakdown: one bar, one segment per bucket, one legend row, one
 * quoted total. Segments tile the block exactly — the fixed preamble is priced
 * as the remainder — so the legend adds up to the headline number by
 * construction and nothing on screen needs a rounding caveat.
 */
function BudgetPanel({
  data,
  theme,
  styles: s,
}: {
  data: MemoryView;
  theme: PluginSurfaceProps["theme"];
  styles: Styles;
}) {
  const c = theme.colors;
  const b = data.breakdown;
  const occ = data.occupancy;
  const l1 = b.level_tokens["1"] ?? 0;
  const l2 = b.level_tokens["2"] ?? 0;
  const l3 = b.level_tokens["3"] ?? 0;

  const segments = [
    { key: "preamble", tokens: b.preamble_tokens, color: c.border },
    { key: "core", tokens: b.core_tokens, color: c.statusWarning },
    { key: "L1", tokens: l1, color: c.accent },
    { key: "L2", tokens: l2, color: c.accent, opacity: 0.68 },
    { key: "L3", tokens: l3, color: c.accent, opacity: 0.4 },
  ];
  const total = Math.max(1, b.block_tokens);

  const legend: { label: string; value: string; over?: boolean; color: string; opacity?: number }[] = [
    { label: "preamble", value: fmtTokens(b.preamble_tokens), color: c.border },
    {
      label: "core",
      value: `${fmtTokens(b.core_tokens)} / ${fmtTokens(occ.core.cap_tokens)}`,
      over: occ.core.over,
      color: c.statusWarning,
    },
    { label: "L1", value: `${fmtTokens(l1)} / ${fmtTokens(occ.levels["1"]?.target_tokens ?? 0)}`, color: c.accent },
    {
      label: "L2",
      value: `${fmtTokens(l2)} / ${fmtTokens(occ.levels["2"]?.target_tokens ?? 0)}`,
      over: (occ.levels["2"]?.over_target ?? 0) > 0,
      color: c.accent,
      opacity: 0.68,
    },
    {
      label: "L3",
      value: `${fmtTokens(l3)} / ${fmtTokens(occ.levels["3"]?.target_tokens ?? 0)}`,
      over: (occ.levels["3"]?.over_target ?? 0) > 0,
      color: c.accent,
      opacity: 0.4,
    },
  ];

  return (
    <View style={s.summary}>
      <View style={s.summaryHead}>
        <Text style={s.summaryValue}>{fmtTokens(b.block_tokens)} tok</Text>
        <Text style={s.summaryLabel}>
          injected block
          {b.truncated ? " · truncated to fit" : ""}
          {data.headline.over_total || data.headline.core_over ? " · over budget" : ""}
        </Text>
      </View>
      <View style={s.bar}>
        {segments.map((seg) => (
          <View
            key={seg.key}
            style={[
              s.barSegment,
              {
                flex: seg.tokens,
                backgroundColor: seg.color,
                opacity: seg.opacity ?? 1,
              },
            ]}
          />
        ))}
        {b.preamble_tokens + b.core_tokens + l1 + l2 + l3 === 0 && (
          <View style={[s.barSegment, { flex: total, backgroundColor: c.border }]} />
        )}
      </View>
      <View style={s.legend}>
        {legend.map((item) => (
          <View key={item.label} style={s.legendItem}>
            <View
              style={[s.dot, { backgroundColor: item.color, opacity: item.opacity ?? 1 }]}
            />
            <Text style={s.legendLabel}>{item.label}</Text>
            <Text style={[s.legendValue, item.over && s.legendOver]}>{item.value}</Text>
          </View>
        ))}
      </View>
    </View>
  );
}

// ---------------------------------------------------------------------------
// rows
// ---------------------------------------------------------------------------

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
  onEdit,
  onDrop,
}: {
  memory: Memory;
  lens: number | null;
  styles: Styles;
  open: boolean;
  onToggle: () => void;
  onEdit: (id: string, text: string) => Promise<MutationResult>;
  onDrop: (id: string, core: boolean) => Promise<MutationResult>;
}) {
  const byLevel = useMemo(() => versionsByLevel(memory), [memory]);
  const counts = useMemo(() => versionCounts(memory), [memory]);
  const [picked, setPicked] = useState<number | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const shown = picked ?? lens ?? memory.level;
  const version = byLevel[shown] ?? byLevel[memory.level];
  const isInjected = shown === memory.level;

  const faded = memory.state !== "active";
  const canDrop = memory.state === "active";
  const editing = draft !== null;

  async function save() {
    if (draft === null || busy) return;
    const text = draft.trim();
    if (!text) {
      setError("an entry cannot be empty");
      return;
    }
    setBusy(true);
    setError(null);
    const result = await onEdit(memory.id, text);
    setBusy(false);
    if (result.ok) {
      setDraft(null);
    } else {
      setError(humanError(result));
    }
  }

  async function remove() {
    if (busy) return;
    setBusy(true);
    setError(null);
    const result = await onDrop(memory.id, memory.core);
    setBusy(false);
    setConfirming(false);
    if (!result.ok) setError(humanError(result));
  }

  return (
    <View style={[s.card, faded && s.cardFaded]}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Toggle ${memory.id}`}
        onPress={() => {
          if (!editing) onToggle();
        }}
      >
        <View style={s.cardHead}>
          <Text style={s.id}>[{memory.id}]</Text>
          <View style={[s.levelBadge, isInjected && s.levelBadgeInjected]}>
            <Text style={[s.levelBadgeText, isInjected && s.levelBadgeTextInjected]}>L{shown}</Text>
          </View>
          {memory.core && <Text style={s.core}>core</Text>}
          {memory.state !== "active" && <Text style={s.forgotten}>{memory.state}</Text>}
          <View style={s.spacer} />
          <Text style={s.count}>{fmtTokens(memory.injected_tokens)} tok</Text>
          <Text style={s.count}>↺{memory.uses}</Text>
        </View>
        {!open && (
          <Text numberOfLines={2} style={s.preview}>
            {version?.text ?? "(no text)"}
          </Text>
        )}
      </Pressable>

      {open && (
        <View style={s.body}>
          <View style={s.controls}>
            {[1, 2, 3].map((l) => {
              const has = Boolean(byLevel[l]);
              const active = shown === l;
              return (
                <Pressable
                  key={l}
                  disabled={!has || editing}
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
              <Text style={s.injectedNote}>injected</Text>
            ) : (
              <Text style={s.warnNote}>preview — injected is L{memory.level}</Text>
            )}
          </View>

          {editing ? (
            <>
              <TextInput
                value={draft}
                onChangeText={setDraft}
                multiline
                autoFocus
                style={s.editor}
              />
              <Text style={s.mutedSmall}>
                Saved as L{memory.level}, the version injection uses. Earlier versions are kept.
              </Text>
              <View style={s.actions}>
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Save changes"
                  disabled={busy}
                  onPress={save}
                  style={[s.action, s.actionPrimary, busy && s.actionDisabled]}
                >
                  <Text style={[s.actionText, s.actionTextPrimary]}>
                    {busy ? "saving…" : "Save"}
                  </Text>
                </Pressable>
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Cancel editing"
                  disabled={busy}
                  onPress={() => {
                    setDraft(null);
                    setError(null);
                  }}
                  style={s.action}
                >
                  <Text style={s.actionText}>Cancel</Text>
                </Pressable>
              </View>
            </>
          ) : (
            <Text selectable style={s.text}>
              {version?.text ?? "(no version at this level)"}
            </Text>
          )}

          <Text style={s.meta}>
            {version ? `${fmtTokens(version.tokens)} tok` : "—"}
            {version?.author ? ` · written by ${version.author}` : ""}
            {version ? ` · ${fmtTime(version.ts)}` : ""}
            {" · last recalled "}
            {memory.last_recalled ?? "never"}
            {memory.merged_into ? ` · merged into ${memory.merged_into}` : ""}
          </Text>

          {error && <Text style={s.error}>{error}</Text>}

          {!editing && (
            <View style={s.actions}>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel={`Edit ${memory.id}`}
                onPress={() => {
                  setDraft(version?.text ?? "");
                  setError(null);
                }}
                style={s.action}
              >
                <Text style={s.actionText}>Edit</Text>
              </Pressable>
              {canDrop &&
                (confirming ? (
                  <>
                    <Pressable
                      accessibilityRole="button"
                      accessibilityLabel={`Confirm dropping ${memory.id}`}
                      disabled={busy}
                      onPress={remove}
                      style={[s.action, s.actionDanger, busy && s.actionDisabled]}
                    >
                      <Text style={[s.actionText, s.actionTextDanger]}>
                        {busy ? "dropping…" : "Confirm drop"}
                      </Text>
                    </Pressable>
                    <Pressable
                      accessibilityRole="button"
                      accessibilityLabel="Cancel dropping"
                      onPress={() => setConfirming(false)}
                      style={s.action}
                    >
                      <Text style={s.actionText}>Cancel</Text>
                    </Pressable>
                  </>
                ) : (
                  <Pressable
                    accessibilityRole="button"
                    accessibilityLabel={`Drop ${memory.id} from injection`}
                    onPress={() => {
                      setConfirming(true);
                      setError(null);
                    }}
                    style={s.action}
                  >
                    <Text style={s.actionText}>Drop</Text>
                  </Pressable>
                ))}
            </View>
          )}
          {confirming && (
            <Text style={s.mutedSmall}>
              Dropping removes it from the injected block. The text stays in the archive.
              {memory.core ? " This entry is core, so this drop overrides the permanent flag." : ""}
            </Text>
          )}
        </View>
      )}
    </View>
  );
}

function ThoughtRow({
  thought,
  styles: s,
  onDiscard,
}: {
  thought: Thought;
  styles: Styles;
  onDiscard: (id: number) => Promise<MutationResult>;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const long = thought.text.length > 420;

  async function remove() {
    if (busy) return;
    setBusy(true);
    setError(null);
    const result = await onDiscard(thought.id);
    setBusy(false);
    if (!result.ok) setError(humanError(result));
  }

  return (
    <View style={s.card}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Toggle thought ${thought.id}`}
        onPress={() => setOpen((v) => !v)}
      >
        <View style={s.cardHead}>
          <Text style={s.id}>#{thought.id}</Text>
          <Text style={s.count}>{fmtTime(thought.ts)}</Text>
          <Text style={s.project} numberOfLines={1}>
            {thought.project ?? "unknown project"}
          </Text>
          <View style={s.spacer} />
          <Text style={s.count}>{fmtTokens(thought.tokens)} tok</Text>
        </View>
      </Pressable>
      <Text selectable style={s.text} numberOfLines={open || !long ? undefined : 4}>
        {thought.text}
      </Text>
      <View style={s.actions}>
        {long && (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Toggle full thought"
            onPress={() => setOpen((v) => !v)}
            style={s.action}
          >
            <Text style={s.actionText}>{open ? "Show less" : "Show all"}</Text>
          </Pressable>
        )}
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`Discard thought ${thought.id}`}
          disabled={busy}
          onPress={remove}
          style={[s.action, busy && s.actionDisabled]}
        >
          <Text style={[s.actionText, s.actionTextDanger]}>
            {busy ? "discarding…" : "Discard"}
          </Text>
        </Pressable>
      </View>
      {error && <Text style={s.error}>{error}</Text>}
    </View>
  );
}

/** Every refusal is a named invariant, so it can be said rather than shown raw. */
function humanError(result: MutationResult): string {
  switch (result.error) {
    case "core_is_permanent":
      return "Core memories cannot be dropped. Edit it instead.";
    case "not_active":
      return "This entry has already been forgotten.";
    case "unknown_id":
      return "That entry no longer exists.";
    case "unknown_thought":
      return "That thought has already been dispositioned.";
    case "empty_text":
      return "An entry cannot be empty.";
    case null:
      return "The change was refused.";
    default:
      return `${result.error}${result.detail ? ` — ${result.detail}` : ""}`;
  }
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
    header: { paddingHorizontal: pad, paddingTop: pad, paddingBottom: 10 },
    headerTop: { flexDirection: "row", alignItems: "center", gap: 8 },
    tabBar: {
      flexDirection: "row",
      alignItems: "center",
      gap: 4,
      marginHorizontal: pad,
      marginBottom: 8,
      padding: 3,
      borderRadius: 9,
      backgroundColor: c.surface1,
      alignSelf: "flex-start",
    },
    controls: { flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap", paddingVertical: 6 },
    toolbar: {
      flexDirection: "row",
      alignItems: "center",
      gap: 6,
      flexWrap: "wrap",
      paddingHorizontal: pad,
      paddingVertical: 6,
    },
    tab: { paddingVertical: 6, paddingHorizontal: 12, borderRadius: 7 },
    tabActive: { backgroundColor: c.surface2 },
    chip: {
      paddingVertical: 4,
      paddingHorizontal: 10,
      borderRadius: 999,
      borderWidth: 1,
      borderColor: c.border,
      backgroundColor: "transparent",
    },
    chipActive: { backgroundColor: c.accent, borderColor: c.accent },
    chipDisabled: { opacity: 0.35 },
    button: { paddingVertical: 5, paddingHorizontal: 10, borderRadius: 7, backgroundColor: c.surface1 },
    spacer: { flex: 1 },
    scroll: { flex: 1 },
    scrollContent: { paddingHorizontal: pad, paddingBottom: 24, gap: 8 },
    summary: {
      marginHorizontal: pad,
      marginBottom: 10,
      borderWidth: 1,
      borderColor: c.border,
      borderRadius: 10,
      padding: 12,
      backgroundColor: c.surface1,
    },
    summaryHead: { flexDirection: "row", alignItems: "baseline", gap: 8 },
    bar: {
      flexDirection: "row",
      height: 9,
      borderRadius: 5,
      overflow: "hidden",
      backgroundColor: c.surface2,
      marginTop: 8,
    },
    barSegment: { height: 9 },
    legend: { flexDirection: "row", flexWrap: "wrap", gap: 12, marginTop: 10 },
    legendItem: { flexDirection: "row", alignItems: "center", gap: 5 },
    dot: { width: 7, height: 7, borderRadius: 4 },
    card: {
      borderWidth: 1,
      borderColor: c.border,
      borderRadius: 10,
      padding: 12,
      backgroundColor: c.surface1,
      gap: 7,
    },
    cardFaded: { opacity: 0.5 },
    cardHead: { flexDirection: "row", alignItems: "center", gap: 8, flexWrap: "wrap" },
    body: { gap: 7 },
    levelBadge: {
      paddingHorizontal: 6,
      paddingVertical: 1,
      borderRadius: 5,
      backgroundColor: c.surface2,
    },
    levelBadgeInjected: { backgroundColor: c.accent },
    editor: {
      minHeight: 96,
      padding: 10,
      borderRadius: 8,
      borderWidth: 1,
      borderColor: c.accent,
      backgroundColor: c.surface0,
      color: c.foreground,
      fontSize: 13,
      lineHeight: 19,
      textAlignVertical: "top",
    },
    promptBox: {
      borderWidth: 1,
      borderColor: c.border,
      borderRadius: 10,
      padding: 12,
      backgroundColor: c.surface1,
    },
    notice: {
      marginHorizontal: pad,
      marginBottom: 10,
      padding: 12,
      borderRadius: 10,
      borderWidth: 1,
      borderColor: c.statusDanger,
      backgroundColor: c.surface1,
    },
    input: {
      marginHorizontal: pad,
      marginBottom: 8,
      paddingVertical: 6,
      paddingHorizontal: 10,
      borderRadius: 8,
      borderWidth: 1,
      borderColor: c.border,
      backgroundColor: c.surface1,
      color: c.foreground,
      fontSize: 13,
    },
    actions: { flexDirection: "row", alignItems: "center", gap: 6, flexWrap: "wrap", marginTop: 2 },
    action: {
      paddingVertical: 5,
      paddingHorizontal: 11,
      borderRadius: 7,
      borderWidth: 1,
      borderColor: c.border,
      backgroundColor: "transparent",
    },
    actionPrimary: { backgroundColor: c.accent, borderColor: c.accent },
    actionDanger: { borderColor: c.statusDanger },
    actionDisabled: { opacity: 0.5 },
    footer: {
      borderTopWidth: 1,
      borderTopColor: c.border,
      paddingHorizontal: pad,
      paddingVertical: 6,
    },
  };

  const text: Record<string, TextStyle> = {
    title: { color: c.foreground, fontSize: 17, fontWeight: "600" },
    subtitle: { color: c.foregroundMuted, fontSize: 12, marginTop: 3 },
    tabText: { color: c.foregroundMuted, fontSize: 12 },
    tabTextActive: { color: c.foreground, fontWeight: "600" },
    buttonText: { color: c.foreground, fontSize: 12 },
    chipText: { color: c.foreground, fontSize: 12 },
    chipTextActive: { color: c.accentForeground, fontWeight: "600" },
    summaryValue: { color: c.foreground, fontSize: 20, fontWeight: "700", fontVariant: ["tabular-nums"] },
    summaryLabel: { color: c.foregroundMuted, fontSize: 11 },
    legendLabel: { color: c.foregroundMuted, fontSize: 11 },
    legendValue: { color: c.foreground, fontSize: 11, fontVariant: ["tabular-nums"] },
    legendOver: { color: c.statusWarning, fontWeight: "600" },
    id: { color: c.foreground, fontSize: 13, fontWeight: "600", fontFamily: "monospace" },
    count: { color: c.foregroundMuted, fontSize: 11, fontVariant: ["tabular-nums"] },
    core: { color: c.statusWarning, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5 },
    forgotten: { color: c.statusDanger, fontSize: 10, textTransform: "uppercase", letterSpacing: 0.5 },
    levelBadgeText: { color: c.foreground, fontSize: 10, fontWeight: "600" },
    levelBadgeTextInjected: { color: c.accentForeground },
    preview: { color: c.foregroundMuted, fontSize: 12, marginTop: 5, lineHeight: 17 },
    text: { color: c.foreground, fontSize: 13, lineHeight: 19, marginTop: 4 },
    promptText: { color: c.foreground, fontSize: 12, lineHeight: 17, fontFamily: "monospace" },
    meta: { color: c.foregroundMuted, fontSize: 11, marginTop: 3 },
    actionText: { color: c.foreground, fontSize: 12 },
    actionTextPrimary: { color: c.accentForeground, fontWeight: "600" },
    actionTextDanger: { color: c.statusDanger },
    muted: { color: c.foregroundMuted, fontSize: 12 },
    mutedSmall: { color: c.foregroundMuted, fontSize: 10, marginTop: 3 },
    project: { color: c.foregroundMuted, fontSize: 10, maxWidth: 200 },
    injectedNote: { color: c.statusSuccess, fontSize: 10, fontWeight: "600" },
    warnNote: { color: c.statusWarning, fontSize: 10 },
    error: { color: c.statusDanger, fontSize: 11, marginTop: 3 },
    noticeText: { color: c.foreground, fontSize: 12 },
    footerText: { color: c.foregroundMuted, fontSize: 10 },
  };

  return Object.assign({} as Styles, view, text);
}
