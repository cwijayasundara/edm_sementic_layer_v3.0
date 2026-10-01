import pytest

from prism.sim.project_marketmaster import project_marketmaster
from prism.sim.universe import VENDOR_COVERAGE, SimConfig, build_universe


@pytest.fixture(scope="module")
def mm(universe):
    return project_marketmaster(universe)


def _conflict_story(mm, u):
    """'This week' = the last 5 business days ending at as-of; 'previous week' = the 5 before."""
    this_week, prev_week = set(u.days[-5:]), set(u.days[-10:-5])
    conflicts = [r for r in mm["price_suspects"].dicts() if r["kind"] == "conflict"]
    now = [r for r in conflicts if r["price_date"] in this_week]
    before = [r for r in conflicts if r["price_date"] in prev_week]
    story = [r for r in now if (r["vendor_id"], r["asset_class"]) == ("V_A", "Corp bond")]
    return now, before, story


def _assert_conflict_story(mm, u):
    now, before, story = _conflict_story(mm, u)
    st = u.stories
    assert len(story) / len(now) >= 0.5
    assert 0.05 <= len(now) / len(before) - 1 <= 0.45
    assert 0.15 <= len(now) / len(before) - 1 <= 0.25
    # Background conflicts replay week to week, so the whole delta is the story cell's.
    assert len(now) - len(before) == 5 * (st.conflict_daily_this_week - st.conflict_daily_prev_week)


def test_vendor_a_drives_corp_bond_conflicts_this_week(mm, universe):
    _assert_conflict_story(mm, universe)


@pytest.mark.slow
@pytest.mark.parametrize("seed", [42, 7, 13])
def test_vendor_a_story_holds_on_the_full_profile(seed):
    u = build_universe(SimConfig(seed=seed))
    _assert_conflict_story(project_marketmaster(u), u)


def test_stale_prices_are_carried_forward_and_flagged(mm, universe):
    st = universe.stories
    stale_days = set(universe.days[-st.stale_days:])
    golden = [r for r in mm["golden_prices"].dicts() if r["security_id"] in st.stale_security_ids]
    assert all(r["rule"] == "carry_forward" for r in golden if r["price_date"] in stale_days)
    flagged = {(r["security_id"], r["price_date"]) for r in mm["price_suspects"].dicts()
               if r["kind"] == "stale" and r["status"] != "resolved"}
    assert flagged == {(sid, d) for sid in st.stale_security_ids for d in stale_days}


def test_vendor_quotes_only_from_covering_vendors_within_window(mm, universe):
    window = set(universe.days[-universe.cfg.vendor_window_days:])
    for r in mm["vendor_prices"].dicts():
        assert r["asset_class"] in VENDOR_COVERAGE[r["vendor_id"]]
        assert r["price_date"] in window


def test_dq_stage_counts_are_consistent(mm):
    by_key: dict = {}
    for r in mm["dq_stage_metrics"].dicts():
        by_key.setdefault((r["business_date"], r["domain"]), {})[r["stage"]] = r["count"]
    for stages in by_key.values():
        assert stages["validated"] + stages["suspect"] == stages["acquired"]
        assert stages["approved"] >= stages["validated"]
