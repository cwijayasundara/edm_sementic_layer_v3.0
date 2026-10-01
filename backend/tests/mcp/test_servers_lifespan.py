"""The source-server lifespan closes the backend on every exit path (no database needed)."""
import asyncio

import anyio
import pytest

from prism.config import Settings
from prism.mcp import servers


class CountingBackend:
    name = "cashrecon"
    kind = "sql"

    def __init__(self) -> None:
        self.closed = 0

    async def aclose(self) -> None:
        await anyio.sleep(0)  # a checkpoint: only a shielded close gets past it once the scope is cancelled
        self.closed += 1


@pytest.fixture
def app_and_backend(monkeypatch):
    backend = CountingBackend()
    monkeypatch.setattr(servers, "create_backend", lambda source, settings, transport=None: backend)
    _, app = servers.create_app("cashrecon", Settings())
    return app, backend


async def test_backend_is_exposed_on_app_state(app_and_backend):
    app, backend = app_and_backend
    assert app.state.prism_backend is backend


async def test_lifespan_closes_the_backend_on_normal_exit(app_and_backend):
    app, backend = app_and_backend
    async with app.router.lifespan_context(app):
        assert backend.closed == 0
    assert backend.closed == 1


async def test_lifespan_closes_the_backend_when_cancelled_after_startup(app_and_backend):
    app, backend = app_and_backend
    started = asyncio.Event()

    async def serve():
        async with app.router.lifespan_context(app):
            started.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(serve())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.closed == 1


async def test_lifespan_closes_the_backend_on_an_error(app_and_backend):
    app, backend = app_and_backend
    with pytest.raises((RuntimeError, ExceptionGroup)):
        async with app.router.lifespan_context(app):
            raise RuntimeError("boom")
    assert backend.closed == 1


async def test_close_completes_inside_a_cancelled_scope(app_and_backend):
    # anyio cancellation is level-triggered: without a shield the close would be cancelled at its first await
    app, backend = app_and_backend
    with anyio.CancelScope() as scope:
        async with app.router.lifespan_context(app):
            scope.cancel()
            await anyio.sleep(3600)
    assert scope.cancelled_caught and backend.closed == 1
