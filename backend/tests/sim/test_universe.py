import pytest

from prism.sim.calendar import business_days
from prism.sim.ids import is_valid_isin, is_valid_lei
from prism.sim.model import TableData, id_sequence
from prism.sim.universe import (
    LATE_CUSTODIAN_PORTFOLIOS,
    NAV_PORTFOLIOS,
    SimConfig,
    build_universe,
)


def test_business_days_skip_weekends_and_end_at_as_of(universe):
    days = business_days(universe.cfg.as_of, 10)
    assert days[-1] == universe.cfg.as_of and len(days) == 10
    assert all(d.weekday() < 5 for d in days) and days == sorted(days)


def test_table_data_rejects_wrong_arity_and_ids_are_sequential():
    t = TableData(("a", "b"))
    with pytest.raises(ValueError):
        t.add(1)
    nid = id_sequence()
    assert [nid("X", 3), nid("X", 3), nid("Y", 2)] == ["X001", "X002", "Y01"]


def test_counts_follow_config(universe):
    cfg = universe.cfg
    assert len(universe.days) == cfg.n_days
    assert len(universe.securities) == cfg.n_securities
    assert len(universe.entities) == cfg.n_entities
    assert len(universe.portfolios) == cfg.n_portfolios
    assert len(universe.cash_accounts) == cfg.n_cash_accounts
    assert all(len(path) == cfg.n_days for path in universe.prices.values())


def test_identifiers_are_valid_and_unique(universe):
    isins = [s.isin for s in universe.securities]
    leis = [e.lei for e in universe.entities]
    assert len(set(isins)) == len(isins) and all(map(is_valid_isin, isins))
    assert len(set(leis)) == len(leis) and all(map(is_valid_lei, leis))


def test_build_is_deterministic():
    a, b = build_universe(SimConfig.small()), build_universe(SimConfig.small())
    assert [s.isin for s in a.securities] == [s.isin for s in b.securities]
    assert a.prices == b.prices and a.holdings == b.holdings


def test_story_wiring(universe):
    st = universe.stories
    by_id = {p.portfolio_id: p for p in universe.portfolios}
    assert st.late_portfolio_ids == LATE_CUSTODIAN_PORTFOLIOS
    assert all(by_id[pid].custodian_source_id == st.late_custodian_source_id for pid in st.late_portfolio_ids)
    assert by_id["PF001"].fund_group == "Growth"
    assert all(
        p.custodian_source_id != st.late_custodian_source_id
        for p in universe.portfolios
        if p.portfolio_id not in st.late_portfolio_ids
    )
    for pid in NAV_PORTFOLIOS:
        held = {sid for sid, _ in universe.holdings[pid]}
        assert set(st.stale_security_ids) <= held
    others = [pid for pid in universe.holdings if pid not in NAV_PORTFOLIOS]
    assert not any(sid in st.stale_security_ids for pid in others for sid, _ in universe.holdings[pid])
    accounts = {a.account_id: a for a in universe.cash_accounts}
    for aid in st.usd_break_account_ids:
        assert accounts[aid].ccy == "USD" and accounts[aid].region == "EMEA"
        assert accounts[aid].legal_entity_id == st.usd_break_entity_id


def test_golden_price_is_carried_forward_for_stale_securities(universe):
    st, n = universe.stories, len(universe.days)
    sid = st.stale_security_ids[0]
    frozen = universe.prices[sid][n - st.stale_days - 1]
    assert all(universe.golden(sid, i) == frozen for i in range(n - st.stale_days, n))
    assert universe.prices[sid][-1] != frozen


def test_config_guards_story_prerequisites():
    with pytest.raises(ValueError):
        SimConfig(n_portfolios=5)
    with pytest.raises(ValueError):
        SimConfig(n_days=10)


@pytest.mark.parametrize("cfg", [SimConfig.small(), SimConfig()], ids=["small", "full"])
def test_tickers_and_names_are_unique(cfg):
    u = build_universe(cfg)
    equities = [s for s in u.securities if s.asset_class == "Equity"]
    tickers = [s.ticker for s in equities]
    assert all(tickers) and len(set(tickers)) == len(tickers)
    assert all(s.ticker is None for s in u.securities if s.asset_class != "Equity")
    names = [s.name for s in u.securities]
    assert len(set(names)) == len(names)
    isins = [s.isin for s in u.securities]
    assert len(set(isins)) == len(isins) and all(map(is_valid_isin, isins))


def test_scale_multiplies_the_universe_counts_once():
    from dataclasses import replace
    cfg = SimConfig(scale=2)
    assert (cfg.n_securities, cfg.n_entities, cfg.n_portfolios, cfg.n_cash_accounts) == (4000, 800, 60, 120)
    assert cfg.scale == 1.0 and cfg.profile == "fullx2"
    assert replace(cfg) == cfg                                    # never scaled twice
    assert SimConfig() == SimConfig(scale=1.0)                    # the default profile is unchanged
    small = SimConfig.small(scale=1.5)
    assert small.n_portfolios == 18 and small.profile == "smallx1.5"


def test_scale_still_guards_story_prerequisites():
    with pytest.raises(ValueError, match="n_portfolios"):
        SimConfig(scale=0.2)                                      # 30 -> 6 portfolios
    with pytest.raises(ValueError, match="scale"):
        SimConfig(scale=0)
