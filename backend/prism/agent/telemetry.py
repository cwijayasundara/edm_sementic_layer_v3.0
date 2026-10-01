"""Per-run telemetry: a cost estimate and a best-effort app.agent_runs row (question hashed, never stored)."""
import asyncio
import logging

from prism.agent.state import RunState
from prism.gateway.audit import question_hash

log = logging.getLogger(__name__)

# model -> (input, output, cache_read) USD per 1M tokens; estimates for the dev panel, not billing
PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "claude-sonnet-5-5": (3.0, 15.0, 0.3),
    "claude-opus-5-5": (5.0, 25.0, 0.5),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.1),
}

_INSERT = (
    "INSERT INTO app.agent_runs (run_id, sub, question_hash, path, models, input_tokens, output_tokens, "
    "cache_read_input_tokens, llm_turns, tool_calls, tool_latency_ms, cost_usd, status, error_code) "
    "VALUES (%(run_id)s, %(sub)s, %(question_hash)s, %(path)s, %(models)s, %(input_tokens)s, %(output_tokens)s, "
    "%(cache_read_input_tokens)s, %(llm_turns)s, %(tool_calls)s, %(tool_latency_ms)s, %(cost_usd)s, %(status)s, "
    "%(error_code)s)"
)


def estimate_cost(by_model: dict[str, dict]) -> float:
    total = 0.0
    for model, use in by_model.items():
        price_in, price_out, price_cache = PRICES_PER_MTOK.get(model, (0.0, 0.0, 0.0))
        total += (use.get("input", 0) * price_in + use.get("output", 0) * price_out
                  + use.get("cache_read", 0) * price_cache) / 1_000_000
    return total


class AgentRunWriter:
    def __init__(self, pool, *, hmac_key: str, timeout_s: float = 2.0):
        self._pool, self._key, self._timeout = pool, hmac_key, timeout_s

    async def write(self, state: RunState, *, path: str | None, status: str) -> bool:
        """Insert one row; never raises. True if inserted."""
        try:
            m = state.meter
            params = {"run_id": state.run_id, "sub": state.sub,
                      "question_hash": question_hash(state.question, self._key), "path": path,
                      "models": list(m.models), "input_tokens": m.input_tokens, "output_tokens": m.output_tokens,
                      "cache_read_input_tokens": m.cache_read_input_tokens, "llm_turns": m.llm_turns,
                      "tool_calls": m.tool_calls, "tool_latency_ms": m.tool_latency_ms,
                      "cost_usd": estimate_cost(m.by_model), "status": status, "error_code": state.error_code}
            async with asyncio.timeout(self._timeout):
                async with self._pool.connection(timeout=self._timeout) as conn:
                    await conn.execute(_INSERT, params)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - telemetry must never break a run
            log.warning("agent_runs write failed: %s", type(exc).__name__)
            return False
