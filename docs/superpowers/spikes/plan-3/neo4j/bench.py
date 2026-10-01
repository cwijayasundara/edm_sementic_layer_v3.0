"""Latency + pack-size benchmark at real size and at 10x (synthetic nodes in ns='prism', label :Synthetic)."""
from __future__ import annotations

import json
import random
import statistics
import time

from neo4j import GraphDatabase

from catalog import load_catalog
from loader import SOURCES, counts, fake_embed, load
from prism.security.personas import PERSONAS
from retrieval import expand, pack_size, search_context
from schema import AUTH, URI, create_schema

QUESTIONS = [
    "security price conflicts by issuer country",
    "how many open breaks on nostro accounts in EMEA by currency",
    "which custodian feeds were late yesterday",
    "open position exceptions market value for Growth funds",
    "data quality exceptions on bonds by rule",
]
VOCAB = ("break price security feed late position exception nostro account vendor golden copy nav custodian "
         "portfolio issuer country entity rule quality match manual delivery ticket latency currency region "
         "amount fund growth bond equity isin ledger statement run recon value").split()


def pct(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))], 2)


def timeit(fn, n=100, warm=20):
    for i in range(warm):
        fn(i)
    out = []
    for i in range(n):
        t = time.perf_counter(); fn(i); out.append(1000 * (time.perf_counter() - t))
    return {"p50_ms": pct(out, 50), "p95_ms": pct(out, 95), "mean_ms": round(statistics.mean(out), 2),
            "max_ms": round(max(out), 2)}


def bench(d, label):
    cases = [(q, pid) for q in QUESTIONS for pid in PERSONAS]
    pre = {}
    for q, pid in cases:
        p = PERSONAS[pid]
        pre[(q, pid)] = search_context(d, fake_embed(q), q, p.scopes, 8, metrics_only=p.metrics_only)

    def s(i):
        q, pid = cases[i % len(cases)]; p = PERSONAS[pid]
        search_context(d, fake_embed(q), q, p.scopes, 8, metrics_only=p.metrics_only)

    def e(i):
        q, pid = cases[i % len(cases)]; p = PERSONAS[pid]
        expand(d, pre[(q, pid)], p.scopes, metrics_only=p.metrics_only)

    def full(i):
        s(i); e(i)

    res = {"graph": counts(d), "search": timeit(s), "expand+paths": timeit(e), "full_pack": timeit(full),
           "catalog_ms": load_catalog(d)["elapsed_ms"],
           "cash_ops_hits": {q: len(pre[(q, "cash_ops_emea")]) for q in QUESTIONS}}
    print(f"== {label}: nodes={res['graph']['nodes']} rels={res['graph']['rels']}")
    for k in ("search", "expand+paths", "full_pack"):
        print(f"  {k:13s} {res[k]}")
    print("  catalog_ms", res["catalog_ms"], " cash_ops hit counts", list(res["cash_ops_hits"].values()))
    return res


def add_synthetic(d, factor=10, seed=42):
    """Adds ~ (factor-1) x real node count of synthetic nodes, with embeddings, fulltext text, allowed_scopes and
    edges, so they really compete in the vector/fulltext indexes and in the path search."""
    rnd = random.Random(seed)
    real = counts(d)["nodes"]
    target = real * (factor - 1)
    v = d.execute_query("MATCH (n:Ctx {ns:'prism'}) RETURN max(n.loaded_version) AS v").records[0]["v"]
    nodes, edges = [], []

    def text(n):
        return " ".join(rnd.choice(VOCAB) for _ in range(n))

    def add(uid, labels, **p):
        nodes.append((labels, {"uid": uid, "ns": "prism", "loaded_version": v, **p}))

    i = 0
    col_uids = []
    while len(nodes) < target:
        src = rnd.choice(SOURCES); t = f"syn_t{i}"; sc = [src, f"{src}.{t}"]
        add(f"table:{src}.{t}", "Table:Synthetic", name=t, source=src, allowed_scopes=sc)
        for c in range(7):
            cn = f"{rnd.choice(VOCAB)}_{c}"; cu = f"column:{src}.{t}.{cn}"
            add(cu, "Column:Searchable:Synthetic", name=cn, table=t, source=src, type="text",
                description=text(6), allowed_scopes=sc, embedding=fake_embed(text(6)))
            edges.append(("HAS_COLUMN", f"table:{src}.{t}", cu)); col_uids.append(cu)
        if i % 2 == 0:
            mu = f"metric:syn_m{i}"
            add(mu, "Metric:Searchable:Synthetic", id=f"syn_m{i}", name=f"syn_m{i}", source=src, kind="sql",
                description=text(10), definition=text(10), dimensions=["region"], filters=[], tables=[t],
                required_dimensions=[], sensitive_dimensions=[], mcp_server=f"{src}-mcp", mcp_tool="run_metric",
                allowed_scopes=sc, embedding=fake_embed(text(10)))
            edges.append(("COMPUTED_FROM", mu, f"table:{src}.{t}"))
            for dn in ("region", "ccy", "status"):
                add(f"dim:syn_m{i}.{dn}", "Dimension:Synthetic", name=dn, expr=dn, sensitive=False, required=False,
                    allowed_scopes=sc)
                edges.append(("HAS_DIMENSION", mu, f"dim:syn_m{i}.{dn}"))
            tu = f"term:syn_{i}"
            add(tu, "BusinessTerm:Searchable:Synthetic", name=text(2), definition=text(12), description=text(12),
                synonyms=[text(2), text(2)], allowed_scopes=sc, embedding=fake_embed(text(12)))
            edges.append(("DEFINES", tu, mu))
        if i % 5 == 0:
            qu = f"question:syn_{i}"
            add(qu, "Question:Searchable:Synthetic", name=text(8), text=text(8), allowed_scopes=["*"],
                embedding=fake_embed(text(8)))
            add(f"exec:syn_{i}", "Execution:Synthetic", kind="metric", text=text(5), status="validated",
                allowed_scopes=["*"])
            edges.append(("ANSWERED_BY", qu, f"exec:syn_{i}"))
            edges.append(("USED", f"exec:syn_{i}", f"table:{src}.{t}"))
        i += 1
    for _ in range(len(col_uids) // 8):                         # random FK / cross-source keys
        a, b = rnd.sample(col_uids, 2)
        edges.append((rnd.choice(["REFERENCES", "SAME_KEY_AS"]), a, b))
    t0 = time.perf_counter()
    by = {}
    for labels, p in nodes:
        by.setdefault(labels, []).append(p)
    with d.session(database="neo4j") as s:
        for labels, rows in by.items():
            for k in range(0, len(rows), 1000):
                s.run(f"UNWIND $rows AS r CREATE (n:Ctx:{labels}) SET n = r", rows=rows[k:k + 1000]).consume()
        et = {}
        for typ, a, b in edges:
            et.setdefault(typ, []).append({"a": a, "b": b})
        for typ, rows in et.items():
            for k in range(0, len(rows), 2000):
                s.run(f"UNWIND $rows AS r MATCH (a:Ctx {{uid:r.a}}) MATCH (b:Ctx {{uid:r.b}}) "
                      f"CREATE (a)-[:{typ} {{ns:'prism', loaded_version:{v}}}]->(b)", rows=rows[k:k + 2000]).consume()
    t1 = time.perf_counter()
    d.execute_query("CALL db.awaitIndexes(300)")
    print(f"synthetic: +{len(nodes)} nodes +{len(edges)} rels, write {t1 - t0:.2f}s, awaitIndexes {time.perf_counter() - t1:.2f}s")


def drop_synthetic(d):
    with d.session(database="neo4j") as s:
        s.run("MATCH (n:Synthetic) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 2000 ROWS").consume()


def pack_table(d):
    rows = []
    for q in QUESTIONS:
        for pid, p in PERSONAS.items():
            hits = search_context(d, fake_embed(q), q, p.scopes, 8, metrics_only=p.metrics_only)
            pk = {"question": q, "hits": [h["uid"] for h in hits],
                  **expand(d, hits, p.scopes, metrics_only=p.metrics_only)}
            rows.append((q, pid, pack_size(pk)["chars"], pack_size(pk)["est_tokens"]))
    print("pack sizes (chars / est_tokens = chars/4):")
    for q, pid, c, t in rows:
        print(f"  {q[:55]:55s} {pid:18s} {c:6d} {t:5d}")
    print("  max est_tokens:", max(r[3] for r in rows))
    return rows


if __name__ == "__main__":
    with GraphDatabase.driver(URI, auth=AUTH, notifications_min_severity="OFF", max_connection_pool_size=20) as d:
        create_schema(d)
        drop_synthetic(d)
        print("reload real graph:", load(d))
        pack_table(d)
        r1 = bench(d, "real size")
        add_synthetic(d, 10)
        r10 = bench(d, "10x synthetic")
        pack_table(d)
        drop_synthetic(d)
        print("after cleanup:", counts(d)["nodes"])
        json.dump({"real": r1, "x10": r10}, open("bench_results.json", "w"), indent=1, default=str)
