"""Downstream: the gateway's only path to the source MCP servers.

Every call re-mints a short-lived source token from the caller's claims (never the incoming token), runs under a
per-(sub, source) semaphore (the source servers allow 3 concurrent requests per principal; the gateway queues
instead of failing), retries the sources' transient refusals 3 times with jittered exponential backoff (only a tool
error whose whole message is exactly one of prism.mcp.results.SOURCE_BUSY_MESSAGES, so neither an unrelated message
mentioning "busy" nor a caller-supplied value echoed by the source can trigger a retry), and maps failures to stable codes: HTTP 401/403 -> source_auth (seen by an httpx response hook, because the
SDK reports every HTTP error as the same MCPError inside nested ExceptionGroups), timeouts -> source_timeout, any
other transport failure -> source_unavailable, a tool error -> source_error with the source's own (scrubbed) message.
Errors are raised `from None` and scrubbed of the token, so no token reaches a message, a log line or the audit.
When constructed with an audit writer, each source call also writes a `source.<tool>` audit row carrying only catalog
names (metric id, dimension names), counts and codes. The running gateway passes audit=None (server.py): its record
is the Gateway's one audit row per tool call, so no source.* rows are written in production.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp.types import CallToolResult

from prism.config import Settings
from prism.gateway.audit import valid_sub
from prism.gateway.errors import GatewayError, GatewaySourceError
from prism.gateway.policy import MetricPlan, is_metrics_only
from prism.mcp.client import call_tool
from prism.mcp.results import SOURCE_BUSY_MESSAGES, mint_source_token
from prism.mcp.servers import MCP_PORTS

CallFn = Callable[[str, str, str, dict], Awaitable[CallToolResult]]
BUSY = SOURCE_BUSY_MESSAGES
MAX_REQUEST_KEYS = 20
MAX_MESSAGE = 500
_TOOL_PREFIX = re.compile(r"^Error executing tool \w+: ")
_JWT = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


class SourceAuthRejected(Exception):
    """Raised by the response hook on HTTP 401/403 (carries the status only)."""


async def mcp_call(url: str, token: str, tool: str, arguments: dict, *, asgi_app: Any | None = None,
                   timeout_s: float = 30.0) -> CallToolResult:
    async def on_response(response: httpx2.Response) -> None:
        if response.status_code in (401, 403):
            raise SourceAuthRejected(response.status_code)

    return await call_tool(url, token, tool, arguments, asgi_app=asgi_app, event_hooks={"response": [on_response]},
                           timeout_s=timeout_s, read_timeout_s=timeout_s)


@dataclass(frozen=True)
class SourceMetricResult:
    source: str
    metric_id: str
    unit: str | None
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    as_of: str | None = None
    window: dict | None = None


@dataclass(frozen=True)
class SourceQueryResult:
    source: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for e in exc.exceptions for leaf in _leaves(e)]
    return [exc]


def _map_failure(exc: Exception, source: str) -> GatewayError:
    leaves = _leaves(exc)
    if any(isinstance(e, SourceAuthRejected) for e in leaves):
        return GatewayError("source_auth", f"the {source} source refused the gateway's credentials")
    if any(isinstance(e, (TimeoutError, httpx2.TimeoutException)) for e in leaves):
        return GatewayError("source_timeout", f"the {source} source did not answer in time")
    return GatewayError("source_unavailable", f"the {source} source is unavailable")


def _scrub(text: str, token: str) -> str:
    text = _TOOL_PREFIX.sub("", text).replace(token, "[redacted]")
    for part in token.split("."):
        if len(part) >= 8:
            text = text.replace(part, "[redacted]")
    return _JWT.sub("[redacted]", text)[:MAX_MESSAGE] or "the source refused the request"


def _bad_reply(source: str) -> GatewayError:
    return GatewayError("source_unavailable", f"the {source} source sent an unexpected reply")


class Downstream:
    def __init__(self, settings: Settings, audit: Any = None, cap: int = 3, *, call: CallFn | None = None,
                 urls: Mapping[str, str] | None = None, retries: int = 3, backoff_s: float = 0.2,
                 timeout_s: float = 30.0, token_ttl_s: int = 60, sleep: Callable[[float], Awaitable] = asyncio.sleep,
                 rand: Callable[[], float] = random.random):
        self.settings, self.audit, self.cap = settings, audit, cap
        self._call = call or (lambda url, token, tool, args: mcp_call(url, token, tool, args, timeout_s=timeout_s))
        self.urls = dict(urls) if urls is not None else {s: f"http://127.0.0.1:{p}/mcp" for s, p in MCP_PORTS.items()}
        self.retries, self.backoff_s, self.timeout_s, self.token_ttl_s = retries, backoff_s, timeout_s, token_ttl_s
        self._sleep, self._rand = sleep, rand
        self._sems: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

    def _semaphore(self, sub: str, source: str) -> asyncio.Semaphore:
        sem = self._sems.get((sub, source))
        if sem is None:
            sem = asyncio.Semaphore(self.cap)
            self._sems[(sub, source)] = sem
        return sem

    async def _invoke(self, claims: dict, source: str, tool: str, arguments: dict) -> CallToolResult:
        sem = self._semaphore(claims["sub"], source)  # held by this frame: never collected while in use
        for attempt in range(self.retries + 1):
            token = mint_source_token(claims, source, self.settings, ttl_s=self.token_ttl_s)
            failure = None
            async with sem:
                try:
                    async with asyncio.timeout(self.timeout_s):
                        result = await self._call(self.urls[source], token, tool, arguments)
                except Exception as exc:  # noqa: BLE001 - mapped to a stable code; CancelledError passes through
                    failure = _map_failure(exc, source)
            if failure is not None:  # raised outside the handler: no __context__ chain back to token-bearing errors
                raise failure
            if not isinstance(result, CallToolResult):
                raise _bad_reply(source)
            if not result.is_error:
                return result
            text = " ".join(getattr(c, "text", "") for c in result.content)
            message = _scrub(text, token)
            if message.strip() in BUSY:
                if attempt < self.retries:
                    await self._sleep(self.backoff_s * 2**attempt * (0.5 + self._rand()))
                    continue
                raise GatewayError("source_busy", f"the {source} source is busy; try again shortly")
            raise GatewaySourceError(message)
        raise AssertionError("unreachable")

    def _caller(self, claims: Any, source: Any) -> None:
        if not isinstance(claims, Mapping) or not valid_sub(claims.get("sub")) or source not in self.urls:
            raise GatewayError("not_permitted", "this source is not available to your role")

    async def _audited(self, claims: Mapping, source: str, tool: str, names: dict, run) -> Any:
        started, status, code, out = time.perf_counter(), "error", None, None
        try:
            out = await run()
            status = "ok"
            return out
        except GatewayError as e:
            code = e.code
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception:
            code = "internal_error"
            raise
        finally:
            if self.audit is not None:
                roles = claims.get("roles")
                await self.audit.write({
                    "sub": claims.get("sub"), "persona": roles[0] if isinstance(roles, list) and roles else None,
                    "tool": f"source.{tool}", "source": source, **names, "status": status, "error_code": code,
                    "rows": out.row_count if out is not None else None,
                    "truncated": out.truncated if out is not None else None,
                    "ms": round((time.perf_counter() - started) * 1000, 1)})

    async def run_metric(self, claims: dict, plan: MetricPlan) -> SourceMetricResult:
        self._caller(claims, plan.source)

        async def run() -> SourceMetricResult:
            result = await self._invoke(dict(claims), plan.source, plan.tool, dict(plan.arguments))
            sc = result.structured_content
            try:
                if sc["source"] != plan.source or sc["metric_id"] != plan.metric_id:
                    raise _bad_reply(plan.source)
                dims = [d for d in sc["dimensions"] if isinstance(d, str)]
                columns = [*dims, "value"]
                for row in sc["rows"]:
                    columns += [k for k in row if k not in columns]
                rows = [[row.get(c) for c in columns] for row in sc["rows"]]
                return SourceMetricResult(source=plan.source, metric_id=plan.metric_id, unit=sc.get("unit"),
                                          columns=columns, rows=rows, row_count=len(rows),
                                          truncated=sc.get("truncated") is True, as_of=sc.get("as_of"),
                                          window=sc.get("window"))
            except (TypeError, KeyError, AttributeError):
                raise _bad_reply(plan.source) from None

        names = {"metric_id": plan.metric_id, "dimensions": list(plan.arguments.get("dimensions", []))}
        return await self._audited(claims, plan.source, plan.tool, names, run)

    async def query(self, claims: dict, source: str, request: dict) -> SourceQueryResult:
        self._caller(claims, source)
        if is_metrics_only(claims):
            raise GatewayError("metrics_only", "free-form queries are not available to metrics-only principals")
        if not isinstance(request, Mapping) or len(request) > MAX_REQUEST_KEYS:
            raise GatewayError("invalid_request", f"request must be an object with at most {MAX_REQUEST_KEYS} keys")

        async def run() -> SourceQueryResult:
            result = await self._invoke(dict(claims), source, "query", {"request": dict(request)})
            sc = result.structured_content
            try:
                if sc["source"] != source:
                    raise _bad_reply(source)
                columns = [str(c) for c in sc["columns"]]
                rows = [list(r) for r in sc["rows"]]
                return SourceQueryResult(source=source, columns=columns, rows=rows, row_count=len(rows),
                                         truncated=sc.get("truncated") is True)
            except (TypeError, KeyError, AttributeError):
                raise _bad_reply(source) from None

        return await self._audited(claims, source, "query", {}, run)


__all__ = ["Downstream", "SourceAuthRejected", "SourceMetricResult", "SourceQueryResult", "mcp_call"]
