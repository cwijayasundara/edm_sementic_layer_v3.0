from collections import Counter, defaultdict

import pytest

from prism.sim.project_cashrecon import project_cashrecon


@pytest.fixture(scope="module")
def cr(universe):
    return project_cashrecon(universe)


def test_aged_usd_breaks_concentrate_on_story_entity(cr, universe):
    aged = [b for b in cr["breaks"].dicts() if b["ccy"] == "USD" and b["status"] != "closed" and b["age_days"] > 5]
    by_entity = Counter(b["legal_entity_id"] for b in aged)
    top_entity, top_count = by_entity.most_common(1)[0]
    assert top_entity == universe.stories.usd_break_entity_id
    assert top_count / len(aged) >= 0.5


def test_statement_balances_chain(cr):
    by_account = defaultdict(list)
    for s in cr["statements"].dicts():
        by_account[s["account_id"]].append(s)
    for stmts in by_account.values():
        stmts.sort(key=lambda s: s["value_date"])
        for prev, cur in zip(stmts, stmts[1:]):
            assert cur["opening_bal"] == prev["closing_bal"]


def test_every_match_has_both_sides(cr):
    sides = defaultdict(set)
    for m in cr["match_items"].dicts():
        sides[m["match_id"]].add(m["side"])
    assert sides and all(s == {"ledger", "statement"} for s in sides.values())


def test_break_state_is_consistent(cr):
    for b in cr["breaks"].dicts():
        assert (b["root_cause"] is None) == (b["status"] != "closed")
        assert b["amount"] > 0


def test_closed_breaks_and_actions_do_not_postdate_as_of(cr, universe):
    from datetime import UTC, datetime, time

    as_of = universe.cfg.as_of
    end_of_day = datetime.combine(as_of, time(23, 59), tzinfo=UTC)
    for b in cr["breaks"].dicts():
        if b["status"] == "closed":
            assert 1 <= b["age_days"] <= (as_of - b["opened_on"]).days
    assert all(a["ts"] <= end_of_day for a in cr["break_actions"].dicts())
