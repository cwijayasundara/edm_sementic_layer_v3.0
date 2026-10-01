"""Provider-neutral message and model types for the agent loop."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class RawBlock:
    """A response block we do not interpret (thinking, redacted_thinking, ...): kept as the provider's own dict and
    replayed unchanged, in its original position, because the API requires thinking blocks to round-trip."""
    raw: dict


@dataclass(frozen=True)
class Message:
    role: Literal["user", "assistant"]
    content: tuple[TextBlock | ToolUse | ToolResult | RawBlock, ...]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass(frozen=True)
class ModelResponse:
    content: tuple[TextBlock | ToolUse | RawBlock, ...]
    stop_reason: str
    usage: Usage = Usage()


@dataclass(frozen=True)
class ModelRequest:
    model: str
    stable_system: str
    dynamic_system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolDef, ...]
    max_tokens: int = 16000   # thinking tokens count toward it
    force_tool: str | None = None
