"use client";
import { WidgetCard } from "@/components/WidgetCard";
import type { CanvasAction, CanvasItem } from "@/lib/canvas";

export function Canvas({ items, dispatch, onProvenance }:
  { items: CanvasItem[]; dispatch: (a: CanvasAction) => void; onProvenance: (item: CanvasItem) => void }) {
  if (items.length === 0) {
    return <div className="rounded-md border border-dashed bg-white p-10 text-center text-sm text-muted-foreground">
      Ask the assistant a question. Its charts appear here.</div>;
  }
  return (
    <section aria-label="Canvas" className="grid gap-4 xl:grid-cols-2">
      {items.map((item) => (
        <WidgetCard key={item.key} item={item}
          onRemove={() => dispatch({ type: "remove", key: item.key })}
          onTogglePin={() => dispatch({ type: "togglePin", key: item.key })}
          onProvenance={() => onProvenance(item)} />
      ))}
    </section>
  );
}
