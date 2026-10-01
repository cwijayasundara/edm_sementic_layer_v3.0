"""The agent's HTTP surface: SSE chat, dashboard KPIs, result paging and a dev-only token mint. Every data call is
made through the gateway with the caller's own token; error bodies never carry gateway or auth detail."""
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field

from prism.agent.auth import AuthError, UserContext, verify_user
from prism.agent.gateway_client import GatewayClient, GatewayError, GatewayPort
from prism.agent.kpis import KpiService
from prism.agent.model import AnthropicModelClient, ModelRequest, ModelResponse
from prism.agent.service import MAX_QUESTION_CHARS, AgentService
from prism.agent.telemetry import AgentRunWriter
from prism.config import Settings
from prism.gateway.audit import open_audit_pool
from prism.gateway.server import GATEWAY_AUDIENCE
from prism.security.personas import PERSONAS, claims_for
from prism.security.tokens import mint

log = logging.getLogger("prism.agent")
UI_ORIGIN = "http://localhost:3000"
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]   # DNS-rebinding guard: the Host header must be a loopback name
HANDLE = re.compile(r"^r_[0-9a-f]{12}$")
DEV_TOKEN_TTL_S = 3600
GatewayFactory = Callable[[UserContext], AbstractAsyncContextManager[GatewayPort]]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(max_length=MAX_QUESTION_CHARS)


class DevTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_id: str


async def _sse(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    try:
        async for e in events:
            yield f"event: {e['type']}\ndata: {json.dumps(e, default=str)}\n\n"
    except Exception as exc:  # noqa: BLE001 - headers are sent; end the stream with one fixed error frame
        log.error("chat stream failed: %s", type(exc).__name__)
        err = {"type": "error", "code": "internal_error", "message": "Something went wrong. Please try again."}
        yield f"event: error\ndata: {json.dumps(err)}\n\n"
    finally:   # client disconnect cancels this generator; close the service run so its cleanup executes
        await events.aclose()  # type: ignore[attr-defined]


def create_app(*, settings: Settings | None = None, service: AgentService | None = None,
               gateway_factory: GatewayFactory | None = None, kpi_service: KpiService | None = None,
               model_configured: bool = True, lifespan=None) -> FastAPI:
    settings = settings or Settings()
    factory: GatewayFactory = gateway_factory or (lambda user: GatewayClient(settings.gateway_url, user.token))
    kpis = kpi_service or KpiService()
    app = FastAPI(title="Prism agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.add_middleware(CORSMiddleware, allow_origins=[UI_ORIGIN], allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type"])
    # outermost; Starlette's TestClient sends Host: testserver, so that name is allowed outside production only
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=ALLOWED_HOSTS if settings.env == "production" else [*ALLOWED_HOSTS, "testserver"])

    def current_user(authorization: str | None = Header(None)) -> UserContext:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(401, "unauthorized")
        try:
            return verify_user(token.strip(), settings)
        except AuthError:
            raise HTTPException(401, "unauthorized") from None

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "agent"}

    @app.post("/dev/token")
    async def dev_token(body: DevTokenRequest, request: Request) -> dict:
        host = request.client.host if request.client else None
        if settings.env == "production" or not settings.agent_dev_token_enabled or host not in LOOPBACK:
            raise HTTPException(404, "not found")
        if body.persona_id not in PERSONAS:
            raise HTTPException(422, "unknown persona")
        return {"token": mint(claims_for(body.persona_id), GATEWAY_AUDIENCE, settings.jwt_secret.get_secret_value(),
                              ttl_s=DEV_TOKEN_TTL_S)}

    @app.post("/chat")
    async def chat(body: ChatRequest, request: Request, user: UserContext = Depends(current_user)):
        if not model_configured:
            raise HTTPException(503, "model access is not configured")
        svc: AgentService = request.app.state.service
        return StreamingResponse(_sse(svc.chat(user, body.question)), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/kpis")
    async def get_kpis(user: UserContext = Depends(current_user)) -> dict:
        if (hit := kpis.cached(user)) is not None:
            return {"tiles": hit}
        try:
            async with factory(user) as gateway:
                return {"tiles": await kpis.tiles(user, gateway)}
        except GatewayError:
            raise HTTPException(502, "data service unavailable") from None

    @app.get("/results/{handle}")
    async def results(handle: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                      user: UserContext = Depends(current_user)) -> dict:
        if not HANDLE.match(handle):
            raise HTTPException(404, "not found")
        try:
            async with factory(user) as gateway:
                return await gateway.call("get_rows", {"handle": handle, "offset": offset, "limit": limit})
        except GatewayError as exc:
            if exc.code in ("unknown_handle", "not_permitted"):
                raise HTTPException(404, "not found") from None
            raise HTTPException(502, "data service unavailable") from None

    return app


class _LazyModelClient:
    """Builds the Anthropic client on first use so the app starts (and serves everything else) without a key."""

    def __init__(self, api_key: str | None):
        self._key, self._client = api_key, None

    async def create(self, request: ModelRequest) -> ModelResponse:
        if self._client is None:
            if not self._key:
                raise RuntimeError("model access is not configured")
            self._client = AnthropicModelClient(self._key)
        return await self._client.create(request)


def create_app_from_env() -> FastAPI:
    settings = Settings()
    key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = await open_audit_pool(settings)
        try:
            app.state.service = AgentService(
                settings=settings, model_client=_LazyModelClient(key),
                gateway_factory=lambda user: GatewayClient(settings.gateway_url, user.token),
                run_writer=AgentRunWriter(pool, hmac_key=settings.audit_hmac_key.get_secret_value()))
            yield
        finally:
            await pool.close()

    return create_app(settings=settings, model_configured=key is not None, lifespan=lifespan)
