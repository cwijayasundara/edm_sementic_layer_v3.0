"""Model access behind one protocol: the Anthropic Messages API (Bedrock later = another implementation) and a
scripted fake for tests. The system prompt is sent as [stable (cache breakpoint), dynamic]; the last tool carries the
second breakpoint, so the fixed tool list + frozen prompt stay byte-stable and cacheable."""
import itertools
from collections.abc import Callable
from typing import Protocol

import anthropic
import httpx2

from prism.agent.types import (Message, ModelRequest, ModelResponse, RawBlock, TextBlock, ToolResult, ToolUse,
                               Usage)

CACHE = {"type": "ephemeral"}
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
DETAIL_CHARS = 300


class ModelError(Exception):
    """str() carries no provider text. `status`, `error_type`, `detail` (the provider's error message, truncated;
    it never holds the API key or the caller's token) and `retry_after_s` are for the log and the retry policy."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None, error_type: str | None = None,
                 detail: str = "", retry_after_s: float | None = None):
        super().__init__(message)
        self.retryable, self.status, self.error_type = retryable, status, error_type
        self.detail, self.retry_after_s = detail[:DETAIL_CHARS], retry_after_s


class ModelTruncated(ModelError):
    """The model ran out of max_tokens before finishing (thinking tokens count): not a usable answer."""

    def __init__(self):
        super().__init__("model output truncated at max_tokens", retryable=False)


class ModelClient(Protocol):
    async def create(self, request: ModelRequest) -> ModelResponse: ...


def reply_text(text: str, usage: Usage | None = None) -> ModelResponse:
    return ModelResponse((TextBlock(text),), "end_turn", usage or Usage())


def reply_tools(*calls: tuple[str, dict], usage: Usage | None = None) -> ModelResponse:
    ids = itertools.count(1)
    return ModelResponse(tuple(ToolUse(f"toolu_{next(ids)}", n, i) for n, i in calls), "tool_use", usage or Usage())


class ScriptedModelClient:
    """Deterministic fake: each create() consumes the next item (a ModelResponse or a callable(request))."""

    def __init__(self, script: list[ModelResponse | Callable[[ModelRequest], ModelResponse]]):
        self._script = list(script)
        self.requests: list[ModelRequest] = []

    async def create(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        assert self._script, "script exhausted"
        item = self._script.pop(0)
        return item(request) if callable(item) else item


def _block_out(b) -> dict:
    if isinstance(b, RawBlock):
        return b.raw
    if isinstance(b, TextBlock):
        return {"type": "text", "text": b.text}
    if isinstance(b, ToolUse):
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    assert isinstance(b, ToolResult)
    return {"type": "tool_result", "tool_use_id": b.tool_use_id, "content": b.content,
            **({"is_error": True} if b.is_error else {})}


def _message_out(m: Message) -> dict:
    # the API rejects empty/whitespace-only text blocks
    return {"role": m.role, "content": [_block_out(b) for b in m.content
                                        if not (isinstance(b, TextBlock) and not b.text.strip())]}


def _error_from(exc: anthropic.APIStatusError) -> ModelError:
    body = exc.body if isinstance(exc.body, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else body
    etype, msg = err.get("type"), err.get("message")
    detail = " ".join(x for x in (etype if isinstance(etype, str) else "", msg if isinstance(msg, str) else "") if x)
    retry_after = None
    try:
        retry_after = float(exc.response.headers.get("retry-after", ""))
    except (TypeError, ValueError):
        pass
    return ModelError(f"model API error {exc.status_code}", retryable=exc.status_code in RETRYABLE_STATUS,
                      status=exc.status_code, error_type=etype if isinstance(etype, str) else None,
                      detail=detail, retry_after_s=retry_after)


class AnthropicModelClient:
    def __init__(self, api_key: str, *, http_client: httpx2.AsyncClient | None = None, timeout_s: float = 60.0):
        self._client = anthropic.AsyncAnthropic(api_key=api_key, http_client=http_client, timeout=timeout_s,
                                                max_retries=0)

    async def create(self, request: ModelRequest) -> ModelResponse:
        tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                 for t in request.tools]
        if tools:
            tools[-1]["cache_control"] = CACHE
        messages = [_message_out(m) for m in request.messages]
        if messages and messages[-1]["content"] and messages[-1]["content"][-1]["type"] in ("text", "tool_result"):
            # third breakpoint: the history up to the newest result (copy; a replayed dict is never mutated)
            messages[-1]["content"][-1] = {**messages[-1]["content"][-1], "cache_control": CACHE}
        kwargs = {"model": request.model, "max_tokens": request.max_tokens,
                  "system": [{"type": "text", "text": request.stable_system, "cache_control": CACHE},
                             {"type": "text", "text": request.dynamic_system}],
                  "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if request.force_tool:
            kwargs["tool_choice"] = {"type": "tool", "name": request.force_tool}
        try:
            msg = await self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise _error_from(exc) from None
        except anthropic.APIConnectionError:
            raise ModelError("model API unreachable", retryable=True) from None
        out = []
        for b in msg.content:
            if b.type == "text":
                out.append(TextBlock(b.text))
            elif b.type == "tool_use":
                out.append(ToolUse(b.id, b.name, dict(b.input)))
            else:   # thinking, redacted_thinking, ...: replayed as-is on the next turn
                out.append(RawBlock(b.model_dump(mode="json", exclude_none=True)))
        u = msg.usage
        return ModelResponse(tuple(out), msg.stop_reason or "end_turn", Usage(
            u.input_tokens, u.output_tokens, getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0))
