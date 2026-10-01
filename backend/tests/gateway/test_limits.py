"""Per-caller limits: the combine permit outlives a cancelled call (held until the DuckDB thread really finishes),
a per-sub cap and a bounded queue refuse with stable codes; search_context has per-sub concurrency and rate limits."""
import asyncio
import threading

import pytest

from prism.config import Settings
from prism.gateway import service as gateway_service
from prism.gateway.errors import GatewayError
from prism.gateway.limits import Limiter
from prism.security.personas import claims_for
from tests.gateway.test_server import FakeAudit, make_gateway, text


# ------------------------------------------------------------------------------------------------ Limiter
async def test_limiter_caps_per_sub_and_bounds_the_queue():
    lim = Limiter("combine", total=1, per_sub=1, queue=1)
    release_a = await lim.acquire("a")
    with pytest.raises(GatewayError) as same_sub:
        await lim.acquire("a")
    assert same_sub.value.code == "rate_limited"
    waiter = asyncio.create_task(lim.acquire("b"))   # queued: the one permit is held by "a"
    await asyncio.sleep(0)
    with pytest.raises(GatewayError) as full:
        await lim.acquire("c")
    assert full.value.code == "gateway_busy"
    release_a()
    release_b = await asyncio.wait_for(waiter, 1)
    assert lim.running == 1
    release_b()
    assert lim.running == 0 and lim.waiting == 0


async def test_a_cancelled_waiter_frees_its_queue_slot_and_sub_count():
    lim = Limiter("combine", total=1, per_sub=1, queue=1)
    release = await lim.acquire("a")
    waiter = asyncio.create_task(lim.acquire("b"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert lim.waiting == 0
    release()
    (await lim.acquire("b"))()   # "b" is not stuck at its cap


async def test_release_is_idempotent():
    lim = Limiter("x", total=1, per_sub=1, queue=0)
    release = await lim.acquire("a")
    release()
    release()
    assert lim.running == 0
    (await lim.acquire("a"))()


# ------------------------------------------------------------------------------------------------ combine (S1)
class SlowCombine:
    """Stands in for combine(): blocks its worker thread until released, counting concurrent threads."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gate = threading.Event()
        self.current = self.peak = self.started = 0

    def __call__(self, store, sub, sql, handles):
        with self.lock:
            self.current += 1
            self.started += 1
            self.peak = max(self.peak, self.current)
        try:
            self.gate.wait(10)
        finally:
            with self.lock:
                self.current -= 1
        return store.put(sub, ["a"], [[1]], {"source": "combine"})


async def _until(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_cancel_and_retry_never_exceeds_the_combine_thread_cap(fake_catalog, monkeypatch):
    slow = SlowCombine()
    monkeypatch.setattr(gateway_service, "combine_results", slow)
    audit = FakeAudit()
    gw = make_gateway(Settings(), fake_catalog, audit=audit)
    args = {"sql": "SELECT 1", "handles": {"t": "r_x"}}
    subs = [claims_for(p) for p in ("head_data", "cash_ops_emea", "invest_ops_growth")]
    try:
        for _ in range(3):   # cancel-and-retry x3, three callers each time
            tasks = [asyncio.create_task(gw.call_tool("combine", c, args)) for c in subs]
            await asyncio.sleep(0.1)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            assert slow.current <= gateway_service.COMBINE_CONCURRENCY
        assert slow.peak <= gateway_service.COMBINE_CONCURRENCY
        assert gw.combine_limiter.running == slow.current   # the permits are still held by the orphaned threads
    finally:
        slow.gate.set()
    await _until(lambda: gw.combine_limiter.running == 0)   # released from the thread's completion, not the awaiter
    assert slow.peak <= gateway_service.COMBINE_CONCURRENCY
    out = await gw.call_tool("combine", subs[0], args)
    assert not out.is_error


async def test_one_sub_gets_one_combine_at_a_time_and_a_full_queue_is_refused(fake_catalog, monkeypatch):
    slow = SlowCombine()
    monkeypatch.setattr(gateway_service, "combine_results", slow)
    audit = FakeAudit()
    gw = make_gateway(Settings(), fake_catalog, audit=audit)
    args = {"sql": "SELECT 1", "handles": {"t": "r_x"}}
    head = claims_for("head_data")
    try:
        first = asyncio.create_task(gw.call_tool("combine", head, args))
        await _until(lambda: slow.current == 1)
        second = await gw.call_tool("combine", head, args)
        assert second.is_error and text(second).startswith("Error executing tool combine: rate_limited")
        # fill every permit and the queue with other callers, then one more is refused outright
        others = [{**head, "sub": f"user-{i}"} for i in range(gateway_service.COMBINE_CONCURRENCY - 1
                                                               + gateway_service.COMBINE_QUEUE)]
        held = [asyncio.create_task(gw.call_tool("combine", c, args)) for c in others]
        await _until(lambda: gw.combine_limiter.waiting == gateway_service.COMBINE_QUEUE)
        busy = await gw.call_tool("combine", {**head, "sub": "user-late"}, args)
        assert busy.is_error and "gateway_busy" in text(busy)
    finally:
        slow.gate.set()
    results = await asyncio.gather(first, *held)
    assert all(not r.is_error for r in results)
    codes = [r["error_code"] for r in audit.rows]
    assert codes.count("rate_limited") == 1 and codes.count("gateway_busy") == 1


# ------------------------------------------------------------------------------------------------ search_context (S4)
class BlockingContext:
    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.in_flight = 0

    async def __call__(self, question, claims, k):
        self.in_flight += 1
        try:
            await self.gate.wait()
            return {"metrics": []}
        finally:
            self.in_flight -= 1


async def test_search_context_caps_concurrent_calls_per_caller(fake_catalog):
    ctx, audit = BlockingContext(), FakeAudit()
    gw = make_gateway(Settings(), fake_catalog, audit=audit, context=ctx)
    head, other = claims_for("head_data"), claims_for("cash_ops_emea")
    q = {"question": "open breaks"}
    held = [asyncio.create_task(gw.call_tool("search_context", head, q))
            for _ in range(gateway_service.CONTEXT_PER_SUB)]
    await _until(lambda: ctx.in_flight == gateway_service.CONTEXT_PER_SUB)
    refused = await gw.call_tool("search_context", head, q)
    assert refused.is_error and text(refused).startswith("Error executing tool search_context: rate_limited")
    assert ctx.in_flight == gateway_service.CONTEXT_PER_SUB          # refused before anything was embedded
    other_call = asyncio.create_task(gw.call_tool("search_context", other, q))   # another caller is not affected
    await _until(lambda: ctx.in_flight == gateway_service.CONTEXT_PER_SUB + 1)
    ctx.gate.set()
    assert all(not r.is_error for r in await asyncio.gather(*held, other_call))
    assert gw.context_limiter.running == 0
    assert [r["error_code"] for r in audit.rows].count("rate_limited") == 1


async def test_search_context_rate_limit_per_caller_refills_over_time(fake_catalog):
    now = [1000.0]
    gw = make_gateway(Settings(), fake_catalog)
    gw.context_limiter = Limiter("search_context", total=8, per_sub=2, queue=8, rate_per_min=60, burst=3,
                                 clock=lambda: now[0])
    head = claims_for("head_data")
    q = {"question": "open breaks"}
    ok = [await gw.call_tool("search_context", head, q) for _ in range(3)]
    limited = await gw.call_tool("search_context", head, q)
    other = await gw.call_tool("search_context", claims_for("cash_ops_emea"), q)
    now[0] += 1.0   # 60 a minute: one more token a second
    again = await gw.call_tool("search_context", head, q)
    assert all(not r.is_error for r in ok) and not other.is_error and not again.is_error
    assert limited.is_error and "rate_limited" in text(limited)


async def test_search_context_queue_is_bounded(fake_catalog):
    ctx = BlockingContext()
    gw = make_gateway(Settings(), fake_catalog, context=ctx)
    gw.context_limiter = Limiter("search_context", total=1, per_sub=1, queue=1)
    q = {"question": "open breaks"}
    running = asyncio.create_task(gw.call_tool("search_context", {**claims_for("head_data"), "sub": "a"}, q))
    await _until(lambda: ctx.in_flight == 1)
    queued = asyncio.create_task(gw.call_tool("search_context", {**claims_for("head_data"), "sub": "b"}, q))
    await _until(lambda: gw.context_limiter.waiting == 1)
    busy = await gw.call_tool("search_context", {**claims_for("head_data"), "sub": "c"}, q)
    assert busy.is_error and "gateway_busy" in text(busy)
    ctx.gate.set()
    assert all(not r.is_error for r in await asyncio.gather(running, queued))


async def test_a_timed_out_search_context_frees_its_permit(fake_catalog):
    async def hang(question, claims, k):
        await asyncio.sleep(60)

    gw = make_gateway(Settings(), fake_catalog, context=hang, context_timeout_s=0.05)
    head = claims_for("head_data")
    for _ in range(gateway_service.CONTEXT_PER_SUB + 1):
        r = await gw.call_tool("search_context", head, {"question": "q"})
        assert "context_unavailable" in text(r)
    assert gw.context_limiter.running == 0
