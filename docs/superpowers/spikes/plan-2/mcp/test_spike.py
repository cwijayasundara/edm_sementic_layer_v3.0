from __future__ import annotations

import asyncio
import contextlib
import time
from typing import AsyncIterator

import httpx2
import jwt
import pytest
import uvicorn

from spike.auth import JWTConfig, mint_token
from spike.client import mcp_client
from spike.server import build_app, build_mounted_app

CFG = JWTConfig(secret="s" * 48, audience="mcp:pg-sales")
BASE = "http://127.0.0.1:8000"
URL = f"{BASE}/mcp"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@contextlib.asynccontextmanager
async def inproc(**kw) -> AsyncIterator[object]:
    mcp, app = build_app("pg", CFG, **kw)
    async with mcp.session_manager.run():  # ASGITransport runs no lifespan
        yield app


@contextlib.asynccontextmanager
async def uvicorn_server(app) -> AsyncIterator[str]:
    """Real server on an ephemeral port inside the test's event loop (runs lifespan)."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)


MATRIX = [
    pytest.param(mode, stateless, jr, auth, id=f"{mode}-{'stateless' if stateless else 'stateful'}-{'json' if jr else 'sse'}-{auth}")
    for mode in ("auto", "legacy")
    for stateless in (True, False)
    for jr in (True, False)
    for auth in ("sdk", "asgi")
]


@pytest.mark.parametrize("mode,stateless,json_response,auth_mode", MATRIX)
async def test_claims_visible_and_isolated(mode, stateless, json_response, auth_mode):
    async with inproc(stateless=stateless, json_response=json_response, auth_mode=auth_mode) as app:

        async def one(i: int) -> dict:
            async with mcp_client(URL, mint_token(CFG, f"user-{i}"), mode=mode, asgi_app=app) as c:
                r = await c.call_tool("whoami", {})
                assert not r.is_error, r.content
                return r.structured_content

        results = await asyncio.wait_for(asyncio.gather(*(one(i) for i in range(12))), 60)

    for i, sc in enumerate(results):
        assert sc["sub"] == sc["sub_after_await"] == f"user-{i}"
        assert sc["aud"] == CFG.audience
    if mode == "auto":
        assert results[0]["protocol_version"] == "2026-07-28" and results[0]["session_id"] is None
    else:
        assert results[0]["protocol_version"] == "2025-11-25"
        if stateless:
            assert results[0]["session_id"] is None
        else:  # the stateful session code really was exercised
            assert results[0]["session_id"]
            assert len({r["session_id"] for r in results}) == 12


async def test_many_calls_one_session_concurrently():
    """Same client, concurrent tool calls, then a different principal concurrently."""
    async with inproc(stateless=False, json_response=False) as app:
        async with (
            mcp_client(URL, mint_token(CFG, "alice"), mode="legacy", asgi_app=app) as a,
            mcp_client(URL, mint_token(CFG, "bob"), mode="legacy", asgi_app=app) as b,
        ):
            rs = await asyncio.gather(*[(a if i % 2 else b).call_tool("whoami", {}) for i in range(20)])
    for i, r in enumerate(rs):
        want = "alice" if i % 2 else "bob"
        assert r.structured_content["sub"] == r.structured_content["sub_after_await"] == want


BAD_TOKENS = {
    "missing": None,
    "not_bearer": "Basic abc",
    "wrong_aud": mint_token(CFG, "eve", aud="mcp:other-source"),
    "expired": mint_token(CFG, "eve", ttl_s=-10),
    "bad_sig": jwt.encode({"sub": "eve", "aud": CFG.audience, "exp": int(time.time()) + 60}, "k" * 48, algorithm="HS256"),
    "alg_none": jwt.encode({"sub": "eve", "aud": CFG.audience, "exp": int(time.time()) + 60}, None, algorithm="none"),
    "no_exp": jwt.encode({"sub": "eve", "aud": CFG.audience}, CFG.secret, algorithm="HS256"),
    "garbage": "not.a.jwt",
}


@pytest.mark.parametrize("auth_mode", ["sdk", "asgi"])
@pytest.mark.parametrize("case", list(BAD_TOKENS))
async def test_rejects_bad_tokens_with_401(auth_mode, case):
    tok = BAD_TOKENS[case]
    headers = dict(ACCEPT)
    if tok is not None:
        headers["Authorization"] = tok if case == "not_bearer" else f"Bearer {tok}"
    async with inproc(auth_mode=auth_mode) as app:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as h:
            r = await h.post("/mcp", json=INIT, headers=headers)
            health = await h.get("/healthz")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer ")
    assert r.headers["content-type"] == "application/json"
    assert r.json()["error"] == "invalid_token"
    assert health.status_code == 200 and health.json()["status"] == "ok"


async def test_client_sees_auth_failure():
    async with inproc() as app:
        with pytest.raises(BaseException) as ei:
            async with mcp_client(URL, mint_token(CFG, "eve", aud="nope"), asgi_app=app) as c:
                await c.list_tools()
    print("client-side exception on 401:", type(ei.value).__name__, repr(ei.value)[:300])


async def test_stateful_session_bound_to_token_principal():
    """Alice's Mcp-Session-Id presented with Bob's (valid) token -> 404."""
    for auth_mode in ("sdk", "asgi"):
        async with inproc(stateless=False, json_response=True, auth_mode=auth_mode) as app:
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as h:
                alice = {**ACCEPT, "Authorization": f"Bearer {mint_token(CFG, 'alice')}"}
                bob = {**ACCEPT, "Authorization": f"Bearer {mint_token(CFG, 'bob')}"}
                r = await h.post("/mcp", json=INIT, headers=alice)
                assert r.status_code == 200, r.text
                sid = r.headers["mcp-session-id"]
                ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
                r_bob = await h.post("/mcp", json=ping, headers={**bob, "mcp-session-id": sid, "mcp-protocol-version": "2025-11-25"})
                r_alice = await h.post("/mcp", json=ping, headers={**alice, "mcp-session-id": sid, "mcp-protocol-version": "2025-11-25"})
        assert r_bob.status_code == 404, (auth_mode, r_bob.status_code)
        assert r_alice.status_code == 200, (auth_mode, r_alice.text)


async def test_schemas_structured_output_and_errors():
    async with inproc() as app:
        async with mcp_client(URL, mint_token(CFG, "alice"), asgi_app=app) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            rq = tools["run_query"]
            props = rq.input_schema["properties"]
            assert props["sql"]["description"] == "A single read-only SELECT statement"
            assert props["sql"]["minLength"] == 1
            assert props["limit"] == {"default": 100, "description": "Max rows to return", "maximum": 1000, "minimum": 1, "title": "Limit", "type": "integer"}
            assert rq.input_schema["required"] == ["sql"]
            assert set(rq.output_schema["properties"]) == {"source", "columns", "rows", "row_count"}
            assert rq.annotations.read_only_hint is True
            assert rq.description == "Execute a read-only SQL query against the data source."
            assert tools["add"].output_schema["properties"]["result"]["type"] == "integer"
            assert "ctx" not in tools["whoami"].input_schema.get("properties", {})

            ok = await c.call_tool("run_query", {"sql": "select 1", "limit": 2})
            assert ok.is_error is False
            assert ok.content[0].text == "2 rows from pg"
            assert ok.structured_content == {"source": "pg", "columns": ["caller", "n"], "rows": [["alice", 0], ["alice", 1]], "row_count": 2}

            add = await c.call_tool("add", {"a": 2, "b": 3})
            assert add.structured_content == {"result": 5} and add.content[0].text == "5"

            tool_err = await c.call_tool("run_query", {"sql": "delete from t"})
            assert tool_err.is_error is True
            assert tool_err.content[0].text == "Error executing tool run_query: Only SELECT statements are allowed"
            assert tool_err.structured_content is None

            crash = await c.call_tool("run_query", {"sql": "select boom"})
            assert crash.is_error is True
            assert crash.content[0].text == "Error executing tool run_query"  # no secret leak

            invalid = await c.call_tool("run_query", {"sql": "select 1", "limit": 0})
            assert invalid.is_error is True
            print("validation error text:", repr(invalid.content[0].text))

            unknown = await c.call_tool("no_such_tool", {})
            print("unknown tool ->", unknown.is_error, unknown.content[0].text)
            assert unknown.is_error is True


async def test_real_uvicorn_ephemeral_port_both_modes():
    for stateless in (True, False):
        mcp, app = build_app("pg", CFG, stateless=stateless, json_response=False)
        async with uvicorn_server(app) as base:
            async with httpx2.AsyncClient() as h:
                assert (await h.get(f"{base}/healthz")).json() == {"status": "ok", "source": "pg"}
            for mode in ("auto", "legacy"):
                async with mcp_client(f"{base}/mcp", mint_token(CFG, "carol"), mode=mode) as c:
                    r = await c.call_tool("whoami", {})
                    assert r.structured_content["sub"] == "carol"


async def test_mounted_under_prefix():
    mcp, app = build_mounted_app("pg", CFG, prefix="/sources/pg")
    async with uvicorn_server(app) as base:
        async with httpx2.AsyncClient() as h:
            assert (await h.get(f"{base}/sources/pg/healthz")).status_code == 200
            # trailing-slash trap: /sources/pg/mcp/ is a 307 redirect (Starlette), client won't follow cross-path POST safely
            r = await h.post(f"{base}/sources/pg/mcp/", json=INIT, headers=ACCEPT)
            print("trailing slash status:", r.status_code)
        async with mcp_client(f"{base}/sources/pg/mcp", mint_token(CFG, "dave")) as c:
            assert (await c.call_tool("whoami", {})).structured_content["sub"] == "dave"


async def test_host_header_protection_default_vs_explicit():
    from mcp.server.mcpserver import MCPServer

    tok = {"Authorization": f"Bearer {mint_token(CFG, 'x')}"}
    # 1) SDK default (no transport_security, host="127.0.0.1"): only 127.0.0.1:<port>/localhost:<port>/[::1]:<port>
    m = MCPServer("d")
    default_app = m.streamable_http_app()
    async with m.session_manager.run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=default_app)) as h:
            r_svc = await h.post("http://pg-mcp:8000/mcp", json=INIT, headers=ACCEPT)
            r_noport = await h.post("http://127.0.0.1/mcp", json=INIT, headers=ACCEPT)
            r_ok = await h.post("http://127.0.0.1:8000/mcp", json=INIT, headers=ACCEPT)
    assert r_svc.status_code == 421 and r_noport.status_code == 421 and r_ok.status_code == 200
    # 2) explicit allowlist
    async with inproc(allowed_hosts=["pg-mcp:8000", "127.0.0.1:*"]) as app:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as h:
            assert (await h.post("http://pg-mcp:8000/mcp", json=INIT, headers={**ACCEPT, **tok})).status_code == 200
            assert (await h.post("http://evil:8000/mcp", json=INIT, headers={**ACCEPT, **tok})).status_code == 421


async def test_accept_and_content_type_requirements():
    tok = {"Authorization": f"Bearer {mint_token(CFG, 'x')}"}
    async with inproc(json_response=False) as app:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as h:
            only_json = await h.post("/mcp", json=INIT, headers={**tok, "Accept": "application/json"})
            no_ct = await h.post("/mcp", content=b"{}", headers={**tok, "Accept": ACCEPT["Accept"], "Content-Type": "text/plain"})
            both = await h.post("/mcp", json=INIT, headers={**tok, **ACCEPT})
    print("accept json only (sse mode):", only_json.status_code, "| bad content-type:", no_ct.status_code, "| ok:", both.status_code, both.headers.get("content-type"))
    assert only_json.status_code == 406 and no_ct.status_code == 400 and both.status_code == 200
