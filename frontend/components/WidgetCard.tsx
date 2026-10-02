"use client";
import dynamic from "next/dynamic";
import { Component, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { DataTable } from "@/components/DataTable";
import { useSession } from "@/components/SessionProvider";
import { ApiError, api } from "@/lib/api";
import { type CanvasItem, canPin } from "@/lib/canvas";
import { ChartError, kpiValue, toEChartsOption } from "@/lib/charts";
import { formatValue } from "@/lib/format";
import type { ResultPage } from "@/lib/schemas";

const Chart = dynamic(() => import("@/components/Chart").then((m) => m.Chart), { ssr: false });

export const CHART_ROWS = 200;
export const EXPIRED_TEXT = "This result has expired. Ask again or reopen the dashboard.";
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
      return <div className="py-6 text-4xl font-semibold text-[var(--prism-navy)]">
        {formatValue(kpiValue(widget, page.columns, page.rows), widget.encoding.unit)}</div>;
    } catch { /* falls through to the table */ }
  }
  let option = null;
  try {
    option = toEChartsOption(widget, page.columns, page.rows);
  } catch (e) {
    if (!(e instanceof ChartError)) throw e;
  }
  return option ? <Chart option={option} height={height} /> : <DataTable columns={page.columns} rows={page.rows} />;
}

class Boundary extends Component<{ children: React.ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <p className="text-sm text-muted-foreground">This widget could not be drawn.</p> : this.props.children; }
}

export function WidgetCard({ item, onRemove, onTogglePin, onProvenance }:
  { item: CanvasItem; onRemove: () => void; onTogglePin: () => void; onProvenance: () => void }) {
  const { call } = useSession();
  const callRef = useRef(call);
  callRef.current = call;
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [expanded, setExpanded] = useState(false);
  const live = item.status === "ok" && item.widget.handle !== "";

  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    callRef.current((t) => api.results(t, item.widget.handle, 0, CHART_ROWS))
      .then((page) => { if (!cancelled) setLoad({ kind: "ok", page }); })
      .catch((e) => { if (!cancelled) setLoad(e instanceof ApiError && e.status === 404 ? { kind: "expired" } : { kind: "error" }); });
    return () => { cancelled = true; };
  }, [item.widget.handle, live]);

  const pinnable = canPin(item);
  const body = !live
    ? <p className="py-8 text-center text-sm text-muted-foreground">{STATUS_TEXT[item.status as keyof typeof STATUS_TEXT]}</p>
    : load.kind === "loading" ? <p className="py-8 text-center text-sm text-muted-foreground">Loading…</p>
    : load.kind === "expired" ? <p className="py-8 text-center text-sm text-muted-foreground">{EXPIRED_TEXT}</p>
    : load.kind === "error" ? <p className="py-8 text-center text-sm text-muted-foreground">{STATUS_TEXT.unavailable}</p>
    : <Boundary><WidgetBody item={item} page={load.page} height={260} /></Boundary>;

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0 pb-2">
        <div>
          <CardTitle className="text-sm">{item.widget.title}</CardTitle>
          <p className="text-xs text-muted-foreground">{item.origin}</p>
        </div>
        <div className="flex gap-1">
          <Button size="sm" variant={item.pinned ? "default" : "outline"} disabled={!pinnable}
            title={pinnable ? (item.pinned ? "Unpin" : "Pin") : NO_PIN} aria-label={item.pinned ? "Unpin" : "Pin"}
            onClick={onTogglePin}>{item.pinned ? "Pinned" : "Pin"}</Button>
          <Button size="sm" variant="outline" disabled={load.kind !== "ok"} onClick={() => setExpanded(true)}>Expand</Button>
          <Button size="sm" variant="ghost" aria-label="Remove" onClick={onRemove}>×</Button>
        </div>
      </CardHeader>
      <CardContent className="flex-1">
        {body}
        {load.kind === "ok" && load.page.row_count > CHART_ROWS &&
          <p className="mt-1 text-xs text-muted-foreground">Showing the first {CHART_ROWS} of {load.page.row_count} rows</p>}
        {live && <Button variant="link" size="sm" className="px-0" onClick={onProvenance}>view query · source rows</Button>}
      </CardContent>
      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent className="max-w-5xl">
          <DialogHeader><DialogTitle>{item.widget.title}</DialogTitle></DialogHeader>
          {load.kind === "ok" && <Boundary><WidgetBody item={item} page={load.page} height={520} /></Boundary>}
        </DialogContent>
      </Dialog>
    </Card>
  );
}
