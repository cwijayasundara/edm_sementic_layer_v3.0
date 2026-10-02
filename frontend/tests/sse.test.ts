// @vitest-environment node
import { describe, expect, it } from "vitest";
import { MALFORMED, parseFrame, parseSse } from "@/lib/sse";

function stream(chunks: (string | Uint8Array)[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(c) {
      for (const ch of chunks) c.enqueue(typeof ch === "string" ? enc.encode(ch) : ch);
      c.close();
    },
  });
}

async function collect(s: ReadableStream<Uint8Array>) {
  const out = [];
  for await (const e of parseSse(s)) out.push(e);
  return out;
}

const plan = 'event: plan\ndata: {"type": "plan", "tool": "run_metric", "label": "metric open_breaks"}\n\n';
const summary = 'event: summary\ndata: {"type": "summary", "text": "EMEA has the most."}\n\n';

describe("parseSse", () => {
  it("yields frames split across chunks and several frames in one chunk", async () => {
    const all = plan + summary;
    const out = await collect(stream([all.slice(0, 17), all.slice(17, 90), all.slice(90)]));
    expect(out.map((e) => e.type)).toEqual(["plan", "summary"]);
    expect(await collect(stream([all]))).toEqual(out);
  });

  it("handles CRLF line endings and a multi-byte character split across chunks", async () => {
    const bytes = new TextEncoder().encode('event: summary\r\ndata: {"type":"summary","text":"€5"}\r\n\r\n');
    const cut = bytes.indexOf(0xe2) + 1; // inside the euro sign
    const out = await collect(stream([bytes.slice(0, cut), bytes.slice(cut)]));
    expect(out).toEqual([{ type: "summary", text: "€5" }]);
  });

  it("does not split a frame when CRLF itself is split across chunks", async () => {
    const out = await collect(stream(['event: summary\r', '\ndata: {"type":"summary","text":"x"}\r\n\r\n']));
    expect(out).toEqual([{ type: "summary", text: "x" }]);
  });

  it("ignores unknown event types and turns malformed known frames into a fixed error", async () => {
    const out = await collect(stream([
      'event: debug\ndata: {"type": "debug", "x": 1}\n\n',
      "event: summary\ndata: {not json\n\n",
      'event: widget\ndata: {"type": "widget", "widget": {"id": "w1"}}\n\n',
    ]));
    expect(out).toEqual([MALFORMED, MALFORMED]);
  });

  it("parses a final frame without a trailing blank line", async () => {
    expect(await collect(stream([summary.trimEnd()]))).toEqual([{ type: "summary", text: "EMEA has the most." }]);
  });
});

describe("parseFrame", () => {
  it("returns null for comments and keep-alives", () => {
    expect(parseFrame(": ping")).toBeNull();
    expect(parseFrame("")).toBeNull();
  });
});

describe("answer event", () => {
  it("parses the answer event", () => {
    const rid = "0b8f3c1e-2d4a-4c6b-9e7f-1a2b3c4d5e6f";
    expect(parseFrame(`event: answer\ndata: {"type":"answer","record_id":"${rid}","confirmable":true}`))
      .toEqual({ type: "answer", record_id: rid, confirmable: true });
    expect(parseFrame(`event: answer\ndata: {"type":"answer","record_id":"nope","confirmable":true}`)).toEqual(MALFORMED);
  });
});
