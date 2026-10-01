"""One fictional firm's shared universe. Every platform projection is derived from it,
so identifiers (security, entity, custodian, account) line up across systems."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cached_property

from prism.sim.calendar import business_days
from prism.sim.ids import make_bic, make_isin, make_lei

ASSET_CLASSES = ("Equity", "Corp bond", "Govt", "FX", "Deriv")
ASSET_CLASS_WEIGHTS = (0.35, 0.30, 0.12, 0.08, 0.15)
SUB_CLASSES = {
    "Equity": ("Common", "Preferred", "ADR"),
    "Corp bond": ("IG", "HY"),
    "Govt": ("Bill", "Note", "Bond"),
    "FX": ("Spot", "Forward"),
    "Deriv": ("Future", "Option", "Swap"),
}
DAILY_VOL = {"Equity": 0.018, "Corp bond": 0.004, "Govt": 0.002, "FX": 0.006, "Deriv": 0.03}
PRICE_RANGE = {"Equity": (10, 500), "Corp bond": (88, 112), "Govt": (94, 106), "FX": (0.5, 150), "Deriv": (1, 50)}
REGIONS = {
    "EMEA": ("GB", "DE", "FR", "NL", "CH", "IE", "LU"),
    "AMER": ("US", "CA", "BR"),
    "APAC": ("JP", "SG", "AU", "HK"),
}
REGION_OF = {c: r for r, cs in REGIONS.items() for c in cs}
COUNTRY_CCY = {
    "GB": "GBP", "DE": "EUR", "FR": "EUR", "NL": "EUR", "IE": "EUR", "LU": "EUR", "CH": "CHF",
    "US": "USD", "CA": "CAD", "BR": "BRL", "JP": "JPY", "SG": "SGD", "AU": "AUD", "HK": "HKD",
}
LEGAL_SUFFIX = {
    "GB": "PLC", "DE": "AG", "FR": "SA", "NL": "NV", "CH": "AG", "IE": "Ltd", "LU": "SA",
    "US": "Inc", "CA": "Corp", "BR": "SA", "JP": "KK", "SG": "Pte Ltd", "AU": "Ltd", "HK": "Ltd",
}
SECTORS = ("Financials", "Industrials", "Technology", "Energy", "Utilities",
           "Healthcare", "Consumer", "Materials", "Telecom", "Real Estate")
NAME_PARTS_A = ("Aldr", "Bram", "Cald", "Dorn", "Elst", "Fenw", "Garr", "Hask", "Ingl", "Jarr", "Kell", "Lorr",
                "Mald", "Norr", "Orwe", "Pell", "Quen", "Rask", "Stav", "Tarn", "Ulst", "Vard", "Wynd", "Yell")
NAME_PARTS_B = ("ane", "ex", "ford", "ic", "ion", "is", "ity", "on", "ora", "ova",
                "ridge", "sen", "ton", "trix", "um", "vale", "well", "wick", "worth", "yx")
SOURCE_WORDS = ("Aster", "Briar", "Cobalt", "Dunmore", "Ember", "Fairholt", "Granite", "Halcyon",
                "Ironwood", "Juniper", "Kestrel", "Larkspur", "Marlow", "Northgate", "Oakridge", "Pinecrest",
                "Quillon", "Redfern", "Silverbirch", "Thornbury", "Umberly", "Valemont", "Westbrook", "Yarrow")
SOURCE_LAYOUT = (("custodian", 22, "Custody"), ("bank", 14, "Bank"), ("prime_broker", 4, "Prime"))
VENDORS = (("V_A", "Vendor A"), ("V_B", "Vendor B"), ("V_C", "Vendor C"),
           ("V_D", "Vendor D"), ("V_E", "Vendor E"), ("V_F", "Vendor F"))
VENDOR_RANK = {"V_B": 1, "V_D": 2, "V_A": 3, "V_C": 4, "V_F": 5, "V_E": 6}
VENDOR_COVERAGE = {
    "V_A": ASSET_CLASSES,
    "V_B": ("Equity", "Corp bond", "Govt"),
    "V_C": ("Equity", "FX", "Deriv"),
    "V_D": ("Corp bond", "Govt", "FX"),
    "V_E": ("Equity", "Deriv"),
    "V_F": ASSET_CLASSES,
}
FUND_GROUPS = ("Growth", "Income", "Multi-Asset", "Liquidity")
FUND_GROUP_CLASSES = {
    "Growth": ("Equity",),
    "Income": ("Corp bond", "Govt"),
    "Multi-Asset": ("Equity", "Corp bond", "Govt"),
    "Liquidity": ("Govt", "FX"),
}
CASH_CCYS = ("USD", "EUR", "GBP", "JPY", "CHF", "SGD")
LATE_CUSTODIAN_PORTFOLIOS = ("PF001", "PF002", "PF005")
NAV_PORTFOLIOS = ("PF003", "PF009")
STALE_JUMP = 1.06


@dataclass(frozen=True)
class SimConfig:
    seed: int = 42
    as_of: date = date(2026, 9, 30)
    n_days: int = 90
    n_securities: int = 2000
    n_entities: int = 400
    n_portfolios: int = 30
    n_cash_accounts: int = 60
    holdings_range: tuple[int, int] = (40, 70)
    vendor_window_days: int = 20
    position_window_days: int = 20
    profile: str = "full"

    def __post_init__(self) -> None:
        if self.n_portfolios < 9:
            raise ValueError("n_portfolios must be >= 9 (stories use PF001-PF009)")
        if self.n_days < 25:
            raise ValueError("n_days must be >= 25 (stories need ~5 weeks of history)")
        if self.n_cash_accounts < 4:
            raise ValueError("n_cash_accounts must be >= 4")
        if self.n_entities < 40:
            raise ValueError("n_entities must be >= 40")

    @classmethod
    def small(cls, **overrides) -> SimConfig:
        base = dict(n_days=25, n_securities=240, n_entities=60, n_portfolios=12,
                    n_cash_accounts=18, holdings_range=(12, 20), profile="small")
        return cls(**{**base, **overrides})


@dataclass(frozen=True)
class Entity:
    entity_id: str
    lei: str
    name: str
    country: str
    region: str
    sector: str
    parent_entity_id: str | None
    status: str


@dataclass(frozen=True)
class Security:
    security_id: str
    isin: str
    cusip: str | None
    sedol: str | None
    ticker: str | None
    name: str
    asset_class: str
    sub_class: str
    ccy: str
    issuer_entity_id: str | None
    country: str
    status: str
    valid_from: date


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    source_type: str
    bic: str
    country: str


@dataclass(frozen=True)
class Portfolio:
    portfolio_id: str
    name: str
    fund_group: str
    base_ccy: str
    custodian_source_id: str
    region: str


@dataclass(frozen=True)
class CashAccount:
    account_id: str
    legal_entity_id: str
    bank_source_id: str
    bank_bic: str
    nostro_no: str
    ccy: str
    region: str


@dataclass(frozen=True)
class Stories:
    """Planted, verifiable demo narratives (spec §2 'Planted stories')."""
    late_custodian_source_id: str
    late_start_idx: int
    usd_break_entity_id: str
    usd_break_extra: int
    stale_security_ids: tuple[str, ...]
    conflict_vendor_id: str = "V_A"
    conflict_asset_class: str = "Corp bond"
    conflict_daily_this_week: int = 12
    conflict_daily_prev_week: int = 9
    late_portfolio_ids: tuple[str, ...] = LATE_CUSTODIAN_PORTFOLIOS
    usd_break_account_ids: tuple[str, ...] = ("CA0001", "CA0002")
    nav_portfolio_ids: tuple[str, ...] = NAV_PORTFOLIOS
    stale_days: int = 3


@dataclass
class Universe:
    cfg: SimConfig
    days: list[date]
    entities: list[Entity]
    securities: list[Security]
    prices: dict[str, list[float]]
    sources: list[Source]
    portfolios: list[Portfolio]
    holdings: dict[str, list[tuple[str, float]]]
    cash_accounts: list[CashAccount]
    stories: Stories

    @cached_property
    def securities_by_class(self) -> dict[str, list[Security]]:
        out: dict[str, list[Security]] = {ac: [] for ac in ASSET_CLASSES}
        for s in self.securities:
            out[s.asset_class].append(s)
        return out

    @cached_property
    def security_by_id(self) -> dict[str, Security]:
        return {s.security_id: s for s in self.securities}

    def golden(self, sid: str, i: int) -> float:
        """EDM golden price: equals the market price, except the stale story carries it forward."""
        n, st = len(self.days), self.stories
        if sid in st.stale_security_ids and i >= n - st.stale_days:
            return self.prices[sid][n - st.stale_days - 1]
        return self.prices[sid][i]


def _make_entities(rng: random.Random, n: int) -> list[Entity]:
    entities: list[Entity] = []
    leis: set[str] = set()
    countries = [c for cs in REGIONS.values() for c in cs]

    def new_lei() -> str:
        while True:
            lei = make_lei(rng)
            if lei not in leis:
                leis.add(lei)
                return lei

    for i, country in enumerate(countries):  # one sovereign issuer per country
        entities.append(Entity(f"LE{i + 1:05d}", new_lei(), f"{country} Treasury", country,
                               REGION_OF[country], "Sovereign", None, "active"))
    names: set[str] = set()
    while len(entities) < n:
        country = rng.choice(countries)
        base = f"{rng.choice(NAME_PARTS_A)}{rng.choice(NAME_PARTS_B)}"
        if base in names:
            continue
        names.add(base)
        corporates = [e for e in entities if e.sector != "Sovereign"]
        parent = rng.choice(corporates).entity_id if corporates and rng.random() < 0.1 else None
        entities.append(Entity(
            f"LE{len(entities) + 1:05d}", new_lei(), f"{base} {LEGAL_SUFFIX[country]}", country,
            REGION_OF[country], rng.choice(SECTORS), parent, "active" if rng.random() < 0.97 else "inactive",
        ))
    return entities


_BASE36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _base36(k: int) -> str:
    out = ""
    while True:
        k, r = divmod(k, 36)
        out = _BASE36[r] + out
        if k == 0:
            return out


def _equity_ticker(issuer_short: str, seq: int) -> str:
    """Issuer's 4-letter stem + base-36 security sequence: unique because the sequence is (no RNG draw)."""
    return f"{issuer_short[:4].upper()}{_base36(seq)}"


def _unique_name(base: str, asset_class: str, seen: set[str], dup_count: dict[str, int]) -> str:
    """Disambiguate repeated display names with a share-class (equity) or series suffix (no RNG draw)."""
    name, k = base, dup_count.get(base, 0)
    while name in seen:
        k += 1
        name = f"{base} Class {_BASE36[10 + k]}" if asset_class == "Equity" and k < 26 else f"{base} Series {k + 1}"
    dup_count[base] = k
    seen.add(name)
    return name


def _make_securities(rng: random.Random, n: int, entities: list[Entity], start: date) -> list[Security]:
    sovereigns = {e.country: e for e in entities if e.sector == "Sovereign"}
    corporates = [e for e in entities if e.sector != "Sovereign" and e.status == "active"]
    fx_pairs = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "USDSGD", "EURGBP")
    isins: set[str] = set()
    names: set[str] = set()
    name_dups: dict[str, int] = {}
    out: list[Security] = []
    while len(out) < n:
        ac = rng.choices(ASSET_CLASSES, weights=ASSET_CLASS_WEIGHTS)[0]
        sub = rng.choice(SUB_CLASSES[ac])
        if ac == "Govt":
            issuer = sovereigns[rng.choice(list(sovereigns))]
        elif ac in ("Equity", "Corp bond"):
            issuer = rng.choice(corporates)
        else:
            issuer = None
        country = issuer.country if issuer else rng.choice(("US", "GB", "DE", "JP"))
        isin, cusip, sedol = make_isin(rng, country)
        if isin in isins:
            continue
        isins.add(isin)
        short = issuer.name.split(" ")[0] if issuer else ""
        year = start.year + rng.randint(1, 12)
        coupon = rng.choice((1.25, 2.0, 2.75, 3.5, 4.125, 5.0, 6.25))
        name = {
            "Equity": f"{short} {sub}",
            "Corp bond": f"{short} {coupon:.3f}% {year}",
            "Govt": f"{country} Govt {sub} {coupon:.3f}% {year}",
            "FX": f"{rng.choice(fx_pairs)} {sub}",
            "Deriv": f"{rng.choice(('Index', 'Rates', 'Commodity', 'Credit'))} {sub} {year}",
        }[ac]
        name = _unique_name(name, ac, names, name_dups)
        ticker = _equity_ticker(short, len(out) + 1) if ac == "Equity" else None
        out.append(Security(
            f"SEC{len(out) + 1:06d}", isin, cusip, sedol, ticker, name, ac, sub, COUNTRY_CCY[country],
            issuer.entity_id if issuer else None, country,
            "active" if rng.random() < 0.98 else "matured", start - timedelta(days=rng.randint(30, 3650)),
        ))
    return out


def _make_prices(rng: random.Random, securities: list[Security], n_days: int) -> dict[str, list[float]]:
    prices: dict[str, list[float]] = {}
    for s in securities:
        lo, hi = PRICE_RANGE[s.asset_class]
        p, vol, path = rng.uniform(lo, hi), DAILY_VOL[s.asset_class], []
        for _ in range(n_days):
            path.append(round(p, 6))
            p *= math.exp(rng.gauss(0, vol))
        prices[s.security_id] = path
    return prices


def _make_sources(rng: random.Random) -> list[Source]:
    countries = [c for cs in REGIONS.values() for c in cs]
    out: list[Source] = []
    bics: set[str] = set()
    idx = 0
    for source_type, count, suffix in SOURCE_LAYOUT:
        for _ in range(count):
            country = rng.choice(countries)
            bic = make_bic(rng, country)
            while bic in bics:
                bic = make_bic(rng, country)
            bics.add(bic)
            out.append(Source(f"SRC{idx + 1:03d}", f"{SOURCE_WORDS[idx % len(SOURCE_WORDS)]} {suffix}",
                              source_type, bic, country))
            idx += 1
    return out


def _make_portfolios(rng, cfg, sources, securities, late_src, excluded):
    custodians = [s for s in sources if s.source_type == "custodian" and s.source_id != late_src]
    by_class: dict[str, list[Security]] = {ac: [] for ac in ASSET_CLASSES}
    for s in securities:
        if s.status == "active" and s.security_id not in excluded:
            by_class[s.asset_class].append(s)
    portfolios, holdings = [], {}
    for i in range(cfg.n_portfolios):
        pid, group = f"PF{i + 1:03d}", FUND_GROUPS[i % len(FUND_GROUPS)]
        ccy = rng.choice(("USD", "EUR", "GBP"))
        custodian = late_src if pid in LATE_CUSTODIAN_PORTFOLIOS else rng.choice(custodians).source_id
        portfolios.append(Portfolio(pid, f"{group} Fund {i + 1:02d}", group, ccy, custodian,
                                    "AMER" if ccy == "USD" else "EMEA"))
        pool = [s for ac in FUND_GROUP_CLASSES[group] for s in by_class[ac]]
        chosen = rng.sample(pool, min(len(pool), rng.randint(*cfg.holdings_range)))
        holdings[pid] = [(s.security_id, float(rng.randint(10, 1000) * 100)) for s in chosen]
    return portfolios, holdings


def _pick_house_entities(entities: list[Entity]) -> dict[str, list[Entity]]:
    house = {
        region: [e for e in entities if e.region == region and e.sector != "Sovereign" and e.status == "active"][:3]
        for region in REGIONS
    }
    missing = [r for r, es in house.items() if not es]
    if missing:
        raise ValueError(f"no active corporate entity in region(s) {missing}; increase n_entities")
    return house


def _make_cash_accounts(rng, cfg, house, sources, story_entity) -> list[CashAccount]:
    banks = [s for s in sources if s.source_type == "bank"]
    all_house = [e for es in house.values() for e in es]
    out = []
    for i in range(cfg.n_cash_accounts):
        entity, ccy = (story_entity, "USD") if i < 2 else (rng.choice(all_house), rng.choice(CASH_CCYS))
        bank = rng.choice(banks)
        nostro = "".join(rng.choice("0123456789") for _ in range(12))
        out.append(CashAccount(f"CA{i + 1:04d}", entity.entity_id, bank.source_id, bank.bic, nostro, ccy, entity.region))
    return out


def build_universe(cfg: SimConfig) -> Universe:
    rng = random.Random(cfg.seed)
    days = business_days(cfg.as_of, cfg.n_days)
    entities = _make_entities(rng, cfg.n_entities)
    securities = _make_securities(rng, cfg.n_securities, entities, days[0])
    prices = _make_prices(rng, securities, len(days))
    sources = _make_sources(rng)
    late_src = next(s.source_id for s in sources if s.source_type == "custodian")
    equities = sorted(
        (s for s in securities if s.asset_class == "Equity" and s.status == "active"),
        key=lambda s: -prices[s.security_id][0],
    )
    stale = tuple(s.security_id for s in equities[:5])
    portfolios, holdings = _make_portfolios(rng, cfg, sources, securities, late_src, set(stale))
    for pid in NAV_PORTFOLIOS:
        holdings[pid].extend((sid, float(rng.randint(10, 1000) * 100)) for sid in stale)
    house = _pick_house_entities(entities)
    story_entity = house["EMEA"][0]
    cash_accounts = _make_cash_accounts(rng, cfg, house, sources, story_entity)
    stories = Stories(
        late_custodian_source_id=late_src,
        late_start_idx=len(days) - 6,
        usd_break_entity_id=story_entity.entity_id,
        usd_break_extra=max(12, cfg.n_cash_accounts),
        stale_security_ids=stale,
    )
    for sid in stale:  # the market moved; the EDM golden copy did not (see Universe.golden)
        path = prices[sid]
        for i in range(len(days) - stories.stale_days, len(days)):
            path[i] = round(path[i] * STALE_JUMP, 6)
    return Universe(cfg, days, entities, securities, prices, sources, portfolios, holdings, cash_accounts, stories)
