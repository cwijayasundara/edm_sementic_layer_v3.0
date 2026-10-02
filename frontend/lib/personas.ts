import type { SourceId } from "@/lib/sources";

type Persona = {
  id: string; name: string; access: string; sources: readonly SourceId[]; examples: readonly string[];
};

// `sources` mirrors backend/prism/security/personas.py for display only; the gateway enforces access.
export const PERSONAS: readonly Persona[] = [
  { id: "steward", name: "Reference Data Steward", access: "RefMaster and MarketMaster, all asset classes",
    sources: ["refmaster", "marketmaster"],
    examples: ["Which price vendor drives the most corporate bond price conflicts?",
      "How many data quality exceptions are open, by domain?"] },
  { id: "cash_ops_emea", name: "Cash Ops Analyst - EMEA", access: "CashRecon and bank feeds, EMEA only",
    sources: ["cashrecon", "feedhub"],
    examples: ["How many open breaks do we have by region?", "What is the open break amount by currency?",
      "Which of our legal entities has the most aged USD nostro breaks?"] },
  { id: "invest_ops_growth", name: "Investment Ops - Growth Funds",
    access: "AssetRecon, custodian feeds and securities, Growth funds",
    sources: ["assetrecon", "feedhub", "refmaster"],
    examples: ["How many position exceptions are open, by cause?", "What share of recon runs were clean, by recon type?",
      "Which custodian has had the most late feeds since 23 September?"] },
  { id: "bi_analyst", name: "BI Analyst", access: "All sources, governed metrics only",
    sources: ["refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub"],
    examples: ["Break down open breaks by break type.", "How many recon items are unmatched by fund group?",
      "What is the average feed latency in minutes by source type?"] },
  { id: "head_data", name: "Head of Data Operations", access: "All sources and rows, including sensitive fields",
    sources: ["refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub"],
    examples: ["Which portfolios had NAV breaches above 5 basis points?", "What is the auto-match rate by region?",
      "For each source, compare late feeds since 23 September with its open support tickets."] },
];

export function personaById(id: string): Persona | undefined {
  return PERSONAS.find((p) => p.id === id);
}
