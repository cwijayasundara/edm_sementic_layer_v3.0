"use client";
import { MessageSquareText, PanelRightClose, PanelRightOpen, X } from "lucide-react";
import { useCallback, useEffect, useReducer, useState } from "react";
import { AssistantPanel } from "@/components/AssistantPanel";
import { Canvas } from "@/components/Canvas";
import { DashboardsMenu } from "@/components/DashboardsMenu";
import { Header } from "@/components/Header";
import { KpiStrip } from "@/components/KpiStrip";
import { ProvenanceDrawer } from "@/components/ProvenanceDrawer";
import { useSession } from "@/components/SessionProvider";
import { type CanvasItem, canvasReducer, pinnedForSave } from "@/lib/canvas";
import { personaById } from "@/lib/personas";
import type { DashboardRun, WidgetEvent } from "@/lib/schemas";

const OPEN_KEY = "prism.assistant.open";
const RAIL_BTN = "grid size-8 place-items-center rounded-md text-[var(--prism-muted)] transition-colors hover:bg-muted hover:text-[var(--prism-ink)]";

function readOpen(): boolean {
  try { return localStorage.getItem(OPEN_KEY) !== "false"; } catch { return true; }
}

export function Workspace() {
  const { claims } = useSession();
  const [items, dispatch] = useReducer(canvasReducer, []);
  const [drawer, setDrawer] = useState<CanvasItem | null>(null);
  const [assistantOpen, setAssistantOpen] = useState(true);
  // below lg the assistant is a full-screen panel; it stays mounted so the conversation survives closing it
  const [mobileOpen, setMobileOpen] = useState(false);

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
    <div className="flex min-h-screen flex-col lg:h-screen">
      <Header actions={<DashboardsMenu pinned={pinnedForSave(items)} onOpen={onOpen} />} />
      <div className="flex min-h-0 flex-1">
        <main className="min-w-0 flex-1 space-y-5 overflow-y-auto px-4 py-5 sm:px-6">
          <KpiStrip />
          <Canvas items={items} dispatch={dispatch} onProvenance={setDrawer} onAsk={() => setMobileOpen(true)} />
        </main>
        <aside aria-label="Assistant"
          className={`${mobileOpen ? "fixed inset-0 z-40 flex" : "hidden"} flex-col bg-white lg:static lg:z-auto lg:flex lg:shrink-0 lg:border-l ${assistantOpen ? "lg:w-[400px]" : "lg:w-14"}`}>
          <div className={`flex h-14 shrink-0 items-center gap-2 border-b px-3 ${assistantOpen ? "" : "lg:justify-center lg:border-b-0 lg:px-0"}`}>
            <div className={`ml-1 flex-1 ${assistantOpen ? "" : "lg:hidden"}`}>
              <h2 className="text-[0.95rem] font-semibold text-[var(--prism-ink)]">Assistant</h2>
            </div>
            <button type="button" className={`${RAIL_BTN} lg:hidden`} aria-label="Close assistant" onClick={() => setMobileOpen(false)}>
              <X className="size-4" aria-hidden /></button>
            <button type="button" className={`${RAIL_BTN} hidden lg:grid`} aria-expanded={assistantOpen} onClick={toggle}
              aria-label={assistantOpen ? "Collapse assistant" : "Open assistant"} title={assistantOpen ? "Collapse assistant" : "Open assistant"}>
              {assistantOpen ? <PanelRightClose className="size-4" aria-hidden /> : <PanelRightOpen className="size-4" aria-hidden />}</button>
          </div>
          <div className={`min-h-0 flex-1 ${assistantOpen ? "" : "lg:hidden"}`}>
            <AssistantPanel onWidget={onWidget} examples={personaById(claims.sub)?.examples} />
          </div>
        </aside>
      </div>
      {!mobileOpen && (
        <button type="button" onClick={() => setMobileOpen(true)}
          className="fixed right-4 bottom-4 z-30 inline-flex h-11 items-center gap-2 rounded-full bg-[var(--prism-navy)] px-4 text-sm font-medium text-white shadow-[0_8px_24px_-6px_rgba(20,33,61,0.5)] lg:hidden">
          <MessageSquareText className="size-4" aria-hidden />Assistant</button>
      )}
      <ProvenanceDrawer item={drawer} onClose={() => setDrawer(null)} />
    </div>
  );
}
