"""One MCP server per data source (mcp 2.2.x, streamable HTTP)."""

from __future__ import annotations

from typing import Annotated, Literal

import anyio
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from spike.auth import HS256TokenVerifier, JWTAuthMiddleware, JWTConfig, current_claims


class QueryResult(BaseModel):
    source: str = Field(description="Data source this server fronts")
    columns: list[str]
    rows: list[list[str | int | float | None]]
    row_count: int


class WhoAmI(BaseModel):
    sub: str
    sub_after_await: str
    aud: str
    protocol_version: str | None
    session_id: str | None


def build_server(source_name: str, cfg: JWTConfig, *, auth_mode: Literal["sdk", "asgi"] = "sdk") -> MCPServer:
    if auth_mode == "sdk":
        mcp = MCPServer(
            f"edm-{source_name}",
            instructions=f"Read-only access to the {source_name} data source.",
            token_verifier=HS256TokenVerifier(cfg),
            auth=AuthSettings(
                issuer_url="https://auth.internal.example",  # required field; only used for metadata
                resource_server_url=None,  # no RFC 9728 metadata route; verifier checks `aud` itself
            ),
        )
    else:
        mcp = MCPServer(f"edm-{source_name}")

    @mcp.custom_route("/healthz", methods=["GET"])  # never behind RequireAuthMiddleware
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "source": source_name})

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def whoami(ctx: Context) -> WhoAmI:
        """Return the verified caller identity (test/diagnostic tool)."""
        first = current_claims()["sub"]
        await anyio.sleep(0.05)  # yield so concurrent requests interleave
        claims = current_claims()
        headers = ctx.headers or {}
        return WhoAmI(
            sub=first,
            sub_after_await=claims["sub"],
            aud=claims["aud"],
            protocol_version=ctx.protocol_version,
            session_id=headers.get("mcp-session-id"),
        )

    @mcp.tool(title="Run a read-only query", annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def run_query(
        sql: Annotated[str, Field(description="A single read-only SELECT statement", min_length=1)],
        limit: Annotated[int, Field(description="Max rows to return", ge=1, le=1000)] = 100,
    ) -> Annotated[CallToolResult, QueryResult]:
        """Execute a read-only SQL query against the data source."""
        claims = current_claims()
        if not sql.lstrip().lower().startswith("select"):
            raise ToolError("Only SELECT statements are allowed")  # -> isError=true, clean message
        if "boom" in sql:
            raise RuntimeError("db password=hunter2 leaked in driver error")  # -> generic message
        result = QueryResult(
            source=source_name,
            columns=["caller", "n"],
            rows=[[claims["sub"], i] for i in range(min(limit, 3))],
            row_count=min(limit, 3),
        )
        return CallToolResult(
            content=[TextContent(type="text", text=f"{result.row_count} rows from {source_name}")],
            structured_content=result.model_dump(mode="json"),
        )

    @mcp.tool()
    def add(a: int, b: int) -> int:
        """Plain typed tool: primitive return -> structuredContent {'result': n}."""
        return a + b

    return mcp


def build_app(
    source_name: str,
    cfg: JWTConfig,
    *,
    auth_mode: Literal["sdk", "asgi"] = "sdk",
    stateless: bool = True,
    json_response: bool = True,
    allowed_hosts: list[str] | None = None,
) -> tuple[MCPServer, Starlette]:
    """Returns (server, asgi_app). Build a fresh pair per process/test: session_manager.run() is one-shot."""
    mcp = build_server(source_name, cfg, auth_mode=auth_mode)
    # Explicit transport security. The default (host="127.0.0.1") only allows Host "127.0.0.1:<port>",
    # "localhost:<port>", "[::1]:<port>" -> container/service DNS names get 421.
    security = (
        TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed_hosts, allowed_origins=[])
        if allowed_hosts
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=stateless,
        json_response=json_response,
        transport_security=security,
    )
    if auth_mode == "asgi":
        app = JWTAuthMiddleware(app, cfg)  # type: ignore[assignment]
    return mcp, app


def build_mounted_app(source_name: str, cfg: JWTConfig, prefix: str = "/sources/pg") -> tuple[MCPServer, Starlette]:
    """Mounting under a prefix: inner lifespan does NOT run -> outer lifespan must run the session manager."""
    import contextlib

    from starlette.routing import Mount

    mcp, inner = build_app(source_name, cfg)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        async with mcp.session_manager.run():
            yield

    return mcp, Starlette(routes=[Mount(prefix, app=inner)], lifespan=lifespan)
