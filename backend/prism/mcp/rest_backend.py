"""SourceBackend for the REST-fronted platforms. Each call re-mints a short-lived token for the downstream API
(audience `<source>-api`) from the caller's verified claims — the incoming token is never passed through — and lets
the API's own row-level security be the boundary. Endpoints and endpoint-backed metrics are declared in
rest/<source>.yaml; parameters are validated against that registry before any request is made."""
import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from prism.config import Settings
from prism.mcp.base import DescribeResult, MetricResult, QueryResult
from prism.mcp.metrics import guarded_filter_aliases, resolve_time_range
from prism.mcp.results import (TOO_MANY_CONCURRENT, SourceError, describe_notes, forward_claims, has_dataset,
                               jsonable, sensitive_dimension_error)
from prism.security.access import can
from prism.security.tokens import mint

REST_DIR = Path(__file__).parent / "rest"
TOKEN_TTL_S = 60
HTTP_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 2_000_000
TOTAL_DEADLINE_S = 15.0
DEFAULT_INT_MAX = 1_000_000
MAX_LIST_ITEMS = 20
MAX_INFLIGHT_PER_PRINCIPAL = 3
_PATH_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ORDER_BY = re.compile(r"^-?[A-Za-z_][A-Za-z0-9_]*$")


class Param(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["str", "int", "date", "str_list"] = "str"
    location: Literal["query", "path"] = "query"
    required: bool = False
    enum: list[str] | None = None
    min: int | None = None
    max: int | None = None


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    path: str
    table: str
    result_key: str | None
    description: str
    params: dict[str, Param]


class EndpointFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    param: str
    type: Literal["str", "int", "date"] = "str"


class EndpointMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    endpoint: str
    value_field: str
    description: str
    unit: str
    dimensions: dict[str, str]
    filters: dict[str, EndpointFilter]
    sensitive_dimensions: list[str] = []  # blocked for metrics-only principals, as for SQL metrics
    fine_grain_dimensions: list[str] = []  # identifier / date grain, as for SQL metrics (prism.gateway.policy)

    @model_validator(mode="after")
    def _sensitive_are_dimensions(self) -> "EndpointMetric":
        for key in ("sensitive_dimensions", "fine_grain_dimensions"):
            if bad := [d for d in getattr(self, key) if d not in self.dimensions]:
                raise ValueError(f"metric {self.id}: {key} {bad} are not dimensions of this metric")
        if bad := guarded_filter_aliases(self.dimensions, {n: f.param for n, f in self.filters.items()},
                                         self.sensitive_dimensions, self.fine_grain_dimensions):
            raise ValueError(f"metric {self.id}: {bad}")
        return self


def load_rest_config(source: str) -> tuple[dict[str, Endpoint], dict[str, EndpointMetric]]:
    raw = yaml.safe_load((REST_DIR / f"{source}.yaml").read_text())
    endpoints = {e["id"]: Endpoint.model_validate(e) for e in raw["endpoints"]}
    metrics = {m["id"]: EndpointMetric.model_validate(m) for m in raw["metrics"]}
    for m in metrics.values():
        if m.endpoint not in endpoints:
            raise ValueError(f"metric {m.id} references unknown endpoint {m.endpoint}")
    return endpoints, metrics


def _clean_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or len(value) > 200:
        raise SourceError(f"parameter {name!r} must be a string (<= 200 chars)")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SourceError(f"parameter {name!r} contains invalid characters") from None
    if "\x00" in value:
        raise SourceError(f"parameter {name!r} contains invalid characters")
    return value


def _coerce(name: str, value: Any, p: Param) -> Any:
    if p.type == "str_list":
        if not isinstance(value, (list, tuple)) or not value:
            raise SourceError(f"parameter {name!r} needs a non-empty list")
        out: list[str] = []
        for v in value:
            v = _clean_str(name, v)
            if p.enum and v not in p.enum:
                raise SourceError(f"parameter {name!r} must be a list of {p.enum}; got {v!r}")
            if v not in out:
                out.append(v)
                if len(out) > MAX_LIST_ITEMS:
                    raise SourceError(f"parameter {name!r} accepts at most {MAX_LIST_ITEMS} distinct values")
        return out
    if p.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SourceError(f"parameter {name!r} must be an integer")
        hi = p.max if p.max is not None else DEFAULT_INT_MAX
        if (p.min is not None and value < p.min) or value > hi:
            raise SourceError(f"parameter {name!r} must be between {p.min} and {hi}")
        return value
    if p.type == "date":
        if isinstance(value, date) and not isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str) and _ISO_DATE.fullmatch(value):
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                pass
        raise SourceError(f"parameter {name!r} must be an ISO date (YYYY-MM-DD)")
    value = _clean_str(name, value)
    if p.location == "path" and (not _PATH_VALUE.fullmatch(value) or value.strip(".") == ""):
        raise SourceError(f"parameter {name!r} must be 1-64 characters of letters, digits, '_', '.', ':' or '-'")
    if p.enum and value not in p.enum:
        raise SourceError(f"parameter {name!r} must be one of {p.enum}")
    return value


_TOO_BIG = "the source response exceeds the size limit; narrow the request"
_UNEXPECTED = "unexpected response from the source"


def _rows(data: dict, key: str | None) -> list[dict]:
    rows = data.get(key or "rows")
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SourceError(_UNEXPECTED)
    return rows


class RestBackend:
    kind = "rest"

    def __init__(self, source: str, settings: Settings, *, base_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = source
        self._settings = settings
        self._endpoints, self._metrics = load_rest_config(source)
        url = base_url or getattr(settings, f"{source}_api_url")
        self._http = httpx.AsyncClient(base_url=url, timeout=HTTP_TIMEOUT_S, transport=transport)
        self._closed = False
        self._inflight: dict[str, int] = {}

    async def aclose(self) -> None:
        self._closed = True
        await self._http.aclose()

    @asynccontextmanager
    async def _limit_inflight(self, claims: dict):
        """No await between the check and the increment (or in the decrement), so this is safe under asyncio."""
        if self._closed:
            raise SourceError("backend is closed")
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise SourceError("invalid principal claims")
        if self._inflight.get(sub, 0) >= MAX_INFLIGHT_PER_PRINCIPAL:
            raise SourceError(TOO_MANY_CONCURRENT)
        self._inflight[sub] = self._inflight.get(sub, 0) + 1
        try:
            yield
        finally:
            left = self._inflight[sub] - 1
            if left:
                self._inflight[sub] = left
            else:
                del self._inflight[sub]

    # ------------------------------------------------------------------ HTTP
    async def _get(self, claims: dict, ep: Endpoint, params: dict[str, Any]) -> dict:
        path = ep.path
        query: dict[str, Any] = {}
        for name, value in params.items():  # path values were validated by _validate; nothing here is caller-steerable
            if ep.params[name].location == "path":
                path = path.replace("{" + name + "}", value)
            else:
                query[name] = value
        token = mint(forward_claims(claims), f"{self.name}-api", self._settings.jwt_secret.get_secret_value(), ttl_s=TOKEN_TTL_S)
        try:
            async with asyncio.timeout(TOTAL_DEADLINE_S):
                async with self._http.stream("GET", path, params=query,
                                             headers={"Authorization": f"Bearer {token}"}) as resp:
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                        raise SourceError(_TOO_BIG)
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise SourceError(_TOO_BIG)
                    status = resp.status_code
        except TimeoutError:
            raise SourceError("the source API timed out") from None
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError) as exc:
            raise SourceError(f"{self.name} API unavailable ({type(exc).__name__})") from None
        try:
            payload = json.loads(bytes(body))
        except (ValueError, RecursionError):
            payload = None
        if status == 200:
            if not isinstance(payload, dict):
                raise SourceError(_UNEXPECTED)
            return payload
        detail = str(payload.get("detail", ""))[:200] if isinstance(payload, dict) else ""
        if status in (401, 403):
            raise SourceError(f"{self.name} API denied the request (not entitled): {detail}".rstrip(": "))
        if status == 404:
            raise SourceError(f"not found: {detail or ep.id}")
        if status == 422:
            raise SourceError(f"invalid request: {detail}")
        raise SourceError(f"{self.name} API error ({status})")

    def _validate(self, ep: Endpoint, params: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(params) - set(ep.params))
        if unknown:
            raise SourceError(f"unknown parameter(s) {unknown} for {ep.id}; valid: {sorted(ep.params)}")
        missing = [n for n, p in ep.params.items() if p.required and n not in params]
        if missing:
            raise SourceError(f"missing required parameter(s) {missing} for {ep.id}")
        return {n: _coerce(n, v, ep.params[n]) for n, v in params.items()}

    def _dataset(self, claims: dict) -> None:
        """First check of run_metric/query: a caller without the dataset learns nothing about the catalog."""
        if not has_dataset(claims, self.name):
            raise SourceError(f"not entitled to the {self.name} dataset")

    def _readable_endpoints(self, claims: dict) -> list[str]:
        return sorted(i for i, e in self._endpoints.items() if can(claims, self.name, e.table))

    def _readable_metrics(self, claims: dict) -> list[str]:
        return sorted(i for i, m in self._metrics.items()
                      if can(claims, self.name, self._endpoints[m.endpoint].table))

    def _entitled(self, claims: dict, ep: Endpoint) -> None:
        if not can(claims, self.name, ep.table):
            raise SourceError(f"not entitled to {self.name}.{ep.table}")

    # ------------------------------------------------------------------ describe
    async def describe(self, claims: dict) -> DescribeResult:
        async with self._limit_inflight(claims):
            return self._describe(claims)

    def _describe(self, claims: dict) -> DescribeResult:
        base = dict(source=self.name, kind="rest", as_of=self._settings.as_of.isoformat())
        if not has_dataset(claims, self.name):
            return DescribeResult(**base, metrics=[], objects=[], notes=["no access to this dataset"])
        allowed = {i: e for i, e in self._endpoints.items() if can(claims, self.name, e.table)}
        metrics = [
            {"id": m.id, "description": m.description, "type": "endpoint", "unit": m.unit,
             "dimensions": sorted(m.dimensions), "filters": {k: f.type for k, f in sorted(m.filters.items())},
             "time_column": "from/to", "sensitive_dimensions": sorted(m.sensitive_dimensions),
             "required_dimensions": []}
            for m in sorted(self._metrics.values(), key=lambda m: m.id) if m.endpoint in allowed
        ]
        objects = [{"name": e.id, "kind": "endpoint", "description": e.description,
                    "params": {n: {"type": p.type, "required": p.required, **({"enum": p.enum} if p.enum else {})}
                               for n, p in e.params.items()}}
                   for e in sorted(allowed.values(), key=lambda e: e.id)]
        return DescribeResult(**base, metrics=metrics, objects=objects,
                              notes=describe_notes(claims, "{'endpoint_id', 'params'}"))

    # ------------------------------------------------------------------ run_metric
    async def run_metric(self, claims: dict, *, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                         time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult:
        async with self._limit_inflight(claims):
            return await self._run_metric(claims, metric_id, dimensions, filters, time_range, order_by, limit)

    async def _run_metric(self, claims: dict, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                          time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult:
        self._dataset(claims)
        m = self._metrics.get(metric_id) if isinstance(metric_id, str) else None
        if m is None or not can(claims, self.name, self._endpoints[m.endpoint].table):
            if m is not None:
                self._entitled(claims, self._endpoints[m.endpoint])
            raise SourceError(f"unknown metric {str(metric_id)[:80]!r} on {self.name}; "
                              f"valid: {self._readable_metrics(claims)}")
        ep = self._endpoints[m.endpoint]
        if not isinstance(dimensions, list) or not all(isinstance(d, str) for d in dimensions) \
                or len(set(dimensions)) != len(dimensions):
            raise SourceError("dimensions must be a list of unique strings")
        if not isinstance(filters, dict) or not all(isinstance(k, str) for k in filters):
            raise SourceError("filters must be an object")
        unknown = [d for d in dimensions if d not in m.dimensions]
        if unknown:
            raise SourceError(f"unknown dimension(s) {unknown} for {m.id}; valid: {sorted(m.dimensions)}")
        err = sensitive_dimension_error(claims, dimensions, m.sensitive_dimensions, filters=list(filters))
        if err is not None:  # unconditional: the type checks above already failed closed
            raise err
        params: dict[str, Any] = {}
        if dimensions:
            params["group_by"] = [m.dimensions[d] for d in dimensions]
        for name, value in filters.items():
            f = m.filters.get(name)
            if f is None:
                raise SourceError(f"unknown filter {name!r} for {m.id}; valid: {sorted(m.filters)}")
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise SourceError(f"filter {name!r} on {self.name} metrics accepts a single value")
            params[f.param] = value
        window = None
        if time_range is not None:
            lo, hi = resolve_time_range(time_range, self._settings.as_of)
            params["from"], params["to"] = lo.isoformat(), hi.isoformat()
            window = {"from": params["from"], "to": params["to"]}
        data = await self._get(claims, ep, self._validate(ep, params))
        try:
            rows = _rows(data, ep.result_key)
            if dimensions:
                out = [{**{d: r[m.dimensions[d]] for d in dimensions}, "value": r[m.value_field]} for r in rows]
            else:
                out = [{"value": sum(r[m.value_field] for r in rows)}]
            if any(isinstance(r["value"], bool) or not isinstance(r["value"], (int, float, type(None))) for r in out):
                raise TypeError
            out = self._order(out, order_by, dimensions)
            truncated = len(out) > limit  # decided before the cut, so the caller knows rows were dropped
            out = out[:limit]
        except SourceError:
            raise
        except (TypeError, KeyError, ValueError, RecursionError, AttributeError):
            raise SourceError(_UNEXPECTED) from None
        return MetricResult(source=self.name, metric_id=m.id, unit=m.unit, dimensions=dimensions,
                            rows=jsonable(out), row_count=len(out), truncated=truncated,
                            as_of=self._settings.as_of.isoformat(), window=window)

    @staticmethod
    def _order(rows: list[dict], order_by: str | None, dims: list[str]) -> list[dict]:
        key = order_by or "metric_desc"
        if key in ("metric", "metric_desc"):
            return sorted(rows, key=lambda r: (r["value"] is None, r["value"] if key == "metric" else -(r["value"] or 0),
                                               *[str(r.get(d)) for d in dims]))
        valid = ["metric", "metric_desc", *dims, *[f"-{d}" for d in dims]]
        if not isinstance(order_by, str) or not _ORDER_BY.fullmatch(order_by) or order_by.lstrip("-") not in dims:
            raise SourceError(f"unknown order_by {str(order_by)[:80]!r}; valid: {valid}")
        desc = key.startswith("-")
        name = key[1:] if desc else key
        return sorted(rows, key=lambda r: str(r[name]), reverse=desc)

    # ------------------------------------------------------------------ query
    async def query(self, claims: dict, request: dict[str, Any]) -> QueryResult:
        async with self._limit_inflight(claims):
            return await self._query(claims, request)

    async def _query(self, claims: dict, request: dict[str, Any]) -> QueryResult:
        self._dataset(claims)
        endpoint_id = request.get("endpoint_id")
        if not isinstance(endpoint_id, str):
            raise SourceError(f"query on {self.name} needs {{'endpoint_id': ..., 'params': {{...}}}}; "
                              f"valid endpoint ids: {self._readable_endpoints(claims)}")
        ep = self._endpoints.get(endpoint_id)
        if ep is None:
            raise SourceError(f"unknown endpoint {endpoint_id[:80]!r} on {self.name}; "
                              f"valid: {self._readable_endpoints(claims)}")
        self._entitled(claims, ep)
        params = request.get("params") or {}
        if not isinstance(params, dict):
            raise SourceError("'params' must be an object")
        data = await self._get(claims, ep, self._validate(ep, params))
        try:
            rows = _rows(data, ep.result_key) if ep.result_key else [data]
            columns = list(dict.fromkeys(k for r in rows for k in r))
            table = [[r.get(c) for c in columns] for r in rows]
            items = jsonable(table)
        except (TypeError, KeyError, ValueError, RecursionError, AttributeError):
            raise SourceError(_UNEXPECTED) from None
        page_limit = data.get("limit")
        if isinstance(page_limit, int) and not isinstance(page_limit, bool):
            truncated = len(table) >= page_limit
        else:
            truncated = bool(params.get("limit")) and len(table) >= params["limit"]
        return QueryResult(source=self.name, columns=columns, rows=items, row_count=len(table), truncated=truncated)
