import { CHAT_EVENT_TYPES, ChatEvent, type ErrorEvent } from "@/lib/schemas";

export const MALFORMED: ErrorEvent = {
  type: "error", code: "internal_error", message: "Something went wrong. Please try again.",
};

/** One SSE frame -> a validated event; null for comments, keep-alives and event types we do not know. */
export function parseFrame(frame: string): ChatEvent | null {
  let type = "";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
  }
  if (data.length === 0) return null;
  let json: unknown;
  try {
    json = JSON.parse(data.join("\n"));
  } catch {
    return CHAT_EVENT_TYPES.has(type) ? MALFORMED : null;
  }
  const declared = (json as { type?: unknown })?.type;
  const kind = typeof declared === "string" ? declared : type;
  if (!CHAT_EVENT_TYPES.has(kind)) return null;
  const parsed = ChatEvent.safeParse(json);
  return parsed.success ? parsed.data : MALFORMED;
}

/** Async iterator over the agent's /chat stream; frames may arrive split across chunks or several per chunk. */
export async function* parseSse(body: ReadableStream<Uint8Array>): AsyncGenerator<ChatEvent> {
  const reader = body.pipeThrough(new TextDecoderStream() as ReadableWritablePair<string, Uint8Array>).getReader();
  let buf = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      const raw = buf + value;
      const keep = raw.endsWith("\r") ? "\r" : "";
      buf = (keep ? raw.slice(0, -1) : raw).replace(/\r\n?/g, "\n") + keep;
      let cut: number;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const event = parseFrame(buf.slice(0, cut));
        buf = buf.slice(cut + 2);
        if (event) yield event;
      }
    }
    const tail = parseFrame(buf.replace(/\r\n?/g, "\n").trim());
    if (tail) yield tail;
  } finally {
    reader.releaseLock();
  }
}
