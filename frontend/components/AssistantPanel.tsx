"use client";
import { ArrowUp, Check, ChevronRight, CircleCheck, Loader2, MessageSquareText, Route, Square, ThumbsUp } from "lucide-react";
import { useEffect, useReducer, useRef, useState } from "react";
import { Markdown } from "@/components/Markdown";
import { Button } from "@/components/ui/button";
import { useSession } from "@/components/SessionProvider";
import { Unauthorized, api } from "@/lib/api";
import { type Turn, chatReducer, widgetKey } from "@/lib/chat";
import type { PlanEvent, WidgetEvent } from "@/lib/schemas";

export const MAX_QUESTION = 2000;
const FAILED = "The assistant is not reachable. Please try again.";
const CONFIRM_FAILED = "Could not confirm this answer.";
const nf = new Intl.NumberFormat("en-US");

export function planText(p: PlanEvent): string {
  if (p.tool === "run_metric") return `Running ${p.label}…`;
  if (p.tool === "query_source") return `Querying ${p.label.replace(/^query /, "")}…`;
  return "Combining results…";
}

export function TurnView({ turn, onConfirm, onExplain }: { turn: Turn; onConfirm?: (turn: Turn) => void; onExplain?: () => void }) {
  const t = turn.telemetry;
  const a = turn.answer;
  const streaming = turn.status === "streaming";
  return (
    <li className="space-y-2.5">
      <p className="ml-6 rounded-lg rounded-tr-sm bg-[var(--prism-navy)] px-3 py-2 text-[0.9rem] text-white">{turn.question}</p>
      {turn.plan.length > 0 && (
        <ul className="space-y-1 text-xs text-muted-foreground">
          {turn.plan.map((p, i) => {
            const done = !streaming || i < turn.plan.length - 1;
            return (
              <li key={i} className="flex items-center gap-2">
                {done ? <Check className="size-3.5 text-[var(--src-cashrecon)]" aria-hidden />
                  : <Loader2 className="size-3.5 animate-spin" aria-hidden />}
                <span>{planText(p)}</span>
              </li>
            );
          })}
        </ul>)}
      {streaming && turn.summary === undefined && turn.plan.length === 0 && (
        <p className="flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3.5 animate-spin" aria-hidden />
          <span>Working…</span></p>)}
      {turn.summary !== undefined && <Markdown text={turn.summary} className="text-[0.9rem] leading-relaxed text-[var(--prism-ink)]" />}
      {turn.status === "error" && <p role="alert" className="rounded-md bg-[#fbecef] px-3 py-2 text-sm text-[var(--prism-crimson)]">{turn.error}</p>}
      {turn.status === "stopped" && <p className="text-xs text-muted-foreground">Stopped</p>}
      {a?.confirmable && (a.state === "confirmed"
        ? <p className="inline-flex items-center gap-1.5 text-xs font-medium text-[var(--src-cashrecon)]">
            <CircleCheck className="size-3.5" aria-hidden /><span>Confirmed</span></p>
        : <div className="flex items-center gap-2">
            <Button type="button" variant="outline" size="sm" aria-label="Confirm this answer"
              disabled={a.state === "sending"} onClick={() => onConfirm?.(turn)}>
              <ThumbsUp aria-hidden /> Correct? Confirm
            </Button>
            {a.state === "failed" && <p role="alert" className="text-xs text-[var(--prism-crimson)]">{CONFIRM_FAILED}</p>}
          </div>)}
      {t && onExplain && <button type="button" onClick={onExplain}
        className="inline-flex items-center gap-1 text-xs font-medium text-[var(--prism-muted)] underline-offset-2 hover:underline">
        <Route className="size-3.5" aria-hidden />How I got this</button>}
      {t && <details className="group text-[11px] text-[#8a93a3]">
        <summary className="inline-flex cursor-pointer list-none items-center gap-0.5 rounded hover:text-[var(--prism-muted)] [&::-webkit-details-marker]:hidden">
          <ChevronRight className="size-3 transition-transform group-open:rotate-90" aria-hidden />Run details</summary>
        <p className="mt-1 pl-3.5">
          {t.path ?? "—"} · {nf.format(t.input_tokens + t.output_tokens)} tokens · {nf.format(t.cache_read_input_tokens)} cached · ~${t.cost_usd.toFixed(4)}
        </p>
      </details>}
    </li>
  );
}

export function AssistantPanel({ onWidget, onRun, onExplain, examples = [] }: {
  onWidget: (key: string, event: WidgetEvent, question: string) => void;
  onRun?: (turnId: string, runId: string) => void;
  onExplain?: (x: { runId: string; handle?: string; title: string }) => void;
  examples?: readonly string[];
}) {
  const { call } = useSession();
  const [turns, dispatch] = useReducer(chatReducer, []);
  const [question, setQuestion] = useState("");
  const active = useRef<{ id: string; ctrl: AbortController } | null>(null);
  const seq = useRef(0);
  const handles = useRef(new Map<string, string>());
  const box = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const last = turns.at(-1);

  useEffect(() => { end.current?.scrollIntoView?.({ block: "end" }); },
    [turns.length, last?.plan.length, last?.summary, last?.status]);

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
          if (event.type === "widget") {
            if (!handles.current.has(id)) handles.current.set(id, event.widget.handle);
            onWidget(widgetKey(id, event.widget.id), event, q);
          }
          if (event.type === "telemetry") onRun?.(id, event.run_id);
          dispatch({ type: "event", id, event });
        }
      });
      dispatch({ type: "ended", id });
    } catch (e) {
      if (ctrl.signal.aborted) dispatch({ type: "stop", id });
      else if (!(e instanceof Unauthorized)) dispatch({ type: "failed", id, message: FAILED });
    } finally {
      if (active.current?.id === id) active.current = null;
    }
  }

  async function confirmTurn(turn: Turn) {
    const a = turn.answer;
    if (!a?.confirmable || a.state === "sending" || a.state === "confirmed") return;
    dispatch({ type: "confirm", id: turn.id, state: "sending" });
    try {
      await call((token) => api.confirm(token, a.recordId, a.runId ?? turn.telemetry?.run_id));
      dispatch({ type: "confirm", id: turn.id, state: "confirmed" });
    } catch (e) {
      if (!(e instanceof Unauthorized)) dispatch({ type: "confirm", id: turn.id, state: "failed" });
    }
  }

  function stop() {
    const a = active.current;
    if (!a) return;
    a.ctrl.abort();
    active.current = null;
    dispatch({ type: "stop", id: a.id });
  }

  const busy = turns.some((t) => t.status === "streaming");
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {turns.length === 0 ? (
          <div className="space-y-4">
            <div className="flex items-start gap-3">
              <span className="grid size-8 shrink-0 place-items-center rounded-md bg-[var(--accent)] text-[var(--prism-navy)]">
                <MessageSquareText className="size-4" aria-hidden /></span>
              <p className="text-sm leading-relaxed text-muted-foreground">
                Ask in plain language. Answers come only from the sources your role can reach, and each chart lands on the canvas.</p>
            </div>
            {examples.length > 0 && (
              <div>
                <p className="mb-2 text-xs font-medium text-[var(--prism-muted)]">Try one of these</p>
                <ul className="space-y-1.5">
                  {examples.map((ex) => (
                    <li key={ex}>
                      <button type="button" onClick={() => { setQuestion(ex); box.current?.focus(); }}
                        className="w-full rounded-md border bg-white px-3 py-2 text-left text-sm text-[var(--prism-ink)] transition-colors hover:border-[#b9c2d0] hover:bg-[#f7f8fa]">
                        {ex}</button>
                    </li>))}
                </ul>
              </div>)}
          </div>
        ) : (
          <ol className="space-y-6">{turns.map((t) => <TurnView key={t.id} turn={t} onConfirm={confirmTurn}
            onExplain={t.telemetry ? () => onExplain?.({ runId: t.telemetry!.run_id, handle: handles.current.get(t.id), title: t.question }) : undefined} />)}</ol>
        )}
        <div ref={end} />
      </div>
      <form className="border-t bg-white p-3" onSubmit={(e) => { e.preventDefault(); void ask(); }}>
        <div className="rounded-lg border bg-white transition-shadow focus-within:border-[var(--ring)] focus-within:ring-3 focus-within:ring-[var(--ring)]/15">
          <textarea ref={box} aria-label="Question" className="block w-full resize-none bg-transparent px-3 pt-2.5 text-sm outline-none placeholder:text-[#8a93a3] focus-visible:outline-none"
            rows={3} maxLength={MAX_QUESTION} value={question} placeholder="Ask about breaks, feeds, positions or prices…"
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void ask(); } }} />
          <div className="flex items-center justify-end gap-2 px-2 pb-2">
            <span className="mr-auto pl-1 text-[11px] text-[#8a93a3]">Shift + Enter for a new line</span>
            {busy && <Button type="button" size="sm" variant="outline" onClick={stop}><Square className="size-3" aria-hidden />Stop</Button>}
            <Button type="submit" size="sm" disabled={busy || !question.trim()}><ArrowUp aria-hidden />Ask</Button>
          </div>
        </div>
      </form>
    </div>
  );
}
