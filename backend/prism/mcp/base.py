"""One generic MCP server per data source. The three tools are identical for every source
(spec §4.3); a SourceBackend supplies the behaviour. Read docs/superpowers/spikes/plan-2/mcp/ for the
pattern this implements (SDK token verifier, stateless JSON responses, explicit host allow-list)."""
import asyncio
import json
import logging
import time
import uuid
from typing import Annotated, Any, Protocol

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from prism.config import Settings
from prism.mcp.auth import PrismTokenVerifier, current_claims
from prism.mcp.results import SourceError, UserFacingError

audit = logging.getLogger("prism.mcp.audit")

MAX_ID_LEN = 128
MAX_DIMENSIONS = 20
MAX_FILTERS = 20
MAX_REQUEST_KEYS = 20


class DescribeResult(BaseModel):
    source: str
    kind: str
    as_of: str
    metrics: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    notes: list[str] = []


class MetricResult(BaseModel):
    source: str
    metric_id: str
    unit: str | None
    dimensions: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool = False  # True when more groups existed than `limit` allowed
    as_of: str
    window: dict[str, str] | None = None


class QueryResult(BaseModel):
    source: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


class SourceBackend(Protocol):
    name: str
    kind: str

    async def describe(self, claims: dict) -> DescribeResult: ...

    async def run_metric(self, claims: dict, *, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                         time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult: ...

    async def query(self, claims: dict, request: dict[str, Any]) -> QueryResult: ...

    async def aclose(self) -> None: ...


def _reply(model: BaseModel, summary: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=summary)],
                          structured_content=model.model_dump(mode="json"))


def build_source_app(backend: SourceBackend, settings: Settings) -> tuple[MCPServer, Starlette]:
    source = backend.name
    mcp = MCPServer(
        f"prism-{source}",
        instructions=(f"Read-only access to the {source} data source. Use describe to see what is available, "
                      "run_metric for governed measures (preferred), query only for exploration."),
        token_verifier=PrismTokenVerifier(f"{source}-mcp", settings.jwt_secret.get_secret_value()),
        # issuer_url is metadata only; resource_server_url=None because the verifier checks `aud` itself.
        auth=AuthSettings(issuer_url="https://prism.invalid", resource_server_url=None),
    )

    @mcp.custom_route("/healthz", methods=["GET"])  # public: the start script polls it
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "source": source})

    async def run(tool: str, call, summarise, **meta) -> CallToolResult:
        claims = current_claims()
        started = time.perf_counter()
        outcome = "ok"
        extra: dict[str, Any] = {}
        try:
            result = await call(claims)
            return _reply(result, summarise(result))  # built inside the audited section
        except UserFacingError as exc:
            outcome = "error"
            raise ToolError(str(exc)) from exc
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = "internal_error"
            ref = uuid.uuid4().hex[:8]
            extra = {"error_type": type(exc).__name__, "ref": ref}
            raise ToolError(f"internal error (ref {ref})") from None
        finally:
            audit.info(json.dumps({"event": "tool_call", "source": source, "tool": tool, "sub": claims.get("sub"),
                                   "outcome": outcome, "ms": round((time.perf_counter() - started) * 1000, 1),
                                   **meta, **extra}))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def describe() -> Annotated[CallToolResult, DescribeResult]:
        """List the governed metrics and the tables/endpoints this caller may use on this source."""
        return await run("describe", backend.describe,
                         lambda r: f"{len(r.metrics)} metrics, {len(r.objects)} objects on {source}")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def run_metric(
        metric_id: Annotated[str, Field(description="Governed metric id from describe()", min_length=1, max_length=MAX_ID_LEN)],
        dimensions: Annotated[list[str], Field(description="Dimension names to group by", max_length=MAX_DIMENSIONS)] = [],
        filters: Annotated[dict[str, Any], Field(description="Filter name -> value | list | {gte,lte,between,ne}")] = {},
        time_range: Annotated[dict[str, Any] | None, Field(
            description="{'last_business_days': N} or {'from': ISO date, 'to': ISO date}")] = None,
        order_by: Annotated[str | None, Field(description="metric | metric_desc | <dimension> | -<dimension>",
                                                max_length=MAX_ID_LEN)] = None,
        limit: Annotated[int, Field(description="Max rows", ge=1, le=1000)] = 100,
    ) -> Annotated[CallToolResult, MetricResult]:
        """Run a governed metric. Preferred over free-form queries: deterministic, cheap and always in policy."""

        async def call(claims: dict) -> MetricResult:
            if len(filters) > MAX_FILTERS:
                raise SourceError(f"too many filters (max {MAX_FILTERS})")
            return await backend.run_metric(claims, metric_id=metric_id, dimensions=list(dimensions),
                                            filters=dict(filters), time_range=time_range, order_by=order_by,
                                            limit=limit)

        return await run("run_metric", call,
                         lambda r: f"{r.row_count} row{'s' if r.row_count != 1 else ''} for {metric_id}"
                                   f"{' (truncated)' if r.truncated else ''}",
                         metric=metric_id)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def query(
        request: Annotated[dict[str, Any], Field(
            description="SQL sources: {'sql': '<single SELECT>'}. REST sources: {'endpoint_id': ..., 'params': {...}}")],
    ) -> Annotated[CallToolResult, QueryResult]:
        """Guarded free-form access for exploration when no governed metric fits. Not available to metrics-only principals."""

        async def call(claims: dict) -> QueryResult:
            if claims.get("metrics_only", True):  # fail closed: a missing claim is restricted
                raise SourceError("free-form queries are not available to metrics-only principals; use run_metric")
            if len(request) > MAX_REQUEST_KEYS:
                raise SourceError(f"too many request keys (max {MAX_REQUEST_KEYS})")
            return await backend.query(claims, dict(request))

        return await run("query", call, lambda r: f"{r.row_count} row{'s' if r.row_count != 1 else ''}"
                                                   f"{' (truncated)' if r.truncated else ''}")

    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=list(settings.mcp_allowed_hosts), allowed_origins=[])
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                  transport_security=security)
    return mcp, app
