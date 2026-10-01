import asyncio
import contextlib
import time

import httpx
import httpx2
import pytest

from prism.mcp.client import mcp_client
from prism.mcp.servers import MCP_PORTS, SOURCES, create_app, cashrecon_app
from prism.sources.marketmaster_api.app import create_app as create_marketmaster_api
from prism.sources.refmaster_api.app import create_app as create_refmaster_api

pytestmark = pytest.mark.db


def url(source):
    return f"http://127.0.0.1:{MCP_PORTS[source]}/mcp"


@contextlib.asynccontextmanager
async def serving(seeded, source):
    """In-process MCP server; REST sources talk to in-process copies of the mock APIs."""
    api = transport = None
    if source == "refmaster":
        api = create_refmaster_api(seeded)
    elif source == "marketmaster":
        api = create_marketmaster_api(seeded)
    if api is not None:
        transport = httpx.ASGITransport(app=api)
    mcp, app = create_app(source, seeded, transport=transport)
    try:
        async with mcp.session_manager.run():
            yield app
    finally:
        # ASGITransport skips the lifespan, so close the backend and API pools explicitly
        await app.state.prism_backend.aclose()
        if api is not None:
            await api.state.dbs.close()


async def call(seeded, source, persona, tool, args, mcp_token):
    async with serving(seeded, source) as app:
        async with mcp_client(url(source), mcp_token(seeded, persona, source), asgi_app=app) as c:
            return await c.call_tool(tool, args)


def test_ports_and_sources_are_fixed():
    assert SOURCES == ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
    assert MCP_PORTS == {"refmaster": 8201, "marketmaster": 8202, "cashrecon": 8203, "assetrecon": 8204,
                         "feedhub": 8205}


async def test_every_source_exposes_the_same_contract_and_a_public_healthz(seeded, mcp_token):
    for source in SOURCES:
        async with serving(seeded, source) as app:
            async with mcp_client(url(source), mcp_token(seeded, "head_data", source), asgi_app=app) as c:
                tools = {t.name for t in (await c.list_tools()).tools}
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                            base_url=f"http://127.0.0.1:{MCP_PORTS[source]}") as h:
                health = await h.get("/healthz")
        assert tools == {"describe", "run_metric", "query"}, source
        assert health.json() == {"status": "ok", "source": source}


async def test_describe_shows_metrics_per_source(seeded, mcp_token):
    for source, expect in {"cashrecon": "open_breaks", "assetrecon": "position_exceptions",
                           "feedhub": "late_feeds", "refmaster": "open_dq_exceptions",
                           "marketmaster": "price_conflicts"}.items():
        r = await call(seeded, source, "head_data", "describe", {}, mcp_token)
        assert not r.is_error, (source, r.content)
        assert expect in {m["id"] for m in r.structured_content["metrics"]}, source


async def test_cashrecon_rls_and_masking_over_mcp(seeded, mcp_token):
    head = await call(seeded, "cashrecon", "head_data", "run_metric",
                      {"metric_id": "open_breaks", "dimensions": ["region"]}, mcp_token)
    emea = await call(seeded, "cashrecon", "cash_ops_emea", "run_metric",
                      {"metric_id": "open_breaks", "dimensions": ["region"]}, mcp_token)
    assert len(head.structured_content["rows"]) >= 2
    assert {r["region"] for r in emea.structured_content["rows"]} == {"EMEA"}
    masked = await call(seeded, "cashrecon", "cash_ops_emea", "query",
                        {"request": {"sql": "SELECT nostro_no FROM cash_accounts LIMIT 3"}}, mcp_token)
    assert masked.structured_content["rows"] and all(r[0].startswith("****") for r in masked.structured_content["rows"])
    unmasked = await call(seeded, "cashrecon", "head_data", "query",
                          {"request": {"sql": "SELECT nostro_no FROM cash_accounts LIMIT 3"}}, mcp_token)
    rows = unmasked.structured_content["rows"]
    assert rows and all(r[0] and not r[0].startswith("****") and "*" not in r[0] for r in rows)


async def test_query_rejections_are_clean_tool_errors(seeded, mcp_token):
    for sql in ("SELECT * FROM private.cash_accounts", "SELECT 1; SELECT 2", "SELECT set_config('a','b',false)"):
        r = await call(seeded, "cashrecon", "head_data", "query", {"request": {"sql": sql}}, mcp_token)
        assert r.is_error and r.content[0].text, sql
        assert "Traceback" not in r.content[0].text


async def test_metrics_only_persona_can_run_metrics_but_not_query(seeded, mcp_token):
    ok = await call(seeded, "cashrecon", "bi_analyst", "run_metric", {"metric_id": "open_breaks"}, mcp_token)
    no = await call(seeded, "cashrecon", "bi_analyst", "query", {"request": {"sql": "SELECT 1"}}, mcp_token)
    assert not ok.is_error and no.is_error and "metrics-only" in no.content[0].text


async def test_dataset_entitlement_is_an_explicit_error(seeded, mcp_token):
    r = await call(seeded, "assetrecon", "steward", "run_metric", {"metric_id": "position_exceptions"}, mcp_token)
    assert r.is_error and "not entitled" in r.content[0].text
    r = await call(seeded, "refmaster", "cash_ops_emea", "run_metric", {"metric_id": "open_dq_exceptions"}, mcp_token)
    assert r.is_error


async def test_the_planted_stories_are_reachable_over_mcp(seeded, mcp_token):
    heat = await call(seeded, "marketmaster", "steward", "run_metric",
                      {"metric_id": "price_conflicts", "dimensions": ["vendor_id", "asset_class"],
                       "time_range": {"last_business_days": 5}}, mcp_token)
    top = heat.structured_content["rows"][0]
    assert (top["vendor_id"], top["asset_class"]) == ("V_A", "Corp bond")
    late = await call(seeded, "feedhub", "head_data", "run_metric",
                      {"metric_id": "late_feeds", "dimensions": ["source_id"],
                       "time_range": {"last_business_days": 6}}, mcp_token)
    assert late.structured_content["rows"][0]["source_id"] == "SRC001"
    nav = await call(seeded, "assetrecon", "head_data", "run_metric",
                     {"metric_id": "nav_break_bps_max", "dimensions": ["portfolio_id"],
                      "time_range": {"last_business_days": 3}, "limit": 2}, mcp_token)
    assert {r["portfolio_id"] for r in nav.structured_content["rows"]} == {"PF003", "PF009"}


async def test_token_for_another_source_is_401(seeded, mcp_token):
    tok = mcp_token(seeded, "head_data", "cashrecon")
    async with serving(seeded, "assetrecon") as app:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                        base_url=f"http://127.0.0.1:{MCP_PORTS['assetrecon']}") as h:
            r = await h.post("/mcp", headers={"Authorization": f"Bearer {tok}",
                                              "Accept": "application/json, text/event-stream",
                                              "Content-Type": "application/json"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "0"}}})
    assert r.status_code == 401


async def test_concurrent_personas_are_isolated(seeded, mcp_token):
    # waves stay within the per-principal in-flight cap (3): 3 head_data + 3 cash_ops_emea at once
    async with serving(seeded, "cashrecon") as app:

        async def one(persona):
            async with mcp_client(url("cashrecon"), mcp_token(seeded, persona, "cashrecon"), asgi_app=app) as c:
                r = await c.call_tool("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"]})
                assert not r.is_error, r.content
                return persona, {x["region"] for x in r.structured_content["rows"]}

        for _ in range(4):
            wave = await asyncio.wait_for(
                asyncio.gather(*(one(p) for p in ["head_data", "cash_ops_emea"] * 3)), 60)
            for persona, regions in wave:
                assert (regions == {"EMEA"}) if persona == "cash_ops_emea" else (len(regions) >= 2)


async def test_fourth_concurrent_call_from_one_principal_is_rejected(seeded, mcp_token):
    async with serving(seeded, "cashrecon") as app:
        backend = app.state.prism_backend
        real = backend._run_metric

        def slow(*a, **k):  # test-only delay so the calls overlap deterministically
            time.sleep(0.5)
            return real(*a, **k)

        backend._run_metric = slow

        async def one():
            async with mcp_client(url("cashrecon"), mcp_token(seeded, "head_data", "cashrecon"),
                                  asgi_app=app) as c:
                return await c.call_tool("run_metric", {"metric_id": "open_breaks"})

        results = await asyncio.wait_for(asyncio.gather(*(one() for _ in range(4))), 60)
    rejected = [r for r in results if r.is_error]
    assert len(rejected) >= 1
    assert all("too many concurrent requests" in r.content[0].text for r in rejected)
    assert any(not r.is_error for r in results)


def test_uvicorn_factory_returns_an_asgi_app(monkeypatch):
    from prism.mcp import servers

    monkeypatch.setattr(servers, "configure_audit_logging", lambda *a, **k: None)  # keep caplog propagation intact
    assert callable(cashrecon_app())
