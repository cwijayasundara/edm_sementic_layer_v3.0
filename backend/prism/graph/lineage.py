"""Lineage of one result for the UI's context-graph view: the role-gated part of the context graph behind the
metrics (and free-form sources) of a result handle. `build_lineage` assembles query rows into the response; the
Cypher (lineage / alineage) applies retrieval.gate() to every node it returns. Nodes are identified by local_uid
only; no row values, embeddings or scopes leave this module."""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from prism.graph.retrieval import DEFAULT_TIMEOUT_S, arun_read, gate, gate_params, run_read

KINDS = ("Metric", "Source", "Dimension", "Table", "Endpoint", "BusinessTerm", "Column", "Field", "Question")
PRIORITY = {"Result": -1, **{k: i for i, k in enumerate(KINDS)}}
MAX_NODES = 150
DETAIL_MAX = 300
MAX_QUESTIONS = 3
RESULT_ID = "result:combined"


def _label(n: dict) -> str:
    kind, name = n["kind"], n.get("name")
    if kind == "Column" and n.get("table") and name:
        return f"{n['table']}.{name}"
    if kind == "Field" and n.get("endpoint_id") and name:
        return f"{n['endpoint_id']}.{name}"
    if kind == "Table" and n.get("qualified_name"):
        return n["qualified_name"]
    if kind == "Question" and n.get("text"):
        return n["text"]
    return name or n["local_uid"]


def _detail(n: dict) -> str | None:
    raw = {"Metric": n.get("definition") or n.get("description"), "BusinessTerm": n.get("definition"),
           "Column": n.get("type"), "Field": n.get("type"), "Endpoint": n.get("description"),
           "Table": n.get("qualified_name"), "Question": n.get("text")}.get(n["kind"])
    return str(raw)[:DETAIL_MAX] if raw else None


def _node(n: dict) -> dict:
    out = {"id": n["local_uid"], "kind": n["kind"], "label": _label(n)}
    if n.get("source"):
        out["source"] = n["source"]
    if (detail := _detail(n)) is not None:
        out["detail"] = detail
    return out


def _known(n: dict | None) -> bool:
    return bool(n) and n.get("kind") in KINDS and bool(n.get("local_uid"))


def build_lineage(rows: list[dict], *, combined_inputs: list[str] | None = None,
                  max_nodes: int = MAX_NODES) -> dict:
    """Rows {a, t, b} (t and b None for a lone node) -> {nodes, edges, truncated}. Nodes of unknown kinds are
    dropped with their edges; past `max_nodes` the lowest-priority kinds go first, and edges to dropped nodes too.
    `combined_inputs` (a combine result) adds a Result root (only when some input is present) linked by COMBINES to each input node present."""
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        a, t, b = r.get("a"), r.get("t"), r.get("b")
        for x in (a, b):
            if _known(x):
                nodes.setdefault(x["local_uid"], _node(x))
        if t and _known(a) and _known(b):
            edges[(a["local_uid"], t, b["local_uid"])] = {"from": a["local_uid"], "to": b["local_uid"], "type": t}
    for src, t, dst in edges:
        if t == "HAS_DIMENSION" and nodes[dst]["kind"] == "Dimension" and "source" not in nodes[dst]:
            if "source" in nodes[src]:
                nodes[dst]["source"] = nodes[src]["source"]
    if combined_inputs is not None and any(i in nodes for i in combined_inputs):
        nodes[RESULT_ID] = {"id": RESULT_ID, "kind": "Result", "label": "Combined result"}
        for target in combined_inputs:
            if target in nodes:
                edges[(RESULT_ID, "COMBINES", target)] = {"from": RESULT_ID, "to": target, "type": "COMBINES"}
    ordered = sorted(nodes.values(), key=lambda x: (PRIORITY[x["kind"]], x["id"]))
    kept = ordered[:max_nodes]
    ids = {x["id"] for x in kept}
    kept_edges = sorted((e for e in edges.values() if e["from"] in ids and e["to"] in ids),
                        key=lambda e: (e["from"], e["type"], e["to"]))
    return {"nodes": kept, "edges": kept_edges, "truncated": len(ordered) > max_nodes}


def _proj(x: str) -> str:
    return (f"{x} {{.local_uid, .name, .qualified_name, .table, .endpoint_id, .source, .definition, .description,"
            f" .type, .text, kind: [l IN labels({x}) WHERE l IN $kinds][0]}}")


_USED_DIM = "d.name IN p.dims AND " + gate("d")
LINEAGE_CYPHER = f"""
UNWIND $plans AS p
MATCH (m:Metric {{ns: $ns, id: p.metric}}) WHERE {gate('m')}
CALL (m, p) {{
  RETURN m AS a, null AS t, null AS b
  UNION
  MATCH (m)-[r:HAS_DIMENSION]->(d:Dimension) WHERE {_USED_DIM}
  RETURN m AS a, type(r) AS t, d AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[r:ON_COLUMN]->(c:Column) WHERE {_USED_DIM} AND {gate('c')}
  RETURN d AS a, type(r) AS t, c AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[:ON_COLUMN]->(c:Column)<-[r:HAS_COLUMN]-(tb:Table)
  WHERE {_USED_DIM} AND {gate('c')} AND {gate('tb')}
  RETURN tb AS a, type(r) AS t, c AS b
  UNION
  MATCH (m)-[r:COMPUTED_FROM]->(x) WHERE (x:Table OR x:Endpoint) AND {gate('x')}
  RETURN m AS a, type(r) AS t, x AS b
  UNION
  MATCH (m)-[:COMPUTED_FROM]->(e:Endpoint)-[r:BACKED_BY]->(tb:Table) WHERE {gate('e')} AND {gate('tb')}
  RETURN e AS a, type(r) AS t, tb AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[:ON_COLUMN]->(c:Column)<-[:MAPS_TO]-(f:Field)<-[r:RETURNS]-(e:Endpoint)
  WHERE (m)-[:COMPUTED_FROM]->(e) AND {_USED_DIM} AND {gate('c')} AND {gate('f')} AND {gate('e')}
  RETURN e AS a, type(r) AS t, f AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[:ON_COLUMN]->(c:Column)<-[r:MAPS_TO]-(f:Field)<-[:RETURNS]-(e:Endpoint)
  WHERE (m)-[:COMPUTED_FROM]->(e) AND {_USED_DIM} AND {gate('c')} AND {gate('f')} AND {gate('e')}
  RETURN f AS a, type(r) AS t, c AS b
  UNION
  MATCH (m)-[:COMPUTED_FROM]->(x)<-[r:HAS_TABLE|HAS_ENDPOINT]-(src:Source) WHERE {gate('x')} AND {gate('src')}
  RETURN src AS a, type(r) AS t, x AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[:ON_COLUMN]->(c:Column)<-[:HAS_COLUMN]-(tb:Table)<-[r:HAS_TABLE]-(src:Source)
  WHERE {_USED_DIM} AND {gate('c')} AND {gate('tb')} AND {gate('src')}
  RETURN src AS a, type(r) AS t, tb AS b
  UNION
  MATCH (src:Source {{ns: $ns, name: m.source}}) WHERE {gate('src')}
  RETURN src AS a, 'PROVIDES' AS t, m AS b
  UNION
  MATCH (bt:BusinessTerm)-[r:DEFINES]->(m) WHERE {gate('bt')}
  RETURN bt AS a, type(r) AS t, m AS b
  UNION
  CALL (m) {{
    MATCH (q:Question)-[:ANSWERED_BY]->(e:Execution)-[:USED]->(m) WHERE {gate('q')} AND {gate('e')}
    WITH q, max(coalesce(e.count, 0)) AS n
    ORDER BY q.origin IS NOT NULL, n DESC, q.local_uid
    LIMIT $max_questions
    RETURN q
  }}
  RETURN q AS a, 'ASKED_ABOUT' AS t, m AS b
}}
RETURN {_proj('a')} AS a, t, CASE WHEN b IS NULL THEN null ELSE {_proj('b')} END AS b
"""

SOURCES_CYPHER = f"""
UNWIND $sources AS sn
MATCH (src:Source {{ns: $ns, name: sn}}) WHERE {gate('src')}
RETURN {_proj('src')} AS a, null AS t, null AS b
"""


def _params(plans: Mapping[str, Iterable[str]], sources: Iterable[str], claims: dict, ns: str | None) -> dict:
    scopes = [str(s) for s in claims.get("scopes") or []]
    return {"plans": [{"metric": m, "dims": sorted(set(d))} for m, d in sorted(plans.items())],
            "sources": sorted(set(sources)), "kinds": list(KINDS), "max_questions": MAX_QUESTIONS,
            **gate_params(scopes, bool(claims.get("metrics_only")), ns)}


def lineage(driver, plans: Mapping[str, Iterable[str]], sources: Iterable[str], claims: dict, *,
            combined_inputs: list[str] | None = None, ns: str | None = None,
            timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Role-gated lineage graph of the given metrics (with the dimensions a result used) and free-form sources."""
    params = _params(plans, sources, claims, ns)
    rows = run_read(driver, LINEAGE_CYPHER, params, timeout_s) if params["plans"] else []
    if params["sources"]:
        rows += run_read(driver, SOURCES_CYPHER, params, timeout_s)
    return build_lineage(rows, combined_inputs=combined_inputs)


async def alineage(adriver, plans: Mapping[str, Iterable[str]], sources: Iterable[str], claims: dict, *,
                   combined_inputs: list[str] | None = None, ns: str | None = None,
                   timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Async twin of lineage (neo4j.AsyncDriver)."""
    params = _params(plans, sources, claims, ns)
    rows = await arun_read(adriver, LINEAGE_CYPHER, params, timeout_s) if params["plans"] else []
    if params["sources"]:
        rows += await arun_read(adriver, SOURCES_CYPHER, params, timeout_s)
    return build_lineage(rows, combined_inputs=combined_inputs)


__all__ = ["DETAIL_MAX", "KINDS", "LINEAGE_CYPHER", "MAX_NODES", "MAX_QUESTIONS", "RESULT_ID", "SOURCES_CYPHER",
           "alineage", "build_lineage", "lineage"]
