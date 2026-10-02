"""Live: a saved dashboard re-runs with the caller's own entitlements (needs scripts/start_backend.sh)."""
import uuid

import pytest
from fastapi.testclient import TestClient

from prism.agent.api import create_app
from prism.agent.dashboards import MemoryDashboardStore
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.security.personas import claims_for
from prism.security.tokens import mint
from tests.agent.live_support import SETTINGS, live_gateway_factory, require_live

pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def live():
    require_live()


@pytest.fixture
def client():
    app = create_app(settings=SETTINGS, gateway_factory=live_gateway_factory, dashboard_store=MemoryDashboardStore())
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def auth():
    sub = f"agent-live-{uuid.uuid4().hex[:10]}"

    def headers(persona: str) -> dict:
        claims = {**claims_for(persona, ttl_s=600), "sub": sub}
        token = mint(claims, GATEWAY_AUDIENCE, SETTINGS.jwt_secret.get_secret_value(), ttl_s=600)
        return {"Authorization": f"Bearer {token}"}
    return headers


def test_cash_ops_dashboard_runs_its_own_rows_and_is_refused_refmaster(client, auth):
    items = [
        {"widget": {"id": "w1", "type": "table", "title": "Open breaks", "handle": "", "encoding": {}},
         "recipe": {"tool": "run_metric", "args": {"metric_id": "open_breaks", "dimensions": ["region"]}}},
        {"widget": {"id": "w2", "type": "table", "title": "Securities", "handle": "", "encoding": {}},
         "recipe": {"tool": "query_source", "args": {"source": "refmaster",
                                                     "request": {"sql": "SELECT 1 AS one"}}}},
    ]
    dash_id = client.post("/dashboards", json={"title": "agent-live-dash", "items": items},
                          headers=auth("cash_ops_emea")).json()["id"]
    try:
        out = client.post(f"/dashboards/{dash_id}/run", headers=auth("cash_ops_emea")).json()["widgets"]
        assert [w["status"] for w in out] == ["ok", "not_permitted"]
        rows = client.get(f"/results/{out[0]['widget']['handle']}?limit=200", headers=auth("cash_ops_emea")).json()
        region = rows["columns"].index("region")
        assert {r[region] for r in rows["rows"]} == {"EMEA"}
    finally:
        client.delete(f"/dashboards/{dash_id}", headers=auth("cash_ops_emea"))
