"""Lineage of one result for the UI's context-graph view: the role-gated part of the context graph behind the
metrics (and free-form sources) of a result handle. `build_lineage` assembles query rows into the response; the
Cypher (lineage / alineage) applies retrieval.gate() to every node it returns. Nodes are identified by local_uid
only; no row values, embeddings or scopes leave this module."""
from __future__ import annotations

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
    `combined_inputs` (a combine result) adds a Result root linked by COMBINES to each input node present."""
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        a, t, b = r.get("a"), r.get("t"), r.get("b")
        for x in (a, b):
            if _known(x):
                nodes.setdefault(x["local_uid"], _node(x))
        if t and _known(a) and _known(b):
            edges[(a["local_uid"], t, b["local_uid"])] = {"from": a["local_uid"], "to": b["local_uid"], "type": t}
    if combined_inputs is not None:
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


__all__ = ["DETAIL_MAX", "KINDS", "MAX_NODES", "MAX_QUESTIONS", "RESULT_ID", "build_lineage"]
