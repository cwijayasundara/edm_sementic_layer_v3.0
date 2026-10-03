"""Role-filtered hybrid retrieval over the context graph (vector + BM25 fulltext, RRF-fused per kind in Cypher) and
the context pack the gateway hands to an agent. Ported from the plan-3 Neo4j spike.

Security model: `gate(x)` is the ONE visibility predicate and every Cypher statement here applies it to every node it
returns or traverses through (hits, DEFINES / IMPLEMENTED_BY / IDENTIFIED_BY / TAGGED_WITH / BROADER / HAS_COLUMN /
BACKED_BY / ANSWERED_BY / USED targets, and every node of a join path). Terms, concepts and questions carry the union
of their links' scopes, so a readable term never vouches for the objects it links to. Role nodes are never visible
(the gateway reads them through `catalog.load_catalog`). `prism.graph.model.visible` is the Python twin; the tests
check that both agree on every node of the loaded graph.

Query history: a distilled question (origin = "history", prism.graph.history) contributes its example only, never a
metric, and curated seed examples always rank before distilled ones in `examples`.

User text never reaches the Lucene parser raw (`lucene_query`), and every statement runs as an auto-commit read with
a server-side timeout; driver failures and timeouts surface as `GraphUnavailable`. A statement the server rejects
(our bug) surfaces as `GraphError` with a fixed message; the server's own text is only logged at debug.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass

from neo4j import Query, READ_ACCESS
from neo4j.exceptions import DriverError, Neo4jError

from prism.graph.loader import check_ns, production_ns
from prism.graph.schema import FULLTEXT_INDEX, VECTOR_INDEX

log = logging.getLogger(__name__)

DATABASE = "neo4j"
GRAPH_ERROR_MESSAGE = "context graph query failed"
RRF_C = 60
MAX_LUCENE_TOKENS = 64            # Lucene's default clause limit is 1024; a 50k-char question must not reach it
PACK_MAX_TOKENS = 3000            # estimated as chars / 4 of the compact JSON
DEFAULT_TIMEOUT_S = 5.0
# Typed lists (embedding spike: one shared list lets glossary terms push metrics down): per-kind top-n.
KIND_LIMITS = {"Metric": 5, "BusinessTerm": 3, "Concept": 2, "Column": 5, "Question": 3}
PATH_RELS = "COMPUTED_FROM|BACKED_BY|HAS_COLUMN|REFERENCES|SAME_KEY_AS"
PACK_KEYS = ("metrics", "terms", "concepts", "columns", "examples", "join_paths", "metric_links")
# Trimming drops the lowest-ranked item first; on equal rank, the first kind listed here goes first.
DROP_ORDER = ("examples", "columns", "join_paths", "metric_links", "concepts", "terms", "metrics")
MAX_LINKED_METRICS = 4   # one-hop JOINABLE_ON expansion: extra metric slots after the direct hits, one per new source


class GraphUnavailable(Exception):
    """The context graph could not answer in time (down, unreachable, timed out)."""


class GraphError(Exception):
    """The context graph rejected a statement (a bug on our side, not an outage). Always GRAPH_ERROR_MESSAGE: raw
    server text can quote the statement or its parameters and must not reach callers."""

    def __init__(self) -> None:
        super().__init__(GRAPH_ERROR_MESSAGE)


@dataclass(frozen=True)
class Hit:
    uid: str                 # graph uid (namespace-prefixed outside the production namespace)
    local_uid: str           # namespace-independent uid, e.g. "metric:open_breaks"
    kind: str
    name: str
    source: str | None
    score: float             # RRF score within its kind
    vector_rank: int | None
    text_rank: int | None


# ----------------------------------------------------------------------------------------------- the gate
def _scoped(x: str) -> str:
    return (f"{x}.ns = $ns AND NOT {x}:Role AND any(s IN coalesce({x}.allowed_scopes, []) WHERE s IN $scopes)"
            f" AND NOT ($metrics_only AND ({x}:Table OR {x}:Column OR {x}:Field OR {x}:Endpoint"
            f" OR coalesce({x}.sensitive, false)))")


def gate(x: str) -> str:
    """Visibility of node variable `x` for the caller ($ns, $scopes incl. '*', $metrics_only): namespace, any-of
    scope, never a Role, no physical schema or sensitive dimension for metrics-only callers, and a Question /
    Execution only with at least one USED object and every USED object visible."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", x):
        raise ValueError(f"bad Cypher variable {x!r}")
    u = f"{x}_used"
    used = f"({x})-[:ANSWERED_BY]->{{0,1}}(:Execution)-[:USED]->({u})"
    return (f"({_scoped(x)} AND (NOT ({x}:Question OR {x}:Execution) OR"
            f" (EXISTS {{ MATCH {used} }} AND NOT EXISTS {{ MATCH {used} WHERE NOT ({_scoped(u)}) }})))")


def _metric_map(x: str) -> str:
    """Cypher map projection of Metric `x` with its visible dimension names (shared by expand and linking)."""
    return (f"{x} {{.uid, .local_uid, .id, .source, .kind, .mcp_tool, .endpoint_id, .unit, .definition, "
            f".required_dimensions, .sensitive_dimensions, .filters, .time_column, .tables, "
            f"dims: COLLECT {{ MATCH ({x})-[:HAS_DIMENSION]->(d:Dimension) WHERE {gate('d')} "
            f"RETURN d.name ORDER BY d.name }}}}")


SEARCH_CYPHER = f"""
UNWIND $kinds AS kd
CALL (kd) {{
  CALL db.index.vector.queryNodes('{VECTOR_INDEX}', $fetch, $qv) YIELD node, score
  WITH node, score WHERE kd.label IN labels(node) AND {gate('node')}
  WITH node ORDER BY score DESC
  WITH collect(node) AS found
  RETURN found[0..kd.n] AS vec
}}
CALL (kd) {{
  WITH kd WHERE $ft <> ''
  CALL db.index.fulltext.queryNodes('{FULLTEXT_INDEX}', $ft, {{limit: $fetch}}) YIELD node, score
  WITH node, score WHERE kd.label IN labels(node) AND {gate('node')}
  WITH node ORDER BY score DESC
  WITH collect(node) AS found
  RETURN found[0..kd.n] AS ft
}}
UNWIND vec + ft AS n
WITH DISTINCT kd, n, vec, ft
WITH kd, n, [i IN range(0, size(vec) - 1) WHERE vec[i] = n][0] AS vr, [i IN range(0, size(ft) - 1) WHERE ft[i] = n][0] AS fr
WITH kd, n, vr, fr, coalesce(1.0 / ($c + vr + 1), 0.0) + coalesce(1.0 / ($c + fr + 1), 0.0) AS rrf
ORDER BY kd.order, rrf DESC, n.uid
WITH kd, collect({{uid: n.uid, local_uid: n.local_uid, name: n.name, source: n.source, score: rrf,
                   vector_rank: vr, text_rank: fr}}) AS ranked
UNWIND ranked[0..kd.n] AS h
RETURN kd.label AS kind, h.uid AS uid, h.local_uid AS local_uid, h.name AS name, h.source AS source,
       h.score AS score, h.vector_rank AS vector_rank, h.text_rank AS text_rank
ORDER BY kd.order
"""

EXPAND_CYPHER = f"""
UNWIND range(0, size($hits) - 1) AS rank
MATCH (h:Ctx {{uid: $hits[rank], ns: $ns}}) WHERE {gate('h')}
WITH h, rank ORDER BY rank
WITH collect(h) AS hs
CALL (hs) {{
  UNWIND range(0, size(hs) - 1) AS i
  WITH i, hs[i] AS h
  CALL (h) {{
    WITH h WHERE h:Metric RETURN h AS m
    UNION
    MATCH (h)-[:DEFINES]->(m:Metric) WHERE {gate('m')} RETURN m
    UNION
    MATCH (h)-[:ANSWERED_BY]->(e:Execution)-[:USED]->(m:Metric)
    WHERE h.origin IS NULL AND {gate('e')} AND {gate('m')} RETURN m        // curated seed questions only
  }}
  WITH m, min(i) AS r ORDER BY r, m.id LIMIT $max_metrics
  RETURN collect({_metric_map('m')}) AS metrics
}}
CALL (hs) {{
  UNWIND hs AS t WITH t WHERE t:BusinessTerm
  RETURN collect(t {{.local_uid, .name, .definition, .synonyms, .rule,
                    broader: COLLECT {{ MATCH (t)-[:BROADER]->(b:BusinessTerm) WHERE {gate('b')}
                                        RETURN b.name ORDER BY b.name }}}}) AS terms
}}
CALL (hs) {{
  UNWIND range(0, size(hs) - 1) AS i
  WITH i, hs[i] AS h
  CALL (h) {{
    WITH h WHERE h:Concept RETURN h AS c
    UNION
    MATCH (h)-[:DEFINES]->(c:Concept) WHERE {gate('c')} RETURN c
  }}
  WITH c, min(i) AS r ORDER BY r, c.name
  RETURN collect(c {{.local_uid, .name, .description,
                    implemented_by: COLLECT {{ MATCH (c)-[:IMPLEMENTED_BY]->(o) WHERE {gate('o')}
                                               RETURN o {{.uid, .local_uid, .source}} ORDER BY o.local_uid }},
                    keys: COLLECT {{ MATCH (c)-[:IDENTIFIED_BY]->(o) WHERE {gate('o')}
                                     RETURN o.local_uid ORDER BY o.local_uid }},
                    related: COLLECT {{ MATCH (c)-[rel]->(o:Concept) WHERE {gate('o')}
                                        RETURN type(rel) + ' ' + o.name ORDER BY o.name }}}}) AS concepts
}}
CALL (hs) {{
  UNWIND range(0, size(hs) - 1) AS i
  WITH i, hs[i] AS h
  CALL (h) {{
    WITH h WHERE h:Column RETURN h AS col
    UNION
    MATCH (h:BusinessTerm)<-[:TAGGED_WITH]-(col:Column) WHERE {gate('col')} RETURN col
  }}
  WITH col, min(i) AS r
  MATCH (tb:Table)-[:HAS_COLUMN]->(col) WHERE {gate('tb')}
  WITH tb, min(r) AS r, collect(DISTINCT col {{.uid, .local_uid, .name, .type, .source}}) AS cols
  ORDER BY r, tb.qualified_name
  RETURN collect({{table: tb.qualified_name, cols: cols,
                   endpoints: COLLECT {{ MATCH (ep:Endpoint)-[:BACKED_BY]->(tb) WHERE {gate('ep')}
                                         RETURN ep.endpoint_id ORDER BY ep.endpoint_id }}}}) AS columns
}}
CALL (hs) {{
  UNWIND range(0, size(hs) - 1) AS i
  WITH i, hs[i] AS q WHERE q:Question
  MATCH (q)-[:ANSWERED_BY]->(e:Execution) WHERE {gate('e')}
  WITH q, e, i ORDER BY q.origin IS NOT NULL, i, coalesce(e.count, 0) DESC, e.uid   // seed examples first
  RETURN collect({{local_uid: q.local_uid, question: q.text, plan: e.plan, status: e.status}})[0..3] AS examples
}}
RETURN metrics, terms, concepts, columns, examples
"""

PATH_CYPHER = f"""
UNWIND $pairs AS pr
MATCH (a:Ctx {{uid: pr[0], ns: $ns}}), (b:Ctx {{uid: pr[1], ns: $ns}})
WHERE {gate('a')} AND {gate('b')}
OPTIONAL CALL (a, b) {{
  MATCH p = SHORTEST 1 (a)(()-[:{PATH_RELS}]-(y) WHERE {gate('y')}){{1,12}}(b)
  RETURN p
}}
WITH pr, p WHERE p IS NOT NULL
RETURN pr[0] AS start, pr[1] AS end, length(p) AS hops,
       [n IN nodes(p) WHERE n:Table OR n:Column | coalesce(n.qualified_name, n.source + '.' + n.table)] AS tables,
       [r IN relationships(p) WHERE type(r) IN ['REFERENCES', 'SAME_KEY_AS'] |
          startNode(r).source + '.' + startNode(r).table + '.' + startNode(r).name + ' = ' +
          endNode(r).source + '.' + endNode(r).table + '.' + endNode(r).name] AS joins
"""

LINKED_CYPHER = f"""
CALL () {{
  UNWIND range(0, size($direct) - 1) AS i
  MATCH (a:Metric {{uid: $direct[i], ns: $ns}})-[:JOINABLE_ON]-(m:Metric)
  WHERE {gate('a')} AND {gate('m')} AND NOT m.uid IN $direct AND NOT m.source IN $sources
  WITH m, min(i) AS r ORDER BY r, m.id
  RETURN collect({_metric_map('m')}) AS linked
}}
CALL (linked) {{
  WITH $direct + [x IN linked | x.uid] AS uids
  MATCH (a:Metric)-[r:JOINABLE_ON]->(b:Metric)
  WHERE a.uid IN uids AND b.uid IN uids AND {gate('a')} AND {gate('b')}
    AND EXISTS {{ MATCH (a)-[:HAS_DIMENSION]->(da:Dimension {{name: r.key}}) WHERE {gate('da')} }}
    AND EXISTS {{ MATCH (b)-[:HAS_DIMENSION]->(dm:Dimension {{name: r.other_key}}) WHERE {gate('dm')} }}
  RETURN collect({{a: a.uid, b: b.uid, key: r.key, other_key: r.other_key}}) AS links
}}
RETURN linked, links
"""


# ----------------------------------------------------------------------------------------------- parameters
def lucene_query(text: object) -> str:
    """Safe Lucene query from free text: lower-cased \\w+ tokens longer than one character (so AND/OR/NOT are never
    operators), de-duplicated, capped, OR-joined. '' when there is nothing to search for."""
    toks = [t for t in re.findall(r"\w+", str(text or "").lower()) if len(t) > 1]
    return " OR ".join(list(dict.fromkeys(toks))[:MAX_LUCENE_TOKENS])


def resolve_ns(ns: str | None) -> str:
    return production_ns() if ns is None else check_ns(ns)


def gate_params(scopes, metrics_only: bool, ns: str | None) -> dict:
    """Caller scopes always get '*' appended (public glossary terms)."""
    return {"scopes": [str(s) for s in scopes] + ["*"], "metrics_only": bool(metrics_only), "ns": resolve_ns(ns)}


def fetch_size(k: int) -> int:
    """Over-fetch: vector top-k is global (all kinds, all namespaces) and the role gate runs after it."""
    return min(max(k * 25, 100), 1000)


def _kinds(metrics_only: bool) -> list[dict]:
    return [{"label": label, "n": n, "order": i} for i, (label, n) in enumerate(KIND_LIMITS.items())
            if not (metrics_only and label == "Column")]


def _search_params(qvec, text, scopes, k, metrics_only, ns) -> dict:
    return {"qv": [float(x) for x in qvec], "ft": lucene_query(text), "fetch": fetch_size(k), "c": RRF_C,
            "kinds": _kinds(metrics_only), **gate_params(scopes, metrics_only, ns)}


def _hits(rows: list[dict]) -> list[Hit]:
    return [Hit(**r) for r in rows]


# ----------------------------------------------------------------------------------------------- execution
def _query(cypher: str, timeout_s: float) -> Query:
    return Query(cypher, timeout=timeout_s)


def _translate(exc: Exception) -> Exception:
    """Driver failure -> GraphError (statement rejected: our bug, not an outage) or GraphUnavailable."""
    if isinstance(exc, Neo4jError) and exc.code and "ClientError.Statement" in exc.code:
        log.debug("context graph rejected a statement: %s %s", exc.code, exc.message)
        return GraphError()
    return GraphUnavailable(f"context graph unavailable: {type(exc).__name__}")


def run_read(driver, cypher: str, params: dict, timeout_s: float) -> list[dict]:
    try:
        with driver.session(database=DATABASE, default_access_mode=READ_ACCESS) as s:
            return [r.data() for r in s.run(_query(cypher, timeout_s), params)]
    except (DriverError, Neo4jError, OSError) as exc:
        err, cause = _translate(exc), exc
    raise err from (None if isinstance(err, GraphError) else cause)  # raised outside `except`: no raw text chained


async def arun_read(adriver, cypher: str, params: dict, timeout_s: float) -> list[dict]:
    async def go() -> list[dict]:
        async with adriver.session(database=DATABASE, default_access_mode=READ_ACCESS) as s:
            res = await s.run(_query(cypher, timeout_s), params)
            return [r.data() async for r in res]

    try:
        return await asyncio.wait_for(go(), timeout_s)
    except TimeoutError as exc:
        raise GraphUnavailable(f"context graph did not answer within {timeout_s}s") from exc
    except (DriverError, Neo4jError, OSError) as exc:
        err, cause = _translate(exc), exc
    raise err from (None if isinstance(err, GraphError) else cause)


# ----------------------------------------------------------------------------------------------- search
def search_context(driver, qvec, text, scopes, k: int = 8, *, metrics_only: bool = False, ns: str | None = None,
                   timeout_s: float = DEFAULT_TIMEOUT_S) -> list[Hit]:
    """Typed, role-gated hybrid search: per kind (KIND_LIMITS; no Column for metrics-only callers) the vector and
    fulltext rankings are RRF-fused. Hits come back grouped by kind in KIND_LIMITS order, best first."""
    return _hits(run_read(driver, SEARCH_CYPHER, _search_params(qvec, text, scopes, k, metrics_only, ns), timeout_s))


async def asearch_context(adriver, qvec, text, scopes, k: int = 8, *, metrics_only: bool = False,
                          ns: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> list[Hit]:
    """Async twin (neo4j.AsyncDriver; bounded by asyncio.wait_for)."""
    return _hits(await arun_read(adriver, SEARCH_CYPHER, _search_params(qvec, text, scopes, k, metrics_only, ns),
                             timeout_s))


# ----------------------------------------------------------------------------------------------- expand
def _expand_params(hits, scopes, metrics_only, ns, max_metrics) -> dict:
    uids = [h.uid if isinstance(h, Hit) else str(h) for h in hits]
    return {"hits": uids, "max_metrics": max_metrics, **gate_params(scopes, metrics_only, ns)}


def _metric_entry(m: dict, metrics_only: bool) -> dict:
    hidden = set(m.get("sensitive_dimensions") or []) if metrics_only else set()
    entry = {"id": m["id"], "source": m["source"], "kind": m["kind"], "tool": m["mcp_tool"],
             "unit": m.get("unit"), "definition": m.get("definition"),
             "dimensions": [d for d in m["dims"] if d not in hidden],
             "required": [d for d in m.get("required_dimensions") or [] if d not in hidden],
             "filters": [f for f in m.get("filters") or [] if f.split(":", 1)[0] not in hidden]}
    if not metrics_only:     # physical schema (endpoint, time column, tables) and sensitive names only for full callers
        entry["endpoint"] = m.get("endpoint_id")
        entry["time"] = m.get("time_column")
        entry["tables"] = m.get("tables")
        entry["sensitive"] = m.get("sensitive_dimensions")
    return entry


def _path_pairs(metrics: list[dict], columns: list[dict], concepts: list[dict], max_paths: int) -> list[tuple]:
    """(metric uid, target uid) pairs: each metric to readable columns / concept tables of OTHER sources, one path per
    target table, metrics and targets in rank order."""
    targets: list[tuple[str, str, str]] = []          # (uid, source, table key)
    for t in columns:
        for c in t["cols"]:
            targets.append((c["uid"], c["source"], t["table"]))
    for c in concepts:
        for o in c["implemented_by"]:
            if o["local_uid"].startswith("table:"):
                targets.append((o["uid"], o["source"], o["local_uid"][len("table:"):]))
    pairs, seen = [], set()
    for m in metrics:
        for uid, source, table in targets:
            if source and source != m["source"] and (m["uid"], table) not in seen:
                seen.add((m["uid"], table))
                pairs.append((m["uid"], uid))
    return pairs[:max_paths]


def _expand_finish(rec: dict, metrics_only: bool) -> tuple[dict, list[dict]]:
    pack = {
        "metrics": [_metric_entry(m, metrics_only) for m in rec["metrics"]],
        "terms": [{"term": t["name"], "definition": t.get("definition"), "synonyms": t.get("synonyms"),
                   "rule": t.get("rule"), "broader": t["broader"]} for t in rec["terms"]],
        "concepts": [{"concept": c["name"], "description": c.get("description"),
                      "implemented_by": [o["local_uid"] for o in c["implemented_by"]], "keys": c["keys"],
                      "related": c["related"]} for c in rec["concepts"]],
        "columns": [{"table": t["table"], "columns": [f"{c['name']}:{c['type']}" for c in t["cols"]],
                     "endpoints": t["endpoints"]} for t in rec["columns"]],
        "examples": [{"question": e["question"], "plan": e["plan"], "status": e["status"]} for e in rec["examples"]],
    }
    return pack, rec["metrics"]


def _paths_params(pairs, scopes, metrics_only, ns) -> dict:
    return {"pairs": [list(p) for p in pairs], **gate_params(scopes, metrics_only, ns)}


def _paths_finish(rows: list[dict], metric_ids: dict[str, str], local: dict[str, str]) -> list[dict]:
    return [{"from": metric_ids[r["start"]], "to": local.get(r["end"], r["end"]), "hops": r["hops"],
             "tables": [t for i, t in enumerate(r["tables"]) if i == 0 or t != r["tables"][i - 1]],
             "on": r["joins"]} for r in rows]


def _local_targets(rec: dict) -> dict[str, str]:
    out = {c["uid"]: c["local_uid"] for t in rec["columns"] for c in t["cols"]}
    out.update({o["uid"]: o["local_uid"] for c in rec["concepts"] for o in c["implemented_by"]})
    return out


def _linked_params(direct: list[dict], scopes, metrics_only, ns) -> dict:
    return {"direct": [m["uid"] for m in direct], "sources": sorted({m["source"] for m in direct}),
            **gate_params(scopes, metrics_only, ns)}


def _link_finish(direct: list[dict], rec: dict, max_linked: int = MAX_LINKED_METRICS) -> tuple[list[dict], list[dict]]:
    """Linked metrics in rank order, one per source not yet in the pack, each joined to a direct metric by a visible
    JOINABLE_ON edge; then the links whose both ends are in the pack, as {a, b, on: "<a dim> = <b dim>"}."""
    direct_uids = {m["uid"] for m in direct}
    attached = {e[x] for e in rec["links"] for x, y in (("a", "b"), ("b", "a")) if e[y] in direct_uids}
    sources, picked = {m["source"] for m in direct}, []
    for m in rec["linked"]:
        if len(picked) < max_linked and m["uid"] in attached and m["source"] not in sources:
            picked.append(m)
            sources.add(m["source"])
    metrics = [*direct, *picked]
    ids = {m["uid"]: m["id"] for m in metrics}
    links = [{"a": ids[e["a"]], "b": ids[e["b"]], "on": f"{e['key']} = {e['other_key']}"}
             for e in rec["links"] if e["a"] in ids and e["b"] in ids]
    return metrics, sorted(links, key=lambda x: (x["a"], x["b"], x["on"]))


def prune_links(pack: dict) -> dict:
    """After budget trimming: drop links whose metrics were trimmed away."""
    ids = {m["id"] for m in pack["metrics"]}
    pack["metric_links"] = [x for x in pack["metric_links"] if x["a"] in ids and x["b"] in ids]
    return pack


def expand(driver, hits, scopes, *, metrics_only: bool = False, ns: str | None = None,
           max_metrics: int = KIND_LIMITS["Metric"], max_paths: int = 3,
           timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Hits (Hit or uid, best first) -> pack sections. Unreadable hits are dropped; every hop is re-gated."""
    (rec,) = run_read(driver, EXPAND_CYPHER, _expand_params(hits, scopes, metrics_only, ns, max_metrics), timeout_s)
    pack, metrics = _expand_finish(rec, metrics_only)
    lrec = run_read(driver, LINKED_CYPHER, _linked_params(metrics, scopes, metrics_only, ns), timeout_s)[0] \
        if metrics else {"linked": [], "links": []}
    linked, links = _link_finish(metrics, lrec)
    pack["metrics"] += [_metric_entry(m, metrics_only) for m in linked[len(metrics):]]
    pack["metric_links"] = links
    pairs = [] if metrics_only else _path_pairs(metrics, rec["columns"], rec["concepts"], max_paths)
    rows = run_read(driver, PATH_CYPHER, _paths_params(pairs, scopes, metrics_only, ns), timeout_s) if pairs else []
    pack["join_paths"] = _paths_finish(rows, {m["uid"]: m["id"] for m in metrics}, _local_targets(rec))
    return _compact(pack)


async def aexpand(adriver, hits, scopes, *, metrics_only: bool = False, ns: str | None = None,
                  max_metrics: int = KIND_LIMITS["Metric"], max_paths: int = 3,
                  timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    (rec,) = await arun_read(adriver, EXPAND_CYPHER, _expand_params(hits, scopes, metrics_only, ns, max_metrics),
                               timeout_s)
    pack, metrics = _expand_finish(rec, metrics_only)
    lrec = (await arun_read(adriver, LINKED_CYPHER, _linked_params(metrics, scopes, metrics_only, ns), timeout_s))[0] \
        if metrics else {"linked": [], "links": []}
    linked, links = _link_finish(metrics, lrec)
    pack["metrics"] += [_metric_entry(m, metrics_only) for m in linked[len(metrics):]]
    pack["metric_links"] = links
    pairs = [] if metrics_only else _path_pairs(metrics, rec["columns"], rec["concepts"], max_paths)
    rows = await arun_read(adriver, PATH_CYPHER, _paths_params(pairs, scopes, metrics_only, ns), timeout_s) \
        if pairs else []
    pack["join_paths"] = _paths_finish(rows, {m["uid"]: m["id"] for m in metrics}, _local_targets(rec))
    return _compact(pack)


def _compact(x):
    if isinstance(x, dict):
        return {k: _compact(v) for k, v in x.items() if v not in (None, [], {}, "") or k in PACK_KEYS}
    if isinstance(x, list):
        return [_compact(v) for v in x if v not in (None, [], {}, "")]
    return x


# ----------------------------------------------------------------------------------------------- pack
def empty_pack() -> dict:
    return {k: [] for k in PACK_KEYS}


def pack_tokens(pack: dict) -> int:
    return round(len(json.dumps(pack, separators=(",", ":"), ensure_ascii=False)) / 4)


def trim_pack(pack: dict, max_tokens: int = PACK_MAX_TOKENS) -> dict:
    """Drop the lowest-ranked item (largest position in its list; DROP_ORDER breaks ties) until the pack fits."""
    out = {k: list(pack.get(k, [])) for k in PACK_KEYS}
    while pack_tokens(out) > max_tokens and any(out.values()):
        victim = max((k for k in PACK_KEYS if out[k]),
                     key=lambda k: (len(out[k]), -DROP_ORDER.index(k)))
        out[victim].pop()
    return out


def _claims_scopes(claims: dict) -> tuple[list[str], bool]:
    return list(claims.get("scopes") or []), bool(claims.get("metrics_only"))


def _grants_data(scopes: list[str]) -> bool:
    from prism.security.personas import ALL_SOURCES  # local: keeps the module import light for the CLI
    return any(str(s).split(".", 1)[0] in ALL_SOURCES for s in scopes)


def context_pack(question: str, claims: dict, *, driver, embedder=None, qvec=None, ns: str | None = None, k: int = 8,
                 timeout_s: float = DEFAULT_TIMEOUT_S, max_tokens: int = PACK_MAX_TOKENS) -> dict:
    """Role-filtered context pack for one question: {metrics, terms, concepts, columns, examples, join_paths}, at most
    `max_tokens` estimated tokens. A caller whose scopes name no data source gets the empty pack."""
    scopes, metrics_only = _claims_scopes(claims)
    if not _grants_data(scopes):
        return empty_pack()
    vec = qvec if qvec is not None else embedder.embed_query(question)
    hits = search_context(driver, vec, question, scopes, k, metrics_only=metrics_only, ns=ns, timeout_s=timeout_s)
    pack = expand(driver, hits, scopes, metrics_only=metrics_only, ns=ns, timeout_s=timeout_s) if hits else {}
    return prune_links(trim_pack(pack, max_tokens))


async def acontext_pack(question: str, claims: dict, *, driver, embedder=None, qvec=None, ns: str | None = None,
                        k: int = 8, timeout_s: float = DEFAULT_TIMEOUT_S, max_tokens: int = PACK_MAX_TOKENS) -> dict:
    """Async twin of context_pack (neo4j.AsyncDriver; the embedding runs off the event loop)."""
    scopes, metrics_only = _claims_scopes(claims)
    if not _grants_data(scopes):
        return empty_pack()
    vec = qvec if qvec is not None else await embedder.aembed_query(question)
    hits = await asearch_context(driver, vec, question, scopes, k, metrics_only=metrics_only, ns=ns,
                                 timeout_s=timeout_s)
    pack = await aexpand(driver, hits, scopes, metrics_only=metrics_only, ns=ns, timeout_s=timeout_s) if hits else {}
    return prune_links(trim_pack(pack, max_tokens))


__all__ = ["GRAPH_ERROR_MESSAGE", "GraphError", "GraphUnavailable", "Hit", "KIND_LIMITS", "PACK_KEYS", "acontext_pack", "aexpand", "asearch_context",
           "context_pack", "empty_pack", "expand", "fetch_size", "gate", "gate_params", "lucene_query",
           "pack_tokens", "search_context", "trim_pack"]
