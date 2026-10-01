"""Client helper (reusable by the gateway). NOTE: mcp 2.x client uses httpx2, not httpx."""

from __future__ import annotations

import contextlib
from typing import Any, AsyncIterator

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.client import ConnectMode


@contextlib.asynccontextmanager
async def mcp_client(
    url: str,
    token: str,
    *,
    mode: ConnectMode = "auto",
    asgi_app: Any | None = None,
    timeout_s: float = 30.0,
    read_timeout_s: float = 300.0,
) -> AsyncIterator[Client]:
    """Connect to an MCP streamable-HTTP endpoint with a bearer token.

    asgi_app: if given, requests go in-process via httpx2.ASGITransport (no socket). The caller must
    then run `mcp.session_manager.run()` itself (ASGITransport does not run lifespan).
    """
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(timeout_s, read=read_timeout_s),
        transport=httpx2.ASGITransport(app=asgi_app) if asgi_app is not None else None,
    )
    async with http:
        transport = streamable_http_client(url, http_client=http)
        async with Client(transport, mode=mode, cache=None) as client:
            yield client
