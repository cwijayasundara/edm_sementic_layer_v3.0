"""AssetRecon: internal (IBOR/ABOR) vs custodian positions & transactions, recon runs, exceptions, NAV checks."""
import random
from datetime import timedelta

from prism.sim.keys import delivery_id
from prism.sim.model import TableData, id_sequence
from prism.sim.universe import Portfolio, Universe

BASE_CAUSES = ("trade_date_vs_settle_date", "corporate_action_pending", "custodian_booking_error", "fx_rate", "unknown")
TXN_TYPES = ("buy", "sell", "dividend", "fee")
RECON_TYPES = ("position", "cash", "transaction", "nav")
INV_OPS = ("invops01", "invops02", "invops03", "invops04")


def project_assetrecon(u: Universe) -> dict[str, TableData]:
    rng, nid = random.Random(u.cfg.seed + 4), id_sequence()
    n = len(u.days)
    t = {
        "custodians": TableData(("custodian_id", "name", "feed_source_id")),
        "portfolios": TableData(("portfolio_id", "name", "fund_group", "base_ccy", "custodian_id", "region")),
        "internal_positions": TableData(("portfolio_id", "security_id", "book", "qty", "mv", "as_of", "fund_group")),
        "custodian_positions": TableData(("portfolio_id", "security_id", "qty", "mv", "as_of", "delivery_id",
                                          "fund_group")),
        "internal_transactions": TableData(("txn_id", "portfolio_id", "security_id", "trade_date", "settle_date",
                                            "txn_type", "qty", "amount", "fund_group")),
        "custodian_transactions": TableData(("txn_id", "portfolio_id", "security_id", "trade_date", "settle_date",
                                             "txn_type", "qty", "amount", "internal_ref", "fund_group")),
        "recon_runs": TableData(("run_id", "portfolio_id", "recon_type", "business_date", "matched", "unmatched",
                                 "status", "signed_off_by", "fund_group")),
        "recon_exceptions": TableData(("exc_id", "run_id", "portfolio_id", "security_id", "business_date", "diff_qty",
                                       "diff_mv", "cause_code", "assigned_to", "status", "sla_due", "fund_group")),
        "nav_checks": TableData(("portfolio_id", "nav_date", "admin_nav", "internal_nav", "diff_bps", "fund_group")),
    }
    custodian_ids: dict[str, str] = {}
    for k, s in enumerate(x for x in u.sources if x.source_type == "custodian"):
        custodian_ids[s.source_id] = f"CUS{k + 1:02d}"
        t["custodians"].add(custodian_ids[s.source_id], s.name, s.source_id)
    window = range(n - min(u.cfg.position_window_days, n), n)
    for p in u.portfolios:
        t["portfolios"].add(p.portfolio_id, p.name, p.fund_group, p.base_ccy, custodian_ids[p.custodian_source_id],
                            p.region)
        _transactions(rng, u, p, t, nid)
        for i in window:
            _positions_day(rng, u, p, i, t, nid)
    return t


def _is_late(u: Universe, p: Portfolio, i: int) -> bool:
    return p.custodian_source_id == u.stories.late_custodian_source_id and i >= u.stories.late_start_idx


def _positions_day(rng, u: Universe, p: Portfolio, i: int, t, nid) -> None:
    st, n, d, fg = u.stories, len(u.days), u.days[i], p.fund_group
    late = _is_late(u, p, i)
    delivered_on = u.days[st.late_start_idx - 1] if late else d
    dlv = delivery_id(p.custodian_source_id, "positions", delivered_on)
    p_exception = 0.12 if late else 0.008
    exceptions, internal_nav, admin_nav = [], 0.0, 0.0
    for sid, qty in u.holdings[p.portfolio_id]:
        golden, true_px = u.golden(sid, i), u.prices[sid][i]
        internal_nav += qty * golden
        admin_nav += qty * true_px
        mv = round(qty * golden, 2)
        for book in ("IBOR", "ABOR"):
            t["internal_positions"].add(p.portfolio_id, sid, book, qty, mv, d, fg)
        custodian_qty = qty
        if rng.random() < p_exception:
            delta = max(1.0, round(qty * rng.uniform(0.01, 0.05))) * rng.choice((-1, 1))
            custodian_qty = qty + delta
            cause = "stale_custodian_data" if late else rng.choice(BASE_CAUSES)
            exceptions.append((sid, delta, round(delta * true_px, 2), cause))
        t["custodian_positions"].add(p.portfolio_id, sid, custodian_qty, round(custodian_qty * true_px, 2), d, dlv, fg)

    for recon_type in RECON_TYPES:
        if recon_type == "position":
            unmatched = len(exceptions)
            matched = len(u.holdings[p.portfolio_id]) - unmatched
        elif recon_type == "nav":
            unmatched = int(p.portfolio_id in st.nav_portfolio_ids and i >= n - st.stale_days)
            matched = 1 - unmatched
        else:
            unmatched, matched = rng.choices((0, 1, 2), weights=(85, 12, 3))[0], rng.randint(20, 120)
        if i == n - 1:
            status, signer = "in_progress", None
        elif unmatched and i >= n - 3:
            status, signer = "exceptions_open", None
        else:
            status, signer = "signed_off", rng.choice(INV_OPS)
        run_id = nid("RR")
        t["recon_runs"].add(run_id, p.portfolio_id, recon_type, d, matched, unmatched, status, signer, fg)
        if recon_type == "position":
            for sid, diff_qty, diff_mv, cause in exceptions:
                is_open = i >= n - 3 or rng.random() < 0.15
                t["recon_exceptions"].add(nid("RX"), run_id, p.portfolio_id, sid, d, diff_qty, diff_mv, cause,
                                          rng.choice(INV_OPS), "open" if is_open else "closed",
                                          d + timedelta(days=2), fg)

    admin = admin_nav * (1 + rng.gauss(0, 0.00005))
    t["nav_checks"].add(p.portfolio_id, d, round(admin, 2), round(internal_nav, 2),
                        round((internal_nav - admin) / admin * 1e4, 3), fg)


def _transactions(rng, u: Universe, p: Portfolio, t, nid) -> None:
    holdings = u.holdings[p.portfolio_id]
    for i, d in enumerate(u.days):
        late = _is_late(u, p, i)
        for _ in range(rng.randint(0, 3)):
            sid, _ = rng.choice(holdings)
            txn_type = rng.choice(TXN_TYPES)
            qty = float(rng.randint(1, 50) * 100) if txn_type in ("buy", "sell") else 0.0
            amount = round(qty * u.prices[sid][i], 2) if qty else round(rng.uniform(100, 25_000), 2)
            internal_id, settle = nid("ITX"), d + timedelta(days=2)
            t["internal_transactions"].add(internal_id, p.portfolio_id, sid, d, settle, txn_type, qty, amount,
                                           p.fund_group)
            if not late and rng.random() < 0.98:
                t["custodian_transactions"].add(nid("CTX"), p.portfolio_id, sid, d, settle, txn_type, qty, amount,
                                                internal_id, p.fund_group)
