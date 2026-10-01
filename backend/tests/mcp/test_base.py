import asyncio
import contextlib
import json
import logging

import httpx2
import pytest

from prism.config import Settings
from prism.mcp.base import DescribeResult, MetricResult, QueryResult, build_source_app
from prism.mcp.client import mcp_client
from prism.mcp.results import SourceError
from prism.security.personas import claims_for
from prism.security.tokens import mint

URL = "http://127.0.0.1:8000/mcp"
SETTINGS = Settings()


class FakeBackend:
    name = "fake"
    kind = "sql"

    async def describe(self, claims):
        return DescribeResult(source="fake", kind="sql", as_of="2026-09-30", metrics=[], objects=[],
                              notes=[f"sub={claims['sub']}"])

    async def run_metric(self, claims, *, metric_id, dimensions, filters, time_range, order_by, limit):
        if metric_id == "boom":
            raise RuntimeError("password=hunter2 leaked in driver error")
        if metric_id == "denied":
            raise SourceError("not entitled to this dataset")
        if metric_id == "unserializable":
            return MetricResult.model_construct(source="fake", metric_id=metric_id, unit=None, dimensions=[],
                                                rows=[{"v": object()}], row_count=1, as_of="x", window=None)
        if metric_id == "cut":
            return MetricResult(source="fake", metric_id=metric_id, unit=None, dimensions=[], rows=[{"value": 1}],
                                row_count=1, truncated=True, as_of="2026-09-30")
        await asyncio.sleep(0.02)  # let concurrent requests interleave
        return MetricResult(source="fake", metric_id=metric_id, unit=None, dimensions=dimensions,
                            rows=[{"who": claims["sub"], "value": limit}], row_count=1, as_of="2026-09-30",
                            window=None)

    async def query(self, claims, request):
        return QueryResult(source="fake", columns=["who"], rows=[[claims["sub"]]], row_count=1, truncated=False)

    async def aclose(self):
        pass


@contextlib.asynccontextmanager
async def running():
    mcp, app = build_source_app(FakeBackend(), SETTINGS)
    async with mcp.session_manager.run():  # ASGITransport does not run the lifespan
        yield app


def tok(persona="steward", aud="fake-mcp", ttl=300):
    return mint(claims_for(persona), aud, SETTINGS.jwt_secret.get_secret_value(), ttl_s=ttl)


async def test_uniform_tool_contract_and_schemas():
    async with running() as app:
        async with mcp_client(URL, tok(), asgi_app=app) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"describe", "run_metric", "query"}
    rm = tools["run_metric"].input_schema
    assert rm["required"] == ["metric_id"]
    assert rm["properties"]["limit"]["minimum"] == 1 and rm["properties"]["limit"]["maximum"] == 1000
    assert tools["run_metric"].annotations.read_only_hint is True
    assert "rows" in tools["run_metric"].output_schema["properties"]


async def test_claims_reach_the_backend_and_text_is_a_short_summary():
    async with running() as app:
        async with mcp_client(URL, tok("cash_ops_emea"), asgi_app=app) as c:
            r = await c.call_tool("run_metric", {"metric_id": "m", "limit": 7})
    assert not r.is_error
    assert r.structured_content["rows"] == [{"who": "cash_ops_emea", "value": 7}]
    assert "1 row" in r.content[0].text and "cash_ops_emea" not in r.content[0].text


async def test_describe_and_query_are_wired():
    async with running() as app:
        async with mcp_client(URL, tok("head_data"), asgi_app=app) as c:
            d = await c.call_tool("describe", {})
            q = await c.call_tool("query", {"request": {"sql": "select 1"}})
    assert d.structured_content["notes"] == ["sub=head_data"]
    assert q.structured_content["rows"] == [["head_data"]]


async def test_metrics_only_principals_cannot_query_but_can_run_metrics():
    async with running() as app:
        async with mcp_client(URL, tok("bi_analyst"), asgi_app=app) as c:
            q = await c.call_tool("query", {"request": {"sql": "select 1"}})
            m = await c.call_tool("run_metric", {"metric_id": "m"})
    assert q.is_error and "metrics-only" in q.content[0].text
    assert not m.is_error


async def test_error_mapping_is_clean_and_does_not_leak():
    async with running() as app:
        async with mcp_client(URL, tok(), asgi_app=app) as c:
            denied = await c.call_tool("run_metric", {"metric_id": "denied"})
            boom = await c.call_tool("run_metric", {"metric_id": "boom"})
            bad = await c.call_tool("run_metric", {"metric_id": "m", "limit": 0})
    assert denied.is_error and denied.content[0].text.endswith("not entitled to this dataset")
    assert boom.is_error and "hunter2" not in boom.content[0].text
    assert bad.is_error


@pytest.mark.parametrize("case", ["wrong_audience", "expired", "missing"])
async def test_wrong_audience_and_expired_are_401(case):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if case == "wrong_audience":
        headers["Authorization"] = f"Bearer {tok(aud='other-mcp')}"
    elif case == "expired":
        headers["Authorization"] = f"Bearer {tok(ttl=-5)}"
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    async with running() as app:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as h:
            r = await h.post("/mcp", json=init, headers=headers)
            health = await h.get("/healthz")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert health.status_code == 200 and health.json() == {"status": "ok", "source": "fake"}


# identity is captured once per call and passed by value, so concurrent callers cannot cross.
async def test_concurrent_callers_never_see_each_others_identity():
    personas = ["steward", "cash_ops_emea", "invest_ops_growth", "bi_analyst", "head_data"] * 3
    async with running() as app:

        async def one(p):
            async with mcp_client(URL, tok(p), asgi_app=app) as c:
                r = await c.call_tool("run_metric", {"metric_id": "m"})
                return p, r.structured_content["rows"][0]["who"]

        results = await asyncio.wait_for(asyncio.gather(*(one(p) for p in personas)), 60)
    assert all(asked == got for asked, got in results)


def audit_lines(caplog):
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "prism.mcp.audit"]


async def test_audit_lines(caplog):
    caplog.set_level(logging.INFO, logger="prism.mcp.audit")
    token = tok("cash_ops_emea")
    async with running() as app:
        async with mcp_client(URL, token, asgi_app=app) as c:
            await c.call_tool("run_metric", {"metric_id": "m"})
            await c.call_tool("run_metric", {"metric_id": "denied"})
            await c.call_tool("run_metric", {"metric_id": "boom"})
    ok, err, internal = audit_lines(caplog)
    assert ok["outcome"] == "ok" and ok["sub"] == "cash_ops_emea" and ok["source"] == "fake" and ok["tool"] == "run_metric"
    raw = json.dumps(ok)
    assert token not in raw and "who" not in raw and "roles" not in ok and "scopes" not in ok
    assert err["outcome"] == "error"
    assert internal["outcome"] == "internal_error" and internal["error_type"] == "RuntimeError"
    assert "hunter2" not in json.dumps(internal)


async def test_unexpected_error_client_message_has_ref_only():
    async with running() as app:
        async with mcp_client(URL, tok(), asgi_app=app) as c:
            boom = await c.call_tool("run_metric", {"metric_id": "boom"})
    assert boom.is_error and "hunter2" not in boom.content[0].text


async def test_serialization_failure_is_audited_as_error(caplog):
    caplog.set_level(logging.INFO, logger="prism.mcp.audit")
    async with running() as app:
        async with mcp_client(URL, tok(), asgi_app=app) as c:
            r = await c.call_tool("run_metric", {"metric_id": "unserializable"})
    assert r.is_error
    assert audit_lines(caplog)[-1]["outcome"] != "ok"


@pytest.mark.parametrize("host,status", [("evil.example", 421), ("127.0.0.1:8203", 200)])
async def test_host_allow_list(host, status):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "Authorization": f"Bearer {tok()}", "Host": host}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    async with running() as app:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as h:
            r = await h.post("/mcp", json=init, headers=headers)
    assert r.status_code == status


async def test_input_bounds():
    async with running() as app:
        async with mcp_client(URL, tok(), asgi_app=app) as c:
            big_limit = await c.call_tool("run_metric", {"metric_id": "m", "limit": 1001})
            long_id = await c.call_tool("run_metric", {"metric_id": "x" * 129})
            dims = await c.call_tool("run_metric", {"metric_id": "m", "dimensions": [f"d{i}" for i in range(21)]})
            filt = await c.call_tool("run_metric", {"metric_id": "m", "filters": {f"f{i}": 1 for i in range(21)}})
            req = await c.call_tool("query", {"request": {f"k{i}": 1 for i in range(21)}})
    assert big_limit.is_error and "1000" in big_limit.content[0].text
    assert long_id.is_error and dims.is_error
    assert filt.is_error and "too many filters" in filt.content[0].text
    assert req.is_error and "too many request keys" in req.content[0].text


async def test_missing_metrics_only_claim_fails_closed():
    claims = {k: v for k, v in claims_for("head_data").items() if k != "metrics_only"}
    token = mint(claims, "fake-mcp", SETTINGS.jwt_secret.get_secret_value(), ttl_s=300)
    async with running() as app:
        async with mcp_client(URL, token, asgi_app=app) as c:
            q = await c.call_tool("query", {"request": {"sql": "select 1"}})
            m = await c.call_tool("run_metric", {"metric_id": "m"})
    assert q.is_error and "metrics-only" in q.content[0].text
    assert not m.is_error


async def test_metric_summary_mentions_truncation():
    async with running() as app:
        async with mcp_client(URL, tok(), asgi_app=app) as c:
            cut = await c.call_tool("run_metric", {"metric_id": "cut"})
            whole = await c.call_tool("run_metric", {"metric_id": "m"})
    assert cut.content[0].text == "1 row for cut (truncated)" and cut.structured_content["truncated"] is True
    assert whole.content[0].text == "1 row for m" and whole.structured_content["truncated"] is False
