"use client";
import { useCallback, useEffect, useReducer, useState } from "react";
import { AssistantPanel } from "@/components/AssistantPanel";
import { Canvas } from "@/components/Canvas";
import { DashboardsMenu } from "@/components/DashboardsMenu";
import { Header } from "@/components/Header";
import { KpiStrip } from "@/components/KpiStrip";
import { ProvenanceDrawer } from "@/components/ProvenanceDrawer";
import { Button } from "@/components/ui/button";
import { type CanvasItem, canvasReducer, pinnedForSave } from "@/lib/canvas";
import type { DashboardRun, WidgetEvent } from "@/lib/schemas";

const OPEN_KEY = "prism.assistant.open";

function readOpen(): boolean {
  try { return localStorage.getItem(OPEN_KEY) !== "false"; } catch { return true; }
}

export function Workspace() {
  const [items, dispatch] = useReducer(canvasReducer, []);
  const [drawer, setDrawer] = useState<CanvasItem | null>(null);
  const [assistantOpen, setAssistantOpen] = useState(true);

  useEffect(() => { setAssistantOpen(readOpen()); }, []);
  const toggle = () => setAssistantOpen((o) => {
    try { localStorage.setItem(OPEN_KEY, String(!o)); } catch { /* storage blocked: keep in memory */ }
    return !o;
  });

  const onWidget = useCallback((key: string, e: WidgetEvent, question: string) => {
    dispatch({ type: "add", item: { key, widget: e.widget, info: e.handle_info, origin: question, status: "ok" } });
  }, []);
  const onOpen = useCallback((run: DashboardRun) => {
    dispatch({ type: "openDashboard", dashboardId: run.id, title: run.title, widgets: run.widgets });
  }, []);

  return (
    <div className="flex min-h-screen flex-col">
      <Header actions={<DashboardsMenu pinned={pinnedForSave(items)} onOpen={onOpen} />} />
      <div className="flex flex-1">
        <main className="flex-1 space-y-4 p-6">
          <KpiStrip />
          <Canvas items={items} dispatch={dispatch} onProvenance={setDrawer} />
        </main>
        <aside aria-label="Assistant" className={assistantOpen ? "w-[380px] shrink-0 border-l bg-white p-4" : "w-12 shrink-0 border-l bg-white p-2"}>
          <Button size="sm" variant="ghost" aria-expanded={assistantOpen} onClick={toggle}
            aria-label={assistantOpen ? "Collapse assistant" : "Open assistant"}>{assistantOpen ? "›" : "‹"}</Button>
          <div className={assistantOpen ? "mt-2 h-[calc(100vh-8rem)]" : "hidden"}>
            <AssistantPanel onWidget={onWidget} />
          </div>
        </aside>
      </div>
      <ProvenanceDrawer item={drawer} onClose={() => setDrawer(null)} />
    </div>
  );
}
