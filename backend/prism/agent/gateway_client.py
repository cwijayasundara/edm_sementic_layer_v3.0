"""The agent's only data connection: the gateway MCP, one session per user request, bearer token set at the
transport (never in tool arguments)."""
import re
from typing import Any, Protocol

from prism.mcp.client import mcp_client

_ERR = re.compile(r"^Error executing tool \S+: (?P<code>[a-z_]+)(?: \(ref [^)]*\))?: ?(?P<msg>.*)$", re.S)


class GatewayError(Exception):
    CALLER_FIXABLE = frozenset({"invalid_request", "unknown_tool", "unknown_metric", "sensitive_dimension",
                                "grain_too_fine", "missing_required_dimension", "invalid_sql", "sql_not_allowed",
                                "currency_mixing", "unknown_handle", "empty_result", "result_too_large"})
    RETRY_LATER = frozenset({"rate_limited", "gateway_busy", "source_busy", "source_timeout", "source_unavailable",
                             "context_unavailable", "result_store_full", "combine_timeout", "record_failed",
                             "gateway_unavailable", "confirm_failed"})
    FINAL = frozenset({"not_permitted", "metrics_only", "not_confirmable"})

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message[:500]}")
        self.code, self.message = code, message[:500]

    final = property(lambda self: self.code in self.FINAL)
    caller_fixable = property(lambda self: self.code in self.CALLER_FIXABLE)
    retry_later = property(lambda self: self.code in self.RETRY_LATER)


def parse_tool_error(text: str) -> GatewayError:
    m = _ERR.match(text.strip())
    return GatewayError(m["code"], m["msg"]) if m else GatewayError("tool_error", text.strip())


class GatewayPort(Protocol):
    async def call(self, tool: str, arguments: dict) -> dict: ...


class GatewayClient:
    def __init__(self, url: str, token: str, *, timeout_s: float = 30.0, read_timeout_s: float = 120.0):
        self._url, self._token = url.rstrip('/').removesuffix('/mcp') + '/mcp', token
        self._timeouts = (timeout_s, read_timeout_s)
        self._cm = None
        self._client = None

    async def __aenter__(self) -> "GatewayClient":
        self._cm = mcp_client(self._url, self._token, timeout_s=self._timeouts[0], read_timeout_s=self._timeouts[1])
        try:
            self._client = await self._cm.__aenter__()
        except Exception:  # noqa: BLE001 - never echo transport details (they can carry the URL/headers)
            raise GatewayError("gateway_unavailable", "the data gateway is not reachable") from None
        return self

    async def __aexit__(self, *exc) -> None:
        if self._cm is not None:
            try:
                await self._cm.__aexit__(*exc)
            except Exception:  # noqa: BLE001
                pass

    async def call(self, tool: str, arguments: dict) -> dict:
        assert self._client is not None, "use `async with GatewayClient(...)`"
        try:
            result = await self._client.call_tool(tool, arguments)
        except Exception:  # noqa: BLE001
            raise GatewayError("gateway_unavailable", "the data gateway is not reachable") from None
        if result.is_error:
            raise parse_tool_error(getattr(result.content[0], "text", "") if result.content else "")
        out: Any = result.structured_content
        return out if isinstance(out, dict) else {"result": out}
