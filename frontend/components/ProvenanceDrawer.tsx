"use client";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { DataTable } from "@/components/DataTable";
import { EXPIRED_TEXT, STATUS_TEXT } from "@/components/WidgetCard";
import { useSession } from "@/components/SessionProvider";
import { ApiError, api } from "@/lib/api";
import type { CanvasItem } from "@/lib/canvas";
import { type RecipeView, describeRecipe } from "@/lib/recipe";
import type { ResultPage } from "@/lib/schemas";
import { SOURCES } from "@/lib/sources";

export const PAGE_SIZE = 50;

export function RecipeBlock({ view }: { view: RecipeView }) {
  switch (view.kind) {
    case "metric":
      return (
        <dl className="grid grid-cols-[7.5rem_1fr] gap-x-3 gap-y-1.5 text-sm">
          <dt className="text-muted-foreground">Metric</dt><dd className="font-mono">{view.metricId}</dd>
          <dt className="text-muted-foreground">Grouped by</dt><dd>{view.dimensions.join(", ") || "—"}</dd>
          <dt className="text-muted-foreground">Filters</dt>
          <dd>{view.filters.length ? view.filters.map(([k, v]) => `${k} = ${v}`).join("; ") : "—"}</dd>
          {view.limit !== null && <><dt className="text-muted-foreground">Limit</dt><dd>{view.limit}</dd></>}
        </dl>
      );
    case "sql":
      return <pre className="overflow-x-auto rounded-md bg-[var(--prism-navy)] p-3.5 font-mono text-xs leading-relaxed whitespace-pre-wrap text-[#dbe3f0]">{view.sql}</pre>;
    case "endpoint":
      return <p className="text-sm">Endpoint <span className="font-mono">{view.endpoint}</span>
        {view.params.length > 0 && ` (${view.params.map(([k, v]) => `${k}=${v}`).join(", ")})`}</p>;
    case "combine":
      return (
        <div className="space-y-2">
          <pre className="overflow-x-auto rounded-md bg-[var(--prism-navy)] p-3.5 font-mono text-xs leading-relaxed whitespace-pre-wrap text-[#dbe3f0]">{view.sql}</pre>
          {view.inputs.map((i) => (
            <div key={i.table} className="space-y-2 border-l-2 border-[var(--prism-line)] pl-3">
              <div className="font-mono text-xs font-medium">{i.table}</div>
              <RecipeBlock view={i.view} />
            </div>
          ))}
        </div>
      );
  }
}

function ProvenanceBody({ item }: { item: CanvasItem }) {
  const { call } = useSession();
  const handle = item.widget.handle;
  const status = item.status;
  const callRef = useRef(call);
  callRef.current = call;
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<ResultPage | null>(null);
  const [failed, setFailed] = useState<{ text: string; retry: boolean } | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (status !== "ok") return;
    let cancelled = false;
    setFailed(null);
    callRef.current((t) => api.results(t, handle, offset, PAGE_SIZE))
      .then((p) => { if (!cancelled) setPage(p); })
      .catch((e) => { if (!cancelled) setFailed(e instanceof ApiError && e.status === 404
        ? { text: EXPIRED_TEXT, retry: false } : { text: STATUS_TEXT.unavailable, retry: true }); });
    return () => { cancelled = true; };
  }, [handle, status, offset, nonce]);

  const info = item.info;
  const total = page?.row_count ?? info?.row_count ?? 0;
  if (!info) return null;
  const source = SOURCES.find((s) => s.id === info.source);
  return (
    <div className="space-y-6 px-5 py-5">
      <Section title="Where it came from">
        <dl className="grid grid-cols-[7.5rem_1fr] gap-x-3 gap-y-1.5 text-sm">
          <dt className="text-muted-foreground">Source</dt>
          <dd className="flex items-center gap-1.5">
            {source && <span aria-hidden className="size-2 rounded-[2px]" style={{ background: source.color }} />}
            {info.source ?? "combined"}</dd>
          {info.metric_id && <><dt className="text-muted-foreground">Metric</dt><dd className="font-mono">{info.metric_id}</dd></>}
          <dt className="text-muted-foreground">Rows</dt><dd>{total}</dd>
        </dl>
      </Section>
      <Section title="Query">
        {info.recipe ? <RecipeBlock view={describeRecipe(info.recipe)} />
          : <p className="text-sm text-muted-foreground">Query details are not available for this result.</p>}
      </Section>
      <Section title="Source rows">
        {failed ? (
          <div className="space-y-2 rounded-md border border-dashed p-4 text-sm text-muted-foreground">
            <p>{failed.text}</p>
            {failed.retry && <Button size="sm" variant="outline" onClick={() => setNonce((n) => n + 1)}>Retry</Button>}
          </div>
        ) : page && (
          <div className="overflow-hidden rounded-md border">
            <DataTable columns={page.columns} rows={page.rows} />
            <div className="flex items-center justify-between border-t bg-[#f7f8fa] px-3 py-2 text-sm">
              <span className="text-muted-foreground">Rows {total === 0 ? 0 : offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}</span>
              <div className="flex gap-1.5">
                <Button size="sm" variant="outline" disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</Button>
                <Button size="sm" variant="outline" disabled={offset + PAGE_SIZE >= total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}>Next</Button>
              </div>
            </div>
          </div>
        )}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="space-y-2.5"><h3 className="text-sm font-semibold text-[var(--prism-ink)]">{title}</h3>{children}</section>;
}

export function ProvenanceDrawer({ item, onClose }: { item: CanvasItem | null; onClose: () => void }) {
  return (
    <Sheet open={item !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent className="w-full! gap-0 overflow-y-auto sm:max-w-2xl!">
        <SheetHeader className="border-b px-5 py-4">
          <SheetTitle className="pr-8 text-base font-semibold">{item?.widget.title}</SheetTitle>
          <SheetDescription>How this result was produced, and its source rows.</SheetDescription>
        </SheetHeader>
        {item && <ProvenanceBody key={item.key} item={item} />}
      </SheetContent>
    </Sheet>
  );
}
