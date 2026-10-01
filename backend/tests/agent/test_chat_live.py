"""Live planted stories through the real FastAPI app: httpx ASGITransport -> create_app -> AgentService (scripted model,
no API key) -> GatewayClient -> the running gateway and source MCP servers. Also pins that every configured KPI tile
is answerable for its persona. Skipped unless the full stack is up (`make test-live`)."""
import json

import httpx
import psycopg
import pytest

from prism.agent.api import create_app
from prism.agent.kpis import KPIS
from prism.agent.model import ScriptedModelClient, reply_text, reply_tools
from prism.agent.telemetry import AgentRunWriter
from prism.config import APP_DB
from prism.gateway.audit import open_audit_pool
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.security.personas import claims_for
from prism.security.tokens import mint
from tests.agent.live_support import (SETTINGS, last_handle, live_gateway_factory, live_service, require_live,
                                      unique_question, user_for)

pytestmark = pytest.mark.live
AS_OF = SETTINGS.as_of.isoformat()


@pytest.fixture(scope="module", autouse=True)
def live():
    require_live()


def sse(text: str) -> list[dict]:
    return [json.loads(frame.split("\ndata: ", 1)[1]) for frame in text.strip().split("\n\n")]


def visualize(intent: str):
    return lambda req: reply_tools(("visualize", {"handles": [last_handle(req)], "intent": intent}))


def spec(title: str, narrative: str):
    return lambda req: reply_tools(("emit_dashboard_spec", {"widgets": [{
        "id": "w1", "type": "table", "title": title, "handle": last_handle(req), "encoding": {}}],
        "narrative": narrative}))


def agent_run_rows(sub: str) -> list[tuple]:
    with psycopg.connect(SETTINGS.dsn(APP_DB, admin=True), connect_timeout=3) as conn:
        return conn.execute("SELECT run_id, path, llm_turns, status FROM app.agent_runs WHERE sub = %s", (sub,)).fetchall()


async def drive(persona: str, script: list, question: str):
    """POST /chat on the app; returns (events, model, user, the app's client for follow-up calls, cleanup)."""
    user = user_for(persona)
    model = ScriptedModelClient(script)
    pool = await open_audit_pool(SETTINGS)
    writer = AgentRunWriter(pool, hmac_key=SETTINGS.audit_hmac_key.get_secret_value())
    app = create_app(settings=SETTINGS, service=live_service(model, run_writer=writer),
                     gateway_factory=live_gateway_factory)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 50000)),
                               base_url="http://localhost", headers={"Authorization": f"Bearer {user.token}"})
    try:
        async with client:
            r = await client.post("/chat", json={"question": question})
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
            events = sse(r.text)
            # the widget's handle and the combine result are readable through the same app, as the same caller
            handle = next(e["widget"]["handle"] for e in events if e["type"] == "widget")
            page = await client.get(f"/results/{handle}", params={"limit": 5})
            assert page.status_code == 200
            return events, model, user, page.json()
    finally:
        await pool.close()


async def test_story_aged_usd_breaks_through_chat():
    script = [
        reply_tools(("search_context", {"question": "aged USD breaks by legal entity"})),
        reply_tools(("run_metric", {"metric_id": "aged_open_breaks", "dimensions": ["legal_entity_id", "ccy"]})),
        lambda req: reply_tools(("combine", {
            "sql": "SELECT legal_entity_id, sum(value) AS breaks FROM b WHERE ccy = 'USD' "
                   "GROUP BY legal_entity_id ORDER BY breaks DESC LIMIT 1", "handles": {"b": last_handle(req)}})),
        visualize("table of the legal entity with the most aged USD breaks"),
        spec("Aged USD breaks", "LE00016 holds 64 of the aged USD breaks."),
        reply_text("LE00016 has the most aged USD breaks (64)."),
    ]
    events, model, user, page = await drive("head_data", script, unique_question("aged USD breaks"))
    types = [e["type"] for e in events]
    assert types == ["plan", "plan", "widget", "summary", "telemetry"], types
    assert [e["tool"] for e in events if e["type"] == "plan"] == ["run_metric", "combine"]
    assert page["rows"] == [["LE00016", 64]] and page["columns"] == ["legal_entity_id", "breaks"]
    telemetry = events[-1]
    assert telemetry["llm_turns"] == len(script) == len(model.requests) and telemetry["path"] == "direct"
    assert events[-2]["text"] == "LE00016 has the most aged USD breaks (64)."   # the supervisor text, not the spec narrative
    rows = agent_run_rows(user.sub)
    assert len(rows) == 1 and rows[0][0] == telemetry["run_id"] and rows[0][2] == len(script) and rows[0][3] == "ok"


async def test_story_src001_late_feeds_through_chat():
    last_six = (f"business_date IN (SELECT DISTINCT business_date FROM f WHERE business_date <= '{AS_OF}' "
                "ORDER BY business_date DESC LIMIT 6)")
    script = [
        reply_tools(("run_metric", {"metric_id": "late_feeds", "dimensions": ["source_id", "business_date"]})),
        lambda req: reply_tools(("combine", {
            "sql": f"SELECT source_id, sum(value) AS late FROM f WHERE {last_six} GROUP BY source_id "
                   "ORDER BY late DESC LIMIT 3", "handles": {"f": last_handle(req)}})),
        visualize("table of the sources with the most late feeds in the last 6 business days"),
        spec("Late feeds by source", "SRC001 is late far more than any other source."),
        reply_text("SRC001 leads the late feeds."),
    ]
    events, _, user, page = await drive("head_data", script, unique_question("which source is late"))
    assert [e["type"] for e in events] == ["plan", "plan", "widget", "summary", "telemetry"]
    assert events[-2]["text"] == "SRC001 leads the late feeds."
    top, runner_up = page["rows"][0], page["rows"][1]
    assert top[0] == "SRC001" and top[1] >= 3 * runner_up[1], page["rows"]
    assert events[-1]["llm_turns"] == len(script) and len(agent_run_rows(user.sub)) == 1


@pytest.mark.parametrize("persona", sorted(KPIS))
async def test_every_configured_kpi_tile_is_answerable_for_its_persona(persona):
    app = create_app(settings=SETTINGS, gateway_factory=live_gateway_factory)
    token = mint({**claims_for(persona, ttl_s=600), "sub": user_for(persona).sub}, GATEWAY_AUDIENCE,
                 SETTINGS.jwt_secret.get_secret_value(), ttl_s=600)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        r = await client.get("/kpis")
    assert r.status_code == 200
    tiles = r.json()["tiles"]
    assert [t["metric_id"] for t in tiles] == [k.metric_id for k in KPIS[persona]]
    assert all(t["status"] == "ok" and isinstance(t["value"], (int, float)) for t in tiles), tiles
    # units and scaling (not run in the fix wave: needs the live stack): rate tiles are percentages in 0..100,
    # and the amount tile is the USD total, not an unlabelled largest-currency figure
    for tile, d in zip(tiles, KPIS[persona]):
        assert tile["unit"] == d.unit
        if d.unit == "%":
            assert 0 <= tile["value"] <= 100, tile
        if d.filters:
            assert d.unit and d.unit in d.label, tile
