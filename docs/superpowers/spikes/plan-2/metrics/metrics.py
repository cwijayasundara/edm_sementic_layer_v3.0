"""Governed metrics: YAML definitions (trusted, human-reviewed SQL fragments) + a compiler that binds every
user-supplied value as a query parameter. Runs only on a connection already scoped to bi_reader (RLS/masking)."""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import psycopg
import yaml
from psycopg import sql
from psycopg.rows import tuple_row
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Tables each source (= one physical database) may reference in a metric's FROM clause. Defence in depth:
# the hard boundary is that a metric only ever runs on a connection to its own source database as bi_reader.
ALLOWED_TABLES: dict[str, frozenset[str]] = {
    "cashrecon": frozenset({"breaks", "break_actions", "match_groups", "match_items", "match_rules",
                            "statements", "statement_entries", "ledger_entries", "cash_accounts"}),
    "assetrecon": frozenset({"custodians", "portfolios", "internal_positions", "custodian_positions",
                             "internal_transactions", "custodian_transactions", "recon_runs",
                             "recon_exceptions", "nav_checks"}),
    "feedhub": frozenset({"sources", "feeds", "feed_deliveries", "support_tickets"}),
}

# Tokens never allowed in a trusted fragment (statement chaining, comments, schema escapes, subqueries,
# GUC tampering, psycopg placeholder markers).
_FORBIDDEN = re.compile(
    r"""(;|--|/\*|\*/|%|\\|\$|"|\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|\bdrop\b|\balter\b|\bcreate\b"""
    r"""|\bgrant\b|\btruncate\b|\bcopy\b|\bunion\b|\binto\b|\bprivate\s*\.|\bprism_sec\b|\bpg_\w*"""
    r"""|\binformation_schema\b|\bset_config\b|\bcurrent_setting\b|\bset\s+role\b|\bdblink\b)""",
    re.IGNORECASE,
)
_IDENT = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_FROM_HEAD = re.compile(r"^\s*([A-Za-z_][\w.]*)(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?", re.IGNORECASE)
_JOIN_TABLE = re.compile(r"\bjoin\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_AGG_PREFIX = {"count": "count(", "sum": "sum(", "avg": "avg(", "count_distinct": "count(distinct",
               "max": "max("}
_OPS = ("eq", "ne", "in", "gt", "gte", "lt", "lte", "between")
_EXCLUSIVE_OPS = {"eq", "in", "between"}
_CMP = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
_PY_TYPES = {"str": str, "int": int, "float": float, "date": date, "bool": bool}


class MetricError(ValueError):
    """Invalid metric definition or invalid request against a metric."""


def _check_fragment(text: str, what: str) -> str:
    if (m := _FORBIDDEN.search(text)) is not None:
        raise MetricError(f"{what}: forbidden token {m.group(0)!r} in trusted fragment")
    if text.count("'") % 2:
        raise MetricError(f"{what}: unbalanced quote in trusted fragment")
    if text.count("(") != text.count(")"):
        raise MetricError(f"{what}: unbalanced parentheses in trusted fragment")
    return text


class FilterDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expr: str
    type: Literal["str", "int", "float", "date", "bool"] = "str"


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    source: Literal["cashrecon", "assetrecon", "feedhub"]
    description: str
    type: Literal["count", "sum", "avg", "count_distinct", "max", "ratio"]
    expr: str | None = None               # trusted aggregate; derived for ratio
    numerator: str | None = None          # trusted predicate, ratio only
    from_: str = Field(alias="from")      # trusted FROM fragment (tables of `source` only, optional JOINs)
    base_filter: str | None = None        # trusted predicate always applied
    time_column: str | None = None
    dimensions: dict[str, str] = {}       # public name -> trusted SQL expression
    filters: dict[str, FilterDef] = {}    # public name -> trusted column expression + value type
    unit: str | None = None
    default_order: str | None = None      # same vocabulary as order_by

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _IDENT.match(v):
            raise ValueError(f"metric id {v!r} must match {_IDENT.pattern}")
        return v

    @model_validator(mode="after")
    def _validate(self) -> Metric:
        where = f"metric {self.id}"
        # FROM: only this source's tables; no comma joins; every JOIN names an allowed table.
        if "," in self.from_:
            raise MetricError(f"{where}: comma joins are not allowed in from; use explicit JOIN ... ON")
        _check_fragment(self.from_, f"{where}.from")
        head = _FROM_HEAD.match(self.from_)
        tables = ([head.group(1)] if head else []) + _JOIN_TABLE.findall(self.from_)
        if not tables:
            raise MetricError(f"{where}.from: no table found")
        allowed = ALLOWED_TABLES[self.source]
        bad = [t for t in tables if t.lower().removeprefix("public.") not in allowed]
        if bad:
            raise MetricError(f"{where}.from references {bad}; allowed for {self.source}: {sorted(allowed)}")
        # Aggregate expression must match the declared type.
        if self.type == "ratio":
            if not self.numerator or self.expr:
                raise MetricError(f"{where}: ratio requires `numerator` and no `expr`")
            _check_fragment(self.numerator, f"{where}.numerator")
        else:
            if not self.expr or self.numerator:
                raise MetricError(f"{where}: {self.type} requires `expr` and no `numerator`")
            _check_fragment(self.expr, f"{where}.expr")
            if not re.sub(r"\s+", "", self.expr.lower()).startswith(_AGG_PREFIX[self.type].replace(" ", "")):
                raise MetricError(f"{where}: expr must start with {_AGG_PREFIX[self.type]!r} for type {self.type}")
        for label, frag in (("base_filter", self.base_filter), ("time_column", self.time_column)):
            if frag is not None:
                _check_fragment(frag, f"{where}.{label}")
        for name, frag in self.dimensions.items():
            if not _IDENT.match(name) or name == "value":
                raise MetricError(f"{where}: bad dimension name {name!r}")
            _check_fragment(frag, f"{where}.dimensions.{name}")
        for name, f in self.filters.items():
            if not _IDENT.match(name):
                raise MetricError(f"{where}: bad filter name {name!r}")
            _check_fragment(f.expr, f"{where}.filters.{name}")
        if self.default_order is not None:
            _order_clause(self, self.default_order, list(self.dimensions))
        return self

    @property
    def aggregate_sql(self) -> str:
        if self.type == "ratio":
            return f"(count(*) FILTER (WHERE {self.numerator}))::numeric / nullif(count(*), 0)"
        return self.expr  # type: ignore[return-value]


def load_metrics(directory: str | Path) -> dict[str, Metric]:
    out: dict[str, Metric] = {}
    for path in sorted(Path(directory).glob("*.yaml")):
        m = Metric.model_validate(yaml.safe_load(path.read_text()))
        if m.id in out:
            raise MetricError(f"duplicate metric id {m.id} in {path}")
        out[m.id] = m
    return out


# ---------------------------------------------------------------------------------------------- dates
def last_business_days(n: int, as_of: date) -> tuple[date, date]:
    """The N most recent weekdays ending at as_of (inclusive if as_of is a weekday). No holiday calendar."""
    if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 366:
        raise MetricError("last_business_days must be an integer in 1..366")
    end = as_of
    while end.weekday() >= 5:
        end -= timedelta(days=1)
    start, remaining = end, n - 1
    while remaining:
        start -= timedelta(days=1)
        if start.weekday() < 5:
            remaining -= 1
    return start, end


def _resolve_time_range(tr: dict, as_of: date) -> tuple[date, date]:
    if not isinstance(tr, dict):
        raise MetricError("time_range must be an object")
    if set(tr) == {"last_business_days"}:
        return last_business_days(tr["last_business_days"], as_of)
    if set(tr) == {"from", "to"}:
        try:
            lo, hi = _coerce(tr["from"], "date"), _coerce(tr["to"], "date")
        except (TypeError, ValueError) as e:
            raise MetricError(f"time_range from/to must be ISO dates: {e}") from None
        if lo > hi:
            raise MetricError("time_range from must be <= to")
        return lo, hi
    raise MetricError("time_range must be {'last_business_days': N} or {'from': iso, 'to': iso}")


# -------------------------------------------------------------------------------------------- compile
def _coerce(value: Any, typ: str) -> Any:
    py = _PY_TYPES[typ]
    if typ == "date":
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value)
        raise TypeError(f"expected ISO date, got {type(value).__name__}")
    if typ == "bool":
        if isinstance(value, bool):
            return value
        raise TypeError("expected bool")
    if typ == "str":
        if isinstance(value, str) and len(value) <= 200:
            return value
        raise TypeError("expected string (<=200 chars)")
    if isinstance(value, bool):
        raise TypeError(f"expected {typ}")
    return py(value)


def _order_clause(metric: Metric, order_by: str, dims: list[str]) -> sql.Composable:
    if order_by == "metric":
        return sql.SQL("value ASC NULLS LAST")
    if order_by == "metric_desc":
        return sql.SQL("value DESC NULLS LAST")
    desc = order_by.startswith("-")
    name = order_by[1:] if desc else order_by
    if name not in dims:
        valid = ["metric", "metric_desc", *dims, *[f"-{d}" for d in dims]]
        raise MetricError(f"unknown order_by {order_by!r} for {metric.id}; valid: {valid}")
    return sql.SQL("{} {}").format(sql.Identifier(name), sql.SQL("DESC" if desc else "ASC"))


def compile_metric(
    metric: Metric,
    *,
    dimensions: list[str] | None = None,
    filters: dict[str, object] | None = None,
    time_range: dict | None = None,
    as_of: date,
    order_by: str | None = None,
    limit: int = 100,
    max_limit: int = 1000,
) -> tuple[sql.Composed, list]:
    dimensions = list(dimensions or [])
    filters = dict(filters or {})
    params: list = []

    unknown = [d for d in dimensions if d not in metric.dimensions]
    if unknown:
        raise MetricError(f"unknown dimension(s) {unknown} for {metric.id}; valid: {sorted(metric.dimensions)}")
    if len(set(dimensions)) != len(dimensions):
        raise MetricError("duplicate dimension")

    select_items = [sql.SQL("{} AS {}").format(sql.SQL(metric.dimensions[d]), sql.Identifier(d))
                    for d in dimensions]
    select_items.append(sql.SQL("{} AS value").format(sql.SQL(metric.aggregate_sql)))

    where: list[sql.Composable] = []
    if metric.base_filter:
        where.append(sql.SQL("({})").format(sql.SQL(metric.base_filter)))

    for name, raw in filters.items():
        fdef = metric.filters.get(name)
        if fdef is None:
            raise MetricError(f"unknown filter {name!r} for {metric.id}; valid: {sorted(metric.filters)}")
        col = sql.SQL(fdef.expr)
        try:
            if isinstance(raw, dict):
                ops = raw
            elif isinstance(raw, (list, tuple)):
                ops = {"in": raw}
            else:
                ops = {"eq": raw}
            bad = [k for k in ops if k not in _OPS]
            if bad or not ops:
                raise MetricError(f"unknown operator(s) {bad} on filter {name!r}; valid: {list(_OPS)} "
                                  f"(gt/gte/lt/lte/ne combine; eq/in/between must be alone)")
            if len(ops) > 1 and set(ops) & _EXCLUSIVE_OPS:
                raise MetricError(f"filter {name!r}: {sorted(set(ops) & _EXCLUSIVE_OPS)} cannot be combined")
            for op, val in ops.items():
                if op == "in":
                    if not isinstance(val, (list, tuple)) or not 1 <= len(val) <= 1000:
                        raise MetricError(f"filter {name!r}: 'in' needs a list of 1..1000 values")
                    where.append(sql.SQL("{} = ANY(%s)").format(col))
                    params.append([_coerce(v, fdef.type) for v in val])
                elif op == "between":
                    if not isinstance(val, (list, tuple)) or len(val) != 2:
                        raise MetricError(f"filter {name!r}: 'between' needs [low, high]")
                    where.append(sql.SQL("{} BETWEEN %s AND %s").format(col))
                    params.extend(_coerce(v, fdef.type) for v in val)
                else:
                    where.append(sql.SQL("{} " + _CMP[op] + " %s").format(col))
                    params.append(_coerce(val, fdef.type))
        except (TypeError, ValueError) as e:
            if isinstance(e, MetricError):
                raise
            raise MetricError(f"filter {name!r}: bad value for type {fdef.type}: {e}") from None

    if time_range is not None:
        if not metric.time_column:
            raise MetricError(f"metric {metric.id} has no time_column; time_range not supported")
        lo, hi = _resolve_time_range(time_range, as_of)
        where.append(sql.SQL("{} BETWEEN %s AND %s").format(sql.SQL(metric.time_column)))
        params.extend([lo, hi])

    query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(select_items), sql.SQL(metric.from_))
    if where:
        query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(where)
    if dimensions:
        query += sql.SQL(" GROUP BY ") + sql.SQL(", ").join(sql.Literal(i + 1) for i in range(len(dimensions)))
        order = order_by or metric.default_order or "metric_desc"
        tiebreak = [sql.Identifier(d) for d in dimensions]
        query += sql.SQL(" ORDER BY ") + sql.SQL(", ").join([_order_clause(metric, order, dimensions), *tiebreak])
    elif order_by is not None and order_by not in ("metric", "metric_desc"):
        raise MetricError(f"order_by {order_by!r} needs dimensions; valid without dimensions: metric, metric_desc")

    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise MetricError("limit must be a positive integer")
    query += sql.SQL(" LIMIT %s")
    params.append(min(limit, max_limit))
    return query, params


# ------------------------------------------------------------------------------------------------ run
def _plain(v: Any) -> Any:
    return float(v) if isinstance(v, Decimal) else v


def run_metric(conn: psycopg.Connection, metric: Metric, *, db_prefix: str = "", **kwargs) -> list[dict]:
    """Execute on a connection already inside a scoped transaction (READ ONLY, SET LOCAL ROLE bi_reader,
    signed app.ctx). Refuses to run anywhere else, so it can never use admin privileges or cross sources."""
    expected_db = f"{db_prefix}{metric.source}"
    if conn.info.dbname != expected_db:
        raise MetricError(f"metric {metric.id} must run on database {expected_db}, not {conn.info.dbname}")
    with conn.cursor(row_factory=tuple_row) as c:
        role, read_only = c.execute(
            "SELECT current_user::text, current_setting('transaction_read_only')").fetchone()
    if role != "bi_reader" or read_only != "on":
        raise MetricError(f"run_metric requires a read-only bi_reader transaction (got {role}, read_only={read_only})")
    query, params = compile_metric(metric, **kwargs)
    with conn.cursor(row_factory=tuple_row) as c:
        c.execute(query, params)
        names = [d.name for d in c.description]
        return [{k: _plain(v) for k, v in zip(names, r)} for r in c.fetchall()]
