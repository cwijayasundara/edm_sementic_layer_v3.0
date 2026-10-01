"""RefMaster EDM: security, entity, product/account master, corporate actions, DQ, 4-eyes, data dictionary."""
import random
from datetime import timedelta

from prism.sim.calendar import at
from prism.sim.model import TableData
from prism.sim.universe import Universe

PRODUCTS = (
    ("PR01", "Global Equity Fund", "fund"), ("PR02", "Euro Credit Fund", "fund"),
    ("PR03", "US Treasury Mandate", "mandate"), ("PR04", "Asia Growth Fund", "fund"),
    ("PR05", "Liquidity Reserve", "fund"), ("PR06", "Multi-Asset Income", "fund"),
    ("PR07", "Segregated Pension Mandate", "mandate"), ("PR08", "Securities Lending Programme", "service"),
)
DQ_RULES = (
    ("R001", "security", "ISIN check digit valid", "high"),
    ("R002", "security", "Mandatory issuer populated", "medium"),
    ("R003", "security", "Asset class consistent with sub-class", "medium"),
    ("R004", "price", "Price within tolerance of prior close", "high"),
    ("R005", "price", "Price received before cut-off", "medium"),
    ("R006", "price", "At least two vendor quotes", "low"),
    ("R007", "entity", "LEI checksum valid", "high"),
    ("R008", "entity", "Parent entity exists", "medium"),
    ("R009", "corporate_action", "Ex-date before pay-date", "high"),
    ("R010", "corporate_action", "Ratio populated for split", "medium"),
    ("R011", "security", "Duplicate identifier across records", "high"),
    ("R012", "entity", "Country code valid ISO 3166", "low"),
)
STEWARDS = ("steward01", "steward02", "steward03", "steward04", "steward05")
CA_EVENTS = (("dividend", 60), ("split", 10), ("merger", 5), ("rights_issue", 10), ("name_change", 15))
DATA_DICTIONARY = (
    ("security", "isin", "International Securities Identification Number (ISO 6166), 12 characters with check digit",
     "Reference Data Office", "Vendor feeds", "vendor file -> staging -> validation -> golden copy"),
    ("security", "asset_class", "Top-level classification: Equity, Corp bond, Govt, FX, Deriv",
     "Reference Data Office", "Derived", "vendor classification -> mapping rule -> golden copy"),
    ("security", "sub_class", "Second-level classification within the asset class",
     "Reference Data Office", "Derived", "mapping rule"),
    ("security", "ccy", "ISO 4217 trading currency", "Reference Data Office", "Vendor feeds", "vendor file -> golden copy"),
    ("security", "issuer_entity_id", "Legal entity that issued the security",
     "Entity Data Office", "Entity master", "entity match -> link"),
    ("security", "status", "Lifecycle status: active or matured", "Reference Data Office", "Derived", "maturity rule"),
    ("entity", "lei", "Legal Entity Identifier (ISO 17442), 20 characters",
     "Entity Data Office", "LEI registry", "registry file -> entity match -> golden copy"),
    ("entity", "parent_entity_id", "Direct parent legal entity",
     "Entity Data Office", "LEI registry", "relationship file -> golden copy"),
    ("entity", "sector", "Industry sector of the entity", "Entity Data Office", "Vendor feeds", "vendor file -> mapping rule"),
    ("price", "golden_price", "Chosen price after vendor ranking and validation",
     "Pricing Team", "Derived", "vendor quotes -> validation -> source ranking -> golden copy"),
    ("price", "price_conflict", "Vendor quote deviating more than 1% from the golden price",
     "Pricing Team", "Derived", "vendor quote vs golden price comparison"),
    ("price", "stale_price", "Golden price carried forward while vendor quotes moved",
     "Pricing Team", "Derived", "carry-forward rule"),
    ("corporate_action", "ex_date", "First date the security trades without the entitlement",
     "Corporate Actions Team", "Vendor feeds", "announcement -> scrubbing -> golden event"),
    ("corporate_action", "pay_date", "Date the entitlement is paid",
     "Corporate Actions Team", "Vendor feeds", "announcement -> golden event"),
    ("quality", "exception", "A record failing a data-quality rule, awaiting steward action",
     "Data Quality Office", "Derived", "rule engine -> exception queue"),
    ("quality", "four_eyes", "Maker-checker approval: a change needs a second approver",
     "Data Quality Office", "Workflow", "change request -> approval"),
    ("quality", "golden_copy_completeness", "Share of active instruments with a golden price for the business date",
     "Data Quality Office", "Derived", "golden prices / active instruments"),
    ("account", "lifecycle_state", "Account state: onboarding, active or closed",
     "Client Data Office", "Onboarding workflow", "onboarding -> activation"),
    ("account", "account_type", "Segregated, pooled or omnibus", "Client Data Office", "Onboarding workflow", "onboarding"),
    ("product", "product_type", "Fund, mandate or service", "Client Data Office", "Product master", "product setup"),
)


def project_refmaster(u: Universe) -> dict[str, TableData]:
    rng = random.Random(u.cfg.seed + 1)
    t = {
        "legal_entities": TableData(("entity_id", "lei", "name", "country", "region", "sector", "parent_entity_id", "status")),
        "securities": TableData(("security_id", "isin", "cusip", "sedol", "ticker", "name", "asset_class", "sub_class",
                                 "ccy", "issuer_entity_id", "country", "status", "valid_from", "valid_to")),
        "products": TableData(("product_id", "name", "product_type")),
        "accounts": TableData(("account_id", "product_id", "name", "account_type", "owner_entity_id", "region",
                               "lifecycle_state")),
        "corporate_actions": TableData(("ca_id", "security_id", "event_type", "ex_date", "pay_date", "ratio", "status")),
        "dq_rules": TableData(("rule_id", "domain", "name", "severity")),
        "exceptions": TableData(("exc_id", "rule_id", "domain", "record_ref", "asset_class", "status", "assignee",
                                 "opened_at", "closed_at")),
        "change_requests": TableData(("change_id", "domain", "record_ref", "maker", "checker", "status", "created_at")),
        "data_dictionary": TableData(("domain", "attribute", "definition", "owner", "source", "lineage")),
    }
    for e in u.entities:
        t["legal_entities"].add(e.entity_id, e.lei, e.name, e.country, e.region, e.sector, e.parent_entity_id, e.status)
    for s in u.securities:
        t["securities"].add(s.security_id, s.isin, s.cusip, s.sedol, s.ticker, s.name, s.asset_class, s.sub_class,
                            s.ccy, s.issuer_entity_id, s.country, s.status, s.valid_from, None)
    for row in PRODUCTS:
        t["products"].add(*row)
    corporates = [e for e in u.entities if e.sector != "Sovereign"]
    for i in range(max(40, len(u.securities) // 10)):
        product, owner = rng.choice(PRODUCTS), rng.choice(corporates)
        t["accounts"].add(f"AC{i + 1:05d}", product[0], f"{product[1]} - {owner.name}",
                          rng.choice(("segregated", "pooled", "omnibus")), owner.entity_id, owner.region,
                          rng.choices(("active", "onboarding", "closed"), weights=(85, 10, 5))[0])
    _corporate_actions(rng, u, t["corporate_actions"])
    for row in DQ_RULES:
        t["dq_rules"].add(*row)
    _exceptions(rng, u, t["exceptions"])
    _change_requests(rng, u, t["change_requests"])
    for row in DATA_DICTIONARY:
        t["data_dictionary"].add(*row)
    return t


def _corporate_actions(rng: random.Random, u: Universe, table: TableData) -> None:
    events, weights = zip(*CA_EVENTS)
    n = 0
    for s in u.securities_by_class["Equity"]:
        if rng.random() >= 0.15:
            continue
        event = rng.choices(events, weights=weights)[0]
        ex = rng.choice(u.days)
        pay = ex + timedelta(days=rng.randint(3, 20))
        ratio = {"dividend": round(rng.uniform(0.1, 3.0), 4), "split": float(rng.choice((2, 3, 4))),
                 "merger": 1.25, "rights_issue": 0.2, "name_change": None}[event]
        status = "processed" if pay <= u.cfg.as_of else rng.choice(("announced", "confirmed", "confirmed"))
        n += 1
        table.add(f"CA{n:06d}", s.security_id, event, ex, pay, ratio, status)


def _exceptions(rng: random.Random, u: Universe, table: TableData) -> None:
    rules_by_domain: dict[str, list[str]] = {}
    for rule_id, domain, _, _ in DQ_RULES:
        rules_by_domain.setdefault(domain, []).append(rule_id)
    cap = at(u.cfg.as_of, 18)
    n = 0
    for d in u.days:
        age = (u.cfg.as_of - d).days
        for _ in range(max(2, round(len(u.securities) * 0.012 * rng.uniform(0.7, 1.3)))):
            domain = rng.choices(("security", "price", "entity", "corporate_action"), weights=(40, 35, 15, 10))[0]
            if domain == "entity":
                ref, asset_class = rng.choice(u.entities).entity_id, "n/a"
            else:
                s = rng.choice(u.securities)
                ref, asset_class = s.security_id, s.asset_class
            if age <= 10:
                status = rng.choices(("open", "in_review", "closed"), weights=(70, 20, 10))[0]
            else:
                status = rng.choices(("closed", "open", "in_review"), weights=(85, 10, 5))[0]
            opened = at(d, 7) + timedelta(minutes=rng.randrange(600))
            closed = min(opened + timedelta(hours=rng.uniform(2, 120)), cap) if status == "closed" else None
            n += 1
            table.add(f"EX{n:07d}", rng.choice(rules_by_domain[domain]), domain, ref, asset_class, status,
                      rng.choice(STEWARDS), opened, closed)


def _change_requests(rng: random.Random, u: Universe, table: TableData) -> None:
    n = 0
    for d in u.days:
        age = (u.cfg.as_of - d).days
        for _ in range(rng.randint(3, 7)):
            domain = rng.choice(("security", "entity", "price", "corporate_action"))
            ref = rng.choice(u.entities).entity_id if domain == "entity" else rng.choice(u.securities).security_id
            maker, checker = rng.sample(STEWARDS, 2)
            if age <= 2 and rng.random() < 0.6:
                status, checker = "pending", None
            else:
                status = "approved" if rng.random() < 0.9 else "rejected"
            n += 1
            table.add(f"CR{n:06d}", domain, ref, maker, checker, status, at(d, 9) + timedelta(minutes=rng.randrange(480)))
