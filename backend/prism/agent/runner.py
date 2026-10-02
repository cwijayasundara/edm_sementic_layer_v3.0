"""The model/tool loop: one user message in, tool calls executed in parallel, a final text out, hard caps on
turns, tool calls and wall-clock time."""
import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from prism.agent.model import ModelClient, ModelError, ModelTruncated
from prism.agent.state import UsageMeter
from prism.agent.types import Message, ModelRequest, ModelResponse, TextBlock, ToolDef, ToolResult, ToolUse

log = logging.getLogger("prism.agent")
MAX_TOOL_CHARS = 12_000
RETRY_DELAY_S = 0.5   # first back-off; doubles per retry, plus up to 25% jitter
MODEL_RETRIES = 2
MAX_RETRY_AFTER_S = 30.0
REFUSAL_TEXT = "I can't help with that request."


def retry_delay(attempt: int, exc: ModelError) -> float:
    """Back-off before retry `attempt` (0-based): the provider's retry-after when it sent one, else 0.5 s, 1 s."""
    if exc.retry_after_s is not None:
        return min(max(exc.retry_after_s, 0.0), MAX_RETRY_AFTER_S)
    return RETRY_DELAY_S * 2 ** attempt * (1 + random.random() * 0.25)


@dataclass(frozen=True)
class ToolOutcome:
    content: str
    is_error: bool = False
    max_chars: int | None = None   # per-tool cap on the content sent back to the model (default MAX_TOOL_CHARS)


ToolHandler = Callable[[str, dict], Awaitable[ToolOutcome]]


@dataclass(frozen=True)
class RunLimits:
    max_turns: int
    max_tool_calls: int
    wall_clock_s: float


class RunLimitExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason   # "turns" | "tool_calls" | "wall_clock"


@dataclass
class RunResult:
    text: str
    messages: tuple[Message, ...]


class Runner(Protocol):
    async def run(self, user_text: str) -> RunResult: ...


class MessagesRunner:
    def __init__(self, client: ModelClient, *, model: str, stable_system: str, dynamic_system: str,
                 tools: tuple[ToolDef, ...], handler: ToolHandler, limits: RunLimits, meter: UsageMeter,
                 max_tokens: int = 16000, force_tool: str | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 observe: Callable[[str], None] | None = None):
        self._observe = observe
        self._client, self._model = client, model
        self._stable, self._dynamic = stable_system, dynamic_system
        self._tools, self._handler, self._limits, self._meter = tools, handler, limits, meter
        self._max_tokens, self._force_tool, self._sleep = max_tokens, force_tool, sleep

    async def run(self, user_text: str) -> RunResult:
        messages = [Message("user", (TextBlock(user_text),))]
        try:
            async with asyncio.timeout(self._limits.wall_clock_s) as cm:
                return await self._loop(messages)
        except TimeoutError:
            if cm.expired():   # only our own deadline is a cap hit; an upstream timeout propagates unchanged
                raise RunLimitExceeded("wall_clock") from None
            raise

    async def _loop(self, messages: list[Message]) -> RunResult:
        calls = 0
        for turn in range(self._limits.max_turns):
            response = await self._create(messages, first=(turn == 0))
            messages.append(Message("assistant", response.content))
            uses = [b for b in response.content if isinstance(b, ToolUse)]
            if uses and self._observe is not None:
                note = "".join(b.text for b in response.content if isinstance(b, TextBlock)).strip()
                if note:
                    self._observe(note)
            if not uses:
                # end_turn, stop_sequence and pause_turn (no server tools are configured) all end the run as-is
                if response.stop_reason == "max_tokens":
                    raise ModelTruncated
                if response.stop_reason == "refusal":
                    return RunResult(REFUSAL_TEXT, tuple(messages))
                text = "".join(b.text for b in response.content if isinstance(b, TextBlock))
                return RunResult(text, tuple(messages))
            calls += len(uses)
            if calls > self._limits.max_tool_calls:
                raise RunLimitExceeded("tool_calls")
            outcomes = await asyncio.gather(*(self._exec(u) for u in uses))
            messages.append(Message("user", tuple(ToolResult(u.id, o.content, o.is_error)
                                                  for u, o in zip(uses, outcomes))))
        raise RunLimitExceeded("turns")

    async def _create(self, messages: list[Message], *, first: bool) -> ModelResponse:
        request = ModelRequest(model=self._model, stable_system=self._stable, dynamic_system=self._dynamic,
                               messages=tuple(messages), tools=self._tools, max_tokens=self._max_tokens,
                               force_tool=self._force_tool if first else None)
        for attempt in range(MODEL_RETRIES + 1):
            try:
                response = await self._client.create(request)
                break
            except ModelError as exc:
                if not exc.retryable or attempt == MODEL_RETRIES:
                    raise
                await self._sleep(retry_delay(attempt, exc))
        self._meter.add(self._model, response.usage)
        return response

    async def _exec(self, use: ToolUse) -> ToolOutcome:
        start = time.monotonic()
        try:
            outcome = await self._handler(use.name, use.input)
        except Exception as exc:  # noqa: BLE001 - a tool failure is a result for the model, never a crash
            log.warning("tool %s failed: %s", use.name, type(exc).__name__)   # type only; messages can hold secrets
            if isinstance(exc, ModelError):   # the provider detail is safe to log (no key, no token)
                log.warning("tool %s model error: status=%s type=%s detail=%s", use.name, exc.status, exc.error_type,
                            exc.detail)
            outcome = ToolOutcome("the tool failed", True)
        finally:
            self._meter.add_tool((time.monotonic() - start) * 1000)
        cap = outcome.max_chars or MAX_TOOL_CHARS
        if len(outcome.content) > cap:
            outcome = ToolOutcome(outcome.content[:cap] + "…[truncated]", outcome.is_error)
        return outcome
