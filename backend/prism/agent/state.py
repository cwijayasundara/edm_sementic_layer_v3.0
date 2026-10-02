"""Per-run mutable state: usage metering and the event queue the service drains as SSE."""
import asyncio
from dataclasses import dataclass, field

from prism.agent.spec import DashboardSpec, HandleInfo
from prism.agent.types import Usage


@dataclass
class UsageMeter:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    llm_turns: int = 0
    tool_calls: int = 0
    tool_latency_ms: float = 0.0
    models: list[str] = field(default_factory=list)
    by_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, model: str, usage: Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_input_tokens += usage.cache_read_input_tokens
        self.llm_turns += 1
        if model not in self.models:
            self.models.append(model)
        per = self.by_model.setdefault(model, {"input": 0, "output": 0, "cache_read": 0})
        per["input"] += usage.input_tokens
        per["output"] += usage.output_tokens
        per["cache_read"] += usage.cache_read_input_tokens

    def add_tool(self, ms: float) -> None:
        self.tool_calls += 1
        self.tool_latency_ms += ms


@dataclass
class RunState:
    run_id: str
    sub: str
    question: str
    meter: UsageMeter
    handles: dict[str, HandleInfo] = field(default_factory=dict)
    metric_handles: set[str] = field(default_factory=set)
    last_handle: str | None = None
    spec: DashboardSpec | None = None
    refusal: str | None = None
    error_code: str | None = None
    delegated: bool = False
    spec_is_fallback: bool = False   # spec built by the harness, so its narrative is a truncated stand-in
    events: asyncio.Queue = field(default_factory=asyncio.Queue)   # dict events; None ends the stream
    steps: list[dict] = field(default_factory=list)   # the decision trace, sent with record_trace
    pending_note: str | None = None   # text the model wrote before its latest tool calls; the next step takes it

    def add_step(self, **fields) -> int:
        seq = len(self.steps)
        step = {"seq": seq, "parent": None, "kind": "error", "label": "", "note": None, "considered": [],
                "ms": None, "status": "ok", "error_code": None, "tool": None, "args": None, "handle": None}
        step.update(fields)
        if self.pending_note and step.get("note") is None:
            step["note"] = self.pending_note[:500]
        self.pending_note = None
        self.steps.append(step)
        return seq

    def emit(self, type: str, **data) -> None:
        self.events.put_nowait({"type": type, **data})
