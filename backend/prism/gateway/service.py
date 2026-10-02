"""The Semantic Gateway's eleven tools, independent of the MCP transport (prism.gateway.server wires them up).

`Gateway.call_tool(name, claims, arguments)` is the single entry point. `claims` are the VERIFIED token claims (the
caller's identity never comes from tool arguments). Every call, including unknown tools, invalid arguments, refusals,
timeouts, internal errors and cancellation, writes exactly one audit row (shielded from cancellation). The audit row
carries catalog names only: a metric id / dimension list / source appear only once the policy accepted them, a result
handle only once the store returned it, and the question only as its HMAC (audit_hmac_key), computed here so the raw
text never reaches the audit path (record_answer hands it to the query log, which keeps it only if store_questions).

record_answer / confirm_answer trust: record_answer always stores verified=false. It stores metric_backed=true only
when the caller passed >= 1 handle, every handle is its own and still live, and EVERY handle resolves to catalog
metrics (a run_metric result, or a combine whose inputs all do and are still stored; a query_source result never
does). It returns a fresh record_id. confirm_answer(record_id) is the human confirmation (the UI's thumbs-up through
the agent): it sets verified=true only on the caller's own metric-backed, ok row, answers not_confirmable for every
other case (never saying which), and needs no live handle. Both are rate-limited per caller. The metric ids and
dimensions always come from the handles, never from the caller. A dropped insert is reported (`record_failed`), never
answered with recorded=true.

Errors come back as an `is_error` result `Error executing tool <name>: <code>: <message>`, where the message is a
GatewayError's caller-safe text; anything else becomes `internal_error` with a reference logged server-side (exception
type only). Argument validation uses the tool's own pydantic model (`ARG_MODELS`, also the advertised input schema);
its messages name the argument and the rule, never the value.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Protocol

import anyio
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError

from prism.gateway.audit import RECORD_ID, AuditUnavailable, question_hash, valid_caller_id
from prism.gateway.combine import MAX_SQL_CHARS
from prism.gateway.combine import combine as combine_results
from prism.gateway.errors import GatewayError
from prism.gateway.limits import Limiter, run_in_thread_holding
from prism.gateway.policy import MAX_DIMENSIONS, MAX_FILTERS, MAX_LIMIT, Policy, is_metrics_only
from prism.gateway.results import PAGE_DEFAULT, PAGE_MAX, ResultStore
from prism.graph.catalog import Catalog, CatalogError
from prism.graph.retrieval import GraphError, GraphUnavailable
from prism.graph.traces import TraceOwned, args_json

log = logging.getLogger("prism.gateway")

TOOLS = ("search_context", "run_metric", "query_source", "get_rows", "combine", "record_answer", "confirm_answer",
         "lineage", "record_trace", "get_trace", "mark_trace_confirmed")
MAX_TRACE_STEPS = 40
TRACE_TEXT = {"label": 200, "note": 500, "question": 2000, "answer": 2000}
TRACE_ARGS_CHARS = 2000
TRACE_CONSIDERED = 20
RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
TRACE_KINDS = ("context", "metric", "query", "combine", "delegate", "visualize", "answer", "refusal", "error")
MAX_QUESTION_CHARS = 2000
MAX_PLAN_CHARS = 4000
MAX_ITEMS = 20
MAX_ARGUMENT_BYTES = 64 * 1024     # the whole arguments object as JSON
MAX_RECORD_HANDLES = 20
MAX_REQUEST_KEYS = 20
MAX_NAME = 128
CONTEXT_TIMEOUT_S = 4.5            # search_context answers (or fails) within 5 s
CATALOG_STALE_AFTER = 5            # consecutive failed refreshes before the catalog is reported stale (/healthz)
COMBINE_CONCURRENCY = 2            # each DuckDB engine may use 256 MB
COMBINE_PER_SUB = 1                # one combine thread per caller (a cancelled one counts until its thread ends)
COMBINE_QUEUE = 8                  # callers waiting for a combine permit; past that: gateway_busy
# search_context: the embedder (4 threads) and the graph are shared by everyone. A caller gets CONTEXT_PER_SUB calls
# in flight and CONTEXT_RATE_PER_MIN a minute (bursts of CONTEXT_BURST); an embedder thread that outlives the timeout
# is therefore bounded by the caller's rate, not by how fast it can retry.
CONTEXT_CONCURRENCY = 8
CONTEXT_PER_SUB = 2
CONTEXT_QUEUE = 16
CONTEXT_RATE_PER_MIN = 60
CONTEXT_BURST = 20
# record_answer feeds the query history: a caller records RECORD_RATE_PER_MIN answers a minute (bursts of RECORD_BURST)
# so one sub cannot flood app.query_log; past that `rate_limited`, like every other per-caller limit.
RECORD_CONCURRENCY = 16
RECORD_PER_SUB = 2
RECORD_QUEUE = 32
RECORD_RATE_PER_MIN = 10
RECORD_BURST = 10
QUERY_SHAPE = ('query_source takes request={"sql": "SELECT ..."} for SQL sources or '
               'request={"endpoint_id": "...", "params": {...}} for REST sources')

ContextFn = Callable[[str, dict, int], Awaitable[dict]]
# plans (metric id -> dimensions used), free-form sources, claims (metrics_only fail-closed), combine root targets
LineageFn = Callable[[dict[str, set[str]], list[str], dict, list[str] | None], Awaitable[dict]]


# ------------------------------------------------------------------------------------------------ arguments
class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchContextArgs(_Args):
    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS,
                                   description="The user's question in plain language")]
    max_items: Annotated[StrictInt, Field(description=f"Items per kind to retrieve (clamped to 1..{MAX_ITEMS})")] = 8


class RunMetricArgs(_Args):
    metric_id: Annotated[str, Field(min_length=1, max_length=MAX_NAME,
                                    description="Governed metric id (from search_context)")]
    dimensions: Annotated[list[Annotated[str, Field(max_length=MAX_NAME)]],
                          Field(max_length=MAX_DIMENSIONS, description="Dimension names to group by")] = []
    filters: Annotated[dict[str, Any], Field(max_length=MAX_FILTERS,
                                             description="Filter name -> value | list | {gte,lte,between,ne}")] = {}
    limit: Annotated[StrictInt | None, Field(ge=1, le=MAX_LIMIT, description="Max rows")] = None


class QuerySourceArgs(_Args):
    source: Annotated[str, Field(min_length=1, max_length=64, description="Source name")]
    request: Annotated[dict[str, Any], Field(max_length=MAX_REQUEST_KEYS, description=(
        "SQL sources: {'sql': '<single SELECT>'}. REST sources: {'endpoint_id': ..., 'params': {...}}"))]


class GetRowsArgs(_Args):
    handle: Annotated[str, Field(min_length=1, max_length=64, description="Result handle")]
    offset: Annotated[StrictInt, Field(ge=0, description="First row (0-based)")] = 0
    limit: Annotated[StrictInt, Field(ge=1, le=PAGE_MAX, description=f"Rows per page (max {PAGE_MAX})")] = PAGE_DEFAULT


class CombineArgs(_Args):
    sql: Annotated[str, Field(min_length=1, max_length=MAX_SQL_CHARS,
                              description="One DuckDB SELECT over the named tables")]
    handles: Annotated[dict[str, Annotated[str, Field(max_length=64)]],
                       Field(min_length=1, max_length=8, description="Table name -> result handle")]


class RecordAnswerArgs(_Args):
    question: Annotated[str, Field(min_length=1, max_length=MAX_QUESTION_CHARS, description="The question answered")]
    plan: Annotated[str, Field(max_length=MAX_PLAN_CHARS, description=(
        "How it was answered (not stored: the gateway records the metrics and dimensions of the handles)"))]
    handles: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]],
                       Field(min_length=1, max_length=MAX_RECORD_HANDLES,
                             description="Your own live result handles the answer used (at least one)")]


class ConfirmAnswerArgs(_Args):
    record_id: Annotated[str, Field(pattern=f"^{RECORD_ID.pattern}$",
                                    description="The record_id record_answer returned for this answer")]


class LineageArgs(_Args):
    handle: Annotated[str, Field(min_length=1, max_length=64, description="One of your own result handles")]


class TraceStepArgs(_Args):
    seq: Annotated[StrictInt, Field(ge=0, le=999)]
    parent: Annotated[StrictInt | None, Field(ge=0, le=999)] = None
    kind: Annotated[str, Field(pattern="^(" + "|".join(TRACE_KINDS) + ")$")]
    label: Annotated[str, Field(max_length=4000)] = ""
    note: Annotated[str | None, Field(max_length=4000)] = None
    considered: Annotated[list[Annotated[str, Field(max_length=MAX_NAME)]], Field(max_length=100)] = []
    ms: Annotated[float | None, Field(ge=0)] = None
    status: Annotated[str | None, Field(max_length=32)] = None
    error_code: Annotated[str | None, Field(max_length=64)] = None
    tool: Annotated[str | None, Field(max_length=64)] = None
    args: Any = None
    handle: Annotated[str | None, Field(max_length=64)] = None
    touched: Any = None          # accepted and ignored: links come from handles only


class RecordTraceArgs(_Args):
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)]
    question: Annotated[str, Field(max_length=8000)]
    answer: Annotated[str, Field(max_length=8000)] = ""
    path: Annotated[str | None, Field(max_length=32)] = None
    status: Annotated[str, Field(max_length=32)]
    steps: Annotated[list[TraceStepArgs], Field(max_length=MAX_TRACE_STEPS)]


class RunIdArgs(_Args):
    run_id: Annotated[str, Field(pattern=RUN_ID_PATTERN)]


class TraceStore(Protocol):
    async def record(self, trace: dict, claims: dict) -> int: ...
    async def get(self, run_id: str, claims: dict) -> dict | None: ...
    async def confirm(self, run_id: str, sub: str) -> bool: ...


ARG_MODELS: dict[str, type[_Args]] = {
    "search_context": SearchContextArgs, "run_metric": RunMetricArgs, "query_source": QuerySourceArgs,
    "get_rows": GetRowsArgs, "combine": CombineArgs, "record_answer": RecordAnswerArgs,
    "confirm_answer": ConfirmAnswerArgs, "lineage": LineageArgs,
    "record_trace": RecordTraceArgs, "get_trace": RunIdArgs, "mark_trace_confirmed": RunIdArgs,
}
_JSON_FIELDS = {name: {f for f, info in model.model_fields.items() if info.annotation is not str}
                for name, model in ARG_MODELS.items()}


def _short(part: object) -> str:
    s = str(part)
    return s if len(s) <= 32 else s[:32] + "..."


def _validation_message(exc: ValidationError) -> str:
    """Argument names and rules only: pydantic's own text quotes the rejected value."""
    parts = [f"{'.'.join(_short(p) for p in e['loc'][:3]) or 'arguments'}: {e['msg']}" for e in exc.errors()[:3]]
    more = len(exc.errors()) - 3
    return "invalid arguments: " + "; ".join(parts) + (f" (+{more} more)" if more > 0 else "")


def _pre_parse(name: str, arguments: dict) -> dict:
    """Some clients send list/object arguments as JSON text; parse those (only for non-string fields)."""
    out = dict(arguments)
    for key in _JSON_FIELDS.get(name, ()):
        value = out.get(key)
        if isinstance(value, str) and value[:1] in "[{":
            try:
                out[key] = json.loads(value)
            except (ValueError, RecursionError):
                pass
    return out


# ------------------------------------------------------------------------------------------------ audit record
@dataclass
class CallRecord:
    tool: str
    status: str = "error"
    error_code: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


def _reply(payload: Any) -> CallToolResult:
    data = payload if isinstance(payload, dict) else {"result": payload}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data, separators=(",", ":"),
                                                                            default=str))],
                          structured_content=data)


def _error(tool: str, code: str, message: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=f"Error executing tool {tool}: {code}: {message}")],
                          is_error=True)


# ------------------------------------------------------------------------------------------------ gateway
class Gateway:
    def __init__(self, settings, *, policy: Policy, store: ResultStore, downstream, audit, context: ContextFn,
                 lineage: LineageFn | None = None, traces: TraceStore | None = None,
                 context_timeout_s: float = CONTEXT_TIMEOUT_S,
                 catalog_probe: Callable[[], Awaitable[int | None]] | None = None,
                 catalog_loader: Callable[[], Awaitable[Catalog]] | None = None,
                 refresh_s: float | None = None):
        self.settings = settings
        self.policy = policy   # replaced as a whole on catalog refresh; each call reads it once
        self.store, self.downstream, self.audit, self.context = store, downstream, audit, context
        self.lineage = lineage
        self.traces = traces
        self.context_timeout_s = context_timeout_s
        self.catalog_probe, self.catalog_loader = catalog_probe, catalog_loader
        self.refresh_s = refresh_s if refresh_s is not None else settings.gateway_catalog_refresh_s
        self.refresh_failures = 0   # consecutive; reset by any refresh that reached the graph and agreed
        self.context_limiter = Limiter("search_context", total=CONTEXT_CONCURRENCY, per_sub=CONTEXT_PER_SUB,
                                       queue=CONTEXT_QUEUE, rate_per_min=CONTEXT_RATE_PER_MIN, burst=CONTEXT_BURST)
        self.combine_limiter = Limiter("combine", total=COMBINE_CONCURRENCY, per_sub=COMBINE_PER_SUB,
                                       queue=COMBINE_QUEUE)
        self.record_limiter = Limiter("record_answer", total=RECORD_CONCURRENCY, per_sub=RECORD_PER_SUB,
                                      queue=RECORD_QUEUE, rate_per_min=RECORD_RATE_PER_MIN, burst=RECORD_BURST)
        self.trace_limiter = Limiter("record_trace", total=RECORD_CONCURRENCY, per_sub=RECORD_PER_SUB,
                                     queue=RECORD_QUEUE, rate_per_min=RECORD_RATE_PER_MIN, burst=RECORD_BURST)
        self.confirm_limiter = Limiter("confirm_answer", total=RECORD_CONCURRENCY, per_sub=RECORD_PER_SUB,
                                       queue=RECORD_QUEUE, rate_per_min=RECORD_RATE_PER_MIN, burst=RECORD_BURST)

    # -------------------------------------------------------------------------------------------- entry point
    async def call_tool(self, name: str, claims: dict, arguments: Any) -> CallToolResult:
        """One tool call, audited exactly once. `arguments` that are not an object are refused (invalid_request)."""
        rec = CallRecord(tool=name if name in TOOLS else "unknown")
        started = time.perf_counter()
        try:
            result = await self._dispatch(name, claims, arguments, rec)
            rec.status = "ok"
            return _reply(result)
        except GatewayError as exc:
            rec.error_code = exc.code
            return _error(name if name in TOOLS else _short(name), exc.code, str(exc))
        except asyncio.CancelledError:
            rec.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - opaque to the caller, type + ref logged here
            ref = uuid.uuid4().hex[:8]
            rec.error_code = "internal_error"
            log.error(json.dumps({"event": "gateway_internal_error", "tool": rec.tool, "ref": ref,
                                  "error_type": type(exc).__name__}))
            return _error(name if name in TOOLS else _short(name), "internal_error", f"internal error (ref {ref})")
        finally:
            await self._audit(claims, rec, started)

    async def _audit(self, claims: Any, rec: CallRecord, started: float) -> None:
        roles = claims.get("roles") if isinstance(claims, dict) else None
        event = {"sub": claims.get("sub") if isinstance(claims, dict) else None,
                 "persona": roles[0] if isinstance(roles, list) and roles else None,
                 "tool": rec.tool, "status": rec.status, "error_code": rec.error_code,
                 "ms": round((time.perf_counter() - started) * 1000, 1), **rec.fields}
        with anyio.CancelScope(shield=True):  # one row per call, cancellation included
            try:
                await self.audit.write(event)
            except Exception as exc:  # noqa: BLE001 - the writer never raises; a fake one might
                log.warning(json.dumps({"event": "audit_failed", "error_type": type(exc).__name__}))

    async def _dispatch(self, name: str, claims: dict, arguments: Any, rec: CallRecord) -> Any:
        if name not in TOOLS:
            raise GatewayError("unknown_tool", f"unknown tool; available: {list(TOOLS)}")
        if not isinstance(claims, dict) or not valid_caller_id(claims.get("sub")):
            raise GatewayError("not_permitted", "no valid caller identity")
        if not isinstance(arguments, dict):
            raise GatewayError("invalid_request", "arguments must be an object")
        try:
            size = len(json.dumps(arguments, allow_nan=False))   # NaN / Infinity are not JSON
        except (TypeError, ValueError, RecursionError):
            raise GatewayError("invalid_request", "arguments must be plain JSON") from None
        if size > MAX_ARGUMENT_BYTES:
            raise GatewayError("invalid_request", f"arguments over {MAX_ARGUMENT_BYTES // 1024} KB; narrow the request")
        if name == "query_source" and ("sql" in arguments or isinstance(arguments.get("request"), str)):
            raise GatewayError("invalid_request", QUERY_SHAPE)
        try:
            args = ARG_MODELS[name].model_validate(_pre_parse(name, arguments))
        except ValidationError as exc:
            raise GatewayError("invalid_request", _validation_message(exc)) from None
        return await getattr(self, f"_{name}")(claims, args, rec)

    def _hash(self, question: str) -> str:
        return question_hash(question, self.settings.audit_hmac_key.get_secret_value())

    # -------------------------------------------------------------------------------------------- tools
    async def _search_context(self, claims: dict, args: SearchContextArgs, rec: CallRecord) -> dict:
        rec.fields["question_hash"] = self._hash(args.question)  # the raw question never reaches the audit
        # retrieval reads a missing metrics_only claim as unrestricted; the gateway fails closed
        safe = {**claims, "metrics_only": is_metrics_only(claims)}
        release = await self.context_limiter.acquire(claims["sub"])   # refused before anything is embedded
        try:
            async with asyncio.timeout(self.context_timeout_s):
                return await self.context(args.question, safe, max(1, min(args.max_items, MAX_ITEMS)))
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()

    async def _run_metric(self, claims: dict, args: RunMetricArgs, rec: CallRecord) -> dict:
        plan = self.policy.check_metric(claims, args.metric_id, args.dimensions, args.filters, limit=args.limit)
        rec.fields.update(source=plan.source, metric_id=plan.metric_id, dimensions=list(plan.arguments["dimensions"]))
        res = await self.downstream.run_metric(claims, plan)
        rec.fields.update(rows=res.row_count, truncated=res.truncated)
        meta = {"source": res.source, "metric_id": res.metric_id, "truncated": res.truncated,
                "units": {"value": res.unit} if res.unit else {}, "plan": plan.audit_plan()}
        if res.as_of:
            meta["as_of"] = res.as_of
        return self._stored(claims, rec, res.columns, res.rows, meta)

    async def _query_source(self, claims: dict, args: QuerySourceArgs, rec: CallRecord) -> dict:
        self.policy.check_query(claims, args.source)
        rec.fields["source"] = args.source
        res = await self.downstream.query(claims, args.source, args.request)
        rec.fields.update(rows=res.row_count, truncated=res.truncated)
        if not res.columns:
            raise GatewayError("empty_result", "the query returned no columns")
        return self._stored(claims, rec, res.columns, res.rows, {"source": res.source, "truncated": res.truncated})

    def _stored(self, claims: dict, rec: CallRecord, columns, rows, meta: dict) -> dict:
        handle = self.store.put(claims["sub"], columns, rows, meta)
        rec.fields["result_handle"] = handle
        summary = self.store.summary(claims["sub"], handle)
        rec.fields["bytes"] = self.store.get(claims["sub"], handle).nbytes
        return {"handle": handle, "summary": summary}

    async def _get_rows(self, claims: dict, args: GetRowsArgs, rec: CallRecord) -> dict:
        page = self.store.page(claims["sub"], args.handle, args.offset, args.limit)
        rec.fields.update(result_handle=page["handle"], rows=len(page["rows"]))
        return page

    async def _combine(self, claims: dict, args: CombineArgs, rec: CallRecord) -> dict:
        rec.fields["source"] = "combine"
        sub = claims["sub"]
        # the permit is released when the DuckDB thread finishes (it runs up to combine's 5 s interrupt even when
        # this call is cancelled), so cancel-and-retry can never run more than COMBINE_CONCURRENCY engines
        release = await self.combine_limiter.acquire(sub)
        handle = await run_in_thread_holding(release, combine_results, self.store, sub, args.sql,
                                             dict(args.handles))
        rec.fields["result_handle"] = handle
        summary = self.store.summary(sub, handle)
        rec.fields.update(rows=summary["row_count"], truncated=summary["truncated"])
        return {"handle": handle, "summary": summary}

    async def _record_answer(self, claims: dict, args: RecordAnswerArgs, rec: CallRecord) -> dict:
        rec.fields["question_hash"] = self._hash(args.question)
        sub = claims["sub"]
        release = await self.record_limiter.acquire(sub)   # refused (rate_limited) before anything is checked
        try:
            return await self._record(claims, args, sub)
        finally:
            release()

    async def _record(self, claims: dict, args: RecordAnswerArgs, sub: str) -> dict:
        handles = list(dict.fromkeys(args.handles))
        for h in handles:
            self.store.get(sub, h)  # another sub's, expired or made-up handle: "unknown handle", nothing logged
        metric_ids, dimensions = self._structured_plan(sub, handles)
        # metric_backed: EVERY handle resolves to governed metrics (D5a: a query_source result, or a combine over
        # one, carries rows the history gate never sees). verified is never set here: only confirm_answer sets it.
        metric_backed = bool(handles) and bool(metric_ids) and all(self._metric_backed(sub, h) for h in handles)
        record_id = str(uuid.uuid4())
        roles = claims.get("roles")
        stored = await self.audit.log_query({
            "sub": sub, "persona": roles[0] if isinstance(roles, list) and roles else None,
            "question": args.question, "plan": {"metric_ids": metric_ids, "dimensions": dimensions},
            "handles": handles, "metric_ids": metric_ids, "verified": False, "metric_backed": metric_backed,
            "record_id": record_id, "status": "ok"})
        if stored is not True:
            raise GatewayError("record_failed", "the answer could not be recorded; try again shortly")
        return {"recorded": True, "record_id": record_id, "metric_backed": metric_backed,
                "metric_ids": metric_ids, "dimensions": dimensions}

    async def _confirm_answer(self, claims: dict, args: ConfirmAnswerArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        release = await self.confirm_limiter.acquire(sub)
        try:
            try:
                confirmed = await self.audit.confirm_answer(sub, args.record_id)
            except AuditUnavailable:
                raise GatewayError("confirm_failed", "the answer could not be confirmed; try again shortly") from None
        finally:
            release()
        if not confirmed:   # unknown, another caller's, not metric-backed or not ok: one answer for all of them
            raise GatewayError("not_confirmable", "this answer cannot be confirmed")
        return {"confirmed": True}

    async def _lineage(self, claims: dict, args: LineageArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        plans, sources, combined = self._lineage_plan(sub, args.handle)   # unknown_handle before any graph read
        rec.fields["result_handle"] = args.handle
        if self.lineage is None:
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly")
        safe = {**claims, "metrics_only": is_metrics_only(claims)}
        release = await self.context_limiter.acquire(sub)   # graph reads share the search_context budget
        try:
            async with asyncio.timeout(self.context_timeout_s):
                graph = await self.lineage(plans, sources, safe, combined)
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()
        rec.fields["rows"] = len(graph["nodes"])
        return {**graph, "governed": bool(plans)}

    def _trace_touched(self, sub: str, handle: str | None) -> tuple[list[str], list[str], int | None, bool | None]:
        """(touched local uids, answered metric uids, rows, truncated) of one of the caller's own handles; nothing
        for another caller's, an expired or a made-up handle."""
        if not handle:
            return [], [], None, None
        try:
            entry = self.store.get(sub, handle)
            plans, sources, _ = self._lineage_plan(sub, handle)
        except GatewayError:
            return [], [], None, None
        metrics = [f"metric:{m}" for m in sorted(plans)]
        dims = [f"dim:{m}.{d}" for m in sorted(plans) for d in sorted(plans[m])]
        touched = metrics + dims + [f"source:{x}" for x in sources]
        return touched, metrics, len(entry.rows), bool(entry.meta.get("truncated", False))

    def _require_traces(self) -> TraceStore:
        if self.traces is None:
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly")
        return self.traces

    async def _record_trace(self, claims: dict, args: RecordTraceArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        store = self._require_traces()
        steps, answered = [], []
        for st in args.steps:
            touched, metrics, rows, truncated = self._trace_touched(sub, st.handle)
            answered += [m for m in metrics if m not in answered]
            steps.append({"seq": st.seq, "parent": st.parent, "kind": st.kind,
                          "label": st.label[:TRACE_TEXT["label"]],
                          "note": st.note[:TRACE_TEXT["note"]] if st.note else None,
                          "considered": list(st.considered[:TRACE_CONSIDERED]), "ms": st.ms, "status": st.status,
                          "error_code": st.error_code, "tool": st.tool,
                          "args_json": args_json(st.args, TRACE_ARGS_CHARS), "handle": st.handle if rows is not None
                          else None, "rows": rows, "truncated": truncated, "touched": touched})
        trace = {"run_id": args.run_id, "sub": sub, "question": args.question[:TRACE_TEXT["question"]],
                 "answer": args.answer[:TRACE_TEXT["answer"]], "path": args.path, "status": args.status,
                 "answered": answered, "steps": steps}
        safe = {**claims, "metrics_only": is_metrics_only(claims)}
        release = await self.trace_limiter.acquire(sub)
        try:
            n = await store.record(trace, safe)
        except TraceOwned:
            raise GatewayError("invalid_request", "invalid trace") from None
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()
        rec.fields["rows"] = n
        return {"recorded": True, "steps": n}

    async def _get_trace(self, claims: dict, args: RunIdArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        store = self._require_traces()
        safe = {**claims, "metrics_only": is_metrics_only(claims)}
        release = await self.context_limiter.acquire(sub)
        try:
            async with asyncio.timeout(self.context_timeout_s):
                trace = await store.get(args.run_id, safe)
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()
        if trace is None:
            raise GatewayError("unknown_trace", "unknown trace")
        rec.fields["rows"] = len(trace.get("steps") or [])
        return trace

    async def _mark_trace_confirmed(self, claims: dict, args: RunIdArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        store = self._require_traces()
        release = await self.trace_limiter.acquire(sub)
        try:
            ok = await store.confirm(args.run_id, sub)
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()
        if not ok:
            raise GatewayError("unknown_trace", "unknown trace")
        return {"confirmed": True}

    def _metric_backed(self, sub: str, handle: str, depth: int = 0) -> bool:
        """True when `handle` is a run_metric result whose recorded plan names >= 1 catalog metric (all of them in the
        catalog), or a combine whose every input is still stored and itself metric-backed. Fails closed: a
        query_source result, an expired input, an unknown metric or a deep chain is not metric-backed."""
        if depth > 8:
            return False
        try:
            entry = self.store.get(sub, handle)
        except GatewayError:
            return False
        plan = entry.meta.get("plan")
        if isinstance(plan, dict):
            ids = plan.get("metric_ids") or ()
            return bool(ids) and all(mid in self.policy.catalog.metrics for mid in ids)
        inputs = entry.meta.get("inputs")
        if isinstance(inputs, (list, tuple)) and inputs:
            return all(isinstance(i, str) and self._metric_backed(sub, i, depth + 1) for i in inputs)
        return False

    def _structured_plan(self, sub: str, handles: list[str]) -> tuple[list[str], list[str]]:
        """Catalog metric ids and dimension names behind the handles (combine outputs: through their inputs that are
        still stored). The caller's free-text plan is never used."""
        catalog = self.policy.catalog
        metrics: set[str] = set()
        dims: set[str] = set()
        seen: set[str] = set()
        todo = list(handles)
        while todo and len(seen) < 64:
            h = todo.pop()
            if h in seen:
                continue
            seen.add(h)
            try:
                entry = self.store.get(sub, h)
            except GatewayError:
                continue  # an expired input of a combine: its metrics are simply not recorded
            plan = entry.meta.get("plan")
            if isinstance(plan, dict):
                for mid in plan.get("metric_ids") or ():
                    metric = catalog.metrics.get(mid)
                    if metric is not None:
                        metrics.add(mid)
                        dims.update(d for d in plan.get("dimensions") or () if d in metric.dimensions)
            inputs = entry.meta.get("inputs")
            if isinstance(inputs, (list, tuple)):
                todo.extend(i for i in inputs if isinstance(i, str))
        return sorted(metrics), sorted(dims)

    def _lineage_plan(self, sub: str, handle: str) -> tuple[dict[str, set[str]], list[str], list[str] | None]:
        """Per-metric dimensions and the sources of free-form results behind `handle` (combine outputs: through
        their inputs that are still stored; an expired input is skipped), plus, for a combine, the local uids its
        root links to. Raises unknown_handle for another caller's, an expired or a made-up handle."""
        top = self.store.get(sub, handle)
        catalog = self.policy.catalog
        plans: dict[str, set[str]] = {}
        sources: set[str] = set()
        seen: set[str] = set()
        todo = [handle]
        while todo and len(seen) < 64:
            h = todo.pop()
            if h in seen:
                continue
            seen.add(h)
            try:
                entry = self.store.get(sub, h)
            except GatewayError:
                continue
            plan, inputs, source = entry.meta.get("plan"), entry.meta.get("inputs"), entry.meta.get("source")
            if isinstance(plan, dict):
                for mid in plan.get("metric_ids") or ():
                    metric = catalog.metrics.get(mid)
                    if metric is not None:
                        plans.setdefault(mid, set()).update(
                            d for d in plan.get("dimensions") or () if d in metric.dimensions)
            elif isinstance(inputs, (list, tuple)):
                todo.extend(i for i in inputs if isinstance(i, str))
            elif isinstance(source, str):
                sources.add(source)
        combined = None
        if isinstance(top.meta.get("inputs"), (list, tuple)):
            combined = [f"metric:{m}" for m in sorted(plans)] + [f"source:{s}" for s in sorted(sources)]
        return plans, sorted(sources), combined

    # -------------------------------------------------------------------------------------------- catalog
    @property
    def catalog_stale(self) -> bool:
        return self.refresh_failures >= CATALOG_STALE_AFTER

    def _refresh_failed(self) -> bool:
        self.refresh_failures += 1
        if self.refresh_failures % CATALOG_STALE_AFTER == 0:   # loud, but not on every tick
            log.error(json.dumps({"event": "catalog_stale", "consecutive_failures": self.refresh_failures,
                                  "catalog_version": self.policy.catalog.version,
                                  "action": "serving the last good catalog; check Neo4j and run `make graph`"}))
        return False

    async def refresh_catalog(self) -> bool:
        """Swap in a newer catalog when the graph's max(loaded_version) moved forward. On any failure (stale graph,
        graph down, empty graph, a version older than the current one) the current catalog stays in force, the
        failure is logged and counted; CATALOG_STALE_AFTER consecutive failures mark the catalog stale (/healthz)."""
        if self.catalog_probe is None or self.catalog_loader is None:
            return False
        current = self.policy.catalog
        try:
            version = await self.catalog_probe()
            if version is None:
                log.warning(json.dumps({"event": "catalog_refresh_failed", "reason": "the graph reports no version",
                                        "action": "keeping the current catalog"}))
                return self._refresh_failed()
            if version == current.version:
                self.refresh_failures = 0
                return False
            catalog = await self.catalog_loader()
        except CatalogError as exc:
            log.error(json.dumps({"event": "catalog_refresh_refused", "reason": str(exc)[:300],
                                  "action": "keeping the current catalog; run `make graph`"}))
            return self._refresh_failed()
        except (GraphUnavailable, GraphError) as exc:
            log.warning(json.dumps({"event": "catalog_refresh_failed", "error_type": type(exc).__name__,
                                    "action": "keeping the current catalog"}))
            return self._refresh_failed()
        if not catalog.metrics:
            log.error(json.dumps({"event": "catalog_refresh_refused", "reason": "the graph has no metrics",
                                  "action": "keeping the current catalog; run `make graph`"}))
            return self._refresh_failed()
        if current.version is not None and (catalog.version is None or catalog.version < current.version):
            log.error(json.dumps({"event": "catalog_refresh_refused", "reason": "the graph holds an older version",
                                  "current": current.version, "graph": catalog.version,
                                  "action": "keeping the current catalog; run `make graph`"}))
            return self._refresh_failed()
        self.policy = Policy(catalog)  # one assignment: a call sees the old snapshot or the new one
        self.refresh_failures = 0
        log.info(json.dumps({"event": "catalog_refreshed", "version": catalog.version,
                             "metrics": len(catalog.metrics)}))
        return True

    async def refresh_forever(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_s)
            try:
                await self.refresh_catalog()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive anything; the old catalog stays
                log.error(json.dumps({"event": "catalog_refresh_failed", "error_type": type(exc).__name__}))
                self._refresh_failed()


__all__ = ["ARG_MODELS", "CallRecord", "Gateway", "MAX_ARGUMENT_BYTES", "QUERY_SHAPE", "TOOLS", "LineageArgs",
           "LineageFn", "RecordTraceArgs", "RunIdArgs", "TraceStore"]
