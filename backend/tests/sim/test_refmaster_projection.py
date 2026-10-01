import pytest

from prism.sim.project_refmaster import project_refmaster


@pytest.fixture(scope="module")
def rm(universe):
    return project_refmaster(universe)


def test_tables_and_counts(rm, universe):
    assert list(rm) == ["legal_entities", "securities", "products", "accounts", "corporate_actions",
                        "dq_rules", "exceptions", "change_requests", "data_dictionary"]
    assert len(rm["legal_entities"].rows) == len(universe.entities)
    assert len(rm["securities"].rows) == len(universe.securities)
    assert len(rm["exceptions"].rows) > 0 and len(rm["data_dictionary"].rows) == 20


def test_parent_entities_precede_children(rm):
    seen = set()
    for e in rm["legal_entities"].dicts():
        assert e["parent_entity_id"] is None or e["parent_entity_id"] in seen
        seen.add(e["entity_id"])


def test_exceptions_have_valid_state(rm, universe):
    for x in rm["exceptions"].dicts():
        assert x["status"] in {"open", "in_review", "closed"}
        assert (x["closed_at"] is not None) == (x["status"] == "closed")
        assert x["asset_class"] == "n/a" if x["domain"] == "entity" else x["asset_class"] != "n/a"
    open_count = sum(x["status"] != "closed" for x in rm["exceptions"].dicts())
    assert open_count > 0


def test_four_eyes_and_corporate_action_dates(rm):
    for c in rm["change_requests"].dicts():
        if c["status"] == "pending":
            assert c["checker"] is None
        else:
            assert c["checker"] not in (None, c["maker"])
    for ca in rm["corporate_actions"].dicts():
        assert ca["ex_date"] < ca["pay_date"]
