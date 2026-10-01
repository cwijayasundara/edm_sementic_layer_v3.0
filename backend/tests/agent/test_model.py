import json
from dataclasses import replace

import httpx2
import pytest

from prism.agent.model import (AnthropicModelClient, ModelError, ScriptedModelClient, reply_text, reply_tools)
from prism.agent.types import Message, ModelRequest, RawBlock, TextBlock, ToolDef, ToolResult, ToolUse

REQ = ModelRequest(model="claude-sonnet-5-5", stable_system="STABLE", dynamic_system="DYN",
                   messages=(Message("user", (TextBlock("hi"),)),),
                   tools=(ToolDef("t1", "d", {"type": "object", "properties": {}}),))


async def test_scripted_returns_in_order_and_records_requests():
    c = ScriptedModelClient([reply_tools(("t1", {"a": 1})), reply_text("done")])
    r1 = await c.create(REQ)
    assert r1.content[0] == ToolUse(id="toolu_1", name="t1", input={"a": 1}) and r1.stop_reason == "tool_use"
    r2 = await c.create(REQ)
    assert r2.content[0] == TextBlock("done") and r2.stop_reason == "end_turn"
    assert c.requests == [REQ, REQ]
    with pytest.raises(AssertionError, match="script exhausted"):
        await c.create(REQ)


async def test_scripted_callable_sees_the_request():
    c = ScriptedModelClient([lambda req: reply_text(req.dynamic_system)])
    assert (await c.create(REQ)).content[0] == TextBlock("DYN")


def _client(handler):
    return AnthropicModelClient("sk-test", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))


async def test_anthropic_request_shape_cache_breakpoints_and_parsing():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers["x-api-key"]
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "ok"},
                        {"type": "tool_use", "id": "toolu_9", "name": "t1", "input": {"x": 2}}],
            "stop_reason": "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7,
                      "cache_creation_input_tokens": 3}})

    req = replace(REQ, force_tool="t1",
                  messages=(Message("user", (TextBlock("hi"),)),
                            Message("assistant", (ToolUse("toolu_1", "t1", {}),)),
                            Message("user", (ToolResult("toolu_1", "res", is_error=True),))))
    resp = await _client(handler).create(req)
    body = seen["body"]
    assert seen["key"] == "sk-test" and body["model"] == "claude-sonnet-5-5"
    assert body["system"][0] == {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}}
    assert body["system"][1] == {"type": "text", "text": "DYN"}      # dynamic part after the breakpoint
    assert body["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert body["tool_choice"] == {"type": "tool", "name": "t1"}
    assert body["messages"][2]["content"][0] == {"type": "tool_result", "tool_use_id": "toolu_1",
                                                 "content": "res", "is_error": True,
                                                 "cache_control": {"type": "ephemeral"}}   # message-history breakpoint
    assert body["messages"][0]["content"][0] == {"type": "text", "text": "hi"}               # only the last block
    assert json.dumps(body).count("cache_control") == 3                                       # max 4 allowed
    assert resp.content == (TextBlock("ok"), ToolUse("toolu_9", "t1", {"x": 2}))
    assert (resp.usage.input_tokens, resp.usage.cache_read_input_tokens) == (10, 7)


@pytest.mark.parametrize("status,retryable", [(408, True), (409, True), (429, True), (500, True), (502, True), (503, True),
                                              (504, True), (529, True), (400, False), (401, False)])
async def test_anthropic_errors_become_model_errors(status, retryable):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "secret detail"}})

    with pytest.raises(ModelError) as e:
        await _client(handler).create(REQ)
    assert e.value.retryable is retryable
    assert "secret detail" not in str(e.value) and "sk-test" not in str(e.value)


def _ok(content, stop="end_turn"):
    return httpx2.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5", "content": content,
        "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})


async def test_error_detail_is_kept_for_the_log_and_never_holds_secrets(caplog):
    def handler(request):
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                     "message": "bad request: foo" + "x" * 500}},
                               headers={"retry-after": "7"})

    req = replace(REQ, messages=(Message("user", (TextBlock("tok-jwt-secret"),)),))
    with pytest.raises(ModelError) as e:
        await _client(handler).create(req)
    err = e.value
    assert (err.status, err.error_type, err.retry_after_s) == (400, "invalid_request_error", 7.0)
    assert err.detail.startswith("invalid_request_error bad request: foo") and len(err.detail) == 300
    assert "foo" not in str(err) and str(err) == "model API error 400"
    assert "sk-test" not in repr(vars(err)) and "tok-jwt-secret" not in repr(vars(err))


async def test_api_connection_error_is_retryable_without_detail():
    def handler(request):
        raise httpx2.ConnectError("boom sk-test", request=request)

    with pytest.raises(ModelError) as e:
        await _client(handler).create(REQ)
    assert e.value.retryable is True and e.value.status is None and "sk-test" not in str(e.value)
    assert "sk-test" not in e.value.detail


async def test_thinking_blocks_round_trip_unchanged_and_in_order():
    thinking = {"type": "thinking", "thinking": "hmm", "signature": "SIG=="}
    redacted = {"type": "redacted_thinking", "data": "ENC"}
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok([thinking, redacted, {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "toolu_1", "name": "t1", "input": {"a": 1}}], "tool_use")

    client = _client(handler)
    resp = await client.create(REQ)
    assert resp.content[:2] == (RawBlock(thinking), RawBlock(redacted))
    assert not any(isinstance(b, TextBlock) for b in resp.content[:2])
    follow = replace(REQ, messages=(*REQ.messages, Message("assistant", resp.content),
                                    Message("user", (ToolResult("toolu_1", "r"),))))
    await client.create(follow)
    sent = bodies[1]["messages"][1]["content"]
    assert [json.dumps(b, sort_keys=True) for b in sent[:2]] == [json.dumps(thinking, sort_keys=True),
                                                                 json.dumps(redacted, sort_keys=True)]
    assert [b["type"] for b in sent] == ["thinking", "redacted_thinking", "text", "tool_use"]


async def test_empty_text_blocks_are_not_replayed():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ok([{"type": "text", "text": "ok"}])

    req = replace(REQ, messages=(*REQ.messages, Message("assistant", (TextBlock(""), TextBlock("  "),
                                                                      ToolUse("toolu_1", "t1", {}))),
                                 Message("user", (ToolResult("toolu_1", "r"),))))
    await _client(handler).create(req)
    assert [b["type"] for b in bodies[0]["messages"][1]["content"]] == ["tool_use"]
