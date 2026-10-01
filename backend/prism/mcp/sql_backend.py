"""SourceBackend for the Postgres-backed platforms. Every call runs in a READ ONLY transaction as bi_reader with
the caller's signed context (row-level security is the real boundary), with search_path pinned to public and a
statement timeout. Free-form SQL passes the AST guard and is executed through a named server-side cursor (which
rejects stacked statements on its own) with a row cap, a serialized-byte budget and a cumulative deadline."""
import json
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool, PoolTimeout

from prism.config import Settings
from prism.db.session import ctx_from_claims, prepare_statements
from prism.mcp.base import DescribeResult, MetricResult, QueryResult
from prism.mcp.metrics import ALLOWED_TABLES, DEFAULT_METRICS_DIR, Metric, load_metrics, resolve_time_range, run_metric
from prism.mcp.results import (SOURCE_BUSY, TOO_MANY_CONCURRENT, SourceError, describe_notes, has_dataset, jsonable,
                               sensitive_dimension_error)
from prism.mcp.sql_guard import validate_select
from prism.security.access import can

MAX_RESULT_BYTES = 1_000_000
CURSOR_ITERSIZE = 10
MAX_INFLIGHT_PER_PRINCIPAL = 3
POOL_BORROW_TIMEOUT_S = 10
MAX_METRIC_ROWS = 1000  # same cap as compile_metric's default max_limit and the run_metric tool's `le`


class SqlBackend:
    kind = "sql"

    def __init__(self, source: str, settings: Settings, metrics_dir: Path | None = None) -> None:
        if source not in ALLOWED_TABLES:
            raise ValueError(f"{source} is not a SQL source (expected one of {sorted(ALLOWED_TABLES)})")
        self.name = source
        self._settings = settings
        self._tables = ALLOWED_TABLES[source]
        self._metrics: dict[str, Metric] = {
            m.id: m for m in load_metrics(metrics_dir or DEFAULT_METRICS_DIR).values() if m.source == source
        }
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()
        self._closed = False
        self._inflight: dict[str, int] = {}

    # ------------------------------------------------------------------ infrastructure
    def _get_pool(self) -> ConnectionPool:
        with self._lock:
            if self._closed:
                raise SourceError("backend is closed")
            if self._pool is None:
                pool = ConnectionPool(self._settings.dsn(self.name), min_size=1, max_size=8, open=False)
                pool.open(wait=True, timeout=10)
                self._pool = pool
            return self._pool

    @contextmanager
    def scoped(self, claims: dict):
        """Pooled connection inside the scoped transaction. SET LOCAL settings vanish at COMMIT/ROLLBACK, so a
        pooled connection can never carry one caller's context into the next call."""
        try:
            ctx = ctx_from_claims(claims, self._settings.ctx_hmac_key.get_secret_value())
        except KeyError as exc:
            raise SourceError("invalid principal claims") from exc
        try:
            with self._get_pool().connection(timeout=POOL_BORROW_TIMEOUT_S) as conn:
                with conn.transaction():
                    for statement, args in prepare_statements(ctx, self._settings.statement_timeout_ms):
                        conn.execute(statement, args)
                    conn.execute("SET LOCAL search_path = public")  # the guard resolves bare names to public.<name>
                    yield conn
        except PoolTimeout as exc:  # only raised while borrowing; nothing in the body raises it
            raise SourceError(SOURCE_BUSY) from exc

    @contextmanager
    def _limit_inflight(self, claims: dict):
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            raise SourceError("invalid principal claims")
        with self._lock:
            if self._inflight.get(sub, 0) >= MAX_INFLIGHT_PER_PRINCIPAL:
                raise SourceError(TOO_MANY_CONCURRENT)
            self._inflight[sub] = self._inflight.get(sub, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                left = self._inflight[sub] - 1
                if left:
                    self._inflight[sub] = left
                else:
                    del self._inflight[sub]

    async def aclose(self) -> None:
        with self._lock:
            self._closed = True
            pool, self._pool = self._pool, None
        if pool is not None:
            await anyio.to_thread.run_sync(pool.close)

    @staticmethod
    def _translate(exc: psycopg.Error) -> SourceError:
        if isinstance(exc, psycopg.errors.QueryCanceled):
            return SourceError("query timed out")
        if isinstance(exc, psycopg.errors.InsufficientPrivilege):
            return SourceError("access denied by data policy")
        return SourceError(f"query failed: {exc.diag.message_primary or 'database error'}")

    def _readable(self, claims: dict, metric: Metric) -> bool:
        return all(can(claims, self.name, t) for t in metric.tables)

    def _readable_metrics(self, claims: dict) -> list[str]:
        return sorted(i for i, m in self._metrics.items() if self._readable(claims, m))

    def _entitled(self, claims: dict) -> None:
        if not has_dataset(claims, self.name):
            raise SourceError(f"not entitled to the {self.name} dataset")

    # ------------------------------------------------------------------ describe
    def _describe_sync(self, claims: dict) -> DescribeResult:
        with self._limit_inflight(claims):
            return self._describe(claims)

    def _describe(self, claims: dict) -> DescribeResult:
        base = dict(source=self.name, kind="sql", as_of=self._settings.as_of.isoformat())
        if not has_dataset(claims, self.name):
            return DescribeResult(**base, metrics=[], objects=[], notes=["no access to this dataset"])
        tables = sorted(t for t in self._tables if can(claims, self.name, t))
        metrics = [
            {"id": m.id, "description": m.description, "type": m.type, "unit": m.unit,
             "dimensions": sorted(m.dimensions), "filters": {k: f.type for k, f in sorted(m.filters.items())},
             "time_column": m.time_column, "sensitive_dimensions": sorted(m.sensitive_dimensions),
             "required_dimensions": sorted(m.required_dimensions)}
            for m in sorted(self._metrics.values(), key=lambda m: m.id)
            if self._readable(claims, m)
        ]
        columns: dict[str, list[dict[str, str]]] = {t: [] for t in tables}
        try:
            with self.scoped(claims) as conn:
                for name, col, typ in conn.execute(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = ANY(%s) ORDER BY table_name, ordinal_position",
                    (tables,),
                ):
                    columns[name].append({"name": col, "type": typ})
        except psycopg.Error as exc:
            raise self._translate(exc) from exc
        objects = [{"name": t, "kind": "table", "columns": columns[t]} for t in tables]
        return DescribeResult(**base, metrics=metrics, objects=objects,
                              notes=describe_notes(claims, "a single SELECT"))

    async def describe(self, claims: dict) -> DescribeResult:
        return await anyio.to_thread.run_sync(self._describe_sync, claims)

    # ------------------------------------------------------------------ run_metric
    def _run_metric_sync(self, claims: dict, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                         time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult:
        with self._limit_inflight(claims):
            return self._run_metric(claims, metric_id, dimensions, filters, time_range, order_by, limit)

    def _run_metric(self, claims: dict, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                    time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult:
        self._entitled(claims)  # first, so a non-entitled caller learns nothing about metric ids
        # types first, so the sensitive-dimension check below can never be skipped (fail closed)
        if not isinstance(dimensions, list) or not all(isinstance(d, str) for d in dimensions):
            raise SourceError("dimensions must be a list of strings")
        if not isinstance(filters, dict) or not all(isinstance(k, str) for k in filters):
            raise SourceError("filters must be an object")
        metric = self._metrics.get(metric_id)
        if metric is None:
            raise SourceError(f"unknown metric {str(metric_id)[:80]!r} on {self.name}; "
                              f"valid: {self._readable_metrics(claims)}")
        for table in sorted(metric.tables):
            if not can(claims, self.name, table):
                raise SourceError(f"not entitled to {self.name}.{table}")
        err = sensitive_dimension_error(claims, dimensions, metric.sensitive_dimensions, filters=list(filters))
        if err is not None:
            raise err  # before any SQL runs
        as_of = self._settings.as_of
        window = None
        if time_range is not None and metric.time_column:
            lo, hi = resolve_time_range(time_range, as_of)
            window = {"from": lo.isoformat(), "to": hi.isoformat()}
        try:
            with self.scoped(claims) as conn:
                # one extra row beyond the cap tells us whether the result was cut
                rows = run_metric(conn, metric, db_prefix=self._settings.db_prefix, dimensions=dimensions,
                                  filters=filters, time_range=time_range, as_of=as_of, order_by=order_by, limit=limit,
                                  max_limit=MAX_METRIC_ROWS, fetch_extra=1)
        except psycopg.Error as exc:
            raise self._translate(exc) from exc
        cap = min(limit, MAX_METRIC_ROWS)
        truncated = len(rows) > cap
        rows = rows[:cap]
        return MetricResult(source=self.name, metric_id=metric.id, unit=metric.unit, dimensions=dimensions,
                            rows=jsonable(rows), row_count=len(rows), truncated=truncated, as_of=as_of.isoformat(),
                            window=window)

    async def run_metric(self, claims: dict, *, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                         time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult:
        return await anyio.to_thread.run_sync(self._run_metric_sync, claims, metric_id, dimensions, filters,
                                              time_range, order_by, limit)

    # ------------------------------------------------------------------ query
    def _query_sync(self, claims: dict, request: dict[str, Any]) -> QueryResult:
        with self._limit_inflight(claims):
            return self._query(claims, request)

    def _query(self, claims: dict, request: dict[str, Any]) -> QueryResult:
        sql = request.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise SourceError(f"query on {self.name} needs {{'sql': '<single SELECT>'}}")
        allowed = frozenset(f"public.{t}" for t in self._tables if can(claims, self.name, t))
        if not allowed:
            raise SourceError(f"not entitled to any table of {self.name}")
        max_rows = self._settings.mcp_query_max_rows
        safe_sql = validate_select(sql, allowed_tables=allowed, max_rows=max_rows + 1)  # +1 detects truncation
        rows: list[list[Any]] = []
        used = 0
        truncated = False
        try:
            with self.scoped(claims) as conn:
                cur = conn.cursor(name=f"q_{uuid.uuid4().hex[:12]}", row_factory=tuple_row)
                try:
                    cur.itersize = CURSOR_ITERSIZE
                    # statement_timeout restarts on every FETCH, so also enforce a cumulative deadline
                    deadline = time.monotonic() + self._settings.statement_timeout_ms / 1000
                    cur.execute(safe_sql)
                    done = False
                    while not done:
                        if time.monotonic() > deadline:
                            raise SourceError("query timed out")
                        chunk = cur.fetchmany(CURSOR_ITERSIZE)
                        if not chunk:
                            break
                        for row in chunk:
                            if time.monotonic() > deadline:
                                raise SourceError("query timed out")
                            item = jsonable(list(row))
                            size = len(json.dumps(item, default=str))
                            if used + size > MAX_RESULT_BYTES:
                                if not rows:
                                    raise SourceError(
                                        "result row exceeds the size limit; select fewer or shorter columns")
                                truncated = True
                                done = True
                                break
                            used += size
                            rows.append(item)
                            if len(rows) > max_rows:
                                done = True
                                break
                    columns = [d.name for d in cur.description or []]
                finally:
                    cur.close()
        except psycopg.Error as exc:
            raise self._translate(exc) from exc
        if len(rows) > max_rows:
            rows, truncated = rows[:max_rows], True
        return QueryResult(source=self.name, columns=columns, rows=rows, row_count=len(rows), truncated=truncated)

    async def query(self, claims: dict, request: dict[str, Any]) -> QueryResult:
        return await anyio.to_thread.run_sync(self._query_sync, claims, request)
