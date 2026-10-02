import json

import httpx
import jwt
import pytest

from prism.config import Settings
from prism.evals.client import AgentClient, AgentNotReady, ensure_loopback, mint_case_token, parse_sse

RID = "0b8f3c1e-2d4a-4c6b-9e7f-1a2b3c4d5e6f"


def frame(o: dict) -> str:
    return f"event: {o['type']}\ndata: {json.dumps(o)}\n\n"


BODY = "".join(frame(e) for e in (
    {"type": "plan", "tool": "run_metric", "label": "metric open_breaks"},
    {"type": "widget", "widget": {"id": "w1", "type": "bar", "title": "T", "handle": "r_aaaaaaaaaaaa",
                                  "encoding": {}}, "handle_info": {"metric_id": "open_breaks"}},
    {"type": "summary", "text": "EMEA leads."},
    {"type": "answer", "record_id": RID, "confirmable": True},
    {"type": "telemetry", "cost_usd": 0.02}))


def test_parse_sse_handles_crlf_garbage_and_a_truncated_tail():
    text = BODY.replace("\n", "\r\n") + ": keep-alive\n\nevent: x\ndata: {not json\n\nevent: summary\ndata: {\"ty"
    events = parse_sse(text)
    assert [e["type"] for e in events] == ["plan", "widget", "summary", "answer", "telemetry"]


@pytest.mark.parametrize("url", ["http://10.0.0.5:8000", "https://example.com", "ftp://127.0.0.1", "127.0.0.1:8000"])
def test_ensure_loopback_refuses_other_hosts(url):
    with pytest.raises(ValueError):
        ensure_loopback(url)


def test_ensure_loopback_accepts_local():
    for url in ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8200"):
        ensure_loopback(url)


def test_mint_case_token_has_a_fresh_eval_sub_and_the_persona_claims():
    s = Settings()
    sub1, tok = mint_case_token(s, "cash_ops_emea")
    sub2, _ = mint_case_token(s, "cash_ops_emea")
    claims = jwt.decode(tok, s.jwt_secret.get_secret_value(), algorithms=["HS256"], audience="gateway-mcp")
    assert sub1 != sub2 and sub1.startswith("eval-") and len(sub1) == 17
    assert claims["sub"] == sub1 and claims["roles"] == ["cash_ops_emea"]


def _client(handler) -> AgentClient:
    return AgentClient("http://127.0.0.1:8000", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_chat_collects_events_and_timing():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer tok" and json.loads(req.content) == {"question": "q?"}
        return httpx.Response(200, text=BODY, headers={"content-type": "text/event-stream"})
    async with _client(handler) as agent:
        out = await agent.chat("tok", "q?")
    assert out.summary == "EMEA leads." and out.answer["record_id"] == RID and out.handles == ["r_aaaaaaaaaaaa"]
    assert out.cost_usd == 0.02 and out.error is None and out.seconds >= 0


@pytest.mark.parametrize("status", [401, 503, 500])
async def test_chat_refusals_abort_the_run(status):
    async with _client(lambda req: httpx.Response(status, json={"detail": "x"})) as agent:
        with pytest.raises(AgentNotReady):
            await agent.chat("tok", "q?")


async def test_a_dropped_stream_becomes_an_error_result():
    def handler(req):
        raise httpx.ReadError("boom")
    async with _client(handler) as agent:
        out = await agent.chat("tok", "q?")
    assert out.error == {"type": "error", "code": "transport_error", "message": "the stream ended early"}


async def test_table_pages_until_row_count_and_caps():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("r_bbbbbbbbbbbb"):
            return httpx.Response(404, json={"detail": "not found"})
        offset, limit = int(req.url.params["offset"]), int(req.url.params["limit"])
        rows = [[f"k{i}", i] for i in range(offset, min(offset + limit, 450))]
        return httpx.Response(200, json={"handle": "r_aaaaaaaaaaaa", "columns": ["k", "value"], "offset": offset,
                                         "row_count": 450, "rows": rows})
    async with _client(handler) as agent:
        full = await agent.table("tok", "r_aaaaaaaaaaaa")
        capped = await agent.table("tok", "r_aaaaaaaaaaaa", max_rows=300)
        gone = await agent.table("tok", "r_bbbbbbbbbbbb")
    assert len(full.rows) == 450 and not full.truncated and full.columns == ["k", "value"]
    assert len(capped.rows) == 400 and capped.truncated      # whole pages: stops at the first page past the cap
    assert gone is None


async def test_healthy():
    async with _client(lambda req: httpx.Response(200, json={"status": "ok", "service": "agent"})) as agent:
        assert await agent.healthy() is True

    def down(req):
        raise httpx.ConnectError("refused")
    async with _client(down) as agent:
        assert await agent.healthy() is False
