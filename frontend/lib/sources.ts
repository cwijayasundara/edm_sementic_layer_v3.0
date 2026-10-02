/** The five platforms behind Prism. `color` is a CSS variable so the login diagram, header and charts agree. */
export const SOURCES = [
  { id: "refmaster", name: "RefMaster", what: "Securities and legal entities", color: "var(--src-refmaster)" },
  { id: "marketmaster", name: "MarketMaster", what: "Prices and vendor conflicts", color: "var(--src-marketmaster)" },
  { id: "cashrecon", name: "CashRecon", what: "Cash breaks and matching", color: "var(--src-cashrecon)" },
  { id: "assetrecon", name: "AssetRecon", what: "Positions, NAV and recon runs", color: "var(--src-assetrecon)" },
  { id: "feedhub", name: "FeedHub", what: "Bank and custodian feeds", color: "var(--src-feedhub)" },
] as const;

export type SourceId = (typeof SOURCES)[number]["id"];

/** The source colour for an access chip such as "CashRecon ✓", or null for non-source chips. */
export function chipColor(chip: string): string | null {
  return SOURCES.find((s) => chip.startsWith(s.name))?.color ?? null;
}
