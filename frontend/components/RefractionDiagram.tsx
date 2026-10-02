import { SOURCES, type SourceId } from "@/lib/sources";

const PRISM = { apex: [210, 70], left: [150, 250], right: [270, 250] } as const;
const EXIT = [239, 158];
const BEAM_END = 470;
const ROW_Y = SOURCES.map((_, i) => 44 + i * 58);

/** A question enters the prism and splits into one beam per source; sources outside `visible` dim. */
export function RefractionDiagram({ visible, question }: { visible: readonly SourceId[] | null; question: string }) {
  const on = (id: SourceId) => visible === null || visible.includes(id);
  return (
    <svg viewBox="0 0 680 300" className="h-auto w-full" role="img"
      aria-label={visible === null ? "One question reaches all five sources"
        : `This role reaches ${SOURCES.filter((s) => on(s.id)).map((s) => s.name).join(", ")}`}>
      <text x="0" y="146" className="fill-white/70 text-[12px]">{truncate(question, 28)}</text>
      <line x1="0" y1="158" x2="181" y2="158" stroke="white" strokeWidth="2" pathLength={1} className="prism-beam" />
      <line x1="181" y1="158" x2={EXIT[0]} y2={EXIT[1]} stroke="white" strokeOpacity="0.5" strokeWidth="2" />
      {SOURCES.map((s, i) => (
        <g key={s.id} style={{ opacity: on(s.id) ? 1 : 0.14, transition: "opacity 200ms ease" }}>
          <path d={`M${EXIT[0]} ${EXIT[1]} C ${EXIT[0] + 90} ${EXIT[1]}, ${BEAM_END - 120} ${ROW_Y[i]}, ${BEAM_END} ${ROW_Y[i]}`}
            fill="none" stroke={s.color} strokeWidth="3" strokeLinecap="round" pathLength={1} className="prism-beam"
            style={{ animationDelay: `${300 + i * 70}ms` }} />
          <circle cx={BEAM_END} cy={ROW_Y[i]} r="4" fill={s.color} />
          <text x={BEAM_END + 14} y={ROW_Y[i] - 2} className="fill-white text-[15px] font-medium">{s.name}</text>
          <text x={BEAM_END + 14} y={ROW_Y[i] + 16} className="fill-white/60 text-[12px]">{s.what}</text>
        </g>
      ))}
      <path d={`M${PRISM.apex.join(" ")} L${PRISM.right.join(" ")} L${PRISM.left.join(" ")} Z`}
        fill="rgba(255,255,255,0.06)" stroke="white" strokeWidth="1.5" strokeLinejoin="round" />
      <path d={`M${PRISM.apex.join(" ")} L${PRISM.right.join(" ")}`} stroke="#b3123a" strokeWidth="2.5" />
    </svg>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
