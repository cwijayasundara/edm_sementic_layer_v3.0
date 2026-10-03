"use client";
import { Network, RotateCcw } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useSession } from "@/components/SessionProvider";
import { ReasoningTimeline } from "@/components/ReasoningTimeline";
import { ApiError, Unauthorized, api } from "@/lib/api";
import { EXPIRED_TEXT } from "@/lib/copy";
import { KIND_LABEL, KIND_STYLE, type SourceUse, edgeText, originText, sourcesUsed } from "@/lib/graph";
import { LINEAGE_KINDS, type Lineage, type LineageKind, type Trace } from "@/lib/schemas";
import { touchedIds } from "@/lib/trace";

const ContextGraph = dynamic(() => import("@/components/ContextGraph").then((m) => m.ContextGraph),
  { ssr: false, loading: () => <Skeleton className="h-[min(560px,60vh)] w-full" /> });

export const GRAPH_UNAVAILABLE = "The context graph is unavailable.";
export const GRAPH_EMPTY = "No context is available for this result.";
export const NOT_GOVERNED = "Free-form query: no governed lineage";
export const SHORTENED = "Graph shortened to 150 nodes";
export const TRACE_MISSING = "This reasoning trace has expired or is not available.";
export const TRACE_UNAVAILABLE = "The reasoning trace is unavailable.";
export const TRACE_NONE = "This widget was re-run from a saved dashboard; it has no reasoning trace.";

type Tab = "graph" | "reasoning";
type TraceLoad = { kind: "idle" } | { kind: "loading" } | { kind: "ok"; trace: Trace } | { kind: "missing" } | { kind: "error" };
type Load = { kind: "loading" } | { kind: "ok"; lineage: Lineage } | { kind: "expired" } | { kind: "error" };

export function ContextGraphDialog({ open, onOpenChange, handle, title, runId, initialTab }:
  { open: boolean; onOpenChange: (open: boolean) => void; title: string; handle?: string; runId?: string; initialTab?: Tab }) {
  const { call } = useSession();
  const callRef = useRef(call);
  callRef.current = call;
  const startTab: Tab = initialTab ?? (handle ? "graph" : "reasoning");
  const startRef = useRef(startTab);
  startRef.current = startTab;
  const [tab, setTab] = useState<Tab>(startTab);
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [traceLoad, setTraceLoad] = useState<TraceLoad>({ kind: "idle" });
  const [nonce, setNonce] = useState(0);
  const [traceNonce, setTraceNonce] = useState(0);
  const [layout, setLayout] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [focusSource, setFocusSource] = useState<string | null>(null);
  // stable identity: a new Set each render would make the graph re-apply its option and restart the layout
  const highlight = useMemo(() => (traceLoad.kind === "ok" ? touchedIds(traceLoad.trace) : undefined), [traceLoad]);

  useEffect(() => { if (open) setTab(startRef.current); }, [open]);

  useEffect(() => {
    if (!open || !handle) return;
    let cancelled = false;
    setLoad({ kind: "loading" });
    setSelected(null);
    setFocusSource(null);
    callRef.current((t) => api.lineage(t, handle))
      .then((lineage) => { if (!cancelled) setLoad({ kind: "ok", lineage }); })
      .catch((e) => {
        if (cancelled || e instanceof Unauthorized) return;
        setLoad(e instanceof ApiError && e.status === 404 ? { kind: "expired" } : { kind: "error" });
      });
    return () => { cancelled = true; };
  }, [open, handle, nonce]);

  useEffect(() => {
    if (!open || !runId) { setTraceLoad({ kind: "idle" }); return; }
    let cancelled = false;
    setTraceLoad({ kind: "loading" });
    callRef.current((t) => api.trace(t, runId))
      .then((trace) => { if (!cancelled) setTraceLoad({ kind: "ok", trace }); })
      .catch((e) => {
        if (cancelled || e instanceof Unauthorized) return;
        setTraceLoad(e instanceof ApiError && e.status === 404 ? { kind: "missing" } : { kind: "error" });
      });
    return () => { cancelled = true; };
  }, [open, runId, traceNonce]);

  const tabs: { id: Tab; label: string; disabled: boolean }[] = [
    { id: "graph", label: "Graph", disabled: !handle }, { id: "reasoning", label: "Reasoning", disabled: false }];
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-[min(92vw,1400px)]!">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Network className="size-4" aria-hidden />Context graph</DialogTitle>
          <DialogDescription>{title}</DialogDescription>
        </DialogHeader>
        <div role="tablist" className="flex gap-1 border-b">
          {tabs.map((t) => (
            <button key={t.id} type="button" role="tab" id={`cg-tab-${t.id}`} aria-selected={tab === t.id}
              aria-controls={`cg-panel-${t.id}`} disabled={t.disabled} onClick={() => setTab(t.id)}
              className={`-mb-px border-b-2 px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50 ${
                tab === t.id ? "border-[var(--prism-navy)] text-[var(--prism-ink)]" : "border-transparent text-muted-foreground"}`}>
              {t.label}</button>))}
        </div>
        {tab === "graph" && handle && (
          <div role="tabpanel" id="cg-panel-graph" aria-labelledby="cg-tab-graph">
            <Body load={load} layout={layout} selected={selected} onSelect={setSelected}
              highlight={highlight} focusSource={focusSource} onFocusSource={setFocusSource}
              onRetry={() => setNonce((n) => n + 1)} onReset={() => { setLayout((n) => n + 1); setSelected(null); }} />
          </div>)}
        {tab === "reasoning" && (
          <div role="tabpanel" id="cg-panel-reasoning" aria-labelledby="cg-tab-reasoning">
            <ReasoningPanel runId={runId} load={traceLoad} onRetry={() => setTraceNonce((n) => n + 1)}
              onNode={(id) => { setSelected(id); if (handle) setTab("graph"); }} />
          </div>)}
      </DialogContent>
    </Dialog>
  );
}

function ReasoningPanel({ runId, load, onRetry, onNode }:
  { runId?: string; load: TraceLoad; onRetry: () => void; onNode: (id: string) => void }) {
  if (!runId) return <Notice>{TRACE_NONE}</Notice>;
  if (load.kind === "idle" || load.kind === "loading") return <Skeleton className="h-48 w-full" />;
  if (load.kind === "missing") return <Notice>{TRACE_MISSING}</Notice>;
  if (load.kind === "error") {
    return <Notice><p>{TRACE_UNAVAILABLE}</p>
      <Button size="sm" variant="outline" className="mt-3" onClick={onRetry}>Retry</Button></Notice>;
  }
  return <ReasoningTimeline trace={load.trace} onNode={onNode} />;
}

function Notice({ children }: { children: React.ReactNode }) {
  return <div className="grid min-h-[320px] place-items-center text-center text-sm text-muted-foreground"><div>{children}</div></div>;
}

function Body({ load, layout, selected, onSelect, highlight, focusSource, onFocusSource, onRetry, onReset }: {
  load: Load; layout: number; selected: string | null; onSelect: (id: string) => void; highlight?: Set<string>;
  focusSource: string | null; onFocusSource: (id: string | null) => void; onRetry: () => void; onReset: () => void;
}) {
  if (load.kind === "loading") return <Skeleton className="h-[min(560px,60vh)] w-full" />;
  if (load.kind === "expired") return <Notice>{EXPIRED_TEXT}</Notice>;
  if (load.kind === "error") {
    return <Notice><p>{GRAPH_UNAVAILABLE}</p>
      <Button size="sm" variant="outline" className="mt-3" onClick={onRetry}>Retry</Button></Notice>;
  }
  const g = load.lineage;
  if (g.nodes.length === 0) return <Notice>{GRAPH_EMPTY}</Notice>;
  const node = g.nodes.find((n) => n.id === selected);
  const labelOf = (id: string) => g.nodes.find((n) => n.id === id)?.label ?? id;
  const links = node ? g.edges.filter((e) => e.from === node.id || e.to === node.id) : [];
  const uses = sourcesUsed(g);
  return (
    <div className="space-y-3">
      {(!g.governed || g.truncated) && (
        <div className="flex flex-wrap gap-2 text-xs">
          {!g.governed && <span className="rounded bg-[var(--prism-paper)] px-2 py-1">{NOT_GOVERNED}</span>}
          {g.truncated && <span className="rounded bg-[var(--prism-paper)] px-2 py-1">{SHORTENED}</span>}
        </div>)}
      {uses.length > 0 && <SourceStrip uses={uses} focus={focusSource} onFocus={onFocusSource} />}
      <div className="grid gap-3 lg:grid-cols-[1fr_18rem]">
        <div className="relative rounded-lg border bg-[#fbfcfd]">
          <Button size="sm" variant="outline" className="absolute top-2 left-2 z-10" onClick={onReset}>
            <RotateCcw aria-hidden />Reset layout</Button>
          <ContextGraph key={layout} lineage={g} onSelect={onSelect} highlight={highlight} focusSource={focusSource}
            height="min(560px, 60vh)" />
        </div>
        <aside className="rounded-lg border p-3 text-sm" aria-label="Node details">
          {node ? (
            <div className="space-y-2">
              <p className="text-xs font-medium text-muted-foreground">{KIND_LABEL[node.kind]}</p>
              <p className="font-semibold break-words text-[var(--prism-ink)]">{node.label}</p>
              <p className="text-xs">{originText(node, uses)}</p>
              {node.detail && <p className="break-words text-muted-foreground">{node.detail}</p>}
              {links.length > 0 && <ul className="space-y-1 border-t pt-2 text-xs">
                {links.map((e) => <li key={`${e.from}|${e.type}|${e.to}`} className="break-words">
                  {e.from === node.id ? `${edgeText(e.type)} → ${labelOf(e.to)}` : `${labelOf(e.from)} → ${edgeText(e.type)}`}</li>)}
              </ul>}
            </div>
          ) : <p className="text-muted-foreground">Select a node to see what it is, where it came from and how it connects.</p>}
          <KindKey kinds={LINEAGE_KINDS.filter((k) => g.nodes.some((n) => n.kind === k))} />
        </aside>
      </div>
      <details className="rounded-lg border px-3 py-2 text-sm">
        <summary className="cursor-pointer font-medium">List view</summary>
        <div className="mt-2 grid max-h-48 gap-3 overflow-y-auto sm:grid-cols-2 lg:grid-cols-3">
          {LINEAGE_KINDS.filter((k) => g.nodes.some((n) => n.kind === k)).map((k) => (
            <section key={k}>
              <h4 className="text-xs font-medium text-muted-foreground">{KIND_LABEL[k]}</h4>
              <ul className="mt-1 space-y-0.5">
                {g.nodes.filter((n) => n.kind === k).map((n) => <li key={n.id} className="break-words">{n.label}
                  <span className="text-xs text-muted-foreground"> · {originText(n, uses).replace(/^From /, "")}</span></li>)}
              </ul>
            </section>))}
        </div>
      </details>
    </div>
  );
}

/** The source systems the answer drew on; a chip emphasises that system's nodes in the graph. */
function SourceStrip({ uses, focus, onFocus }:
  { uses: SourceUse[]; focus: string | null; onFocus: (id: string | null) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-xs" aria-label="Data sources">
      <span className="font-medium text-muted-foreground">
        {uses.length === 1 ? "Data from 1 source system:" : `Data from ${uses.length} source systems:`}</span>
      {uses.map((u) => (
        <button key={u.id} type="button" aria-pressed={focus === u.id}
          title={focus === u.id ? "Show all sources" : `Show only what came from ${u.name}`} onClick={() => onFocus(focus === u.id ? null : u.id)}
          className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 ${
            focus === u.id ? "border-[var(--prism-navy)] bg-[var(--prism-paper)]" : "bg-background"}`}>
          <span className="size-2.5 rounded-full" style={{ background: u.hex }} aria-hidden />
          <span className="font-medium text-[var(--prism-ink)]">{u.name}</span>
          {u.access.length > 0 && <span className="text-muted-foreground">· {u.access.join(" + ")}</span>}
          <span className="text-muted-foreground">· {u.count} {u.count === 1 ? "node" : "nodes"}</span>
        </button>))}
    </div>
  );
}

/** The node kinds in this graph, each with the fill and icon it wears in the graph. */
function KindKey({ kinds }: { kinds: LineageKind[] }) {
  return (
    <div className="mt-3 border-t pt-2 text-xs text-muted-foreground">
      <p className="mb-1.5 font-medium">Node types</p>
      <ul className="grid grid-cols-2 gap-x-2 gap-y-1">
        {kinds.map((k) => (
          <li key={k} className="flex items-center gap-1.5">
            <svg viewBox="-4 -4 32 32" className="size-4 shrink-0 rounded-full" style={{ background: KIND_STYLE[k].fill }}
              fill="none" stroke={KIND_STYLE[k].ink} strokeWidth={2.4} strokeLinecap="round" strokeLinejoin="round" aria-hidden
              dangerouslySetInnerHTML={{ __html: KIND_STYLE[k].icon }} />
            {KIND_LABEL[k]}</li>))}
      </ul>
      <p className="mt-1.5">The ring round a node is the colour of its source system; a gold halo marks what the
        reasoning used.</p>
    </div>
  );
}
