"""MarketMaster EDM: multi-vendor prices, golden copy, price suspects, DQ stage metrics, ESG."""
import itertools
import random
from collections import Counter
from datetime import timedelta

from prism.sim.calendar import at
from prism.sim.model import TableData
from prism.sim.universe import ASSET_CLASSES, VENDOR_COVERAGE, VENDOR_RANK, VENDORS, Universe

STAGES = ("acquired", "validated", "suspect", "approved", "distributed")
ESG_PROVIDERS = ("ESG Provider 1", "ESG Provider 2")


def project_marketmaster(u: Universe) -> dict[str, TableData]:
    rng = random.Random(u.cfg.seed + 2)
    n, st = len(u.days), u.stories
    t = {
        "vendors": TableData(("vendor_id", "name", "rank_default")),
        "instruments": TableData(("security_id", "isin", "name", "asset_class", "ccy")),
        "golden_prices": TableData(("security_id", "price_date", "value", "chosen_vendor_id", "rule", "asset_class")),
        "vendor_prices": TableData(("security_id", "vendor_id", "price_date", "price_type", "value", "ccy",
                                    "received_at", "asset_class")),
        "price_suspects": TableData(("suspect_id", "security_id", "vendor_id", "price_date", "kind", "deviation_pct",
                                     "status", "asset_class")),
        "dq_stage_metrics": TableData(("business_date", "domain", "stage", "count", "sla_met")),
        "esg_scores": TableData(("entity_id", "provider", "as_of", "score")),
    }
    for vid, name in VENDORS:
        t["vendors"].add(vid, name, VENDOR_RANK[vid])
    for s in u.securities:
        t["instruments"].add(s.security_id, s.isin, s.name, s.asset_class, s.ccy)

    covering = {ac: [v for v, _ in VENDORS if ac in VENDOR_COVERAGE[v]] for ac in ASSET_CLASSES}
    golden_vendor = {ac: min(covering[ac], key=VENDOR_RANK.__getitem__) for ac in ASSET_CLASSES}
    stale = set(st.stale_security_ids)
    for i, d in enumerate(u.days):
        for s in u.securities:
            is_stale = s.security_id in stale and i >= n - st.stale_days
            t["golden_prices"].add(s.security_id, d, u.golden(s.security_id, i), golden_vendor[s.asset_class],
                                   "carry_forward" if is_stale else "vendor_rank", s.asset_class)

    window = range(n - min(u.cfg.vendor_window_days, n), n)
    conflicts = _pick_conflicts(rng, u, window)
    seq = itertools.count(1)

    def add_suspect(sid, vid, i, kind, deviation, asset_class):
        if i >= n - 3:
            status = rng.choices(("open", "under_review"), weights=(80, 20))[0]
        else:
            status = "resolved" if rng.random() < 0.92 else "open"
        dev = round(deviation, 4) if deviation is not None else None
        t["price_suspects"].add(f"PS{next(seq):07d}", sid, vid, u.days[i], kind, dev, status, asset_class)

    for i in window:
        d = u.days[i]
        for s in u.securities:
            true_px = u.prices[s.security_id][i]
            for vid in covering[s.asset_class]:
                dev = conflicts.get((s.security_id, vid, i))
                if dev is None and rng.random() < 0.0005:  # a planted conflict is never masked by a missing quote
                    add_suspect(s.security_id, vid, i, "missing", None, s.asset_class)
                    continue
                if dev is not None:
                    value = true_px * (1 + dev / 100)
                    add_suspect(s.security_id, vid, i, "conflict", dev, s.asset_class)
                elif rng.random() < 0.0003:
                    dev = rng.choice((-1, 1)) * rng.uniform(8, 20)
                    value = true_px * (1 + dev / 100)
                    add_suspect(s.security_id, vid, i, "spike", dev, s.asset_class)
                else:
                    value = true_px * (1 + rng.gauss(0, 0.0005))
                t["vendor_prices"].add(s.security_id, vid, d, "close", round(value, 6), s.ccy,
                                       at(d, 17, 30) + timedelta(minutes=rng.randrange(120)), s.asset_class)
    for sid in st.stale_security_ids:
        s = u.security_by_id[sid]
        for i in range(n - st.stale_days, n):
            golden, true_px = u.golden(sid, i), u.prices[sid][i]
            add_suspect(sid, golden_vendor[s.asset_class], i, "stale", (true_px - golden) / golden * 100, s.asset_class)
            if t["price_suspects"].rows[-1][6] == "resolved":  # never happens (i >= n-3), guard for clarity
                raise AssertionError("stale story suspects must be unresolved")

    _dq_metrics(rng, u, t, covering, window.start)
    _esg(rng, u, t["esg_scores"])
    return t


def _pick_conflicts(rng: random.Random, u: Universe, window: range) -> dict[tuple[str, str, int], float]:
    """Planted price conflicts. The story cell (Vendor A x Corp bond) has a fixed daily count per week;
    every other cell's daily counts in "this week" (last 5 business days) replay the counts it drew for
    "previous week", so the week-on-week change in total conflicts is driven by the story delta alone."""
    st, n = u.stories, len(u.days)
    this_week, prev_week = range(n - 5, n), range(n - 10, n - 5)
    story_cell = (st.conflict_vendor_id, st.conflict_asset_class)
    background: dict[tuple[str, str, int], int] = {}
    out: dict[tuple[str, str, int], float] = {}
    for i in window:
        for vid, _ in VENDORS:
            for ac in VENDOR_COVERAGE[vid]:
                if (vid, ac) == story_cell and i in this_week:
                    k = st.conflict_daily_this_week
                elif (vid, ac) == story_cell and i in prev_week:
                    k = st.conflict_daily_prev_week
                elif i in this_week and (vid, ac, i - 5) in background:
                    k = background[(vid, ac, i - 5)]
                else:
                    r = rng.random()
                    k = 2 if r < 0.05 else 1 if r < 0.30 else 0
                    background[(vid, ac, i)] = k
                pool = [s.security_id for s in u.securities_by_class[ac]]
                for sid in rng.sample(pool, min(k, len(pool))):
                    out[(sid, vid, i)] = rng.choice((-1, 1)) * rng.uniform(1.0, 4.0)
    return out


def _dq_metrics(rng, u, t, covering, window_start) -> None:
    date_col = t["price_suspects"].columns.index("price_date")
    suspects_per_day = Counter(row[date_col] for row in t["price_suspects"].rows)
    obs_per_day = sum(len(covering[s.asset_class]) for s in u.securities)
    n = len(u.days)
    for i, d in enumerate(u.days):
        for domain in ("security", "price", "entity"):
            if domain == "price":
                acquired = obs_per_day
            elif domain == "security":
                acquired = max(1, int(len(u.securities) * 0.05 * rng.uniform(0.8, 1.2)))
            else:
                acquired = max(1, int(len(u.entities) * 0.02 * rng.uniform(0.8, 1.2)))
            if domain == "price" and i >= window_start:
                suspect = suspects_per_day[d]
            else:
                suspect = int(acquired * rng.uniform(0.002, 0.008))
            validated = acquired - suspect
            approved = validated + (int(suspect * 0.8) if i < n - 1 else 0)
            sla_met = rng.random() > 0.05
            for stage, count in zip(STAGES, (acquired, validated, suspect, approved, approved)):
                t["dq_stage_metrics"].add(d, domain, stage, count, sla_met)


def _esg(rng: random.Random, u: Universe, table: TableData) -> None:
    n = len(u.days)
    dates = (u.days[max(0, n - 22)], u.days[-1])
    for e in u.entities:
        if e.sector == "Sovereign":
            continue
        for provider in ESG_PROVIDERS:
            base = rng.uniform(20, 90)
            for d in dates:
                table.add(e.entity_id, provider, d, round(min(100.0, max(0.0, base + rng.gauss(0, 3))), 2))
