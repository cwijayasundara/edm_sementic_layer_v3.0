import contextlib
import json

from fastapi.testclient import TestClient

from prism.agent.api import create_app
from prism.agent.gateway_client import GatewayError
from prism.agent.kpis import KpiService
from prism.agent.model import ScriptedModelClient, reply_text
from prism.agent.service import AgentService
from prism.config import Settings
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.security.personas import claims_for
from prism.security.tokens import mint
from tests.agent.fakes import FakeGateway, summary

S = Settings(agent_dev_token_enabled=True)
PROD = Settings(env="production", agent_dev_token_enabled=True, ctx_hmac_key="p" * 40, jwt_secret="q" * 40,
                audit_hmac_key="r" * 40, pg_app_password="s1", pg_svc_password="s2", pg_admin_password="s3",
                neo4j_password="s4")
ROWS = {"handle": "r_aaaaaaaaaaaa", "columns": ["a"], "offset": 0, "row_count": 1, "rows": [[1]]}


def token(persona="head_data", **kw):
    return mint(claims_for(persona), GATEWAY_AUDIENCE, S.jwt_secret.get_secret_value(), ttl_s=600)


def app_with(gw, client=None, settings=S, **kw):
    @contextlib.asynccontextmanager
    async def factory(user):
        yield gw
    svc = AgentService(settings=settings, model_client=client or ScriptedModelClient([reply_text("hello")]),
                       gateway_factory=factory, run_writer=None)
    return TestClient(create_app(settings=settings, service=svc, gateway_factory=factory,
                                 kpi_service=kw.pop("kpi_service", None) or KpiService(), **kw),
                      client=("127.0.0.1", 50000))


def sse(text):
    out = []
    for frame in text.strip().split("\n\n"):
        ev, data = frame.split("\n")
        out.append((ev.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


def test_healthz_is_public():
    r = app_with(FakeGateway({})).get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "service": "agent"}


def test_chat_requires_a_valid_bearer_and_never_says_why():
    c = app_with(FakeGateway({}))
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "Basic x"}, {"Authorization": "Bearer"}):
        r = c.post("/chat", json={"question": "hi"}, headers=headers)
        assert r.status_code == 401 and r.json() == {"detail": "unauthorized"}


def test_chat_streams_sse_frames():
    c = app_with(FakeGateway({}))
    r = c.post("/chat", json={"question": "hi"}, headers={"Authorization": f"Bearer {token()}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-accel-buffering"] == "no"
    events = sse(r.text)
    assert [e for e, _ in events] == ["summary", "telemetry"] and events[0][1]["text"] == "hello"


def test_chat_rejects_oversized_and_extra_fields():
    c = app_with(FakeGateway({}))
    h = {"Authorization": f"Bearer {token()}"}
    assert c.post("/chat", json={"question": "x" * 2001}, headers=h).status_code == 422
    assert c.post("/chat", json={"question": "hi", "sub": "admin"}, headers=h).status_code == 422


def test_chat_without_model_access_is_503_before_any_gateway_call():
    gw = FakeGateway({})
    c = app_with(gw, model_configured=False)
    r = c.post("/chat", json={"question": "hi"}, headers={"Authorization": f"Bearer {token()}"})
    assert r.status_code == 503 and r.json() == {"detail": "model access is not configured"}
    assert gw.calls == []
    assert c.post("/chat", json={"question": "hi"}).status_code == 401
    assert c.get("/healthz").status_code == 200


def test_results_passthrough_and_404_masking():
    gw = FakeGateway({"get_rows": ROWS})
    c = app_with(gw)
    h = {"Authorization": f"Bearer {token()}"}
    assert c.get("/results/r_aaaaaaaaaaaa?limit=10", headers=h).json()["rows"] == [[1]]
    assert gw.calls[-1] == ("get_rows", {"handle": "r_aaaaaaaaaaaa", "offset": 0, "limit": 10})
    assert c.get("/results/not-a-handle", headers=h).status_code == 404
    assert c.get("/results/r_aaaaaaaaaaaa?limit=500", headers=h).status_code == 422
    assert c.get("/results/r_aaaaaaaaaaaa?limit=0", headers=h).status_code == 422
    assert c.get("/results/r_aaaaaaaaaaaa?offset=-1", headers=h).status_code == 422
    assert c.get("/results/r_aaaaaaaaaaaa").status_code == 401
    for code in ("unknown_handle", "not_permitted"):
        r = app_with(FakeGateway({"get_rows": GatewayError(code, "secret detail")})).get(
            "/results/r_bbbbbbbbbbbb", headers=h)
        assert r.status_code == 404 and r.json() == {"detail": "not found"}
    r = app_with(FakeGateway({"get_rows": GatewayError("gateway_unavailable", "secret detail")})).get(
        "/results/r_bbbbbbbbbbbb", headers=h)
    assert r.status_code == 502 and r.json() == {"detail": "data service unavailable"}


def test_kpis_tiles_degrade_independently():
    def run_metric(args):
        if args["metric_id"] == "open_breaks":
            raise GatewayError("not_permitted", "no")
        return summary(metric_id=args["metric_id"])

    gw = FakeGateway({"run_metric": run_metric,
                      "get_rows": {"handle": "r_aaaaaaaaaaaa", "columns": ["value"], "offset": 0, "row_count": 1,
                                   "rows": [[42]]}})
    resp = app_with(gw).get("/kpis", headers={"Authorization": f"Bearer {token('head_data')}"})
    by = {t["metric_id"]: t for t in resp.json()["tiles"]}
    assert by["open_breaks"]["status"] == "unavailable" and "value" not in by["open_breaks"]
    assert by["late_feeds"] == {**by["late_feeds"], "status": "ok", "value": 42}
    assert "no" not in json.dumps(by["open_breaks"]).replace("not_permitted", "")


def test_kpis_requires_auth():
    assert app_with(FakeGateway({})).get("/kpis").status_code == 401


def test_kpis_cached_per_role_for_60s():
    gw = FakeGateway({"run_metric": summary(), "get_rows": {"rows": [[1]], "columns": ["value"], "handle": "h",
                                                              "offset": 0, "row_count": 1}})
    c = app_with(gw)
    h = {"Authorization": f"Bearer {token('head_data')}"}
    c.get("/kpis", headers=h)
    n = len(gw.calls)
    c.get("/kpis", headers=h)
    assert len(gw.calls) == n


def test_dev_token_endpoint_is_gated_by_env():
    c = app_with(FakeGateway({"get_rows": ROWS}))
    t = c.post("/dev/token", json={"persona_id": "steward"}).json()["token"]
    assert c.get("/results/r_aaaaaaaaaaaa", headers={"Authorization": f"Bearer {t}"}).status_code != 401
    assert c.post("/dev/token", json={"persona_id": "nobody"}).status_code == 422
    assert app_with(FakeGateway({}), settings=PROD).post("/dev/token", json={"persona_id": "steward"},
                                                         headers={"Host": "localhost"}).status_code == 404


def test_cors_allows_only_the_ui_origin():
    c = app_with(FakeGateway({}))
    ok = c.options("/chat", headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST",
                                     "Access-Control-Request-Headers": "authorization,content-type"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    bad = c.options("/chat", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


def test_create_app_from_env_starts_without_an_api_key(monkeypatch):
    from prism.agent import api
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PRISM_ANTHROPIC_API_KEY", raising=False)
    no_key = Settings(anthropic_api_key=None, _env_file=None)
    monkeypatch.setattr(api, "Settings", lambda: no_key)
    tok = mint(claims_for("head_data"), GATEWAY_AUDIENCE, no_key.jwt_secret.get_secret_value(), ttl_s=600)
    with TestClient(api.create_app_from_env()) as c:
        assert c.get("/healthz").status_code == 200
        r = c.post("/chat", json={"question": "hi"}, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 503 and r.json() == {"detail": "model access is not configured"}


def test_sse_closes_the_service_generator_on_disconnect():
    import asyncio

    from prism.agent.api import _sse
    closed = []

    async def events():
        try:
            yield {"type": "plan"}
            yield {"type": "summary"}
        finally:
            closed.append(True)

    async def go():
        gen = _sse(events())
        assert (await gen.__anext__()).startswith("event: plan")
        await gen.aclose()

    asyncio.run(go())
    assert closed == [True]


def test_dev_token_disabled_by_default_and_loopback_only():
    body = {"persona_id": "steward"}
    assert app_with(FakeGateway({}), settings=Settings()).post("/dev/token", json=body).status_code == 404
    assert app_with(FakeGateway({})).post("/dev/token", json=body).status_code == 200
    c = app_with(FakeGateway({}))
    remote = TestClient(c.app, client=("203.0.113.9", 4000))
    assert remote.post("/dev/token", json=body).status_code == 404


def test_kpi_cache_hit_never_opens_the_gateway():
    gw = FakeGateway({"run_metric": summary(), "get_rows": ROWS})
    svc = KpiService()
    c = app_with(gw, kpi_service=svc)
    h = {"Authorization": f"Bearer {token('head_data')}"}
    assert c.get("/kpis", headers=h).status_code == 200

    def boom(user):
        raise GatewayError("gateway_unavailable", "x")
    c2 = TestClient(create_app(settings=S, service=None, gateway_factory=boom, kpi_service=svc))
    assert c2.get("/kpis", headers=h).status_code == 200


def test_all_unavailable_tiles_are_not_cached():
    from prism.agent.auth import UserContext
    svc = KpiService()
    u = UserContext(sub="u", roles=("head_data",), metrics_only=True, token="t")
    bad = FakeGateway({"run_metric": GatewayError("source_unavailable", "x")})
    import asyncio
    asyncio.run(svc.tiles(u, bad))
    assert svc.cached(u) is None


def test_stream_failure_ends_with_one_fixed_error_frame():
    import asyncio

    from prism.agent.api import _sse

    async def events():
        yield {"type": "plan"}
        raise ValueError("secret detail")

    async def go():
        return [f async for f in _sse(events())]

    frames = asyncio.run(go())
    assert len(frames) == 2 and frames[1].startswith("event: error") and "secret" not in frames[1]
    assert json.loads(frames[1].split("data: ")[1])["code"] == "internal_error"


def test_lazy_model_client_refuses_without_a_key():
    import asyncio

    import pytest

    from prism.agent.api import _LazyModelClient
    with pytest.raises(RuntimeError):
        asyncio.run(_LazyModelClient(None).create(None))


def test_a_foreign_host_header_is_rejected_whatever_the_route():
    c = app_with(FakeGateway({}))
    assert c.get("/healthz").status_code == 200
    for host in ("evil.example", "localhost.evil.example"):
        assert c.get("/healthz", headers={"Host": host}).status_code == 400
        assert c.post("/dev/token", json={"persona_id": "head_data"}, headers={"Host": host}).status_code == 400
    for host in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
        assert c.get("/healthz", headers={"Host": host}).status_code == 200


def test_testserver_host_is_not_allowed_in_production():
    c = app_with(FakeGateway({}), settings=PROD)
    assert c.get("/healthz").status_code == 400            # Host: testserver
    assert c.get("/healthz", headers={"Host": "localhost"}).status_code == 200


RID = "0b8f3c1e-2d4a-4c6b-9e7f-1a2b3c4d5e6f"


def test_confirm_answer_route_maps_gateway_outcomes():
    gw = FakeGateway({"confirm_answer": [{"confirmed": True}, GatewayError("not_confirmable", "secret detail"),
                                         GatewayError("rate_limited", "x"), GatewayError("confirm_failed", "x"),
                                         GatewayError("gateway_unavailable", "x")]})
    c = app_with(gw)
    h = {"Authorization": f"Bearer {token()}"}
    ok = c.post(f"/answers/{RID}/confirm", headers=h)
    assert ok.status_code == 204 and ok.content == b""
    assert gw.calls[-1] == ("confirm_answer", {"record_id": RID})
    nf = c.post(f"/answers/{RID}/confirm", headers=h)
    assert nf.status_code == 404 and nf.json() == {"detail": "not found"}
    assert c.post(f"/answers/{RID}/confirm", headers=h).status_code == 429
    assert c.post(f"/answers/{RID}/confirm", headers=h).status_code == 502
    assert c.post(f"/answers/{RID}/confirm", headers=h).status_code == 502
    calls = len(gw.calls)
    for bad in ("nope", RID.upper(), RID + "0"):
        assert c.post(f"/answers/{bad}/confirm", headers=h).status_code == 422
    assert len(gw.calls) == calls                    # an invalid id never reaches the gateway
    assert c.post(f"/answers/{RID}/confirm").status_code == 401
