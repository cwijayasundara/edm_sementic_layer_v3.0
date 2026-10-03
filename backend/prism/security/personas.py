"""Demo personas (spec §5). Scopes grant datasets ('db' or 'db.table'); rows grant values per RLS dimension."""
import time
from collections.abc import Mapping
from dataclasses import dataclass

ALL_SOURCES = ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
# Display names, spelled as the UI's header chips (frontend/lib/session.ts SOURCE_NAMES).
SOURCE_DISPLAY = {"refmaster": "RefMaster", "marketmaster": "MarketMaster", "cashrecon": "CashRecon",
                  "assetrecon": "AssetRecon", "feedhub": "FeedHub"}
ALL_ROWS = {"asset_class": ("*",), "region": ("*",), "fund_group": ("*",), "source_type": ("*",)}


@dataclass(frozen=True)
class Persona:
    persona_id: str
    display_name: str
    scopes: tuple[str, ...]
    rows: Mapping[str, tuple[str, ...]]
    # metrics_only (bi_analyst): no free-form query (the source MCP servers refuse `query`); sensitive
    # dimensions are blocked at the source (run_metric refuses a metric's sensitive_dimensions); grain /
    # minimum-group-size policy is enforced at the gateway (Plan 3). The database RLS and the mock REST APIs
    # do not inspect this flag.
    metrics_only: bool = False


PERSONAS: dict[str, Persona] = {p.persona_id: p for p in (
    Persona("steward", "Reference Data Steward", ("refmaster", "marketmaster"), {"asset_class": ("*",)}),
    Persona("cash_ops_emea", "Cash Ops Analyst - EMEA", ("cashrecon", "feedhub"),
            {"region": ("EMEA",), "source_type": ("bank",)}),
    Persona("invest_ops_growth", "Investment Ops - Growth Funds",
            ("assetrecon", "feedhub", "refmaster.securities", "refmaster.legal_entities"),
            {"fund_group": ("Growth",), "source_type": ("custodian",), "asset_class": ("*",)}),
    # metrics-only: no free-form query; sensitive dimensions blocked at the source; grain / minimum-group-size
    # policy enforced at the gateway (Plan 3). Not enforced by the DB or the mock APIs.
    Persona("bi_analyst", "BI Analyst", ALL_SOURCES, ALL_ROWS, metrics_only=True),
    Persona("head_data", "Head of Data Operations", (*ALL_SOURCES, "pii:read"), ALL_ROWS),
)}


def claims_for(persona_id: str, ttl_s: int = 300, now: int | None = None) -> dict:
    p = PERSONAS[persona_id]
    issued = int(time.time() if now is None else now)
    return {
        "sub": p.persona_id,
        "name": p.display_name,
        "roles": [p.persona_id],
        "scopes": list(p.scopes),
        "rows": {dim: list(values) for dim, values in p.rows.items()},
        "metrics_only": p.metrics_only,
        "exp": issued + ttl_s,
    }
