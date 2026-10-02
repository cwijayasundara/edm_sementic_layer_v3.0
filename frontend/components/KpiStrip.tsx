"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useSession } from "@/components/SessionProvider";
import { api } from "@/lib/api";
import { formatValue } from "@/lib/format";
import type { KpiTile } from "@/lib/schemas";

export type KpiState = { kind: "loading" } | { kind: "ok"; tiles: KpiTile[] } | { kind: "error" };
const REFRESH_MS = 60_000;

export function KpiStripView({ state, onRetry }: { state: KpiState; onRetry: () => void }) {
  if (state.kind === "error") {
    return (
      <div className="flex items-center gap-3 rounded-md border bg-white px-4 py-3 text-sm">
        <span>KPIs are unavailable right now.</span>
        <Button size="sm" variant="outline" onClick={onRetry}>Retry</Button>
      </div>
    );
  }
  const tiles = state.kind === "ok" ? state.tiles : null;
  return (
    <section aria-label="Key metrics" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      {tiles === null
        ? [0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-20" />)
        : tiles.map((t) => (
          <Card key={t.metric_id + t.label} className="border-l-4 border-l-[var(--prism-crimson)] px-4 py-3">
            <div className="text-xs uppercase tracking-wide text-muted-foreground">{t.label}</div>
            {t.status === "ok"
              ? <div className="text-2xl font-semibold text-[var(--prism-navy)]">{formatValue(t.value, t.unit)}</div>
              : <div className="text-2xl font-semibold text-muted-foreground">— <span className="text-xs font-normal">Unavailable</span></div>}
          </Card>
        ))}
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
