"""Idempotent, versioned context-graph loader (ported from the plan-3 Neo4j spike).

- Every node is (:Ctx:<Kind> {uid, ns, loaded_version, ...}); MERGE on {uid, ns} in UNWIND batches; `SET n = props`
  replaces properties, so keys removed from the YAML disappear. A same-uid node of another namespace violates the
  ctx_uid constraint instead of being taken over.
- Upserts and the stale cleanup run in ONE write transaction: a reader sees the old graph or the new one.
- Stale cleanup is namespace-scoped: nodes and relationships of `ns` whose loaded_version is older than this load.
- ns must match ^[a-z0-9_-]+$. ns != settings.graph_ns (tests, scratch loads): uids are prefixed "<ns>:", so the real
  graph is never touched; delete_ns refuses the configured namespace unless explicitly allowed.
- Labels and relationship types are interpolated into Cypher, so each is checked against an identifier pattern first.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field

from neo4j import unit_of_work

from prism.config import Settings
from prism.graph.embedder import Embedder
from prism.graph.model import Graph, build_graph, check_label, check_rel_type
from prism.graph.schema import create_schema

_NS = re.compile(r"[a-z0-9_-]+")
BATCH = 500
TX_TIMEOUT_S = 120
HIDDEN_LABELS = ["Ctx", "Searchable"]


@dataclass
class LoadReport:
    version: int
    nodes: int
    rels: int
    deleted: dict[str, int] = field(default_factory=dict)  # stale {"nodes": n, "rels": n}
    seconds: dict[str, float] = field(default_factory=dict)


def production_ns() -> str:
    """The configured (real) namespace, read at call time so PRISM_GRAPH_NS is honoured."""
    return check_ns(Settings().graph_ns)


def check_ns(ns: str) -> str:
    if not isinstance(ns, str) or not _NS.fullmatch(ns):
        raise ValueError(f"bad graph namespace {ns!r}: must match ^[a-z0-9_-]+$")
    return ns


def _uid_prefix(ns: str) -> str:
    return "" if check_ns(ns) == production_ns() else f"{ns}:"


def uid_for(ns: str, local: str) -> str:
    return _uid_prefix(ns) + local


def _check_identifiers(g: Graph) -> None:
    """Graph.node / Graph.edge check these too, but nodes and edges can be assigned directly; check before writing."""
    for n in g.nodes.values():
        for label in n["labels"]:
            check_label(label)
    for _, typ, _, _ in g.edges:
        check_rel_type(typ)


def _write(tx, g: Graph, ns: str, version: int) -> dict[str, int]:
    pre = _uid_prefix(ns)
    by_labels: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for local, n in g.nodes.items():
        props = {**n["props"], "uid": pre + local, "local_uid": local, "ns": ns, "loaded_version": version}
        by_labels[tuple(n["labels"])].append(props)
    for labels, rows in by_labels.items():
        label_expr = ":".join(check_label(lb) for lb in labels)
        for i in range(0, len(rows), BATCH):
            tx.run(f"UNWIND $rows AS r MERGE (n:Ctx {{uid: r.uid, ns: r.ns}}) SET n = r, n:{label_expr}",
                   rows=rows[i:i + BATCH]).consume()
    by_type: dict[str, list[dict]] = defaultdict(list)
    for a, typ, b, props in g.edges:
        by_type[typ].append({"a": pre + a, "b": pre + b,
                             "p": {**props, "ns": ns, "loaded_version": version}})
    for typ, rows in by_type.items():
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            n = tx.run(f"UNWIND $rows AS r MATCH (a:Ctx {{uid: r.a, ns: $ns}}) MATCH (b:Ctx {{uid: r.b, ns: $ns}}) "
                       f"MERGE (a)-[e:{check_rel_type(typ)}]->(b) SET e = r.p RETURN count(e) AS n",
                       rows=chunk, ns=ns).single()["n"]
            if n != len(chunk):  # build_graph checks endpoints; this guards the write itself
                raise RuntimeError(f"{typ}: wrote {n} of {len(chunk)} relationships")
    rels = tx.run("MATCH (:Ctx {ns: $ns})-[r]->() WHERE r.loaded_version < $v DELETE r RETURN count(r) AS c",
                  ns=ns, v=version).single()["c"]
    nodes = tx.run("MATCH (n:Ctx {ns: $ns}) WHERE n.loaded_version < $v DETACH DELETE n RETURN count(n) AS c",
                   ns=ns, v=version).single()["c"]
    return {"nodes": nodes, "rels": rels}


@unit_of_work(timeout=TX_TIMEOUT_S)
def _load_tx(tx, g: Graph, ns: str) -> tuple[int, dict[str, int]]:
    prev = tx.run("MATCH (n:Ctx {ns: $ns}) RETURN coalesce(max(n.loaded_version), 0) AS v", ns=ns).single()["v"]
    version = max(int(time.time()), prev + 1)  # epoch seconds, strictly increasing even within one second
    return version, _write(tx, g, ns, version)


def load(driver, embedder: Embedder | None, ns: str | None = None, *, graph: Graph | None = None,
         schema: bool = True) -> LoadReport:
    """Build (unless `graph` is given) and write the graph into `ns` (default settings.graph_ns); returns what was
    written and deleted."""
    t0 = time.perf_counter()
    ns = production_ns() if ns is None else check_ns(ns)
    if graph is not None:
        _check_identifiers(graph)  # before any driver use
    if schema:
        create_schema(driver)
    g = graph if graph is not None else build_graph(embedder)
    if graph is None:
        _check_identifiers(g)
    t_build = time.perf_counter()
    with driver.session(database="neo4j") as s:
        version, deleted = s.execute_write(_load_tx, g, ns)
    t_write = time.perf_counter()
    driver.execute_query("CALL db.awaitIndexes($t)", t=TX_TIMEOUT_S)
    t_end = time.perf_counter()
    return LoadReport(version=version, nodes=len(g.nodes), rels=len(g.edges), deleted=deleted,
                      seconds={"build": round(t_build - t0, 3), "write": round(t_write - t_build, 3),
                               "total": round(t_end - t0, 3)})


def counts(driver, ns: str | None = None) -> dict:
    """{"nodes", "rels", "by_label", "by_type"} for one namespace (default settings.graph_ns; relationships counted
    from their start node)."""
    ns = production_ns() if ns is None else check_ns(ns)
    r = driver.execute_query(
        "MATCH (n:Ctx {ns: $ns}) WITH count(n) AS nodes "
        "OPTIONAL MATCH (:Ctx {ns: $ns})-[r]->() RETURN nodes, count(r) AS rels", ns=ns).records[0]
    labels = driver.execute_query(
        "MATCH (n:Ctx {ns: $ns}) UNWIND labels(n) AS l WITH l WHERE NOT l IN $hidden "
        "RETURN l, count(*) AS c ORDER BY l", ns=ns, hidden=HIDDEN_LABELS).records
    types = driver.execute_query(
        "MATCH (:Ctx {ns: $ns})-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY t", ns=ns).records
    return {"nodes": r["nodes"], "rels": r["rels"], "by_label": {x["l"]: x["c"] for x in labels},
            "by_type": {x["t"]: x["c"] for x in types}}


def delete_ns(driver, ns: str, *, allow_production: bool = False) -> None:
    """Delete every node of `ns`. Refuses the configured namespace (settings.graph_ns) unless allow_production."""
    if check_ns(ns) == production_ns() and not allow_production:
        raise ValueError(f"refusing to delete the configured graph namespace {ns!r} (pass allow_production=True)")
    # CALL {...} IN TRANSACTIONS needs an implicit (auto-commit) transaction: session.run, not execute_query.
    with driver.session(database="neo4j") as s:
        s.run("MATCH (n:Ctx {ns: $ns}) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS", ns=ns).consume()
