"use client";
import { AlertCircle } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSession } from "@/components/SessionProvider";
import { api } from "@/lib/api";
import { formatValue } from "@/lib/format";
import type { KpiTile } from "@/lib/schemas";
import { sourceById } from "@/lib/sources";

export type KpiState = { kind: "loading" } | { kind: "ok"; tiles: KpiTile[] } | { kind: "error" };
const REFRESH_MS = 60_000;

export function KpiStripView({ state, onRetry }: { state: KpiState; onRetry: () => void }) {
  if (state.kind === "error") {
    return (
      <div className="flex items-center gap-3 rounded-lg border bg-white px-4 py-3 text-sm">
        <AlertCircle className="size-4 text-[var(--prism-crimson)]" aria-hidden />
        <span>KPIs are unavailable right now.</span>
        <Button size="sm" variant="outline" className="ml-auto" onClick={onRetry}>Retry</Button>
      </div>
    );
  }
  const tiles = state.kind === "ok" ? state.tiles : null;
  return (
    <section aria-label="Key metrics" className="grid grid-cols-2 gap-3 sm:grid-cols-[repeat(auto-fill,minmax(13.5rem,1fr))] xl:grid-cols-[repeat(4,minmax(0,16rem))]">
      {tiles === null
        ? Array.from({ length: 4 }, (_, i) => (
          <div key={i} className="space-y-3 rounded-lg border bg-white px-4 py-3.5"><Skeleton className="h-3.5 w-28" /><Skeleton className="h-7 w-20" /></div>))
        : tiles.map((t) => {
          const source = sourceById(t.source);
          return (
            <div key={t.metric_id + t.label} className="rounded-lg border bg-white px-4 py-3.5 shadow-[0_1px_2px_rgba(20,33,61,.04)]">
              <div className="text-[0.8rem] font-medium text-muted-foreground">{t.label}</div>
              {t.status === "ok"
                ? <div className="mt-1 text-[1.65rem] leading-tight font-semibold tracking-[-0.01em] text-[var(--prism-navy)] tabular-nums">{formatValue(t.value, t.unit)}</div>
                : <div className="mt-1 text-[1.65rem] leading-tight font-semibold text-[#9aa3b2]">— <span className="text-xs font-normal text-muted-foreground">Unavailable</span></div>}
              {source && <div className="mt-1.5 inline-flex items-center gap-1.5 text-[11px] text-[#8a93a3]">
                <span aria-hidden className="size-1.5 rounded-full" style={{ background: source.color }} />{source.name}</div>}
            </div>
          );
        })}
    </section>
  );
}

export function KpiStrip() {
  const { call } = useSession();
  const [state, setState] = useState<KpiState>({ kind: "loading" });
  const loadedAt = useRef(0);

  const load = useCallback(() => {
    call((t) => api.kpis(t))
      .then((tiles) => { loadedAt.current = Date.now(); setState({ kind: "ok", tiles }); })
      .catch(() => setState({ kind: "error" }));
  }, [call]);

  useEffect(() => {
    load();
    const onFocus = () => { if (Date.now() - loadedAt.current > REFRESH_MS) load(); };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  return <KpiStripView state={state} onRetry={load} />;
}
