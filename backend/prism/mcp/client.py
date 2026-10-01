"""MCP client helpers (the Plan 3 gateway reuses these). NOTE: the mcp 2.x client uses httpx2, not httpx."""
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from mcp import Client
from mcp.client.client import ConnectMode
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult


@contextlib.asynccontextmanager
async def mcp_client(
    url: str,
    token: str,
    *,
    mode: ConnectMode = "auto",
    asgi_app: Any | None = None,
    timeout_s: float = 30.0,
    read_timeout_s: float = 300.0,
    event_hooks: dict[str, list] | None = None,
) -> AsyncIterator[Client]:
    """Connect to a streamable-HTTP MCP endpoint with a bearer token.

    asgi_app: talk to an ASGI app in-process (no socket); the caller must then run
    `mcp.session_manager.run()` itself because ASGITransport does not run the app lifespan.
    event_hooks: httpx2 event hooks (the gateway uses a response hook to tell a 401 from other HTTP errors, which
    the SDK otherwise reports identically).
    """
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(timeout_s, read=read_timeout_s),
        transport=httpx2.ASGITransport(app=asgi_app) if asgi_app is not None else None,
        event_hooks=event_hooks,
    )
    async with http:
        async with Client(streamable_http_client(url, http_client=http), mode=mode, cache=None) as client:
            yield client


async def call_tool(url: str, token: str, name: str, arguments: dict[str, Any] | None = None, *,
                    asgi_app: Any | None = None, event_hooks: dict[str, list] | None = None,
                    timeout_s: float = 30.0, read_timeout_s: float = 300.0) -> CallToolResult:
    async with mcp_client(url, token, asgi_app=asgi_app, event_hooks=event_hooks, timeout_s=timeout_s,
                          read_timeout_s=read_timeout_s) as client:
        return await client.call_tool(name, arguments or {})
