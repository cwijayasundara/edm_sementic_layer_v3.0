/** The five platforms behind Prism. `color` is a CSS variable so the login diagram and header agree; `hex` is the same
 *  colour for canvas charts, which cannot read CSS variables (keep it in step with globals.css). */
export const SOURCES = [
  { id: "refmaster", name: "RefMaster", what: "Securities and legal entities", color: "var(--src-refmaster)", hex: "#3d6fb6" },
  { id: "marketmaster", name: "MarketMaster", what: "Prices and vendor conflicts", color: "var(--src-marketmaster)", hex: "#7a5195" },
  { id: "cashrecon", name: "CashRecon", what: "Cash breaks and matching", color: "var(--src-cashrecon)", hex: "#2f8f83" },
  { id: "assetrecon", name: "AssetRecon", what: "Positions, NAV and recon runs", color: "var(--src-assetrecon)", hex: "#d08a1c" },
  { id: "feedhub", name: "FeedHub", what: "Bank and custodian feeds", color: "var(--src-feedhub)", hex: "#c2416b" },
] as const;

export type SourceId = (typeof SOURCES)[number]["id"];

/** The source colour for an access chip such as "CashRecon ✓", or null for non-source chips. */
export function chipColor(chip: string): string | null {
  return SOURCES.find((s) => chip.startsWith(s.name))?.color ?? null;
}

export function sourceById(id: string | null | undefined) {
  return SOURCES.find((s) => s.id === id);
}
