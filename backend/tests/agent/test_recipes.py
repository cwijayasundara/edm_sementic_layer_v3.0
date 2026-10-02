import pytest

from prism.agent.gateway_client import GatewayError
from prism.agent.recipes import ReplayError, Replayer, parse_recipe, status_for
from tests.agent.fakes import FakeGateway, summary

METRIC = {"tool": "run_metric", "args": {"metric_id": "open_breaks", "dimensions": ["region"], "filters": {},
                                         "limit": None}}
QUERY = {"tool": "query_source", "args": {"source": "cashrecon", "request": {"sql": "SELECT 1"}}}


def combine(inputs, sql="SELECT * FROM a"):
    return {"tool": "combine", "args": {"sql": sql, "inputs": inputs}}


def test_parse_recipe_fills_defaults_and_rejects_unknown_keys():
    assert parse_recipe({"tool": "run_metric", "args": {"metric_id": "m"}}) == {
        "tool": "run_metric", "args": {"metric_id": "m", "dimensions": [], "filters": {}, "limit": None}}
    for bad in (None, "x", {"tool": "get_rows", "args": {}}, {"tool": "run_metric", "args": {"metric_id": "m",
                "sub": "admin"}}, {"tool": "run_metric", "args": {"metric_id": ""}},
                {"tool": "query_source", "args": {"source": "s", "request": {}}, "extra": 1}):
        with pytest.raises(ValueError):
            parse_recipe(bad)


def test_parse_recipe_bounds_depth_and_nodes():
    deep = combine({"a": combine({"b": combine({"c": combine({"d": METRIC})})})})
    with pytest.raises(ValueError, match="depth"):
        parse_recipe(deep)
    wide = combine({f"t{i}": METRIC for i in range(8)})   # 9 nodes
    with pytest.raises(ValueError, match="nodes"):
        parse_recipe(wide)
    assert parse_recipe(combine({"a": combine({"b": METRIC})}))["tool"] == "combine"   # depth 3 is fine


async def test_replay_metric_passes_args_and_returns_the_summary():
    gw = FakeGateway({"run_metric": summary("r_111111111111", metric_id="open_breaks")})
    s = await Replayer(gw).run(parse_recipe(METRIC))
    assert s["handle"] == "r_111111111111" and s["metric_id"] == "open_breaks"
    assert gw.calls == [("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"], "filters": {}})]


async def test_replay_combine_runs_inputs_first_and_maps_fresh_handles():
    gw = FakeGateway({"run_metric": summary("r_111111111111"), "query_source": summary("r_222222222222"),
                      "combine": summary("r_333333333333")})
    s = await Replayer(gw).run(parse_recipe(combine({"a": METRIC, "b": QUERY}, "SELECT * FROM a JOIN b USING (x)")))
    assert s["handle"] == "r_333333333333"
    assert [c[0] for c in gw.calls] == ["run_metric", "query_source", "combine"]
    assert gw.calls[-1][1] == {"sql": "SELECT * FROM a JOIN b USING (x)",
                               "handles": {"a": "r_111111111111", "b": "r_222222222222"}}


async def test_identical_sub_recipes_run_once_per_replayer():
    gw = FakeGateway({"run_metric": summary("r_111111111111")})
    r = Replayer(gw)
    await r.run(parse_recipe(METRIC))
    await r.run(parse_recipe(METRIC))
    assert len(gw.calls) == 1


@pytest.mark.parametrize("code,status", [("not_permitted", "not_permitted"), ("metrics_only", "not_permitted"),
                                         ("gateway_unavailable", "unavailable"), ("rate_limited", "unavailable"),
                                         ("unknown_metric", "invalid"), ("invalid_sql", "invalid"),
                                         ("tool_error", "unavailable")])
async def test_gateway_errors_map_to_a_status_without_detail(code, status):
    gw = FakeGateway({"run_metric": GatewayError(code, "secret detail")})
    with pytest.raises(ReplayError) as info:
        await Replayer(gw).run(parse_recipe(METRIC))
    assert info.value.status == status and "secret" not in str(info.value)
    assert status_for(GatewayError(code, "x")) == status


def _metric(**args):
    return {"tool": "run_metric", "args": {"metric_id": "m", **args}}


@pytest.mark.parametrize("ok,bad", [
    (_metric(filters={f"f{i}": 1 for i in range(20)}), _metric(filters={f"f{i}": 1 for i in range(21)})),
    (_metric(limit=1000), _metric(limit=1001)),
    (_metric(dimensions=[f"d{i}" for i in range(20)]), _metric(dimensions=[f"d{i}" for i in range(21)])),
    ({"tool": "query_source", "args": {"source": "s" * 64, "request": {}}},
     {"tool": "query_source", "args": {"source": "s" * 65, "request": {}}}),
])
def test_parse_recipe_bounds_mirror_the_gateway(ok, bad):
    parse_recipe(ok)
    with pytest.raises(ValueError):
        parse_recipe(bad)
