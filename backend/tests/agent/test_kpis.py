import asyncio

import pytest

from prism.agent import kpis
from prism.agent.auth import UserContext
from prism.agent.gateway_client import GatewayError
from prism.agent.kpis import KPIS, KpiService, load_kpis
from tests.agent.fakes import FakeGateway, summary

USER = UserContext(sub="u", roles=("head_data",), metrics_only=True, token="t")
ROWS = {"handle": "r_aaaaaaaaaaaa", "columns": ["value"], "offset": 0, "row_count": 1, "rows": [[7]]}


def run(coro):
    return asyncio.run(coro)


def test_shipped_kpis_are_valid():
    assert set(KPIS) == {"steward", "cash_ops_emea", "invest_ops_growth", "bi_analyst", "head_data"}
    assert all(1 <= len(v) <= 4 for v in KPIS.values())
    assert KPIS["cash_ops_emea"][2].unit == "%" and KPIS["cash_ops_emea"][2].scale == 100
    assert KPIS["invest_ops_growth"][2].unit == "%" and KPIS["invest_ops_growth"][2].scale == 100
    amount = KPIS["cash_ops_emea"][3]
    assert (amount.unit, amount.dimensions, dict(amount.filters)) == ("USD", ("ccy",), {"ccy": "USD"})
    assert "USD" in amount.label
    # every percentage tile is scaled; nothing else is
    assert all((k.unit == "%") == (k.scale == 100) for v in KPIS.values() for k in v)


def test_loader_rejects_unknown_keys_and_too_many_tiles(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("a: [{metric_id: m, label: L, colour: red}]")
    with pytest.raises(ValueError):
        load_kpis(p)
    p.write_text("a: [" + ", ".join("{metric_id: m%d, label: L}" % i for i in range(5)) + "]")
    with pytest.raises(ValueError):
        load_kpis(p)


def test_tile_value_is_last_cell_of_first_row():
    gw = FakeGateway({"run_metric": summary(), "get_rows": {**ROWS, "rows": [[1, 2, 9], [4]]}})
    tiles = run(KpiService().tiles(USER, gw))
    assert tiles[0]["status"] == "ok" and tiles[0]["value"] == 9
    assert gw.calls[0] == ("run_metric", {"metric_id": "late_feeds", "dimensions": [], "limit": 1})
    assert gw.calls[1][0] == "get_rows" and gw.calls[1][1]["limit"] == 1


def test_ok_tile_names_the_source_that_answered_it():
    gw = FakeGateway({"run_metric": summary(source="feedhub"), "get_rows": ROWS})
    assert run(KpiService().tiles(USER, gw))[0]["source"] == "feedhub"


def test_missing_row_or_error_is_unavailable_without_message():
    gw = FakeGateway({"run_metric": summary(), "get_rows": {**ROWS, "rows": []}})
    assert {t["status"] for t in run(KpiService().tiles(USER, gw))} == {"unavailable"}
    gw = FakeGateway({"run_metric": GatewayError("source_unavailable", "pg password=hunter2")})
    tiles = run(KpiService().tiles(USER, gw))
    assert all(set(t) == {"label", "metric_id", "unit", "status"} for t in tiles)
    assert "hunter2" not in repr(tiles)


def test_cache_expires_after_ttl():
    now = [0.0]
    gw = FakeGateway({"run_metric": summary(), "get_rows": ROWS})
    svc = KpiService(ttl_s=60, clock=lambda: now[0])

    async def go():
        await svc.tiles(USER, gw)
        n = len(gw.calls)
        await svc.tiles(USER, gw)
        assert len(gw.calls) == n
        now[0] = 61
        await svc.tiles(USER, gw)
        assert len(gw.calls) > n

    run(go())


def test_unknown_role_gets_no_tiles():
    u = UserContext(sub="u", roles=("mystery",), metrics_only=True, token="t")
    assert run(KpiService().tiles(u, FakeGateway({}))) == []


def test_module_exports():
    assert kpis.KpiDef("m", "L").dimensions == ()


def test_loader_validates_filters_and_scale(tmp_path):
    p = tmp_path / "k.yaml"
    p.write_text("a: [{metric_id: m, label: L, unit: '%', scale: 100, filters: {ccy: USD, r: [A, B]}}]")
    d = load_kpis(p)["a"][0]
    assert d.scale == 100 and dict(d.filters) == {"ccy": "USD", "r": ["A", "B"]}
    for bad in ("filters: [x]", "filters: {a: {b: c}}", "filters: {a: []}", "scale: 0", "scale: x", "scale: true"):
        p.write_text("a: [{metric_id: m, label: L, %s}]" % bad)
        with pytest.raises(ValueError):
            load_kpis(p)


def test_scale_and_filters_are_applied_to_the_tile(monkeypatch):
    defs = [kpis.KpiDef("auto_match_rate", "Auto-match", "%", scale=100),
            kpis.KpiDef("open_break_amount", "Amount (USD)", "USD", ("ccy",), (("ccy", "USD"),))]
    monkeypatch.setitem(KPIS, "head_data", defs)
    gw = FakeGateway({"run_metric": summary(), "get_rows": [{**ROWS, "rows": [[0.87]]}, {**ROWS, "rows": [["USD", 5]]}]})
    tiles = run(KpiService().tiles(USER, gw))
    assert [t["value"] for t in tiles] == [87.0, 5] and [t["unit"] for t in tiles] == ["%", "USD"]
    calls = [c[1] for c in gw.calls if c[0] == "run_metric"]
    assert calls[0] == {"metric_id": "auto_match_rate", "dimensions": [], "limit": 1}   # no filters key when unset
    assert calls[1]["filters"] == {"ccy": "USD"} and calls[1]["dimensions"] == ["ccy"]


def test_scaled_tile_with_a_non_number_is_unavailable(monkeypatch):
    monkeypatch.setitem(KPIS, "head_data", [kpis.KpiDef("m", "L", "%", scale=100)])
    gw = FakeGateway({"run_metric": summary(), "get_rows": {**ROWS, "rows": [["n/a"]]}})
    assert run(KpiService().tiles(USER, gw))[0]["status"] == "unavailable"


def test_cache_is_keyed_on_roles_and_the_enforced_scope_digest():
    a = UserContext("u", ("head_data",), True, "t", scope_digest="aaaa")
    b = UserContext("u", ("head_data",), True, "t", scope_digest="bbbb")
    gw = FakeGateway({"run_metric": summary(), "get_rows": ROWS})
    svc = KpiService()

    async def go():
        await svc.tiles(a, gw)
        n = len(gw.calls)
        await svc.tiles(a, gw)
        assert len(gw.calls) == n and svc.cached(b) is None
        await svc.tiles(b, gw)
        assert len(gw.calls) > n

    run(go())
    assert len(svc._cache) == 2
