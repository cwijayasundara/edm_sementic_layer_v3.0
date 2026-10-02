from prism.agent.dashboards import MemoryDashboardStore
from prism.agent.gateway_client import GatewayError
from tests.agent.fakes import FakeGateway, summary
from tests.agent.test_api import app_with, token

METRIC = {"tool": "run_metric", "args": {"metric_id": "open_breaks", "dimensions": ["region"]}}
QUERY = {"tool": "query_source", "args": {"source": "refmaster", "request": {"sql": "SELECT 1"}}}
BAR = {"id": "w1", "type": "bar", "title": "Open breaks", "handle": "r_aaaaaaaaaaaa",
       "encoding": {"x": "region", "y": "value"}}


def h(persona="head_data"):
    return {"Authorization": f"Bearer {token(persona)}"}


def client(gw, store=None):
    return app_with(gw, dashboard_store=store or MemoryDashboardStore())


def save(c, items, title="Board", persona="head_data"):
    return c.post("/dashboards", json={"title": title, "items": items}, headers=h(persona))


def test_dashboards_require_a_token():
    c = client(FakeGateway({}))
    assert c.get("/dashboards").status_code == 401
    assert c.post("/dashboards", json={}).status_code == 401
    assert c.delete("/dashboards/x").status_code == 401
    assert c.post("/dashboards/x/run").status_code == 401


def test_save_list_and_delete_are_per_caller():
    c = client(FakeGateway({}))
    r = save(c, [{"widget": BAR, "recipe": METRIC}])
    assert r.status_code == 201
    dash_id = r.json()["id"]
    listed = c.get("/dashboards", headers=h()).json()
    assert [(d["id"], d["title"], d["widget_count"]) for d in listed["dashboards"]] == [(dash_id, "Board", 1)]
    assert c.get("/dashboards", headers=h("cash_ops_emea")).json() == {"dashboards": []}
    assert c.delete(f"/dashboards/{dash_id}", headers=h("cash_ops_emea")).status_code == 404
    assert c.post(f"/dashboards/{dash_id}/run", headers=h("cash_ops_emea")).status_code == 404
    assert c.delete(f"/dashboards/{dash_id}", headers=h()).status_code == 204
    assert c.delete(f"/dashboards/{dash_id}", headers=h()).status_code == 404


def test_save_rejects_bad_bodies_and_the_21st_dashboard():
    c = client(FakeGateway({}))
    assert save(c, []).status_code == 422
    assert save(c, [{"widget": BAR, "recipe": {"tool": "get_rows", "args": {}}}]).status_code == 422
    for i in range(20):
        assert save(c, [{"widget": BAR, "recipe": METRIC}], title=f"d{i}").status_code == 201
    r = save(c, [{"widget": BAR, "recipe": METRIC}], title="over")
    assert r.status_code == 409 and r.json() == {"detail": "dashboard limit reached"}


def test_run_replays_with_fresh_handles_and_per_widget_status():
    gw = FakeGateway({"run_metric": summary("r_111111111111", metric_id="open_breaks"),
                      "query_source": GatewayError("not_permitted", "refmaster is not yours")})
    c = client(gw)
    table = {**BAR, "id": "w2", "type": "table", "title": "Securities", "encoding": {}}
    dash_id = save(c, [{"widget": BAR, "recipe": METRIC}, {"widget": table, "recipe": QUERY}]).json()["id"]
    r = c.post(f"/dashboards/{dash_id}/run", headers=h())
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Board"
    ok, denied = body["widgets"]
    assert ok["status"] == "ok" and ok["widget"]["handle"] == "r_111111111111"
    assert ok["handle_info"]["columns"] == ["region", "value"] and ok["handle_info"]["recipe"]["tool"] == "run_metric"
    empty = {"x": None, "y": None, "series": None, "value": None, "unit": None}
    assert denied == {"widget": {**table, "handle": "", "encoding": empty}, "status": "not_permitted"}
    assert "refmaster is not yours" not in r.text


def test_run_turns_a_widget_whose_column_vanished_into_a_table():
    gw = FakeGateway({"run_metric": summary("r_111111111111", columns=("desk", "value"))})
    c = client(gw)
    dash_id = save(c, [{"widget": BAR, "recipe": METRIC}]).json()["id"]
    w = c.post(f"/dashboards/{dash_id}/run", headers=h()).json()["widgets"][0]
    assert w["status"] == "ok" and w["widget"]["type"] == "table" and w["widget"]["encoding"] == {
        "x": None, "y": None, "series": None, "value": None, "unit": None}


def test_run_is_502_when_every_widget_is_unavailable():
    gw = FakeGateway({"run_metric": GatewayError("gateway_unavailable", "down")})
    c = client(gw)
    dash_id = save(c, [{"widget": BAR, "recipe": METRIC}]).json()["id"]
    r = c.post(f"/dashboards/{dash_id}/run", headers=h())
    assert r.status_code == 502 and r.json() == {"detail": "data service unavailable"}


def test_store_failure_is_503_without_detail():
    class Broken(MemoryDashboardStore):
        async def list(self, sub):
            raise RuntimeError("connection refused to 10.0.0.1")
    r = client(FakeGateway({}), Broken()).get("/dashboards", headers=h())
    assert r.status_code == 503 and r.json() == {"detail": "dashboards unavailable"}
