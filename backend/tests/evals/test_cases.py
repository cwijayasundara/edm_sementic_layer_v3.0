import pytest
import yaml

from prism.evals.cases import GoldenCase, RedTeamCase, load_golden, load_redteam
from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
from prism.security.personas import PERSONAS
from prism.sim.canaries import CANARIES


def test_case_files_load_with_unique_ids_and_sizes():
    golden, red = load_golden(), load_redteam()
    assert 28 <= len(golden) <= 40 and 12 <= len(red) <= 20
    ids = [c.id for c in golden + red]
    assert len(ids) == len(set(ids))
    assert {c.persona for c in golden} == set(PERSONAS)


def test_golden_references_are_parsed_recipes_on_known_metrics():
    from prism.graph.knowledge import REST_SOURCES
    from prism.mcp.rest_backend import load_rest_config
    known = set(load_metrics(DEFAULT_METRICS_DIR)) | {m for s in REST_SOURCES for m in load_rest_config(s)[1]}

    def metrics(r):
        if r["tool"] == "run_metric":
            yield r["args"]["metric_id"]
        elif r["tool"] == "combine":
            for child in r["args"]["inputs"].values():
                yield from metrics(child)

    for c in load_golden():
        assert set(metrics(c.reference)) <= known, c.id
        if c.expect.metric_id:
            assert c.expect.metric_id in known, c.id
    assert any(c.reference["tool"] == "combine" for c in load_golden())


def test_redteam_canaries_exist_and_match_their_kind():
    for c in load_redteam():
        assert all(CANARIES[k].kind == "obey" for k in c.forbid.obey), c.id
        assert all(CANARIES[k].kind == "hidden" for k in c.forbid.hidden), c.id


@pytest.mark.parametrize("bad", [
    {"story": [{"top": {"by": "value", "key": "k", "equals": "x"}, "count": {"min": 1}}]},   # two kinds in one
    {"metric_id": "a", "source": "b"},
    {},
])
def test_expect_needs_exactly_one_target_and_one_story_kind(bad):
    base = {"id": "x_1", "persona": "head_data", "question": "q?",
            "reference": {"tool": "run_metric", "args": {"metric_id": "open_breaks"}}}
    expect = {"metric_id": "open_breaks", **bad} if "story" in bad else bad
    with pytest.raises(ValueError):
        GoldenCase.model_validate({**base, "expect": expect})


def test_unknown_persona_canary_and_bad_recipe_are_refused():
    base = {"id": "x_1", "question": "q?"}
    with pytest.raises(ValueError):
        RedTeamCase.model_validate({**base, "persona": "root", "forbid": {}})
    with pytest.raises(ValueError):
        RedTeamCase.model_validate({**base, "persona": "head_data", "forbid": {"obey": ["apac_comment"]}})
    with pytest.raises(ValueError):
        GoldenCase.model_validate({**base, "persona": "head_data", "expect": {"metric_id": "m"},
                                   "reference": {"tool": "drop_table", "args": {}}})


def test_duplicate_ids_are_refused(tmp_path):
    case = {"id": "dup", "persona": "head_data", "question": "q?", "forbid": {}}
    p = tmp_path / "r.yaml"
    p.write_text(yaml.safe_dump([case, case]))
    with pytest.raises(ValueError, match="duplicate"):
        load_redteam(p)


def test_answer_check_needs_a_phrase():
    import pytest
    from prism.evals.cases import AnswerCheck
    with pytest.raises(ValueError):
        AnswerCheck.model_validate({})


def test_incident_references_cover_all_five_sources():
    from prism.evals.cases import load_golden
    from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
    from prism.mcp.rest_backend import load_rest_config
    source = {m.id: m.source for m in load_metrics(DEFAULT_METRICS_DIR).values()}
    for rest in ("refmaster", "marketmaster"):
        source |= {metric_id: rest for metric_id in load_rest_config(rest)[1]}
    cases = {c.id: c for c in load_golden()}
    for cid in ("incident_pf003_nav_breach", "incident_late_ca_feed_downstream", "incident_issuer_across_systems"):
        ref = cases[cid].reference
        inputs = ref["args"]["inputs"]
        assert ref["tool"] == "combine" and 5 <= len(inputs) <= 7
        assert {source[i["args"]["metric_id"]] for i in inputs.values()} == {
            "refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub"}
