import pytest

from prism.sim.project_assetrecon import project_assetrecon


@pytest.fixture(scope="module")
def ar(universe):
    return project_assetrecon(universe)


def test_late_custodian_causes_position_exceptions(ar, universe):
    st, n = universe.stories, len(universe.days)
    window_start = n - universe.cfg.position_window_days
    late_days = set(universe.days[st.late_start_idx:])
    before_days = set(universe.days[window_start:st.late_start_idx])
    exc = [r for r in ar["recon_exceptions"].dicts() if r["portfolio_id"] in st.late_portfolio_ids]
    late_rate = sum(r["business_date"] in late_days for r in exc) / len(late_days)
    before_rate = max(1, sum(r["business_date"] in before_days for r in exc)) / len(before_days)
    assert late_rate >= 3 * before_rate
    assert all(r["cause_code"] == "stale_custodian_data" for r in exc if r["business_date"] in late_days)


def test_stale_prices_create_nav_breaks_only_for_story_funds(ar, universe):
    st = universe.stories
    stale_days = set(universe.days[-st.stale_days:])
    for r in ar["nav_checks"].dicts():
        if r["portfolio_id"] in st.nav_portfolio_ids and r["nav_date"] in stale_days:
            assert abs(r["diff_bps"]) > 5
        else:
            assert abs(r["diff_bps"]) < 3


def test_ibor_and_abor_books_are_both_present(ar):
    books = [r["book"] for r in ar["internal_positions"].dicts()]
    assert books.count("IBOR") == books.count("ABOR") > 0


def test_late_custodian_positions_point_at_last_good_delivery(ar, universe):
    st = universe.stories
    last_good = universe.days[st.late_start_idx - 1].strftime("%Y%m%d")
    late_days = set(universe.days[st.late_start_idx:])
    for r in ar["custodian_positions"].dicts():
        if r["portfolio_id"] in st.late_portfolio_ids and r["as_of"] in late_days:
            assert r["delivery_id"].endswith(last_good)
