"""The five source MCP servers (ports 8201-8205), each fronting one simulated platform."""
from contextlib import asynccontextmanager

import anyio
import httpx
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette

from prism.config import Settings
from prism.gateway.audit import configure_audit_logging
from prism.mcp.base import SourceBackend, build_source_app
from prism.mcp.rest_backend import RestBackend
from prism.mcp.sql_backend import SqlBackend

SOURCES = ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
MCP_PORTS = {"refmaster": 8201, "marketmaster": 8202, "cashrecon": 8203, "assetrecon": 8204, "feedhub": 8205}
REST_SOURCES = frozenset({"refmaster", "marketmaster"})


def create_backend(source: str, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> SourceBackend:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; valid: {list(SOURCES)}")
    if source in REST_SOURCES:
        return RestBackend(source, settings, transport=transport)
    return SqlBackend(source, settings)


def create_app(source: str, settings: Settings | None = None, *,
               transport: httpx.AsyncBaseTransport | None = None) -> tuple[MCPServer, Starlette]:
    settings = settings or Settings()
    backend = create_backend(source, settings, transport=transport)
    mcp, app = build_source_app(backend, settings)
    app.state.prism_backend = backend  # tests close it explicitly (ASGITransport does not run the lifespan)

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette):
        async with inner(a):
            try:
                yield
            finally:  # also on cancellation or an error: the pool / HTTP client must not leak
                with anyio.CancelScope(shield=True):
                    await backend.aclose()

    app.router.lifespan_context = lifespan
    return mcp, app


def _factory(source: str):
    def make() -> Starlette:  # uvicorn --factory
        configure_audit_logging()  # explicit: audit lines must not depend on a root handler
        return create_app(source)[1]

    make.__name__ = f"{source}_app"
    return make


refmaster_app = _factory("refmaster")
marketmaster_app = _factory("marketmaster")
cashrecon_app = _factory("cashrecon")
assetrecon_app = _factory("assetrecon")
feedhub_app = _factory("feedhub")
