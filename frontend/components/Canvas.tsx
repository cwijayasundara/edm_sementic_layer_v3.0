"use client";
import { MessageSquareText } from "lucide-react";
import { WidgetCard } from "@/components/WidgetCard";
import { Button } from "@/components/ui/button";
import type { CanvasAction, CanvasItem } from "@/lib/canvas";

export function Canvas({ items, dispatch, onProvenance, onAsk }:
  { items: CanvasItem[]; dispatch: (a: CanvasAction) => void; onProvenance: (item: CanvasItem) => void; onAsk?: () => void }) {
  if (items.length === 0) {
    return (
      <div className="grid place-items-center rounded-lg border border-dashed border-[#c3cad6] bg-white/60 px-6 py-16 text-center">
        <div className="max-w-sm">
          <ChartGlyph />
          <h2 className="mt-5 font-semibold text-[var(--prism-ink)]">Your canvas is empty</h2>
          <p className="mt-1.5 text-sm leading-relaxed text-muted-foreground">
            Ask the assistant a question. Its charts appear here, and you can pin the ones worth keeping as a dashboard.</p>
          {onAsk && <Button className="mt-5 lg:hidden" onClick={onAsk}><MessageSquareText aria-hidden />Open the assistant</Button>}
        </div>
      </div>
    );
  }
  return (
    <section aria-label="Canvas" className="grid gap-4 xl:grid-cols-2 xl:[&>*:only-child]:col-span-2">
      {items.map((item) => (
        <WidgetCard key={item.key} item={item}
          onRemove={() => dispatch({ type: "remove", key: item.key })}
          onTogglePin={() => dispatch({ type: "togglePin", key: item.key })}
          onProvenance={() => onProvenance(item)} />
      ))}
    </section>
  );
}

function ChartGlyph() {
  return (
    <svg viewBox="0 0 64 48" className="mx-auto h-12 w-16" aria-hidden>
      <path d="M4 44 H60" stroke="#c3cad6" strokeWidth="2" strokeLinecap="round" />
      {[[10, 26], [22, 14], [34, 32], [46, 6]].map(([x, y], i) => (
        <rect key={x} x={x} y={y} width="8" height={44 - y - 3} rx="1.5" fill={i === 3 ? "#14213d" : "#d8dde5"} />
      ))}
    </svg>
  );
}
