"""M9 cross-system incident (spec 2026-10-03): ids, every link of the chain, noise, determinism, isolation."""
import copy

import pytest

from prism.sim.incident import (CA_FEED_TYPE, FAILED_FROM_END, NOISE_CORPORATE_ACTIONS, SPLIT_FROM_END, _next_id,
                                apply_incident)
from prism.sim.keys import fund_entity_id
from prism.sim.model import TableData
from prism.sim.seed import PROJECTORS
from prism.sim.universe import (INCIDENT_ANCHOR, INCIDENT_MIN_HALF_WEIGHT_BPS, INCIDENT_SPLIT_FROM_END, SimConfig,
                                build_universe)


def _holders(u) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for pid in sorted(u.holdings):
        for sid, qty in u.holdings[pid]:
            out.setdefault(sid, {})[pid] = qty
    return out


def _eligible(u, sid: str, holders: dict[str, dict[str, float]]) -> bool:
    s, i = u.security_by_id[sid], len(u.days) - INCIDENT_SPLIT_FROM_END
    hs = holders.get(sid, {})
    nav = {p: sum(q * u.golden(x, i) for x, q in u.holdings[p]) for p in hs}
    return (s.asset_class == "Equity" and s.status == "active" and sid not in u.stories.stale_security_ids
            and INCIDENT_ANCHOR in hs and not set(u.stories.late_portfolio_ids) & set(hs)
            and all(q * u.prices[sid][i] / 2 / nav[p] * 1e4 >= INCIDENT_MIN_HALF_WEIGHT_BPS for p, q in hs.items()))


def test_incident_ids_follow_the_rule(universe):
    inc, holders = universe.stories.incident, _holders(universe)
    assert _eligible(universe, inc.security_id, holders)
    assert inc.holder_ids == tuple(sorted(holders[inc.security_id]))
    best = max(len(holders[s.security_id]) for s in universe.securities if _eligible(universe, s.security_id, holders))
    assert len(inc.holder_ids) == best                     # most holders; ties go to the lowest id
    assert inc.issuer_entity_id == universe.security_by_id[inc.security_id].issuer_entity_id
    anchor = next(p for p in universe.portfolios if p.portfolio_id == INCIDENT_ANCHOR)
    assert inc.anchor_portfolio_id == INCIDENT_ANCHOR and inc.source_id == anchor.custodian_source_id
    assert inc.source_id != universe.stories.late_custodian_source_id


@pytest.mark.slow
def test_full_profile_incident_literals():
    inc = build_universe(SimConfig()).stories.incident
    assert (inc.security_id, inc.issuer_entity_id, inc.source_id) == ("SEC001982", "LE00364", "SRC005")
    assert inc.holder_ids == ("PF003", "PF019", "PF021", "PF025")      # the anchor and at least two others


@pytest.fixture(scope="module")
def before(universe):
    return {db: project(universe) for db, project in PROJECTORS.items()}


@pytest.fixture(scope="module")
def after(universe, before):
    tables = copy.deepcopy(before)
    apply_incident(universe, tables)
    return tables


def test_next_id_continues_past_the_largest_id():
    t = TableData(("id",), [("CA000002",), ("CA000010",), ("X9",)])
    nxt = _next_id(t, "id", "CA", 6)
    assert [nxt(), nxt()] == ["CA000011", "CA000012"]


def test_fund_entities_are_appended_one_per_portfolio(after, before, universe):
    rows = after["refmaster"]["legal_entities"].dicts()[len(before["refmaster"]["legal_entities"].rows):]
    assert [r["entity_id"] for r in rows] == [fund_entity_id(universe.cfg.n_entities, k)
                                              for k in range(len(universe.portfolios))]
    assert {r["entity_type"] for r in rows} == {"fund"} and all(r["status"] == "active" for r in rows)
    leis = [r["lei"] for r in after["refmaster"]["legal_entities"].dicts()]
    assert len(leis) == len(set(leis))


def test_refmaster_misses_the_split(after, universe):
    inc, n = universe.stories.incident, len(universe.days)
    pending = [r for r in after["refmaster"]["corporate_actions"].dicts() if r["status"] == "pending"]
    assert len(pending) == 1
    ca = pending[0]
    assert (ca["security_id"], ca["event_type"], ca["ratio"], ca["issuer_entity_id"]) == \
        (inc.security_id, "split", 2.0, inc.issuer_entity_id)
    assert ca["ex_date"] == universe.days[n - SPLIT_FROM_END]
    exc = [r for r in after["refmaster"]["exceptions"].dicts() if r["record_ref"] == inc.security_id
           and r["rule_id"] == "R010" and r["status"] == "open"]
    assert len(exc) == 1 and exc[0]["domain"] == "corporate_action"
    crs = [r for r in after["refmaster"]["change_requests"].dicts() if r["record_ref"] == inc.security_id
           and r["status"] == "pending" and r["domain"] == "corporate_action"]
    assert len(crs) == 1 and crs[0]["checker"] is None


def test_noise_corporate_actions_are_processed_on_held_equities(after, before, universe):
    inc, n = universe.stories.incident, len(universe.days)
    new = after["refmaster"]["corporate_actions"].dicts()[len(before["refmaster"]["corporate_actions"].rows):]
    noise = [r for r in new if r["security_id"] != inc.security_id]
    held = {sid for h in universe.holdings.values() for sid, _ in h}
    assert len(noise) == NOISE_CORPORATE_ACTIONS
    window = set(universe.days[n - universe.cfg.position_window_days:])
    for r in noise:
        assert r["status"] == "processed" and r["event_type"] in ("dividend", "name_change")
        assert r["security_id"] in held and r["ex_date"] in window and r["ex_date"] <= r["pay_date"] <= universe.cfg.as_of


def test_custodian_corporate_actions_feed_fails_then_is_late(after, universe):
    inc, n = universe.stories.incident, len(universe.days)
    feeds = [r for r in after["feedhub"]["feeds"].dicts() if r["source_id"] == inc.source_id
             and r["data_type"] == CA_FEED_TYPE]
    assert len(feeds) == 1
    dl = {r["business_date"]: r for r in after["feedhub"]["feed_deliveries"].dicts()
          if r["feed_id"] == feeds[0]["feed_id"]}
    assert len(dl) == n and {r["feed_type"] for r in dl.values()} == {CA_FEED_TYPE}
    assert dl[universe.days[n - FAILED_FROM_END]]["status"] == "failed"
    assert dl[universe.days[n - SPLIT_FROM_END]]["status"] == "late"
    others = {d: r["status"] for d, r in dl.items()
              if d not in (universe.days[n - FAILED_FROM_END], universe.days[n - SPLIT_FROM_END])}
    assert set(others.values()) == {"on_time"}
