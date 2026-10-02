import asyncio

import pytest

from prism.agent.model import ModelError, ModelTruncated, ScriptedModelClient, reply_text, reply_tools
from prism.agent.runner import MessagesRunner, RunLimitExceeded, RunLimits, ToolOutcome
from prism.agent.state import RunState, UsageMeter
from prism.agent.types import ModelResponse, TextBlock, ToolDef, ToolResult, Usage

TOOLS = (ToolDef("echo", "d", {"type": "object", "properties": {}}),)
LIMITS = RunLimits(max_turns=4, max_tool_calls=6, wall_clock_s=5)


def runner(client, handler, **kw):
    return MessagesRunner(client, model="m", stable_system="S", dynamic_system="D", tools=TOOLS, handler=handler,
                          limits=kw.pop("limits", LIMITS), meter=kw.pop("meter", UsageMeter()), **kw)


async def test_loop_executes_tools_in_parallel_and_returns_final_text():
    started = []

    async def handler(name, args):
        started.append(args["i"])
        await asyncio.sleep(0.05)
        return ToolOutcome(f"r{args['i']}")

    meter = UsageMeter()
    client = ScriptedModelClient([reply_tools(("echo", {"i": 1}), ("echo", {"i": 2}), usage=Usage(10, 5, 4)),
                                  reply_text("final", usage=Usage(20, 6, 8))])
    t0 = asyncio.get_event_loop().time()
    res = await runner(client, handler, meter=meter).run("question")
    assert asyncio.get_event_loop().time() - t0 < 0.09      # parallel, not 0.10
    assert res.text == "final" and meter.llm_turns == 2 and meter.tool_calls == 2
    assert (meter.input_tokens, meter.output_tokens, meter.cache_read_input_tokens) == (30, 11, 12)
    results = client.requests[1].messages[-1].content
    assert [r.content for r in results] == ["r1", "r2"] and all(isinstance(r, ToolResult) for r in results)


async def test_tool_exception_becomes_an_error_result_not_a_crash():
    async def handler(name, args):
        raise RuntimeError("db password=hunter2")

    client = ScriptedModelClient([reply_tools(("echo", {})), reply_text("sorry")])
    res = await runner(client, handler).run("q")
    result = client.requests[1].messages[-1].content[0]
    assert result.is_error and "hunter2" not in result.content and res.text == "sorry"


async def test_turn_cap():
    async def handler(name, args):
        return ToolOutcome("x")

    client = ScriptedModelClient([reply_tools(("echo", {}))] * 10)
    with pytest.raises(RunLimitExceeded) as e:
        await runner(client, handler, limits=RunLimits(3, 99, 5)).run("q")
    assert e.value.reason == "turns"


async def test_tool_call_cap_and_wall_clock():
    async def handler(name, args):
        await asyncio.sleep(1)
        return ToolOutcome("x")

    with pytest.raises(RunLimitExceeded) as e:
        await runner(ScriptedModelClient([reply_tools(("echo", {}))] * 5), handler,
                     limits=RunLimits(9, 99, 0.1)).run("q")
    assert e.value.reason == "wall_clock"

    async def fast(name, args):
        return ToolOutcome("x")

    with pytest.raises(RunLimitExceeded) as e:
        await runner(ScriptedModelClient([reply_tools(("echo", {}), ("echo", {}), ("echo", {}))] * 3), fast,
                     limits=RunLimits(9, 4, 5)).run("q")
    assert e.value.reason == "tool_calls"


async def test_long_tool_output_is_truncated():
    async def handler(name, args):
        return ToolOutcome("y" * 50_000)

    client = ScriptedModelClient([reply_tools(("echo", {})), reply_text("ok")])
    await runner(client, handler).run("q")
    assert len(client.requests[1].messages[-1].content[0].content) <= 12_100


async def test_force_tool_first_request_only_and_retryable_model_error_retries_once():
    async def handler(name, args):
        return ToolOutcome("x")

    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ModelError("busy", retryable=True)
        return reply_tools(("echo", {}))

    client = ScriptedModelClient([flaky, flaky, reply_text("done")])
    res = await runner(client, handler, force_tool="echo", sleep=lambda s: asyncio.sleep(0)).run("q")
    assert res.text == "done"
    assert client.requests[0].force_tool == "echo" and client.requests[-1].force_tool is None


async def test_non_retryable_model_error_is_not_retried():
    async def handler(name, args):
        return ToolOutcome("x")

    client = ScriptedModelClient([lambda req: (_ for _ in ()).throw(ModelError("bad", retryable=False))])
    with pytest.raises(ModelError):
        await runner(client, handler).run("q")
    assert len(client.requests) == 1


def test_usage_meter_tracks_per_model_totals_and_run_state_emits():
    m = UsageMeter()
    m.add("a", Usage(10, 5, 4))
    m.add("b", Usage(1, 2, 3))
    m.add("a", Usage(10, 5, 4))
    assert m.models == ["a", "b"] and m.llm_turns == 3
    assert m.by_model["a"] == {"input": 20, "output": 10, "cache_read": 8}
    state = RunState(run_id="r", sub="s", question="q", meter=m)
    state.emit("status", text="hi")
    assert state.events.get_nowait() == {"type": "status", "text": "hi"}


async def test_upstream_timeout_is_not_reported_as_a_wall_clock_cap():
    async def handler(name, args):
        return ToolOutcome("x")

    def boom(req):
        raise TimeoutError("upstream")

    with pytest.raises(TimeoutError) as e:
        await runner(ScriptedModelClient([boom]), handler).run("q")
    assert not isinstance(e.value, RunLimitExceeded)


async def _noop(name, args):
    return ToolOutcome("x")


async def test_model_retries_twice_with_exponential_backoff_then_gives_up():
    delays = []

    async def sleep(s):
        delays.append(s)

    def busy(req):
        raise ModelError("busy", retryable=True, status=529)

    client = ScriptedModelClient([busy, busy, busy, reply_text("never")])
    with pytest.raises(ModelError):
        await runner(client, _noop, sleep=sleep).run("q")
    assert len(client.requests) == 3 and len(delays) == 2
    assert 0.5 <= delays[0] <= 0.5 * 1.25 and 1.0 <= delays[1] <= 1.0 * 1.25


async def test_retry_after_is_honoured_and_recovers():
    delays = []

    async def sleep(s):
        delays.append(s)

    def limited(req):
        raise ModelError("slow down", retryable=True, status=429, retry_after_s=3.0)

    res = await runner(ScriptedModelClient([limited, reply_text("fine")]), _noop, sleep=sleep).run("q")
    assert res.text == "fine" and delays == [3.0]


def _stop(stop_reason, *content):
    return ModelResponse(tuple(content), stop_reason, Usage())


async def test_max_tokens_without_tool_use_is_a_truncation_error_not_an_empty_answer():
    client = ScriptedModelClient([_stop("max_tokens", TextBlock("partial"))])
    with pytest.raises(ModelTruncated):
        await runner(client, _noop).run("q")


async def test_refusal_stop_reason_gives_a_fixed_final_text():
    res = await runner(ScriptedModelClient([_stop("refusal")]), _noop).run("q")
    assert res.text == "I can't help with that request."


async def test_pause_turn_without_tool_use_ends_the_run_with_its_text():
    res = await runner(ScriptedModelClient([_stop("pause_turn", TextBlock("so far"))]), _noop).run("q")
    assert res.text == "so far"


async def test_raw_blocks_stay_in_the_assistant_message_and_are_never_text():
    from prism.agent.types import RawBlock, ToolUse
    raw = RawBlock({"type": "thinking", "thinking": "t", "signature": "s"})
    client = ScriptedModelClient([_stop("tool_use", raw, ToolUse("toolu_1", "echo", {})),
                                  _stop("end_turn", raw, TextBlock("done"))])
    res = await runner(client, _noop).run("q")
    assert res.text == "done" and client.requests[1].messages[1].content[0] is raw


async def test_per_tool_cap_overrides_the_default():
    async def handler(name, args):
        return ToolOutcome("y" * 30_000, max_chars=20_000)

    client = ScriptedModelClient([reply_tools(("echo", {})), reply_text("ok")])
    await runner(client, handler).run("q")
    n = len(client.requests[1].messages[-1].content[0].content)
    assert 20_000 < n <= 20_100


def test_default_max_tokens_is_16000():
    from prism.agent.types import ModelRequest
    assert ModelRequest("m", "s", "d", (), ()).max_tokens == 16000


async def test_observe_receives_text_written_before_tool_calls():
    seen = []

    async def handler(name, args):
        return ToolOutcome("r")

    first = reply_tools(("echo", {"i": 1}), ("echo", {"i": 2}))
    first = ModelResponse((TextBlock("I will look up the metric."), *first.content), first.stop_reason, first.usage)
    client = ScriptedModelClient([first, reply_text("final")])
    res = await runner(client, handler, observe=seen.append).run("question")
    assert res.text == "final"
    assert seen == ["I will look up the metric."]
