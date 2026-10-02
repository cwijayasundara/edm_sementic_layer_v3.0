"""Planted injection canaries: present, deterministic, region-scoped, and they move no story or balance."""
import pytest

from prism.sim.canaries import CANARIES
from prism.sim.project_cashrecon import project_cashrecon


@pytest.fixture(scope="module")
def cr(universe):
    return project_cashrecon(universe)


def _rows_with(table, column, token):
    return [r for r in table.dicts() if token in (r[column] or "")]


def test_each_canary_is_planted_once_in_its_region(cr):
    for c in CANARIES.values():
        rows = _rows_with(cr[c.table], c.column, c.token)
        assert len(rows) == 1 and rows[0]["region"] == c.region, c.key


def test_canary_tokens_are_unique_and_kinds_match_regions():
    tokens = [c.token for c in CANARIES.values()]
    assert len(set(tokens)) == len(tokens)
    assert {c.key for c in CANARIES.values() if c.kind == "obey"} == {"obey_comment", "obey_narrative"}
    assert all(c.region == "EMEA" for c in CANARIES.values() if c.kind == "obey")
    assert all(c.region == "APAC" for c in CANARIES.values() if c.kind == "hidden")


def test_canary_comments_sit_on_open_non_story_breaks(cr, universe):
    breaks = {b["break_id"]: b for b in cr["breaks"].dicts()}
    story_accounts = set(universe.stories.usd_break_account_ids)
    for key in ("obey_comment", "apac_comment"):
        (row,) = _rows_with(cr["break_actions"], "comment", CANARIES[key].token)
        b = breaks[row["break_id"]]
        assert b["status"] != "closed" and b["account_id"] not in story_accounts and row["action"] == "commented"


def test_probe_sql_counts_the_token():
    c = CANARIES["obey_comment"]
    assert c.probe_sql == f"SELECT count(*) AS n FROM break_actions WHERE comment LIKE '%{c.token}%'"
