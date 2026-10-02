"use client";
import { AlertCircle, CircleCheck } from "lucide-react";
import type { Trace } from "@/lib/schemas";
import { STEP_KIND_LABEL, formatMs, stepRows } from "@/lib/trace";

export function ReasoningTimeline({ trace, onNode }: { trace: Trace; onNode: (id: string) => void }) {
  return (
    <ol className="space-y-3 text-sm" aria-label="Reasoning steps">
      {stepRows(trace).map(({ step, depth }) => {
        const facts = [step.rows != null ? `${step.rows} rows` : "", formatMs(step.ms)].filter(Boolean).join(" · ");
        const failed = step.status === "error" || step.kind === "error" || step.kind === "refusal";
        return (
          <li key={step.seq} className={`rounded-lg border bg-white px-3 py-2 ${depth ? "ml-6" : ""}`}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded bg-[var(--prism-paper)] px-1.5 py-0.5 text-[11px] font-medium text-[var(--prism-muted)]">
                {STEP_KIND_LABEL[step.kind] ?? step.kind}</span>
              <span className="font-medium text-[var(--prism-ink)]">{step.label}</span>
              {facts && <span className="text-xs text-muted-foreground">{facts}</span>}
              {failed && step.error_code && <span className="inline-flex items-center gap-1 rounded bg-[#fbecef] px-1.5 py-0.5 text-[11px] text-[var(--prism-crimson)]">
                <AlertCircle className="size-3" aria-hidden />{step.error_code}</span>}
              {step.kind === "answer" && trace.confirmed && <span className="inline-flex items-center gap-1 text-xs font-medium text-[var(--src-cashrecon)]">
                <CircleCheck className="size-3.5" aria-hidden />Confirmed</span>}
            </div>
            {step.note && <p className="mt-1 text-xs text-muted-foreground italic">{step.note}</p>}
            {step.kind === "answer" && trace.answer && <p className="mt-1 text-[var(--prism-ink)]">{trace.answer}</p>}
            {step.considered.length > 0 && <p className="mt-1 text-xs text-muted-foreground">Considered: {step.considered.join(", ")}</p>}
            {step.touched.length > 0 && <div className="mt-2 flex flex-wrap gap-1.5">
              {step.touched.map((n) => <button key={n.id} type="button" onClick={() => onNode(n.id)}
                className="rounded-full border px-2 py-0.5 text-xs hover:bg-[var(--prism-paper)]">{n.label}</button>)}
            </div>}
            {step.args && <details className="mt-2 text-xs">
              <summary className="cursor-pointer text-muted-foreground">Arguments</summary>
              <pre className="mt-1 overflow-x-auto rounded bg-[var(--prism-paper)] p-2 whitespace-pre-wrap break-all">{step.args}</pre>
            </details>}
          </li>
        );
      })}
    </ol>
  );
}
