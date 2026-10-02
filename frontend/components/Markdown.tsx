import type { ReactNode } from "react";

/** The small Markdown subset the agent writes (paragraphs, - and 1. lists, **bold**, *italic*, `code`), rendered as
 *  React elements: text is never parsed as HTML, so anything else in the answer shows as plain text. */
const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)|(\*[^*\s][^*\n]*?\*)/g;
const BULLET = /^\s*[-*]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let at = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > at) out.push(text.slice(at, m.index));
    const [tok] = m;
    const key = out.length;
    if (m[1]) out.push(<code key={key} className="rounded bg-[var(--prism-paper)] px-1 py-0.5 text-[0.85em]">{tok.slice(1, -1)}</code>);
    else if (m[2]) out.push(<strong key={key}>{inline(tok.slice(2, -2))}</strong>);
    else out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    at = m.index + tok.length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

type Block = { kind: "p" | "ul" | "ol"; lines: string[] };

function blocks(text: string): Block[] {
  const out: Block[] = [];
  for (const line of text.split(/\r?\n/)) {
    const bullet = BULLET.exec(line), numbered = NUMBERED.exec(line);
    const kind = bullet ? "ul" : numbered ? "ol" : line.trim() ? "p" : null;
    const last = out.at(-1);
    if (kind === null) { if (last) out.push({ kind: "p", lines: [] }); continue; }
    const body = (bullet ?? numbered)?.[1] ?? line.trim();
    if (last && last.kind === kind) last.lines.push(body);
    else out.push({ kind, lines: [body] });
  }
  return out.filter((b) => b.lines.length > 0);
}

export function Markdown({ text, className }: { text: string; className?: string }) {
  return (
    <div className={className}>
      {blocks(text).map((b, i) => {
        if (b.kind === "p") return <p key={i} className="[&:not(:first-child)]:mt-2">{inline(b.lines.join(" "))}</p>;
        const items = b.lines.map((l, j) => <li key={j}>{inline(l)}</li>);
        return b.kind === "ul"
          ? <ul key={i} className="mt-2 list-disc space-y-1 pl-5 first:mt-0">{items}</ul>
          : <ol key={i} className="mt-2 list-decimal space-y-1 pl-5 first:mt-0">{items}</ol>;
      })}
    </div>
  );
}
