import type { ChatEvent, PlanEvent, TelemetryEvent } from "@/lib/schemas";

export const NO_RESPONSE = "No answer came back. Please try again.";

export type Turn = {
  id: string;
  question: string;
  status: "streaming" | "done" | "error" | "stopped";
  plan: PlanEvent[];
  widgetKeys: string[];
  summary?: string;
  telemetry?: TelemetryEvent;
  error?: string;
};

export type ChatAction =
  | { type: "start"; id: string; question: string }
  | { type: "event"; id: string; event: ChatEvent }
  | { type: "stop"; id: string }
  | { type: "ended"; id: string }
  | { type: "failed"; id: string; message: string };

export const widgetKey = (turnId: string, widgetId: string) => `${turnId}:${widgetId}`;

function applyEvent(t: Turn, e: ChatEvent): Turn {
  switch (e.type) {
    case "plan": return { ...t, plan: [...t.plan, e] };
    case "widget": return { ...t, widgetKeys: [...t.widgetKeys, widgetKey(t.id, e.widget.id)] };
    case "summary": return { ...t, summary: e.text };
    case "error": return { ...t, status: "error", error: e.message };
    case "telemetry": return { ...t, telemetry: e, status: t.status === "streaming" ? "done" : t.status };
  }
}

export function chatReducer(turns: Turn[], action: ChatAction): Turn[] {
  if (action.type === "start") {
    return [...turns, { id: action.id, question: action.question, status: "streaming", plan: [], widgetKeys: [] }];
  }
  return turns.map((t) => {
    if (t.id !== action.id || t.status === "stopped") return t;
    switch (action.type) {
      case "event": return t.status === "done" ? t : applyEvent(t, action.event);
      case "stop": return t.status === "streaming" ? { ...t, status: "stopped" } : t;
      case "ended":
        if (t.status !== "streaming") return t;
        return t.summary !== undefined ? { ...t, status: "done" } : { ...t, status: "error", error: NO_RESPONSE };
      case "failed": return { ...t, status: "error", error: action.message };
    }
  });
}
