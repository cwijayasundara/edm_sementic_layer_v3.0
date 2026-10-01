"""Role-filtered hybrid retrieval (vector + BM25 fulltext, RRF fused in Cypher) and context-pack expansion.

Role pruning is evaluated INSIDE every Cypher statement: a node is visible iff
  n.ns = $ns AND any(s IN n.allowed_scopes WHERE s IN $scopes)      ($scopes = caller scopes + ['*'])
  AND (metrics-only callers never see Table/Column/Field/Endpoint)
  AND (a Question is visible only if every object its Execution USED is visible).
"""
from __future__ import annotations

import json
import re

from loader import fake_embed

RRF_C = 60
PATH_RELS = "COMPUTED_FROM|BACKED_BY|HAS_COLUMN|REFERENCES|SAME_KEY_AS"


def gate(x: str) -> str:
    return (f"({x}.ns = $ns AND any(s IN {x}.allowed_scopes WHERE s IN $scopes)"
            f" AND (NOT $metrics_only OR NOT ({x}:Column OR {x}:Table OR {x}:Field OR {x}:Endpoint))"
            f" AND (NOT {x}:Question OR all(u IN COLLECT {{ MATCH ({x})-[:ANSWERED_BY]->(:Execution)-[:USED]->(u) RETURN u }}"
            f" WHERE any(s IN u.allowed_scopes WHERE s IN $scopes))))")


SEARCH_CYPHER = f"""
CALL () {{
  CALL db.index.vector.queryNodes('ctx_vec', $fetch, $qv) YIELD node, score
  WHERE {gate('node')}
  WITH node, score ORDER BY score DESC LIMIT $k
  RETURN collect(node) AS vec
}}
CALL () {{
  WITH 1 AS one WHERE $ft <> ''
  CALL db.index.fulltext.queryNodes('ctx_text', $ft, {{limit: $fetch}}) YIELD node, score
  WHERE {gate('node')}
  WITH node, score ORDER BY score DESC LIMIT $k
  RETURN collect(node) AS ft
}}
UNWIND vec + ft AS n
WITH DISTINCT n, vec, ft
WITH n, [i IN range(0, size(vec) - 1) WHERE vec[i] = n][0] AS vr, [i IN range(0, size(ft) - 1) WHERE ft[i] = n][0] AS fr
WITH n, vr, fr,
     coalesce(1.0 / ($c + vr + 1), 0.0) + coalesce(1.0 / ($c + fr + 1), 0.0) AS rrf
RETURN n.uid AS uid, [l IN labels(n) WHERE NOT l IN ['Ctx', 'Searchable', 'SpikeRun']][0] AS kind,
       n.name AS name, n.source AS source, rrf, vr AS vector_rank, fr AS text_rank
ORDER BY rrf DESC, uid LIMIT $k
"""

EXPAND_CYPHER = f"""
MATCH (h:Ctx) WHERE h.uid IN $hits AND {gate('h')}
WITH collect(h) AS hs
// candidate metrics: direct hits, defined by hit terms, used by hit questions
CALL (hs) {{
  UNWIND hs AS h
  OPTIONAL MATCH (h)-[:DEFINES|ANSWERED_BY|USED*1..2]->(m2:Metric)
  WITH h, m2 UNWIND [x IN [h, m2] WHERE x:Metric] AS m
  WITH DISTINCT m WHERE {gate('m')}
  WITH m LIMIT $max_metrics
  OPTIONAL MATCH (m)-[:HAS_DIMENSION]->(d:Dimension) WHERE NOT ($metrics_only AND d.sensitive)
  WITH m, collect(d.name) AS dims
  RETURN collect({{id: m.id, source: m.source, server: m.mcp_server, tool: m.mcp_tool, endpoint: m.endpoint_id,
                  unit: m.unit, definition: m.definition, dims: dims, required: m.required_dimensions,
                  filters: m.filters, time: m.time_column, tables: m.tables}}) AS metrics,
         collect(m.uid) AS metric_uids
}}
CALL (hs) {{
  UNWIND hs AS t WITH t WHERE t:BusinessTerm
  OPTIONAL MATCH (t)-[:BROADER]->(b:BusinessTerm)
  RETURN collect({{term: t.name, definition: t.definition, synonyms: t.synonyms, rule: t.rule, broader: b.name}}) AS terms
}}
CALL (hs) {{
  UNWIND hs AS h
  OPTIONAL MATCH (h)-[:DEFINES]->(c2:Concept)
  WITH h, c2 UNWIND [x IN [h, c2] WHERE x:Concept] AS c
  WITH DISTINCT c WHERE {gate('c')}
  OPTIONAL MATCH (c)-[:IMPLEMENTED_BY]->(i) WHERE {gate('i')}
  WITH c, collect(DISTINCT i.uid) AS impl
  OPTIONAL MATCH (c)-[:IDENTIFIED_BY]->(k) WHERE {gate('k')}
  WITH c, impl, collect(DISTINCT k.uid) AS keys
  RETURN collect({{concept: c.name, implemented_by: impl, keys: keys}}) AS concepts
}}
CALL (hs) {{
  UNWIND hs AS h
  OPTIONAL MATCH (h:BusinessTerm)<-[:TAGGED_WITH]-(tc:Column)
  WITH h, tc UNWIND [x IN [h, tc] WHERE x:Column] AS col
  WITH DISTINCT col WHERE {gate('col')}
  MATCH (tb:Table)-[:HAS_COLUMN]->(col)
  OPTIONAL MATCH (ep:Endpoint)-[:BACKED_BY]->(tb) WHERE {gate('ep')}
  WITH tb, collect(DISTINCT col.name + ':' + col.type) AS cols, collect(DISTINCT ep.endpoint_id) AS eps, collect(DISTINCT col.uid) AS cuids
  RETURN collect({{table: tb.source + '.' + tb.name, columns: cols, endpoints: eps}}) AS columns,
         reduce(a = [], x IN collect(cuids) | a + x) AS column_uids
}}
CALL (hs) {{
  UNWIND hs AS q WITH q WHERE q:Question
  MATCH (q)-[:ANSWERED_BY]->(e:Execution)
  WITH q, e LIMIT 3
  RETURN collect({{question: q.text, kind: e.kind, plan: e.text, status: e.status}}) AS examples
}}
RETURN metrics, metric_uids, terms, concepts, columns, column_uids, examples
"""

PATH_CYPHER = f"""
UNWIND $pairs AS pr
MATCH (a:Ctx {{uid: pr[0]}}), (b:Ctx {{uid: pr[1]}})
WHERE {gate('a')} AND {gate('b')}
OPTIONAL CALL (a, b) {{
  MATCH p = SHORTEST 1 (a)(()-[:{PATH_RELS}]-(y) WHERE {gate('y')}){{1,12}}(b)
  RETURN p
}}
WITH pr, p WHERE p IS NOT NULL
RETURN pr[0] AS start, pr[1] AS end, length(p) AS hops, [n IN nodes(p) | n.uid] AS nodes,
       [r IN relationships(p) WHERE type(r) IN ['REFERENCES', 'SAME_KEY_AS'] |
          startNode(r).source + '.' + startNode(r).table + '.' + startNode(r).name + ' = ' +
          endNode(r).source + '.' + endNode(r).table + '.' + endNode(r).name] AS joins
"""


def lucene_query(text: str) -> str:
    """Safe Lucene query from free text: keep \\w+ tokens (len>1), lower-cased (so AND/OR/NOT are not operators),
    OR-joined. No user character reaches the Lucene parser un-tokenised -> no syntax errors / injection."""
    toks = [t for t in re.findall(r"\w+", text.lower()) if len(t) > 1]
    return " OR ".join(dict.fromkeys(toks))


def params(scopes, metrics_only: bool, ns: str) -> dict:
    return {"scopes": list(scopes) + ["*"], "metrics_only": metrics_only, "ns": ns}


def search_context(driver, question_vector, question_text, roles_scopes, k: int = 8, *, metrics_only: bool = False,
                   ns: str = "prism", fetch: int | None = None) -> list[dict]:
    fetch = fetch or min(max(k * 25, 100), 1000)   # over-fetch: vector top-k is global, pruning happens after
    recs = driver.execute_query(SEARCH_CYPHER, {
        "qv": [float(x) for x in question_vector], "ft": lucene_query(question_text), "k": k, "fetch": fetch,
        "c": RRF_C, **params(roles_scopes, metrics_only, ns)}, routing_="r").records
    return [dict(r) for r in recs]


def join_paths(driver, pairs, roles_scopes, *, metrics_only=False, ns="prism") -> list[dict]:
    if not pairs:
        return []
    recs = driver.execute_query(PATH_CYPHER, {"pairs": [list(p) for p in pairs],
                                              **params(roles_scopes, metrics_only, ns)}, routing_="r").records
    return [dict(r) for r in recs]


def _src(uid: str) -> str:
    return uid.split(":", 1)[1].split(".")[0] if uid.split(":", 1)[0] in ("table", "column", "endpoint") else ""


def expand(driver, hits: list[dict], roles_scopes, *, metrics_only=False, ns="prism", max_metrics=4,
           max_paths=3) -> dict:
    p = params(roles_scopes, metrics_only, ns)
    r = driver.execute_query(EXPAND_CYPHER, {"hits": [h["uid"] for h in hits], "max_metrics": max_metrics, **p},
                             routing_="r").records[0]
    pack = {k: r[k] for k in ("metrics", "terms", "concepts", "columns", "examples")}
    joins = []
    if not metrics_only:
        targets = list(r["column_uids"]) + [u for c in r["concepts"] for u in c["implemented_by"] if u.startswith("table:")]
        pairs = [(m, t) for m in r["metric_uids"] for t in targets
                 if _src(t) and _src(t) != next(x["source"] for x in r["metrics"] if "metric:" + x["id"] == m)]
        seen, uniq = set(), []
        for m, t in pairs:
            if (m, _src(t)) not in seen:     # one path per (metric, other source)
                seen.add((m, _src(t))); uniq.append((m, t))
        joins = [{"from": j["start"], "to": j["end"], "hops": j["hops"], "on": j["joins"]}
                 for j in join_paths(driver, uniq[:max_paths], roles_scopes, metrics_only=metrics_only, ns=ns)]
    pack["join_paths"] = joins
    return _compact(pack)


def _compact(x):
    if isinstance(x, dict):
        return {k: _compact(v) for k, v in x.items() if v not in (None, [], {}, "")}
    if isinstance(x, list):
        return [_compact(v) for v in x if v not in (None, [], {}, "")]
    return x


def context_pack(driver, question: str, roles_scopes, *, k=8, metrics_only=False, ns="prism", qvec=None) -> dict:
    hits = search_context(driver, qvec or fake_embed(question), question, roles_scopes, k, metrics_only=metrics_only, ns=ns)
    pack = expand(driver, hits, roles_scopes, metrics_only=metrics_only, ns=ns)
    return {"question": question, "hits": [h["uid"] for h in hits], **pack}


def pack_size(pack: dict) -> dict:
    s = json.dumps(pack, separators=(",", ":"))
    return {"chars": len(s), "est_tokens": round(len(s) / 4)}


async def asearch_context(adriver, question_vector, question_text, roles_scopes, k=8, *, metrics_only=False, ns="prism"):
    """Async twin (neo4j.AsyncDriver) - same Cypher."""
    res = await adriver.execute_query(SEARCH_CYPHER, {
        "qv": [float(x) for x in question_vector], "ft": lucene_query(question_text), "k": k,
        "fetch": min(max(k * 25, 100), 1000), "c": RRF_C, **params(roles_scopes, metrics_only, ns)}, routing_="r")
    return [dict(r) for r in res.records]


if __name__ == "__main__":
    import sys

    from neo4j import GraphDatabase

    from prism.security.personas import PERSONAS
    from schema import AUTH, URI
    q = sys.argv[1] if len(sys.argv) > 1 else "security price conflicts by issuer country"
    with GraphDatabase.driver(URI, auth=AUTH, notifications_min_severity="OFF") as d:
        for pid in sys.argv[2:] or ["steward", "cash_ops_emea"]:
            per = PERSONAS[pid]
            pack = context_pack(d, q, per.scopes, metrics_only=per.metrics_only)
            print(pid, pack_size(pack))
            print(json.dumps(pack, indent=1))
