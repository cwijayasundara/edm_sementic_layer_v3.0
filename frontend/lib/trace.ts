import type { Trace, TraceStep } from "@/lib/schemas";

export const STEP_KIND_LABEL: Record<string, string> = {
  context: "Context", metric: "Metric", query: "Query", combine: "Combine", delegate: "Delegate",
  visualize: "Dashboard", answer: "Answer", refusal: "Declined", error: "Error",
};

export function formatMs(ms: number | null | undefined): string {
  if (ms == null) return "";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function touchedIds(t: Trace): Set<string> {
  return new Set(t.steps.flatMap((s) => s.touched.map((n) => n.id)));
}

export function stepRows(t: Trace): { step: TraceStep; depth: number }[] {
  const top = t.steps.filter((s) => s.parent === null);
  return top.flatMap((s) => [{ step: s, depth: 0 },
    ...t.steps.filter((c) => c.parent === s.seq).map((c) => ({ step: c, depth: 1 }))]);
}
