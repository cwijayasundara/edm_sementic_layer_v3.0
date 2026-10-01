"""Gateway policy over the in-memory Catalog: who may run which metric, how, and who may query a source freely.

The caller's verified JWT claims are the source of truth (`scopes`, `rows`, `metrics_only`), exactly as at the
source servers; a missing `metrics_only` claim counts as metrics-only (fail closed). A metric the caller cannot read
answers exactly like a metric that does not exist (same class, code and message shape), so the gateway never
confirms that an object exists outside the caller's role. Only catalog names reach the plan (and so the audit).

Grain (metrics-only callers): a metric's `fine_grain` dimensions (identifier / date grain, `grain: fine` in the graph)
may not be pinned two or more at once, by group-by or by filter (a filter can narrow to one value), because such a
result is one underlying row per group. The minimum-group-size escape hatch is not offered: the gateway cannot count
underlying rows of a `max` metric before running it, so the combination is refused outright (fail closed).

Limits of the grain rule (by design, documented so later tools do not widen the hole):
- Filters are matched by NAME. The registries refuse a filter that aliases a sensitive / fine dimension's expression
  under another name (prism.mcp.metrics.guarded_filter_aliases), so the name check covers the expression.
- A non-fine filter can still isolate one entity (e.g. a `fund_group` holding a single portfolio, combined with
  `nav_date`); the rule bounds identifier x date pinning, it is not a k-anonymity guarantee.
- No `time_range` argument reaches the sources today. If one is added, a range covering a single day/period MUST
  count as pinning the metric's time column (its date dimension) in the `pinned` set below.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from prism.db.policies import ROW_SCOPES
from prism.gateway.errors import GatewayError, echo
from prism.graph.catalog import Catalog, CatalogMetric, row_values

METRIC_ID = re.compile(r"[a-z][a-z0-9_]{0,62}")
MAX_DIMENSIONS = 20
MAX_FILTERS = 20
MAX_LIMIT = 1000
SOURCE_TOOL = "run_metric"


@dataclass(frozen=True)
class MetricPlan:
    source: str
    tool: str
    metric_id: str
    unit: str | None
    arguments: dict[str, Any]

    def audit_plan(self) -> dict[str, list[str]]:
        """Catalog names only (never filter values): the structured plan AuditWriter accepts."""
        return {"metric_ids": [self.metric_id], "dimensions": list(self.arguments["dimensions"])}


def is_metrics_only(claims: Any) -> bool:
    """Only an explicit `metrics_only: false` claim opens free-form access."""
    return not (isinstance(claims, Mapping) and claims.get("metrics_only", True) is False)


def _scopes(claims: Any) -> frozenset[str]:
    scopes = claims.get("scopes") if isinstance(claims, Mapping) else None
    if not isinstance(scopes, (list, tuple)):
        return frozenset()
    return frozenset(s for s in scopes if isinstance(s, str))


def _rows(claims: Any) -> Mapping:
    rows = claims.get("rows") if isinstance(claims, Mapping) else None
    return rows if isinstance(rows, Mapping) else {}


def _not_permitted_metric(metric_id: str) -> GatewayError:
    return GatewayError("not_permitted", f"metric {echo(metric_id)} is not available to your role")


def _has_rows(claims: Any, qualified_table: str) -> bool:
    """Every RLS table of the metric must have allowed values for its row dimension (missing dimension = deny)."""
    source, _, table = qualified_table.partition(".")
    if table not in ROW_SCOPES.get(source, {}):
        return False
    scope = ROW_SCOPES[source][table]
    if scope is None:
        return True
    values = row_values(_rows(claims), scope[0])
    return isinstance(values, tuple) and any(isinstance(v, str) for v in values)


def _invalid(message: str) -> GatewayError:
    return GatewayError("invalid_request", message)


class Policy:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog  # swapped atomically by the gateway on reload (one attribute assignment)

    def readable(self, claims: Any, metric: CatalogMetric) -> bool:
        scopes = _scopes(claims)
        return any(s in scopes for s in metric.allowed_scopes) and all(_has_rows(claims, t) for t in metric.tables)

    def check_metric(self, claims: Any, metric_id: Any, dimensions: Any = (), filters: Any = None, *,
                     limit: Any = None) -> MetricPlan:
        if not isinstance(metric_id, str) or not METRIC_ID.fullmatch(metric_id):
            raise GatewayError("unknown_metric", "metric ids are lower-case identifiers (see search_context)")
        metric = self.catalog.metrics.get(metric_id)
        if metric is None or not self.readable(claims, metric):
            raise _not_permitted_metric(metric_id)
        dims = self._dimensions(metric, dimensions)
        filters = self._filters(metric, filters)
        metrics_only = is_metrics_only(claims)
        if metrics_only:
            if blocked := [d for d in dims if d in metric.sensitive]:
                raise GatewayError("sensitive_dimension",
                                   f"dimension(s) {blocked} are not available to metrics-only principals")
            if blocked := [f for f in filters if f in metric.sensitive]:
                raise GatewayError("sensitive_dimension",
                                   f"filter(s) {blocked} are not available to metrics-only principals")
        if missing := [d for d in metric.required if d not in dims]:
            raise GatewayError("missing_required_dimension",
                               f"metric {metric_id} requires dimension(s) {missing} (its value is not meaningful "
                               f"across them)")
        if metrics_only:
            pinned = sorted({d for d in (*dims, *filters) if d in metric.fine_grain})
            if len(pinned) >= 2:
                raise GatewayError("grain_too_fine",
                                   f"{pinned} together are too fine a grain for metrics-only principals; group or "
                                   f"filter by at most one of them")
        arguments: dict[str, Any] = {"metric_id": metric_id, "dimensions": dims, "filters": filters}
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
                raise _invalid(f"limit must be an integer in 1..{MAX_LIMIT}")
            arguments["limit"] = limit
        return MetricPlan(source=metric.source, tool=SOURCE_TOOL, metric_id=metric_id, unit=metric.unit,
                          arguments=arguments)

    @staticmethod
    def _dimensions(metric: CatalogMetric, dimensions: Any) -> list[str]:
        if not isinstance(dimensions, (list, tuple)) or not all(isinstance(d, str) for d in dimensions):
            raise _invalid("dimensions must be a list of strings")
        if len(dimensions) > MAX_DIMENSIONS or len(set(dimensions)) != len(dimensions):
            raise _invalid(f"dimensions must be unique (at most {MAX_DIMENSIONS})")
        if unknown := [d for d in dimensions if d not in metric.dimensions]:
            raise _invalid(f"unknown dimension(s) {[echo(d) for d in unknown[:5]]} for {metric.id}")
        return list(dimensions)

    @staticmethod
    def _filters(metric: CatalogMetric, filters: Any) -> dict[str, Any]:
        if filters is None:
            return {}
        if not isinstance(filters, Mapping) or not all(isinstance(k, str) for k in filters):
            raise _invalid("filters must be an object")
        if len(filters) > MAX_FILTERS:
            raise _invalid(f"too many filters (max {MAX_FILTERS})")
        if unknown := [f for f in filters if f not in metric.filters]:
            raise _invalid(f"unknown filter(s) {[echo(f) for f in unknown[:5]]} for {metric.id}")
        return dict(filters)

    def check_query(self, claims: Any, source: Any) -> None:
        """Free-form `query_source`: refused to metrics-only callers; the source must be in the caller's scopes."""
        if is_metrics_only(claims):
            raise GatewayError("metrics_only", "free-form queries are not available to metrics-only principals; "
                                               "use run_metric")
        scopes = _scopes(claims)
        known = {m.source for m in self.catalog.metrics.values()}
        if not isinstance(source, str) or source not in known or not (
                source in scopes or any(s.startswith(f"{source}.") for s in scopes)):
            raise GatewayError("not_permitted", f"source {echo(source)} is not available to your role")


__all__ = ["MetricPlan", "Policy", "is_metrics_only"]
