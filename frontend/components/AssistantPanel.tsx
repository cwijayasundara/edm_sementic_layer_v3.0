"use client";
import { useReducer, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { useSession } from "@/components/SessionProvider";
import { Unauthorized, api } from "@/lib/api";
import { type Turn, chatReducer, widgetKey } from "@/lib/chat";
import type { PlanEvent, WidgetEvent } from "@/lib/schemas";

export const MAX_QUESTION = 2000;
const FAILED = "The assistant is not reachable. Please try again.";
const nf = new Intl.NumberFormat("en-US");

export function planText(p: PlanEvent): string {
  if (p.tool === "run_metric") return `Running ${p.label}…`;
  if (p.tool === "query_source") return `Querying ${p.label.replace(/^query /, "")}…`;
  return "Combining results…";
}

export function TurnView({ turn }: { turn: Turn }) {
  const t = turn.telemetry;
  return (
    <li className="space-y-1 border-b pb-3">
      <p className="font-medium">{turn.question}</p>
      <ul className="text-xs text-muted-foreground">{turn.plan.map((p, i) => <li key={i}>{planText(p)}</li>)}</ul>
      {turn.status === "streaming" && turn.summary === undefined && <p className="text-xs text-muted-foreground">Working…</p>}
      {turn.summary !== undefined && <p className="text-sm">{turn.summary}</p>}
      {turn.status === "error" && <p role="alert" className="text-sm text-[var(--prism-crimson)]">{turn.error}</p>}
      {turn.status === "stopped" && <p className="text-xs text-muted-foreground">Stopped</p>}
      {t && <p className="text-[11px] text-muted-foreground">
        {t.path ?? "—"} · {nf.format(t.input_tokens + t.output_tokens)} tokens · {nf.format(t.cache_read_input_tokens)} cached · ~${t.cost_usd.toFixed(4)}
      </p>}
    </li>
  );
}

export function AssistantPanel({ onWidget }: { onWidget: (key: string, event: WidgetEvent, question: string) => void }) {
  const { call } = useSession();
  const [turns, dispatch] = useReducer(chatReducer, []);
  const [question, setQuestion] = useState("");
  const active = useRef<{ id: string; ctrl: AbortController } | null>(null);
  const seq = useRef(0);

  async function ask() {
    const q = question.trim();
    if (!q || active.current) return;
    const id = `t${Date.now().toString(36)}${seq.current++}`;
    const ctrl = new AbortController();
    active.current = { id, ctrl };
    setQuestion("");
    dispatch({ type: "start", id, question: q });
    try {
      await call(async (token) => {
        for await (const event of api.chat(token, q, ctrl.signal)) {
          if (ctrl.signal.aborted) break;
          if (event.type === "widget") onWidget(widgetKey(id, event.widget.id), event, q);
          dispatch({ type: "event", id, event });
        }
      });
      dispatch({ type: "ended", id });
    } catch (e) {
      if (ctrl.signal.aborted) dispatch({ type: "stop", id });
      else if (!(e instanceof Unauthorized)) dispatch({ type: "failed", id, message: FAILED });
    } finally {
      active.current = null;
    }
  }

  function stop() {
    const a = active.current;
    if (!a) return;
    a.ctrl.abort();
    dispatch({ type: "stop", id: a.id });
  }

  const busy = turns.some((t) => t.status === "streaming");
  return (
    <div className="flex h-full flex-col gap-3">
      <ol className="flex-1 space-y-3 overflow-y-auto">{turns.map((t) => <TurnView key={t.id} turn={t} />)}</ol>
      <form className="space-y-2" onSubmit={(e) => { e.preventDefault(); void ask(); }}>
        <textarea aria-label="Question" className="w-full rounded-md border p-2 text-sm" rows={3}
          maxLength={MAX_QUESTION} value={question} placeholder="Ask about your data…"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void ask(); } }} />
        <div className="flex justify-end gap-2">
          {busy && <Button type="button" variant="outline" onClick={stop}>Stop</Button>}
          <Button type="submit" disabled={busy || !question.trim()}>Ask</Button>
        </div>
      </form>
    </div>
  );
}
