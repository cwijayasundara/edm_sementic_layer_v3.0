"use client";
import { Network, RotateCcw } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useSession } from "@/components/SessionProvider";
import { ApiError, Unauthorized, api } from "@/lib/api";
import { EXPIRED_TEXT } from "@/lib/copy";
import { KIND_LABEL } from "@/lib/graph";
import { LINEAGE_KINDS, type Lineage } from "@/lib/schemas";

const ContextGraph = dynamic(() => import("@/components/ContextGraph").then((m) => m.ContextGraph),
  { ssr: false, loading: () => <Skeleton className="h-[560px] w-full" /> });

export const GRAPH_UNAVAILABLE = "The context graph is unavailable.";
export const GRAPH_EMPTY = "No context is available for this result.";
export const NOT_GOVERNED = "Free-form query: no governed lineage";
export const SHORTENED = "Graph shortened to 150 nodes";

type Load = { kind: "loading" } | { kind: "ok"; lineage: Lineage } | { kind: "expired" } | { kind: "error" };

export function ContextGraphDialog({ open, onOpenChange, handle, title }:
  { open: boolean; onOpenChange: (open: boolean) => void; handle: string; title: string }) {
  const { call } = useSession();
  const callRef = useRef(call);
  callRef.current = call;
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [nonce, setNonce] = useState(0);
  const [layout, setLayout] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoad({ kind: "loading" });
    setSelected(null);
    callRef.current((t) => api.lineage(t, handle))
      .then((lineage) => { if (!cancelled) setLoad({ kind: "ok", lineage }); })
      .catch((e) => {
        if (cancelled || e instanceof Unauthorized) return;
        setLoad(e instanceof ApiError && e.status === 404 ? { kind: "expired" } : { kind: "error" });
      });
    return () => { cancelled = true; };
  }, [open, handle, nonce]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[min(92vw,1400px)]!">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Network className="size-4" aria-hidden />Context graph</DialogTitle>
          <p className="text-sm text-muted-foreground">{title}</p>
        </DialogHeader>
        <Body load={load} layout={layout} selected={selected} onSelect={setSelected}
          onRetry={() => setNonce((n) => n + 1)} onReset={() => { setLayout((n) => n + 1); setSelected(null); }} />
      </DialogContent>
    </Dialog>
  );
}

function Notice({ children }: { children: React.ReactNode }) {
  return <div className="grid min-h-[320px] place-items-center text-center text-sm text-muted-foreground"><div>{children}</div></div>;
}

function Body({ load, layout, selected, onSelect, onRetry, onReset }: {
  load: Load; layout: number; selected: string | null; onSelect: (id: string) => void; onRetry: () => void; onReset: () => void;
}) {
  if (load.kind === "loading") return <Skeleton className="h-[560px] w-full" />;
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
  return (
    <div className="space-y-3">
      {(!g.governed || g.truncated) && (
        <div className="flex flex-wrap gap-2 text-xs">
          {!g.governed && <span className="rounded bg-[var(--prism-paper)] px-2 py-1">{NOT_GOVERNED}</span>}
          {g.truncated && <span className="rounded bg-[var(--prism-paper)] px-2 py-1">{SHORTENED}</span>}
        </div>)}
      <div className="grid gap-3 lg:grid-cols-[1fr_18rem]">
        <div className="relative rounded-lg border bg-[#fbfcfd]">
          <Button size="sm" variant="outline" className="absolute top-2 left-2 z-10" onClick={onReset}>
            <RotateCcw aria-hidden />Reset layout</Button>
          <ContextGraph key={layout} lineage={g} onSelect={onSelect} height={560} />
        </div>
        <aside className="rounded-lg border p-3 text-sm" aria-label="Node details">
          {node ? (
            <div className="space-y-2">
              <p className="text-xs font-medium text-muted-foreground">{KIND_LABEL[node.kind]}{node.source ? ` · ${node.source}` : ""}</p>
              <p className="font-semibold break-words text-[var(--prism-ink)]">{node.label}</p>
              {node.detail && <p className="break-words text-muted-foreground">{node.detail}</p>}
              {links.length > 0 && <ul className="space-y-1 border-t pt-2 text-xs">
                {links.map((e) => <li key={`${e.from}|${e.type}|${e.to}`} className="break-words">
                  {e.from === node.id ? `${e.type} → ${labelOf(e.to)}` : `${labelOf(e.from)} → ${e.type}`}</li>)}
              </ul>}
            </div>
          ) : <p className="text-muted-foreground">Select a node to see what it is and how it connects.</p>}
        </aside>
      </div>
      <details className="rounded-lg border px-3 py-2 text-sm">
        <summary className="cursor-pointer font-medium">List view</summary>
        <div className="mt-2 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {LINEAGE_KINDS.filter((k) => g.nodes.some((n) => n.kind === k)).map((k) => (
            <section key={k}>
              <h4 className="text-xs font-medium text-muted-foreground">{KIND_LABEL[k]}</h4>
              <ul className="mt-1 space-y-0.5">
                {g.nodes.filter((n) => n.kind === k).map((n) => <li key={n.id} className="break-words">{n.label}</li>)}
              </ul>
            </section>))}
        </div>
      </details>
    </div>
  );
}
