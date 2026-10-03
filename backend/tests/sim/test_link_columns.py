"""Draw-free link columns (M9 spec §2.3): every hop of the incident chain has a column to join on."""
import pytest

from prism.sim.keys import fund_cash_account_id, fund_entity_id
from prism.sim.project_assetrecon import project_assetrecon
from prism.sim.project_cashrecon import project_cashrecon
from prism.sim.project_feedhub import project_feedhub
from prism.sim.project_refmaster import project_refmaster


@pytest.fixture(scope="module")
def rm(universe):
    return project_refmaster(universe)


def test_fund_ids_follow_the_universe_counts():
    assert fund_entity_id(400, 0) == "LE00401" and fund_entity_id(400, 2) == "LE00403"
    assert fund_cash_account_id(60, 2) == "CA0063"


def test_entity_type_marks_sovereigns(rm, universe):
    types = {r["entity_id"]: r["entity_type"] for r in rm["legal_entities"].dicts()}
    for e in universe.entities:
        assert types[e.entity_id] == ("sovereign" if e.sector == "Sovereign" else "corporate")


def test_corporate_actions_carry_the_issuer(rm, universe):
    for r in rm["corporate_actions"].dicts():
        assert r["issuer_entity_id"] == universe.security_by_id[r["security_id"]].issuer_entity_id


def test_portfolios_carry_fund_entity_and_custodian_source(universe):
    rows = project_assetrecon(universe)["portfolios"].dicts()
    for k, (r, p) in enumerate(zip(rows, universe.portfolios, strict=True)):
        assert r["fund_entity_id"] == fund_entity_id(universe.cfg.n_entities, k)
        assert r["custodian_source_id"] == p.custodian_source_id


def test_breaks_carry_the_bank_source(universe):
    bank = {a.account_id: a.bank_source_id for a in universe.cash_accounts}
    for r in project_cashrecon(universe)["breaks"].dicts():
        assert r["bank_source_id"] == bank[r["account_id"]]


def test_deliveries_carry_their_feed_type(universe):
    fh = project_feedhub(universe)
    data_type = {r["feed_id"]: r["data_type"] for r in fh["feeds"].dicts()}
    for r in fh["feed_deliveries"].dicts():
        assert r["feed_type"] == data_type[r["feed_id"]]
