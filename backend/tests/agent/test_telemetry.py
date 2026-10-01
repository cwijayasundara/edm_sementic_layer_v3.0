import pytest

from prism.agent.state import RunState, UsageMeter
from prism.agent.telemetry import AgentRunWriter, PRICES_PER_MTOK, estimate_cost
from prism.agent.types import Usage


def test_estimate_cost_arithmetic():
    model = next(iter(PRICES_PER_MTOK))
    i, o, c = PRICES_PER_MTOK[model]
    got = estimate_cost({model: {"input": 1_000_000, "output": 2_000_000, "cache_read": 3_000_000}})
    assert got == pytest.approx(i + 2 * o + 3 * c)
    assert estimate_cost({"unknown-model": {"input": 5, "output": 5, "cache_read": 5}}) == 0.0


class FakePool:
    def __init__(self):
        self.rows = []

    def connection(self, timeout=None):
        pool = self

        class Ctx:
            async def __aenter__(s):
                class C:
                    async def execute(c, stmt, params):
                        pool.rows.append(params)
                return C()

            async def __aexit__(s, *a): ...

        return Ctx()


async def test_writer_stores_a_hash_not_the_question():
    pool = FakePool()
    state = RunState(run_id="run-1", sub="head_data", question="secret question text", meter=UsageMeter())
    state.meter.add("claude-sonnet-5-5", Usage(100, 20, 50))
    ok = await AgentRunWriter(pool, hmac_key="k" * 32).write(state, path="metric", status="ok")
    assert ok and len(pool.rows[0]["question_hash"]) == 64
    assert "secret question text" not in repr(pool.rows[0]) and pool.rows[0]["input_tokens"] == 100


async def test_writer_never_raises():
    class Boom:
        def connection(self, timeout=None):
            raise RuntimeError("db down")

    state = RunState(run_id="r", sub="s", question="q", meter=UsageMeter())
    assert await AgentRunWriter(Boom(), hmac_key="k" * 32).write(state, path=None, status="error") is False


async def test_writer_bounds_a_hung_insert():
    import asyncio

    class Slow(FakePool):
        def connection(self, timeout=None):
            class Ctx:
                async def __aenter__(s):
                    class C:
                        async def execute(c, stmt, params):
                            await asyncio.sleep(5)
                    return C()

                async def __aexit__(s, *a): ...
            return Ctx()

    state = RunState(run_id="r", sub="s", question="q", meter=UsageMeter())
    assert await AgentRunWriter(Slow(), hmac_key="k" * 32, timeout_s=0.05).write(state, path=None, status="ok") is False
