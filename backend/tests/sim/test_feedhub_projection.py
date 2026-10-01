import pytest

from prism.sim.keys import feed_id
from prism.sim.project_assetrecon import project_assetrecon
from prism.sim.project_cashrecon import project_cashrecon
from prism.sim.project_feedhub import project_feedhub


@pytest.fixture(scope="module")
def fh(universe):
    return project_feedhub(universe)


def test_late_custodian_feeds_degrade_after_credential_change(fh, universe):
    st = universe.stories
    late_days = set(universe.days[st.late_start_idx:])
    story = [r for r in fh["feed_deliveries"].dicts() if r["source_id"] == st.late_custodian_source_id]
    during = [r for r in story if r["business_date"] in late_days]
    before = [r for r in story if r["business_date"] not in late_days]
    assert during and all(r["status"] != "on_time" for r in during)
    assert sum(r["status"] != "on_time" for r in before) / len(before) <= 0.25
    tickets = [r for r in fh["support_tickets"].dicts()
               if r["feed_id"] == feed_id(st.late_custodian_source_id, "positions")
               and r["category"] == "credential_change" and r["status"] == "open"]
    assert len(tickets) == 1


def test_delivery_fields_are_consistent(fh):
    for r in fh["feed_deliveries"].dicts():
        if r["status"] == "missing":
            assert r["received_at"] is None and r["record_count"] is None
        if r["status"] == "failed":
            assert r["error_code"] is not None and r["record_count"] is None
        if r["status"] == "late":
            assert r["latency_min"] > 0


def test_cross_source_keys_resolve(fh, universe):
    delivery_ids = {r["delivery_id"] for r in fh["feed_deliveries"].dicts()}
    ar = project_assetrecon(universe)
    assert {r["delivery_id"] for r in ar["custodian_positions"].dicts()} <= delivery_ids
    source_ids = {r["source_id"] for r in fh["sources"].dicts()}
    assert {r["feed_source_id"] for r in ar["custodians"].dicts()} <= source_ids
    cr = project_cashrecon(universe)
    assert {r["bank_source_id"] for r in cr["private.cash_accounts"].dicts()} <= source_ids


def test_every_source_type_has_a_ticket_and_ids_are_unique(fh):
    tickets = list(fh["support_tickets"].dicts())
    source_types = {r["source_type"] for r in fh["sources"].dicts()}
    assert source_types <= {r["source_type"] for r in tickets}
    ids = [r["ticket_id"] for r in tickets]
    assert len(ids) == len(set(ids))
