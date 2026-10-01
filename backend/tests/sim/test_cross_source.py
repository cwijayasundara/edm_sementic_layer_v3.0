"""Facts that two platforms both record must agree (the agent will join across them)."""
import pytest

from prism.sim.keys import STATEMENT_FORMATS, delivery_id, feed_id, statement_format
from prism.sim.project_assetrecon import project_assetrecon
from prism.sim.project_cashrecon import project_cashrecon
from prism.sim.project_feedhub import project_feedhub


@pytest.fixture(scope="module")
def fh(universe):
    return project_feedhub(universe)


@pytest.fixture(scope="module")
def cr(universe):
    return project_cashrecon(universe)


@pytest.fixture(scope="module")
def ar(universe):
    return project_assetrecon(universe)


def test_statement_format_is_deterministic_and_known():
    assert statement_format("SRC023") == statement_format("SRC023")
    assert {statement_format(f"SRC{k:03d}") for k in range(1, 40)} == set(STATEMENT_FORMATS)


def test_cashrecon_statements_match_feedhub_bank_feed_format(fh, cr, universe):
    feed_format = {r["feed_id"]: r["format"] for r in fh["feeds"].dicts()}
    bank_of = {r["account_id"]: r["bank_source_id"] for r in cr["private.cash_accounts"].dicts()}
    statements = cr["statements"].dicts()
    assert statements
    for r in statements:
        assert r["msg_type"] == feed_format[feed_id(bank_of[r["account_id"]], "cash")], r["stmt_id"]
    banks = [s.source_id for s in universe.sources if s.source_type == "bank"]
    assert all(feed_format[feed_id(b, "cash")] in STATEMENT_FORMATS for b in banks)


def test_assetrecon_positions_cite_only_delivered_feedhub_files(fh, ar):
    status = {r["delivery_id"]: r["status"] for r in fh["feed_deliveries"].dicts()}
    cited = {r["delivery_id"] for r in ar["custodian_positions"].dicts()}
    assert cited and cited <= status.keys()
    assert {status[d] for d in cited} <= {"on_time", "late"}


def test_late_portfolios_cite_the_on_time_last_good_delivery(fh, ar, universe):
    st = universe.stories
    status = {r["delivery_id"]: r["status"] for r in fh["feed_deliveries"].dicts()}
    last_good_day = universe.days[st.late_start_idx - 1]
    last_good = delivery_id(st.late_custodian_source_id, "positions", last_good_day)
    assert status[last_good] == "on_time"
    for r in fh["feed_deliveries"].dicts():
        if r["source_id"] == st.late_custodian_source_id and r["business_date"] == last_good_day:
            assert r["status"] == "on_time", r["delivery_id"]
    late_days = set(universe.days[st.late_start_idx:])
    story_rows = [r for r in ar["custodian_positions"].dicts()
                  if r["portfolio_id"] in st.late_portfolio_ids and r["as_of"] in late_days]
    assert story_rows and {r["delivery_id"] for r in story_rows} == {last_good}
