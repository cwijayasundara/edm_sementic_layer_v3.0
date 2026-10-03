"""M9 cross-system incident (spec 2026-10-03 §2): a 2-for-1 split on one equity that the reference data missed,
surfaced in every system.

FeedHub fails, then delivers late, the custodian's corporate-actions feed. RefMaster leaves the split pending.
MarketMaster accepts the halved price. AssetRecon's internal book keeps the unsplit quantity, so every holder breaks
NAV on two days until ops fix the quantities by hand. CashRecon receives a cash-in-lieu payment with no ledger
entry. Background noise of the same kinds (processed corporate actions, unrelated spikes, matched cash in lieu)
makes an agent follow the keys rather than take the top row.

Runs after the five projectors, on their in-memory tables, with its own stream Random(seed + 6), drawing in a fixed
order (refmaster, feedhub, marketmaster, assetrecon, cashrecon). It appends rows, or edits rows `owned` names, and
never draws from another projector's stream, so every planted story is unchanged."""
import itertools
import random
from collections.abc import Callable
from datetime import time, timedelta

from prism.sim.calendar import at
from prism.sim.ids import make_lei
from prism.sim.keys import delivery_id, feed_id, fund_cash_account_id, fund_entity_id
from prism.sim.model import TableData
from prism.sim.project_refmaster import STEWARDS
from prism.sim.universe import INCIDENT_SPLIT_FROM_END, LEGAL_SUFFIX, REGION_OF, Incident, Universe

FAILED_FROM_END = 6                    # 23 Sep for the default as-of: the corporate-actions delivery fails
SPLIT_FROM_END = INCIDENT_SPLIT_FROM_END   # 24 Sep: the split is effective; that day's delivery is late
FIX_FROM_END = 3                       # 28 Sep: internal ops correct the quantities by hand
CASH_FROM_END = 2                      # 29 Sep: the cash in lieu lands
SPLIT_RATIO = 2
NOISE_CORPORATE_ACTIONS, NOISE_SPIKES, NOISE_CASH_IN_LIEU = 15, 10, 5
CA_FEED_TYPE = "corporate_actions"
CA_FEED = (CA_FEED_TYPE, "MT564", time(6, 30))
FUND_COUNTRY = {"USD": "US", "EUR": "LU", "GBP": "GB"}


# ----------------------------------------------------------------------------------------------- helpers
def _set(t: TableData, i: int, **values) -> None:
    row = list(t.rows[i])
    for name, value in values.items():
        row[t.columns.index(name)] = value
    t.rows[i] = tuple(row)


def _next_id(t: TableData, column: str, prefix: str, width: int) -> Callable[[], str]:
    """Sequential ids continuing past the table's largest `prefix<digits>` id, so appends never collide."""
    c = t.columns.index(column)
    top = max((int(r[c][len(prefix):]) for r in t.rows
               if r[c].startswith(prefix) and r[c][len(prefix):].isdigit()), default=0)
    counter = itertools.count(top + 1)
    return lambda: f"{prefix}{next(counter):0{width}d}"


def _split_days(u: Universe) -> list:
    n = len(u.days)
    return [u.days[n - SPLIT_FROM_END], u.days[n - SPLIT_FROM_END + 1]]


def owned(u: Universe, db: str, table: str, row: dict) -> bool:
    """Projector rows the incident pass may edit (spec §2.5, completed): everything else it only appends to."""
    inc, n = u.stories.incident, len(u.days)
    split_day, split_days = u.days[n - SPLIT_FROM_END], set(_split_days(u))
    x, holders = inc.security_id, set(inc.holder_ids)
    key = f"{db}.{table}"
    if key in ("marketmaster.golden_prices", "marketmaster.vendor_prices"):
        return row["security_id"] == x and row["price_date"] >= split_day
    if key == "marketmaster.dq_stage_metrics":
        return row["domain"] == "price" and row["business_date"] >= u.days[n - min(u.cfg.vendor_window_days, n)]
    if key in ("assetrecon.internal_positions", "assetrecon.custodian_positions"):
        return row["security_id"] == x and row["portfolio_id"] in holders and row["as_of"] >= split_day
    if key == "assetrecon.recon_exceptions":
        return row["security_id"] == x and row["portfolio_id"] in holders and row["business_date"] >= split_day
    if key == "assetrecon.recon_runs":
        return (row["portfolio_id"] in holders and row["recon_type"] in ("position", "nav")
                and row["business_date"] in split_days)
    if key == "assetrecon.nav_checks":
        return row["portfolio_id"] in holders and row["nav_date"] in split_days
    return False


def incident_record(u: Universe) -> dict:
    """The incident ids as recorded in app.seed_info.incident."""
    inc = u.stories.incident
    anchor = next(k for k, p in enumerate(u.portfolios) if p.portfolio_id == inc.anchor_portfolio_id)
    return {"security_id": inc.security_id, "issuer_entity_id": inc.issuer_entity_id, "source_id": inc.source_id,
            "anchor_portfolio_id": inc.anchor_portfolio_id, "holder_ids": list(inc.holder_ids),
            "fund_entity_id": fund_entity_id(u.cfg.n_entities, anchor),
            "fund_cash_account_id": fund_cash_account_id(u.cfg.n_cash_accounts, anchor)}


# ----------------------------------------------------------------------------------------------- refmaster
def _refmaster(u: Universe, inc: Incident, rng: random.Random, t: dict[str, TableData]) -> None:
    n = len(u.days)
    ents = t["legal_entities"]
    leis = {r[ents.columns.index("lei")] for r in ents.rows}
    for k, p in enumerate(u.portfolios):
        country = FUND_COUNTRY[p.base_ccy]
        lei = make_lei(rng)
        while lei in leis:
            lei = make_lei(rng)
        leis.add(lei)
        ents.add(fund_entity_id(u.cfg.n_entities, k), lei, f"{p.name} {LEGAL_SUFFIX[country]}", country,
                 REGION_OF[country], "Financials", None, "active", "fund")
    ca = t["corporate_actions"]
    next_ca = _next_id(ca, "ca_id", "CA", 6)
    split_day = u.days[n - SPLIT_FROM_END]
    ca.add(next_ca(), inc.security_id, "split", split_day, u.days[n - CASH_FROM_END], float(SPLIT_RATIO), "pending",
           inc.issuer_entity_id)
    held = {sid for h in u.holdings.values() for sid, _ in h}
    pool = sorted(s.security_id for s in u.securities_by_class["Equity"]
                  if s.security_id in held and s.security_id != inc.security_id and s.status == "active")
    window = u.days[n - min(u.cfg.position_window_days, n):n - 1]
    for sid in rng.sample(pool, min(NOISE_CORPORATE_ACTIONS, len(pool))):
        event = rng.choice(("dividend", "name_change"))      # no quantity effect: positions keep matching
        ex = rng.choice(window)
        pay = min(ex + timedelta(days=rng.randint(1, 5)), u.cfg.as_of)
        ratio = round(rng.uniform(0.1, 3.0), 4) if event == "dividend" else None
        ca.add(next_ca(), sid, event, ex, pay, ratio, "processed", u.security_by_id[sid].issuer_entity_id)
    exc = t["exceptions"]
    exc.add(_next_id(exc, "exc_id", "EX", 7)(), "R010", "corporate_action", inc.security_id, "Equity", "open",
            STEWARDS[0], at(split_day, 9, 30), None)
    cr = t["change_requests"]
    cr.add(_next_id(cr, "change_id", "CR", 6)(), "corporate_action", inc.security_id, STEWARDS[1], None, "pending",
           at(split_day, 10))


# ----------------------------------------------------------------------------------------------- feedhub
def _feedhub(u: Universe, inc: Incident, rng: random.Random, t: dict[str, TableData]) -> None:
    n = len(u.days)
    src = next(s for s in u.sources if s.source_id == inc.source_id)
    data_type, fmt, expected = CA_FEED
    fid = feed_id(src.source_id, data_type)
    t["feeds"].add(fid, src.source_id, data_type, fmt, "daily", expected, src.source_type)
    for i, d in enumerate(u.days):
        due = at(d, expected.hour, expected.minute)
        if i == n - FAILED_FROM_END:
            status, received, latency, count, error = \
                "failed", due + timedelta(minutes=rng.randint(0, 60)), None, None, "SCHEMA_MISMATCH"
        elif i == n - SPLIT_FROM_END:
            latency = rng.randint(300, 600)
            status, received, count, error = "late", due + timedelta(minutes=latency), rng.randint(1, 40), None
        else:
            status, latency, count, error = "on_time", 0, rng.randint(1, 40), None
            received = due - timedelta(minutes=rng.randint(5, 120))
        t["feed_deliveries"].add(delivery_id(src.source_id, data_type, d), fid, src.source_id, d, status, received,
                                 latency, count, error, src.source_type, data_type)


# ----------------------------------------------------------------------------------------------- entry point
def apply_incident(u: Universe, tables: dict[str, dict[str, TableData]]) -> None:
    inc = u.stories.incident
    rng = random.Random(u.cfg.seed + 6)
    _refmaster(u, inc, rng, tables["refmaster"])
    _feedhub(u, inc, rng, tables["feedhub"])
