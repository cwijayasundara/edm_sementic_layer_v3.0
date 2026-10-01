"""Idempotent context-graph loader: real metrics YAML + REST registries + DDL + hand-written model.yaml.

Design:
- every node: (:Ctx:<Type> {uid, ns, loaded_version, allowed_scopes, ...}); uid is stable (derived from ids).
- MERGE on uid + UNWIND batches; `SET n = props` replaces properties (removed YAML keys disappear).
- the whole load (upserts + stale delete) runs in ONE write transaction.
- stale delete: nodes AND relationships of this namespace with loaded_version < current are deleted.
- ns != 'prism' (tests): uids are prefixed '<ns>:' and nodes get the extra label :SpikeRun.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

REPO = Path("/Users/chamindawijayasundara/Documents/learning_101/edm_sementic_layer_v2.0/backend")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics  # noqa: E402
from prism.security.personas import PERSONAS  # noqa: E402

HERE = Path(__file__).parent
DDL_DIR = REPO / "prism/db/ddl"
REST_DIR = REPO / "prism/mcp/rest"
DIM = 384
N_ANCHORS = 10
SOURCES = ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
_TYPE_RE = re.compile(r"^[A-Z][A-Z_]*$")
_LABEL_RE = re.compile(r"^[A-Z][A-Za-z]*$")


# ------------------------------------------------------------------------------------------ embeddings
def fake_embed(text: str) -> list[float]:
    """Deterministic 384-d 'embedding': signed feature hashing of word tokens + char trigrams into dims 10..383
    (dims 0..9 are reserved for hand-crafted anchor vectors). Lexically similar texts -> high cosine."""
    v = [0.0] * DIM
    toks = re.findall(r"[a-z0-9]+", text.lower())
    feats = toks + [t[i:i + 3] for t in toks if len(t) > 3 for i in range(len(t) - 2)]
    for f in feats or ["<empty>"]:
        h = hashlib.blake2b(f.encode(), digest_size=8).digest()
        idx = N_ANCHORS + int.from_bytes(h[:4], "big") % (DIM - N_ANCHORS)
        v[idx] += (1.0 if h[4] & 1 else -1.0) * (2.0 if f in toks else 0.5)
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


ANCHOR_TEXT = {0: "nostro cash account", 1: "price conflict vendor deviation"}


def anchor_vec(i: int) -> list[float]:
    """Hand-crafted vector: one-hot anchor dim + a dense text part. A PURE one-hot vector is orthogonal to every
    other node, so HNSW greedy search has no gradient towards it (measured: k=5 misses it) - keep a dense part."""
    base = fake_embed(ANCHOR_TEXT[i])
    v = [0.5 * x for x in base]
    v[i] = 1.0
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


# ------------------------------------------------------------------------------------------ DDL parsing
_CREATE = re.compile(r"CREATE TABLE\s+(?:\w+\.)?(\w+)\s*\(", re.IGNORECASE)
_COL = re.compile(r"^\s*([a-z_][a-z0-9_]*)\s+([a-z]+(?:\s*\([\d,\s]+\))?)", re.IGNORECASE)
_REF = re.compile(r"REFERENCES\s+(?:\w+\.)?(\w+)(?:\s*\(\s*(\w+)\s*\))?", re.IGNORECASE)
_CONSTRAINT = re.compile(r"^\s*(PRIMARY|UNIQUE|CHECK|FOREIGN|CONSTRAINT|EXCLUDE)\b", re.IGNORECASE)


def _split_top(body: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def parse_ddl(path: Path) -> dict[str, dict]:
    text = re.sub(r"--[^\n]*", "", path.read_text())
    tables: dict[str, dict] = {}
    for m in _CREATE.finditer(text):
        i, depth = m.end(), 1
        while depth:
            depth += {"(": 1, ")": -1}.get(text[i], 0); i += 1
        body = text[m.end():i - 1]
        cols, pk, refs = [], [], []
        for part in _split_top(body):
            if (pm := re.match(r"^\s*PRIMARY KEY\s*\(([^)]*)\)", part, re.IGNORECASE)):
                pk = [c.strip() for c in pm.group(1).split(",")]
                continue
            if _CONSTRAINT.match(part) or not (cm := _COL.match(part)):
                continue
            name, typ = cm.group(1), re.sub(r"\s+", "", cm.group(2)).lower()
            cols.append((name, typ))
            if re.search(r"PRIMARY KEY", part, re.IGNORECASE):
                pk = [name]
            if (rm := _REF.search(part)):
                refs.append((name, rm.group(1), rm.group(2)))
        tables[m.group(1)] = {"cols": cols, "pk": pk, "refs": refs}
    return tables


# ------------------------------------------------------------------------------------------ graph build
class G:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: list[tuple[str, str, str, dict]] = []

    def node(self, uid: str, labels: list[str], **props):
        assert uid not in self.nodes, uid
        self.nodes[uid] = {"labels": labels, "props": {"uid": uid, **props}}

    def edge(self, a: str, typ: str, b: str, **props):
        self.edges.append((a, typ, b, props))


def _search_text(*parts) -> str:
    return " ".join(str(p).replace("_", " ") for p in parts if p)


def build_graph(exclude: frozenset[str] = frozenset()) -> G:
    """Pure function: inputs -> nodes/edges with LOCAL uids. `exclude` drops objects by uid (tests stale delete)."""
    model = yaml.safe_load((HERE / "model.yaml").read_text())
    g = G()
    scopes: dict[str, list[str]] = {}

    # Sources, tables, columns (DDL)
    ddl = {s: parse_ddl(DDL_DIR / f"{s}.sql") for s in SOURCES}
    for s in SOURCES:
        g.node(f"source:{s}", ["Source"], name=s, kind=model["sources"][s]["kind"],
               mcp_server=model["sources"][s]["mcp_server"], allowed_scopes=[s])
        for t, spec in ddl[s].items():
            tu, sc = f"table:{s}.{t}", [s, f"{s}.{t}"]
            scopes[tu] = sc
            g.node(tu, ["Table"], name=t, source=s, primary_key=spec["pk"], allowed_scopes=sc)
            g.edge(f"source:{s}", "HAS_TABLE", tu)
            for c, typ in spec["cols"]:
                cu = f"column:{s}.{t}.{c}"
                scopes[cu] = sc
                g.node(cu, ["Column", "Searchable"], name=c, table=t, source=s, type=typ,
                       description=_search_text(t, c, typ), allowed_scopes=sc)
                g.edge(tu, "HAS_COLUMN", cu)
        for t, spec in ddl[s].items():  # FK edges within a source
            for c, rt, rc in spec["refs"]:
                target_pk = ddl[s].get(rt, {}).get("pk", [])
                rc = rc or (target_pk[0] if len(target_pk) == 1 else None)
                if rc:
                    g.edge(f"column:{s}.{t}.{c}", "REFERENCES", f"column:{s}.{rt}.{rc}")

    # REST endpoints, params, fields; endpoint-backed metrics
    rest_metrics = []
    for s in ("refmaster", "marketmaster"):
        raw = yaml.safe_load((REST_DIR / f"{s}.yaml").read_text())
        for e in raw["endpoints"]:
            eu, sc = f"endpoint:{s}.{e['id']}", [s, f"{s}.{e['table']}"]
            scopes[eu] = sc
            g.node(eu, ["Endpoint", "Searchable"], name=e["id"], endpoint_id=e["id"], method="GET", path=e["path"],
                   source=s, table=e["table"], result_key=e["result_key"], mcp_tool="query",
                   description=_search_text(e["id"], e["description"]), allowed_scopes=sc)
            g.edge(f"source:{s}", "HAS_ENDPOINT", eu)
            g.edge(eu, "BACKED_BY", f"table:{s}.{e['table']}")
            for p, ps in (e.get("params") or {}).items():
                g.node(f"param:{s}.{e['id']}.{p}", ["Parameter"], name=p, type=ps.get("type", "str"),
                       location=ps.get("location", "query"), required=bool(ps.get("required", False)),
                       enum=ps.get("enum") or [], allowed_scopes=sc)
                g.edge(eu, "HAS_PARAM", f"param:{s}.{e['id']}.{p}")
            for c, typ in ddl[s][e["table"]]["cols"]:
                fu = f"field:{s}.{e['id']}.{c}"
                g.node(fu, ["Field"], name=c, type=typ, allowed_scopes=sc,
                       json_path=f"$.{e['result_key']}[*].{c}" if e["result_key"] else f"$.{c}")
                g.edge(eu, "RETURNS", fu)
                g.edge(fu, "MAPS_TO", f"column:{s}.{e['table']}.{c}")
        for m in raw["metrics"]:
            ep = next(e for e in raw["endpoints"] if e["id"] == m["endpoint"])
            rest_metrics.append((s, m, ep))

    # Metrics (SQL via the real prism loader; REST from registries)
    sql_metrics = load_metrics(DEFAULT_METRICS_DIR)
    metric_rows = []
    for m in sql_metrics.values():
        tables = sorted(m.tables)
        sc = [m.source] + ([f"{m.source}.{tables[0]}"] if len(tables) == 1 else [])
        metric_rows.append(dict(id=m.id, source=m.source, kind="sql", type=m.type, unit=m.unit, description=m.description,
                                time_column=m.time_column, dims=m.dimensions, filters={k: f.type for k, f in m.filters.items()},
                                sensitive=m.sensitive_dimensions, required=m.required_dimensions, tables=tables,
                                endpoint=None, scopes=sc))
    for s, m, ep in rest_metrics:
        metric_rows.append(dict(id=m["id"], source=s, kind="rest", type="endpoint", unit=m["unit"], description=m["description"],
                                time_column=None, dims=m["dimensions"], filters={k: f.get("type", "str") for k, f in m["filters"].items()},
                                sensitive=m.get("sensitive_dimensions", []), required=[], tables=[ep["table"]],
                                endpoint=ep["id"], scopes=[s, f"{s}.{ep['table']}"]))
    for r in metric_rows:
        mu = f"metric:{r['id']}"
        if mu in exclude:
            continue
        scopes[mu] = r["scopes"]
        g.node(mu, ["Metric", "Searchable"], id=r["id"], name=r["id"], source=r["source"], kind=r["kind"], type=r["type"],
               unit=r["unit"], description=_search_text(r["id"], r["description"]), definition=r["description"],
               time_column=r["time_column"], dimensions=sorted(r["dims"]), filters=[f"{k}:{v}" for k, v in r["filters"].items()],
               sensitive_dimensions=r["sensitive"], required_dimensions=r["required"], tables=r["tables"],
               mcp_server=model["sources"][r["source"]]["mcp_server"], mcp_tool="run_metric", endpoint_id=r["endpoint"],
               allowed_scopes=r["scopes"])
        if r["endpoint"]:
            g.edge(mu, "COMPUTED_FROM", f"endpoint:{r['source']}.{r['endpoint']}")
        for t in r["tables"]:
            g.edge(mu, "COMPUTED_FROM", f"table:{r['source']}.{t}")
        table_cols = {c for t in r["tables"] for c, _ in ddl[r["source"]][t]["cols"]}
        for d, expr in r["dims"].items():
            du = f"dim:{r['id']}.{d}"
            g.node(du, ["Dimension"], name=d, expr=expr, sensitive=d in r["sensitive"], required=d in r["required"],
                   allowed_scopes=r["scopes"])
            g.edge(mu, "HAS_DIMENSION", du)
            if expr in table_cols:
                g.edge(du, "ON_COLUMN", f"column:{r['source']}.{r['tables'][0]}.{expr}")

    # Ontology
    for c in model["concepts"]:
        cu = f"concept:{c['name']}"
        sc = sorted({s for ref in c["implemented_by"] for s in scopes[ref]})
        scopes[cu] = sc
        g.node(cu, ["Concept", "Searchable"], name=c["name"], description=c["description"], allowed_scopes=sc)
        for ref in c["implemented_by"]:
            g.edge(cu, "IMPLEMENTED_BY", ref)
        for ref in c["identified_by"]:
            g.edge(cu, "IDENTIFIED_BY", ref)
    for a, typ, b in model["concept_relations"]:
        g.edge(f"concept:{a}", typ, f"concept:{b}")

    # Glossary
    for gl in model["glossaries"]:
        g.node(f"glossary:{gl}", ["Glossary"], name=gl, domain=gl, allowed_scopes=["*"])
    for t in model["terms"]:
        tu = "term:" + re.sub(r"[^a-z0-9]+", "_", t["name"].lower()).strip("_")
        links = [x for x in t.get("defines", []) + t.get("tags", []) if x not in exclude]
        sc = sorted({s for ref in links for s in scopes[ref]}) or ["*"]
        g.node(tu, ["BusinessTerm", "Searchable"], name=t["name"], definition=t["definition"], synonyms=t["synonyms"],
               description=t["definition"], status="approved", owner=f"{t['glossary']}-data-owner", rule=t.get("rule"),
               allowed_scopes=sc, _anchor=t.get("anchor"))
        g.edge(tu, "IN_GLOSSARY", f"glossary:{t['glossary']}")
        for ref in t.get("defines", []):
            if ref not in exclude:
                g.edge(tu, "DEFINES", ref)
        for ref in t.get("tags", []):
            g.edge(ref, "TAGGED_WITH", tu)
    by_name = {n["props"]["name"]: u for u, n in g.nodes.items() if "BusinessTerm" in n["labels"]}
    for t in model["terms"]:
        if t.get("broader"):
            g.edge(by_name[t["name"]], "BROADER", by_name[t["broader"]])

    for a, b in model["same_key_as"]:
        g.edge(a, "SAME_KEY_AS", b)

    # Query history
    for i, h in enumerate(model["history"], 1):
        used = [u for u in h["used"] if u not in exclude]
        g.node(f"question:{i}", ["Question", "GoldenQuestion", "Searchable"], name=h["q"], text=h["q"],
               persona_role=h["persona"], asked_at="2026-09-01T00:00:00Z", allowed_scopes=["*"])
        g.node(f"exec:{i}", ["Execution"], kind=h["kind"], text=h["text"], source=h["source"], status="validated",
               latency_ms=120, rows=10, feedback="up", allowed_scopes=["*"])
        g.edge(f"question:{i}", "ANSWERED_BY", f"exec:{i}")
        for u in used:
            g.edge(f"exec:{i}", "USED", u)

    # Roles + grants
    for p in PERSONAS.values():
        ru = f"role:{p.persona_id}"
        g.node(ru, ["Role"], persona_id=p.persona_id, name=p.display_name, metrics_only=p.metrics_only,
               scopes=list(p.scopes), allowed_scopes=["*"])
        row_scope = json.dumps({k: list(v) for k, v in p.rows.items()}, sort_keys=True)
        for s in p.scopes:
            if s in SOURCES:
                g.edge(ru, "CAN_READ", f"source:{s}", row_scope=row_scope, scope=s)
            elif "." in s:
                g.edge(ru, "CAN_READ", f"table:{s}", row_scope=row_scope, scope=s)

    # Embeddings for :Searchable
    anchors = model["anchors"]
    for u, n in g.nodes.items():
        a = n["props"].pop("_anchor", None)
        if "Searchable" not in n["labels"]:
            continue
        p = n["props"]
        if a is None:
            a = anchors.get(u)
        n["props"]["embedding"] = anchor_vec(a) if a is not None else fake_embed(
            _search_text(p.get("name"), p.get("description"), " ".join(p.get("synonyms", []))))
    for u in exclude:
        g.nodes.pop(u, None)
    g.edges = [e for e in g.edges if e[0] not in exclude and e[2] not in exclude]
    return g


# ------------------------------------------------------------------------------------------ writing
def _uid(ns: str, local: str) -> str:
    return local if ns == "prism" else f"{ns}:{local}"


def _write(tx, g: G, ns: str, version: int, batch: int = 500) -> dict:
    by_labels: dict[tuple, list] = defaultdict(list)
    for local, n in g.nodes.items():
        labels = tuple(n["labels"]) + (("SpikeRun",) if ns != "prism" else ())
        props = {**n["props"], "uid": _uid(ns, local), "local_uid": local, "ns": ns, "loaded_version": version}
        by_labels[labels].append(props)
    for labels, rows in by_labels.items():
        assert all(_LABEL_RE.match(lbl) for lbl in labels), labels
        for i in range(0, len(rows), batch):
            tx.run(f"UNWIND $rows AS r MERGE (n:Ctx {{uid: r.uid}}) SET n = r, n:{':'.join(labels)}",
                   rows=rows[i:i + batch]).consume()
    by_type: dict[str, list] = defaultdict(list)
    for a, typ, b, props in g.edges:
        by_type[typ].append({"a": _uid(ns, a), "b": _uid(ns, b),
                             "p": {**props, "ns": ns, "loaded_version": version}})
    missing = []
    for typ, rows in by_type.items():
        assert _TYPE_RE.match(typ), typ
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            n = tx.run(f"UNWIND $rows AS r MATCH (a:Ctx {{uid: r.a}}) MATCH (b:Ctx {{uid: r.b}}) "
                       f"MERGE (a)-[e:{typ}]->(b) SET e = r.p RETURN count(e) AS n", rows=chunk).single()["n"]
            if n != len(chunk):
                have = {r["u"] for r in tx.run("UNWIND $u AS u MATCH (n:Ctx {uid:u}) RETURN u AS u",
                                               u=[x for r in chunk for x in (r["a"], r["b"])])}
                missing += [(typ, r["a"], r["b"]) for r in chunk if r["a"] not in have or r["b"] not in have]
    if missing:
        raise ValueError(f"dangling edge endpoints: {missing[:10]}")
    # Stale delete (namespace-scoped): relationships first, then nodes.
    rels = tx.run("MATCH (n:Ctx {ns: $ns})-[r]-() WHERE r.loaded_version < $v DELETE r RETURN count(r) AS c",
                  ns=ns, v=version).single()["c"]
    nodes = tx.run("MATCH (n:Ctx {ns: $ns}) WHERE n.loaded_version < $v DETACH DELETE n RETURN count(n) AS c",
                   ns=ns, v=version).single()["c"]
    return {"stale_rels_deleted": rels, "stale_nodes_deleted": nodes}


def load(driver, ns: str = "prism", exclude: frozenset[str] = frozenset(), await_indexes: bool = True) -> dict:
    t0 = time.perf_counter()
    g = build_graph(exclude)
    t_build = time.perf_counter() - t0
    with driver.session(database="neo4j") as s:
        def work(tx):
            v = tx.run("MATCH (n:Ctx {ns: $ns}) RETURN coalesce(max(n.loaded_version), 0) + 1 AS v", ns=ns).single()["v"]
            return {"version": v, **_write(tx, g, ns, v)}
        out = s.execute_write(work)
    t_write = time.perf_counter() - t0 - t_build
    if await_indexes:
        driver.execute_query("CALL db.awaitIndexes(120)")
    out.update(nodes=len(g.nodes), edges=len(g.edges), build_s=round(t_build, 3), write_s=round(t_write, 3),
               total_s=round(time.perf_counter() - t0, 3))
    return out


def counts(driver, ns: str = "prism") -> dict:
    r = driver.execute_query(
        "MATCH (n:Ctx {ns:$ns}) WITH count(n) AS nodes "
        "OPTIONAL MATCH (a:Ctx {ns:$ns})-[r]->() WITH nodes, count(r) AS rels "
        "RETURN nodes, rels", ns=ns).records[0]
    by = driver.execute_query(
        "MATCH (n:Ctx {ns:$ns}) UNWIND labels(n) AS l WITH l WHERE NOT l IN ['Ctx','Searchable','SpikeRun'] "
        "RETURN l, count(*) AS c ORDER BY l", ns=ns).records
    return {"nodes": r["nodes"], "rels": r["rels"], "by_label": {x["l"]: x["c"] for x in by}}


def delete_ns(driver, ns: str) -> None:
    # CALL {...} IN TRANSACTIONS needs an implicit (auto-commit) transaction: session.run, not execute_query/tx.
    with driver.session(database="neo4j") as s:
        s.run("MATCH (n:Ctx {ns:$ns}) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 1000 ROWS", ns=ns).consume()


if __name__ == "__main__":
    from neo4j import GraphDatabase

    from schema import AUTH, URI, create_schema
    with GraphDatabase.driver(URI, auth=AUTH) as d:
        create_schema(d)
        for i in range(2):
            st = load(d)
            print(f"load #{i + 1}:", st)
            print("counts:", counts(d))
