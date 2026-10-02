export const PERSONAS = [
  { id: "steward", name: "Reference Data Steward", access: "RefMaster and MarketMaster, all asset classes" },
  { id: "cash_ops_emea", name: "Cash Ops Analyst - EMEA", access: "CashRecon and bank feeds, EMEA only" },
  { id: "invest_ops_growth", name: "Investment Ops - Growth Funds", access: "AssetRecon, custodian feeds and securities, Growth funds" },
  { id: "bi_analyst", name: "BI Analyst", access: "All sources, governed metrics only" },
  { id: "head_data", name: "Head of Data Operations", access: "All sources and rows, including sensitive fields" },
] as const;
