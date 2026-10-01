"""Row-level security policy definitions: one SELECT policy per table, dataset scope + optional row dimension."""
from psycopg import sql

# logical table -> (claims dimension, column) or None for dataset-level access only.
ROW_SCOPES: dict[str, dict[str, tuple[str, str] | None]] = {
    "refmaster": {
        "legal_entities": None, "securities": ("asset_class", "asset_class"), "products": None, "accounts": None,
        "corporate_actions": None, "dq_rules": None, "exceptions": ("asset_class", "asset_class"),
        "change_requests": None, "data_dictionary": None,
    },
    "marketmaster": {
        "vendors": None, "instruments": ("asset_class", "asset_class"),
        "golden_prices": ("asset_class", "asset_class"), "vendor_prices": ("asset_class", "asset_class"),
        "price_suspects": ("asset_class", "asset_class"), "dq_stage_metrics": None, "esg_scores": None,
    },
    "cashrecon": {
        "cash_accounts": ("region", "region"), "statements": ("region", "region"),
        "statement_entries": ("region", "region"), "ledger_entries": ("region", "region"), "match_rules": None,
        "match_groups": ("region", "region"), "match_items": ("region", "region"), "breaks": ("region", "region"),
        "break_actions": ("region", "region"),
    },
    "assetrecon": {
        "custodians": None, "portfolios": ("fund_group", "fund_group"),
        "internal_positions": ("fund_group", "fund_group"), "custodian_positions": ("fund_group", "fund_group"),
        "internal_transactions": ("fund_group", "fund_group"), "custodian_transactions": ("fund_group", "fund_group"),
        "recon_runs": ("fund_group", "fund_group"), "recon_exceptions": ("fund_group", "fund_group"),
        "nav_checks": ("fund_group", "fund_group"),
    },
    "feedhub": {
        "sources": ("source_type", "source_type"), "feeds": ("source_type", "source_type"),
        "feed_deliveries": ("source_type", "source_type"), "support_tickets": ("source_type", "source_type"),
    },
}
_PHYSICAL = {("cashrecon", "cash_accounts"): "private.cash_accounts"}


def physical_name(db: str, table: str) -> str:
    return _PHYSICAL.get((db, table), table)


def policy_statements(db: str) -> list[sql.Composed]:
    statements: list[sql.Composed] = []
    for table, scope in ROW_SCOPES[db].items():
        ident = sql.Identifier(*physical_name(db, table).split("."))
        # Scalar sub-selects become init-plans: the signature is verified once per query, not per row.
        dataset = sql.SQL("(SELECT prism_sec.can({}, {}))").format(sql.Literal(db), sql.Literal(table))
        if scope is None:
            condition = dataset
        else:
            dim, column = scope
            # The ::text[] cast keeps `ANY((SELECT ..))` from being parsed as the subquery form of ANY.
            allowed = sql.SQL("(SELECT prism_sec.allowed({}))::text[]").format(sql.Literal(dim))
            condition = sql.SQL("{ds} AND ({al} @> ARRAY['*'] OR {col} = ANY({al}))").format(
                ds=dataset, al=allowed, col=sql.Identifier(column)
            )
        statements += [
            sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident),
            sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(ident),
            sql.SQL("CREATE POLICY prism_read ON {} FOR SELECT USING ({})").format(ident, condition),
        ]
    return statements
