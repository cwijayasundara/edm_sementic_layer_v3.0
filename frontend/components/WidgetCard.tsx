"use client";
import dynamic from "next/dynamic";
import { Component, useEffect, useRef, useState } from "react";
import { Maximize2, Network, Pin, PinOff, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ContextGraphDialog } from "@/components/ContextGraphDialog";
import { DataTable } from "@/components/DataTable";
import { useSession } from "@/components/SessionProvider";
import { ApiError, api } from "@/lib/api";
import { EXPIRED_TEXT } from "@/lib/copy";
import { type CanvasItem, canPin } from "@/lib/canvas";
import { ChartError, kpiValue, toEChartsOption } from "@/lib/charts";
import { formatValue } from "@/lib/format";
import { SOURCES, sourceById } from "@/lib/sources";
import type { ResultPage } from "@/lib/schemas";

const Chart = dynamic(() => import("@/components/Chart").then((m) => m.Chart), { ssr: false });

export const CHART_ROWS = 200;
export { EXPIRED_TEXT };
export const STATUS_TEXT = {
  not_permitted: "Not permitted for your role",
  unavailable: "Data service unavailable",
  invalid: "This query is no longer valid",
} as const;
const NO_PIN = "This result cannot be saved: its query details are not available.";

type Load = { kind: "loading" } | { kind: "ok"; page: ResultPage } | { kind: "expired" } | { kind: "error" };

export function WidgetBody({ item, page, height }: { item: CanvasItem; page: ResultPage; height: number }) {
  const { widget } = item;
  if (widget.type === "kpi") {
    try {
      return <div className="py-8 text-[2.75rem] leading-none font-semibold tracking-[-0.02em] text-[var(--prism-navy)]">
        {formatValue(kpiValue(widget, page.columns, page.rows), widget.encoding.unit)}</div>;
    } catch { /* falls through to the table */ }
  }
  let option = null;
  try {
    option = toEChartsOption(widget, page.columns, page.rows, { color: sourceById(item.info?.source)?.hex });
  } catch (e) {
    if (!(e instanceof ChartError)) throw e;
  }
  return option ? <Chart option={option} height={height} /> : <DataTable columns={page.columns} rows={page.rows} />;
}

class Boundary extends Component<{ children: React.ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <Notice>This widget could not be drawn.</Notice> : this.props.children; }
}

export function WidgetCard({ item, onRemove, onTogglePin, onProvenance }:
  { item: CanvasItem; onRemove: () => void; onTogglePin: () => void; onProvenance: () => void }) {
  const { call } = useSession();
  const callRef = useRef(call);
  callRef.current = call;
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [expanded, setExpanded] = useState(false);
  const [graphOpen, setGraphOpen] = useState(false);
  const [nonce, setNonce] = useState(0);
  const live = item.status === "ok" && item.widget.handle !== "";

  useEffect(() => {
    setLoad({ kind: "loading" });
    if (!live) return;
    let cancelled = false;
    callRef.current((t) => api.results(t, item.widget.handle, 0, CHART_ROWS))
      .then((page) => { if (!cancelled) setLoad({ kind: "ok", page }); })
      .catch((e) => { if (!cancelled) setLoad(e instanceof ApiError && e.status === 404 ? { kind: "expired" } : { kind: "error" }); });
    return () => { cancelled = true; };
  }, [item.widget.handle, live, nonce]);

  const pinnable = canPin(item);
  const body = !live
    ? <Notice>{STATUS_TEXT[item.status as keyof typeof STATUS_TEXT]}</Notice>
    : load.kind === "loading" ? <ChartSkeleton />
    : load.kind === "expired" ? <Notice>{EXPIRED_TEXT}</Notice>
    : load.kind === "error" ? (
      <Notice>
        <p>{STATUS_TEXT.unavailable}</p>
        <Button size="sm" variant="outline" className="mt-3" onClick={() => setNonce((n) => n + 1)}>Retry</Button>
      </Notice>)
    : <Boundary><WidgetBody item={item} page={load.page} height={260} /></Boundary>;
  const source = SOURCES.find((s) => s.id === item.info?.source);

  return (
    <div className="flex min-w-0 flex-col rounded-lg border bg-white">
      <header className="flex items-start gap-3 px-4 pt-3.5 pb-2">
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold leading-snug text-[var(--prism-ink)]">{item.widget.title}</h3>
          <p className="mt-0.5 truncate text-[0.8rem] text-muted-foreground" title={item.origin}>{item.origin}</p>
        </div>
        <div className="-mr-1.5 flex shrink-0 items-center">
          <button type="button" className={item.pinned ? PINNED_BTN : ICON_BTN}
            disabled={!pinnable} title={pinnable ? (item.pinned ? "Unpin" : "Pin") : NO_PIN}
            aria-label={item.pinned ? "Unpin" : "Pin"} onClick={onTogglePin}>
            {item.pinned ? <PinOff className="size-4" aria-hidden /> : <Pin className="size-4" aria-hidden />}</button>
          <button type="button" className={ICON_BTN} aria-label="Expand" title="Expand"
            disabled={!live || load.kind !== "ok"} onClick={() => setExpanded(true)}><Maximize2 className="size-4" aria-hidden /></button>
          <button type="button" className={ICON_BTN} aria-label="Remove" title="Remove" onClick={onRemove}>
            <X className="size-4" aria-hidden /></button>
        </div>
      </header>
      <div className="flex-1 px-4 pb-2">{body}</div>
      {live && (
        <footer className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t px-4 py-2 text-xs text-muted-foreground">
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden className="size-2 rounded-[2px]" style={{ background: source?.color ?? "var(--prism-muted)" }} />
            {source?.name ?? "Combined sources"}</span>
          {load.kind === "ok" && load.page.row_count > CHART_ROWS &&
            <span>Showing the first {CHART_ROWS} of {load.page.row_count} rows</span>}
          <button type="button" disabled={load.kind !== "ok"} onClick={() => setGraphOpen(true)}
            className="ml-auto inline-flex items-center gap-1 font-medium text-[var(--prism-navy)] underline-offset-4 hover:underline disabled:pointer-events-none disabled:opacity-40">
            <Network className="size-3.5" aria-hidden />Context graph</button>
          <button type="button" className="font-medium text-[var(--prism-navy)] underline-offset-4 hover:underline"
            onClick={onProvenance}>View query · source rows</button>
        </footer>
      )}
      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent className="sm:max-w-5xl!">
          <DialogHeader><DialogTitle>{item.widget.title}</DialogTitle></DialogHeader>
          {live && load.kind === "ok" && <Boundary><WidgetBody item={item} page={load.page} height={520} /></Boundary>}
        </DialogContent>
      </Dialog>
      {live && <ContextGraphDialog open={graphOpen} onOpenChange={setGraphOpen} handle={item.widget.handle}
        title={item.widget.title} runId={item.runId} />}
    </div>
  );
}

const BTN_BASE = "grid size-8 place-items-center rounded-md transition-colors disabled:pointer-events-none disabled:opacity-40";
const ICON_BTN = `${BTN_BASE} text-[var(--prism-muted)] hover:bg-muted hover:text-[var(--prism-ink)]`;
const PINNED_BTN = `${BTN_BASE} bg-[var(--prism-navy)] text-white hover:bg-[var(--prism-navy-2)]`;

function Notice({ children }: { children: React.ReactNode }) {
  return <div className="grid min-h-[200px] place-items-center text-center text-sm text-muted-foreground"><div>{children}</div></div>;
}

function ChartSkeleton() {
  return (
    <div className="flex h-[260px] items-end gap-3 px-2 pb-4" aria-label="Loading" role="status">
      {[55, 80, 40, 95, 65, 30, 70].map((h, i) => <Skeleton key={i} className="flex-1 rounded-b-none" style={{ height: `${h}%` }} />)}
    </div>
  );
}
