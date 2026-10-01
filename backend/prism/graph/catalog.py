"""The gateway's hot-path catalog: metric -> source address / dimensions / tables / scopes, and the role grants, read
in ONE query (ported from the plan-3 Neo4j spike).

This is gateway-internal. It is deliberately not role-gated: Role nodes are invisible to retrieval
(`allowed_scopes = []`) and reach the gateway only through here. Never hand a Catalog to an agent.

Fail closed on a stale graph: every Metric must carry the current GRAPH_SCHEMA_VERSION and every policy list
(dimensions, required, sensitive, fine_grain, filters, allowed_scopes); otherwise load_catalog raises CatalogError
instead of reading a property the old loader never wrote as "no restriction".
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType

from prism.graph.retrieval import DEFAULT_TIMEOUT_S, arun_read, resolve_ns, run_read
from prism.graph.schema import GRAPH_SCHEMA_VERSION

CATALOG_CYPHER = """
CALL () {
  MATCH (m:Metric {ns: $ns})
  OPTIONAL MATCH (m)-[:COMPUTED_FROM]->(t:Table)
  WITH m, collect(t.qualified_name) AS tables ORDER BY m.id
  RETURN collect({id: m.id, source: m.source, kind: m.kind, server: m.mcp_server, tool: m.mcp_tool,
                  endpoint: m.endpoint_id, unit: m.unit, dimensions: m.dimensions,
                  required: m.required_dimensions, sensitive: m.sensitive_dimensions,
                  fine_grain: m.fine_grain_dimensions, filters: m.filters, tables: tables,
                  allowed_scopes: m.allowed_scopes, schema_version: m.schema_version}) AS metrics
}
CALL () {
  MATCH (r:Role {ns: $ns})
  OPTIONAL MATCH (r)-[g:CAN_READ]->(o:Ctx {ns: $ns})
  WITH r, g, o ORDER BY o.local_uid
  WITH r, collect(CASE WHEN o IS NULL THEN NULL ELSE {object: o.local_uid, scope: g.scope, row_scope: g.row_scope}
                  END) AS grants ORDER BY r.persona_id
  RETURN collect({role: r.persona_id, metrics_only: r.metrics_only, scopes: r.scopes, grants: grants}) AS roles
}
CALL () {
  MATCH (n:Ctx {ns: $ns}) RETURN max(n.loaded_version) AS version
}
RETURN metrics, roles, version
"""


@dataclass(frozen=True)
class CatalogMetric:
    id: str
    source: str
    kind: str                       # "sql" | "rest"
    server: str                     # e.g. "cashrecon-mcp"
    tool: str                       # the source server's tool, "run_metric"
    endpoint: str | None            # REST endpoint id (rest metrics only)
    unit: str | None
    dimensions: tuple[str, ...]
    required: tuple[str, ...]
    sensitive: tuple[str, ...]
    tables: tuple[str, ...]         # "<source>.<table>"
    allowed_scopes: tuple[str, ...]  # any one of these scopes may run the metric
    fine_grain: tuple[str, ...] = ()  # identifier/date dimensions (`grain: fine`); see prism.gateway.policy
    filters: tuple[str, ...] = ()     # public filter names


@dataclass(frozen=True)
class Grant:
    object: str                     # "source:<s>" or "table:<s>.<t>"
    scope: str


@dataclass(frozen=True)
class CatalogRole:
    persona_id: str
    metrics_only: bool
    scopes: tuple[str, ...]
    grants: tuple[Grant, ...]
    row_scope: MappingProxyType     # RLS dimension -> allowed values ("*" = all)

    def row_values(self, dim: str) -> tuple[str, ...]:
        return row_values(self.row_scope, dim)


def row_values(row_scope, dim: str) -> tuple[str, ...]:
    """Allowed values of RLS dimension `dim`; a dimension missing from the row scope allows nothing (deny), exactly
    like prism_sec.allowed() returning an empty array. ('*',) means every value."""
    return tuple(row_scope.get(dim, ()))


@dataclass(frozen=True)
class Catalog:
    version: int | None
    metrics: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))   # id -> CatalogMetric
    roles: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))     # persona_id -> CatalogRole


class CatalogError(RuntimeError):
    """The graph cannot be trusted as a policy source (written by an older loader); internal, never caller-facing."""


# Policy-bearing list properties: a missing one would silently mean "no restriction", so it is an error instead.
_POLICY_LISTS = ("dimensions", "required", "sensitive", "fine_grain", "filters", "allowed_scopes")


def _metric(m: dict) -> CatalogMetric:
    if m.get("schema_version") != GRAPH_SCHEMA_VERSION or any(m.get(k) is None for k in _POLICY_LISTS):
        raise CatalogError(f"metric {m.get('id')!r} was written by an older graph loader (schema "
                           f"{m.get('schema_version')!r}, want {GRAPH_SCHEMA_VERSION}); reload the context graph "
                           f"(make graph)")
    return CatalogMetric(id=m["id"], source=m["source"], kind=m["kind"], server=m["server"], tool=m["tool"],
                         endpoint=m.get("endpoint"), unit=m.get("unit"),
                         filters=tuple(sorted(f.split(":", 1)[0] for f in m.get("filters") or ())),
                         **{k: tuple(m.get(k) or ()) for k in ("dimensions", "required", "sensitive", "tables",
                                                               "allowed_scopes", "fine_grain")})


def _role(r: dict) -> CatalogRole:
    grants = [g for g in r["grants"] if g]
    rows = json.loads(grants[0]["row_scope"]) if grants else {}
    return CatalogRole(persona_id=r["role"], metrics_only=bool(r["metrics_only"]), scopes=tuple(r["scopes"] or ()),
                       grants=tuple(Grant(g["object"], g["scope"]) for g in grants),
                       row_scope=MappingProxyType({k: tuple(v) for k, v in rows.items()}))


def _catalog(rows: list[dict]) -> Catalog:
    (rec,) = rows
    return Catalog(version=rec["version"],
                   metrics=MappingProxyType({m["id"]: _metric(m) for m in rec["metrics"]}),
                   roles=MappingProxyType({r["role"]: _role(r) for r in rec["roles"]}))


def load_catalog(driver, ns: str | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Catalog:
    return _catalog(run_read(driver, CATALOG_CYPHER, {"ns": resolve_ns(ns)}, timeout_s))


async def aload_catalog(adriver, ns: str | None = None, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> Catalog:
    return _catalog(await arun_read(adriver, CATALOG_CYPHER, {"ns": resolve_ns(ns)}, timeout_s))


__all__ = ["Catalog", "CatalogError", "CatalogMetric", "CatalogRole", "Grant", "aload_catalog", "load_catalog", "row_values"]
