"""Hot-path catalog for the gateway: metric -> address/dims/tables and role grants, in ONE query."""
from __future__ import annotations

import time

CATALOG_CYPHER = """
CALL () {
  MATCH (m:Metric {ns: $ns})
  OPTIONAL MATCH (m)-[:COMPUTED_FROM]->(t:Table)
  WITH m, collect(t.source + '.' + t.name) AS tables
  RETURN collect({id: m.id, source: m.source, kind: m.kind, server: m.mcp_server, tool: m.mcp_tool,
                  endpoint: m.endpoint_id, unit: m.unit, dimensions: m.dimensions,
                  required: m.required_dimensions, sensitive: m.sensitive_dimensions, tables: tables,
                  allowed_scopes: m.allowed_scopes}) AS metrics
}
CALL () {
  MATCH (r:Role {ns: $ns})
  OPTIONAL MATCH (r)-[g:CAN_READ]->(o)
  WITH r, collect({object: o.uid, scope: g.scope, row_scope: g.row_scope}) AS grants
  RETURN collect({role: r.persona_id, metrics_only: r.metrics_only, scopes: r.scopes, grants: grants}) AS roles
}
CALL () {
  MATCH (n:Ctx {ns: $ns}) RETURN max(n.loaded_version) AS version
}
RETURN metrics, roles, version
"""


def load_catalog(driver, ns: str = "prism") -> dict:
    t = time.perf_counter()
    rec = driver.execute_query(CATALOG_CYPHER, ns=ns, routing_="r").records[0]
    out = {"version": rec["version"],
           "metrics": {m["id"]: m for m in rec["metrics"]},
           "roles": {r["role"]: r for r in rec["roles"]},
           "elapsed_ms": round(1000 * (time.perf_counter() - t), 2)}
    return out


if __name__ == "__main__":
    import json

    from neo4j import GraphDatabase

    from schema import AUTH, URI
    with GraphDatabase.driver(URI, auth=AUTH, notifications_min_severity="OFF") as d:
        times = [load_catalog(d)["elapsed_ms"] for _ in range(50)]
        c = load_catalog(d)
        print("catalog: metrics", len(c["metrics"]), "roles", len(c["roles"]), "version", c["version"])
        times.sort()
        print(f"catalog query ms: first={times[0]} p50={times[25]} p95={times[47]} max={times[-1]}")
        print(json.dumps(c["metrics"]["manual_matches"]))
        print(json.dumps(c["roles"]["invest_ops_growth"]))
