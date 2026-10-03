"""M9 cross-system incident (spec 2026-10-03): ids, every link of the chain, noise, determinism, isolation."""
import pytest

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
