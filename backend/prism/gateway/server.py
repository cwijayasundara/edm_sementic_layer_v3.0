"""The Semantic Gateway MCP server (:8200): the only agent-facing door to the data (spec §4.3, §5, §6).

Same transport as the source servers (prism.mcp.base): stateless streamable HTTP with JSON responses, the SDK token
verifier (audience `gateway-mcp`, HS256 from prism.security.tokens), an explicit Host allow-list. Requests over
MAX_BODY_BYTES are refused with 413 before the JSON is parsed. The eight tools are in prism.gateway.service; this module
wires them to MCP, builds the runtime and refuses to start when a dependency is missing:

  uvicorn prism.gateway.server:create_app_from_env --factory --host 127.0.0.1 --port 8200

Startup, before the port is bound (create_app_from_env): production secrets check, embedding model load + warmup
("run `make models`"), Neo4j connectivity ("run `make db`"), the catalog (a stale or empty graph refuses to start:
"run `make graph`"), `migrate_app`. The lifespan then opens the audit pool and the async graph driver and starts the
catalog refresh loop (every gateway_catalog_refresh_s; a failed refresh keeps the current catalog).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import anyio
import psycopg
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, ToolAnnotations
from neo4j import AsyncGraphDatabase, GraphDatabase
from neo4j.exceptions import AuthError, ConfigurationError, ServiceUnavailable
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from prism.config import DEV_SECRET_FIELDS, ConfigError, Settings, load_settings, redact_uri
from prism.db.app_migrate import migrate_app
from prism.gateway.audit import (AuditWriter, audit_log, configure_audit_logging, open_audit_pool,
                                 sanitize_audit_event, valid_caller_id)
from prism.gateway.downstream import Downstream
from prism.gateway.policy import Policy
from prism.gateway.results import ResultStore
from prism.gateway.service import ARG_MODELS, TOOLS, Gateway
from prism.graph.catalog import Catalog, CatalogError, aload_catalog, load_catalog
from prism.graph.embedder import Embedder, EmbedderError
from prism.graph.lineage import alineage
from prism.graph.retrieval import GraphError, GraphUnavailable, acontext_pack, arun_read
from prism.mcp.auth import PrismTokenVerifier, current_claims

log = logging.getLogger("prism.gateway")

GATEWAY_AUDIENCE = "gateway-mcp"
MAX_TOKEN_LIFETIME_S = 3600   # a gateway token expiring further ahead than this is refused
MAX_BODY_BYTES = 1024 * 1024
MCP_PATH = "/mcp"
# A tools/call whose `arguments` is not an object is rewritten to {INVALID_ARGUMENTS: "<type>"} before the SDK sees it
# (the SDK would answer -32602 with no audit row); GatewayMCP turns it back into a refused, audited call.
INVALID_ARGUMENTS = "\x00prism:arguments-not-an-object"
GRAPH_STATEMENT_TIMEOUT_S = 4.0
VERSION_CYPHER = "MATCH (n:Ctx {ns: $ns}) RETURN max(n.loaded_version) AS version"
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)

Runtime = Callable[[], Awaitable[tuple[Gateway, Callable[[], Awaitable[None]]]]]


class GatewayStartupError(RuntimeError):
    """A dependency the gateway needs is missing or unsafe; the message says how to fix it."""


# ------------------------------------------------------------------------------------------------ auth / transport
class GatewayTokenVerifier(PrismTokenVerifier):
    """The source servers' verifier plus the gateway's own rules: exactly one audience (`gateway-mcp`, never a list),
    an expiry at most MAX_TOKEN_LIFETIME_S ahead, and a `sub` of the caller-id charset (valid_caller_id)."""

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        claims = (access.claims or {}) if access is not None else {}
        if access is None or not valid_caller_id(claims.get("sub")) or claims.get("aud") != self.audience:
            return None
        exp = claims.get("exp")
        if not isinstance(exp, int) or isinstance(exp, bool) or exp > time.time() + MAX_TOKEN_LIFETIME_S:
            return None
        return access


def _rewrite_tool_arguments(body: bytes) -> bytes | None:
    """The body with every tools/call's non-object `arguments` replaced by the INVALID_ARGUMENTS marker, or None
    when nothing needs rewriting (or the body is not JSON: the SDK answers that itself)."""
    try:
        doc = json.loads(body)
    except (ValueError, RecursionError):
        return None
    changed = False
    for msg in doc if isinstance(doc, list) else [doc]:
        params = msg.get("params") if isinstance(msg, dict) and msg.get("method") == "tools/call" else None
        if isinstance(params, dict) and "arguments" in params and not isinstance(params["arguments"], (dict, type(None))):
            params["arguments"] = {INVALID_ARGUMENTS: type(params["arguments"]).__name__}
            changed = True
    return json.dumps(doc).encode() if changed else None


class BodyLimitMiddleware:
    """In front of everything (auth included): 405 for GET on the MCP path (stateless JSON: no SSE stream to hold
    open); 413 for a request body over `limit` bytes, declared or streamed (chunked), before anything parses it. The
    body (at most `limit` bytes) is buffered, then replayed to the app; a tools/call with non-object `arguments` is
    rewritten so the gateway can refuse and audit it (_rewrite_tool_arguments)."""

    def __init__(self, app, limit: int = MAX_BODY_BYTES) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("method") == "GET" and scope.get("path", "").rstrip("/") == MCP_PATH:
            return await JSONResponse({"error": "method not allowed"}, status_code=405,
                                      headers={"Allow": "POST"})(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        declared = headers.get(b"content-length")
        too_large = JSONResponse({"error": "request too large"}, status_code=413)
        if declared is not None and (not declared.isdigit() or int(declared) > self.limit):
            return await too_large(scope, receive, send)
        if scope.get("method") != "POST":
            return await self.app(scope, receive, send)
        chunks, seen = [], 0
        while True:
            message = await receive()
            if message["type"] != "http.request":   # the client went away: nothing to answer
                return None
            chunk = message.get("body", b"")
            seen += len(chunk)
            if seen > self.limit:
                return await too_large(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        rewritten = _rewrite_tool_arguments(body) if scope.get("path", "").rstrip("/") == MCP_PATH else None
        if rewritten is not None:
            body = rewritten
            scope = {**scope, "headers": [(k, v) for k, v in scope.get("headers") or [] if k != b"content-length"]
                     + [(b"content-length", str(len(body)).encode())]}
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


class GatewayMCP(MCPServer):
    """Every tool call goes to Gateway.call_tool with the verified claims: argument validation, refusals and
    internal errors are all answered (and audited) there, never by the SDK's own validator."""

    def __init__(self, *args: Any, state: SimpleNamespace, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._prism_state = state

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None) -> CallToolResult:
        gateway: Gateway | None = self._prism_state.gateway
        if isinstance(arguments, dict) and INVALID_ARGUMENTS in arguments:
            arguments = None   # not an object: Gateway refuses it as invalid_request, and audits it
        if gateway is None:
            # no audit pool yet: the refusal still reaches the audit log (uvicorn runs the lifespan before it binds,
            # so this is only reachable in-process)
            claims = _claims_or_none()
            row = sanitize_audit_event({"sub": (claims or {}).get("sub"), "tool": name if name in TOOLS else "unknown",
                                        "status": "error", "error_code": "unavailable"}, key="-" * 32)
            audit_log.info(json.dumps({"event": "gateway_call", **row}, default=str))
            raise ToolError(f"Error executing tool {name if name in TOOLS else 'unknown'}: unavailable: "
                            f"the gateway is starting")
        return await gateway.call_tool(name, current_claims(), arguments)


def _claims_or_none() -> dict | None:
    try:
        return current_claims()
    except PermissionError:
        return None


# ------------------------------------------------------------------------------------------------ tools (schemas)
async def search_context(question: str, max_items: int = 8) -> CallToolResult:
    """Role-filtered context for a question: the governed metrics (id, source, unit, dimensions, filters), business
    terms, concepts, columns, example questions and join paths you may use. Start every question here."""
    raise NotImplementedError  # dispatched by GatewayMCP.call_tool


async def run_metric(metric_id: str, dimensions: list[str] = [], filters: dict = {},  # noqa: B006 - schema only
                     limit: int | None = None) -> CallToolResult:
    """Run a governed metric (preferred over query_source). The gateway picks the source from the catalog and checks
    your entitlement. Returns {handle, summary}: row count, columns, units and at most 5 sample rows; page through the
    rows with get_rows."""
    raise NotImplementedError


async def query_source(source: str, request: dict) -> CallToolResult:
    """Guarded free-form read of one source when no metric fits: request={"sql": "SELECT ..."} for SQL sources or
    request={"endpoint_id": ..., "params": {...}} for REST sources. Not available to metrics-only roles. Returns
    {handle, summary}."""
    raise NotImplementedError


async def get_rows(handle: str, offset: int = 0, limit: int = 50) -> CallToolResult:
    """One page of rows of a result handle you created (default 50, max 200 rows)."""
    raise NotImplementedError


async def combine(sql: str, handles: dict) -> CallToolResult:
    """One DuckDB SELECT over your own result handles, each exposed as a table: handles={"t": "<handle>", ...}.
    In-memory only; amounts must be grouped by their currency. Returns {handle, summary}."""
    raise NotImplementedError


async def record_answer(question: str, plan: str, handles: list[str]) -> CallToolResult:
    """Record how a question was answered (query history), citing at least one of your own live result handles. The
    gateway stores the metrics and dimensions of the handles, never your plan text or any result values. Returns a
    record_id; the answer only becomes verified history when the user confirms it."""
    raise NotImplementedError


async def confirm_answer(record_id: str) -> CallToolResult:
    """The user confirmed this answer (record_id from record_answer). Only your own metric-backed answers can be
    confirmed."""
    raise NotImplementedError


async def lineage(handle: str) -> CallToolResult:
    """The part of the context graph behind one of your own result handles (metric, dimensions, tables, columns,
    source, terms, past questions), filtered to what your role may see. For the UI; never needed to answer."""
    raise NotImplementedError


_TOOL_FUNCS = {f.__name__: f for f in (search_context, run_metric, query_source, get_rows, combine, record_answer,
                                       confirm_answer, lineage)}
WRITE_TOOLS = frozenset({"record_answer", "confirm_answer"})


def _register_tools(mcp: MCPServer) -> None:
    for name in TOOLS:
        mcp.add_tool(_TOOL_FUNCS[name], name=name, annotations=None if name in WRITE_TOOLS else READ_ONLY)
        # one source of truth: the advertised schema is the model Gateway validates with
        mcp._tool_manager.get_tool(name).parameters = ARG_MODELS[name].model_json_schema()


# ------------------------------------------------------------------------------------------------ app
def create_app(settings: Settings | None = None, *, gateway: Gateway | None = None,
               runtime: Runtime | None = None) -> tuple[MCPServer, Starlette]:
    """`gateway`: a ready Gateway (tests). `runtime`: builds one in the lifespan (create_app_from_env)."""
    settings = settings or Settings()
    check_startup_secrets(settings)
    state = SimpleNamespace(gateway=gateway)
    mcp = GatewayMCP(
        "prism-gateway",
        instructions=("Governed, role-filtered access to the Prism data platforms. Call search_context first, prefer "
                      "run_metric, keep rows out of the conversation (handles + summaries; get_rows pages), and "
                      "combine handles server-side. Your identity and entitlements come from your session."),
        token_verifier=GatewayTokenVerifier(GATEWAY_AUDIENCE, settings.jwt_secret.get_secret_value()),
        auth=AuthSettings(issuer_url="https://prism.invalid", resource_server_url=None),
        state=state,
    )
    _register_tools(mcp)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        """Public: liveness, catalog freshness and audit health only (no version, counts or names). `audit` is
        degraded while an audit / query-log row was dropped within the last AUDIT_DEGRADED_FOR_S seconds."""
        gw = state.gateway
        if gw is None:
            return JSONResponse({"status": "starting", "service": "gateway", "catalog": None, "audit": None})
        stale = gw.catalog_stale
        degraded = getattr(gw.audit, "degraded", None)
        audit_down = bool(degraded()) if callable(degraded) else False
        return JSONResponse({"status": "degraded" if stale or audit_down else "ok", "service": "gateway",
                             "catalog": "stale" if stale else "current", "audit": "degraded" if audit_down else "ok"})

    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=list(settings.mcp_allowed_hosts), allowed_origins=[])
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                  transport_security=security)
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette):
        close = None
        async with inner(a):
            try:
                if state.gateway is None and runtime is not None:
                    state.gateway, close = await runtime()
                gw = state.gateway
                refresher = asyncio.create_task(gw.refresh_forever()) if gw is not None and \
                    gw.catalog_probe is not None else None
                try:
                    yield
                finally:
                    if refresher is not None:
                        refresher.cancel()
                        with anyio.CancelScope(shield=True):
                            await asyncio.gather(refresher, return_exceptions=True)
            finally:
                if close is not None:
                    with anyio.CancelScope(shield=True):  # the pool and the driver must not leak
                        await close()

    app.router.lifespan_context = lifespan
    app.add_middleware(BodyLimitMiddleware)
    return mcp, app


# ------------------------------------------------------------------------------------------------ startup
def check_startup_secrets(settings: Settings) -> None:
    """Defence in depth over Settings' own validator (a model_construct()ed Settings skips it)."""
    if settings.env != "production":
        return
    fields = type(settings).model_fields
    for name in DEV_SECRET_FIELDS:
        env = f"PRISM_{name.upper()}"
        value, default = getattr(settings, name, None), fields[name].default
        plain = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        plain_default = default.get_secret_value() if hasattr(default, "get_secret_value") else default
        if plain == plain_default:
            raise GatewayStartupError(f"{name} is the dev default; set {env} before running the gateway in production")


def load_embedder(settings: Settings) -> Embedder:
    try:
        embedder = Embedder(settings.embed_model, cache_dir=settings.embed_cache_dir).load()
        embedder.warmup()
    except EmbedderError as exc:
        raise GatewayStartupError(f"embedding model unavailable: run `make models` ({exc})") from None
    return embedder


def load_startup_catalog(driver, settings: Settings) -> Catalog:
    try:
        catalog = load_catalog(driver, settings.graph_ns, timeout_s=max(settings.neo4j_timeout_s, 10.0))
    except CatalogError as exc:
        raise GatewayStartupError(f"refusing to start on a stale context graph: {exc}; run `make graph`") from None
    except (GraphUnavailable, GraphError) as exc:
        raise GatewayStartupError(f"could not read the context graph ({type(exc).__name__}): run `make db`, "
                                  f"then `make graph`") from None
    if not catalog.metrics:
        raise GatewayStartupError(f"the context graph (namespace {settings.graph_ns!r}) has no metrics: "
                                  f"run `make graph`")
    return catalog


def prepare_runtime(settings: Settings) -> Runtime:
    """Synchronous startup checks (fail before the port is bound); returns the lifespan's async half."""
    check_startup_secrets(settings)
    embedder = load_embedder(settings)
    try:
        driver = GraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth(),
                                      connection_timeout=settings.neo4j_timeout_s, notifications_min_severity="OFF")
    except (ConfigurationError, ValueError) as exc:
        raise GatewayStartupError(f"invalid PRISM_NEO4J_URI {redact_uri(settings.neo4j_uri)} "
                                  f"({type(exc).__name__}); credentials go in PRISM_NEO4J_USER / "
                                  f"PRISM_NEO4J_PASSWORD") from None
    try:
        try:
            driver.verify_connectivity()
        except (ServiceUnavailable, AuthError, OSError) as exc:
            raise GatewayStartupError(f"Neo4j not reachable at {redact_uri(settings.neo4j_uri)} ({type(exc).__name__}): "
                                      f"run `make db`") from None
        catalog = load_startup_catalog(driver, settings)
    finally:
        driver.close()
    try:
        migrate_app(settings)
    except (psycopg.Error, OSError) as exc:
        raise GatewayStartupError(f"could not migrate the app database ({type(exc).__name__}): is Postgres up? "
                                  f"run `make db`") from None

    async def start() -> tuple[Gateway, Callable[[], Awaitable[None]]]:
        adriver = AsyncGraphDatabase.driver(settings.neo4j_uri, auth=settings.neo4j_auth(),
                                            connection_timeout=settings.neo4j_timeout_s,
                                            notifications_min_severity="OFF")
        pool = await open_audit_pool(settings)
        audit = AuditWriter(pool, store_questions=settings.store_questions,
                            hmac_key=settings.audit_hmac_key.get_secret_value())
        timeout = min(settings.neo4j_timeout_s, GRAPH_STATEMENT_TIMEOUT_S)

        async def context(question: str, claims: dict, k: int) -> dict:
            return await acontext_pack(question, claims, driver=adriver, embedder=embedder, ns=settings.graph_ns,
                                       k=k, timeout_s=timeout)

        async def probe() -> int | None:
            rows = await arun_read(adriver, VERSION_CYPHER, {"ns": settings.graph_ns}, timeout)
            return rows[0]["version"] if rows else None

        async def loader() -> Catalog:
            return await aload_catalog(adriver, settings.graph_ns, timeout_s=max(timeout, 10.0))

        store = ResultStore(max_bytes=settings.gateway_store_mb * 2**20,
                            max_bytes_per_sub=settings.gateway_store_per_sub_mb * 2**20)
        # audit=None: the Gateway writes the one audit row per tool call (service.py); Downstream writes per-source
        # `source.<tool>` rows only when handed a writer (tests), so the running gateway records no source.* rows.
        async def lineage_fn(plans, sources, claims, combined):
            return await alineage(adriver, plans, sources, claims, combined_inputs=combined, ns=settings.graph_ns,
                                  timeout_s=timeout)

        gateway = Gateway(settings, policy=Policy(catalog), store=store, downstream=Downstream(settings, audit=None),
                          audit=audit, context=context, lineage=lineage_fn, catalog_probe=probe,
                          catalog_loader=loader)

        async def close() -> None:
            try:
                await pool.close()
            finally:
                await adriver.close()

        log.info(json.dumps({"event": "gateway_started", "catalog_version": catalog.version,
                             "metrics": len(catalog.metrics), "roles": len(catalog.roles)}))
        return gateway, close

    return start


def create_app_from_env() -> Starlette:
    """uvicorn --factory entry point. A failed check exits with its message (no traceback)."""
    configure_audit_logging()
    logging.basicConfig(level=logging.INFO)
    try:
        settings = load_settings()   # messages only on refusal: pydantic's own text quotes the environment's secrets
        runtime = prepare_runtime(settings)
    except (GatewayStartupError, ConfigError) as exc:
        raise SystemExit(f"prism gateway: {exc}") from None
    return create_app(settings, runtime=runtime)[1]


__all__ = ["GATEWAY_AUDIENCE", "Gateway", "GatewayStartupError", "TOOLS", "check_startup_secrets", "create_app",
           "create_app_from_env", "load_embedder", "load_startup_catalog", "prepare_runtime"]
