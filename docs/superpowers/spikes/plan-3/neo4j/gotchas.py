"""Q5 gotchas, each one executed. Uses only the spike container (pause/unpause/restart are on prism-neo4j-spike)."""
from __future__ import annotations

import random
import subprocess
import threading
import time
import urllib.request

import neo4j
import numpy as np
from neo4j import GraphDatabase

from schema import AUTH, URI

C = "prism-neo4j-spike"


def show(title, fn):
    t = time.perf_counter()
    try:
        r = fn()
        print(f"[{title}] OK ({1000 * (time.perf_counter() - t):.0f} ms): {r}")
    except Exception as e:  # noqa: BLE001
        print(f"[{title}] {type(e).__module__}.{type(e).__name__} after {1000 * (time.perf_counter() - t):.0f} ms: "
              f"code={getattr(e, 'code', None)} msg={str(getattr(e, 'message', e))[:220]!r}")


def with_deadline(fn, secs):
    box = {}

    def run():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=run, daemon=True); th.start(); th.join(secs)
    if th.is_alive():
        raise TimeoutError(f"still blocked after {secs}s")
    if "e" in box:
        raise box["e"]
    return box.get("r")


def main():
    d = GraphDatabase.driver(URI, auth=AUTH, notifications_min_severity="OFF")
    q = lambda c, **p: [dict(r) for r in d.execute_query(c, p).records]  # noqa: E731

    # --- auth failure (only ONE attempt: 3 failures lock the user for auth_lock_time=5s by default)
    def bad_auth():
        with GraphDatabase.driver(URI, auth=("neo4j", "wrong")) as bd:
            bd.verify_connectivity()
    show("wrong password", bad_auth)

    # --- server not listening
    def refused():
        with GraphDatabase.driver("bolt://127.0.0.1:17999", auth=AUTH, connection_timeout=2) as bd:
            bd.verify_connectivity()
    show("port closed", refused)

    # --- parameter types
    show("numpy array param", lambda: q("RETURN size($v) AS n", v=np.zeros(384, dtype=np.float32)))
    show("numpy .tolist() param", lambda: q("RETURN size($v) AS n", v=np.zeros(384, dtype=np.float32).tolist()))
    show("tuple param -> LIST", lambda: q("RETURN valueType($v) AS t", v=("a", "b")))
    show("set param", lambda: q("RETURN $v AS v", v={"a", "b"}))
    show("nested map param + list of maps", lambda: q("RETURN valueType($m) AS t, $m.rows[0].x AS x", m={"rows": [{"x": 1}]}))
    show("map as node property (not allowed)", lambda: q("CREATE (n:GotchaTmp {m: $m}) RETURN n", m={"a": 1}))
    show("list of lists as property (not allowed)", lambda: q("CREATE (n:GotchaTmp {m: $m}) RETURN n", m=[["a"], ["b"]]))
    show("empty list property", lambda: q("CREATE (n:GotchaTmp {m: []}) RETURN valueType(n.m) AS t"))
    show("mixed-type list property", lambda: q("CREATE (n:GotchaTmp {m: $m}) RETURN n", m=[1, "a"]))
    q("MATCH (n:GotchaTmp) DETACH DELETE n")
    show("IN with list param", lambda: q("RETURN 'a' IN $s AS ok", s=["a", "b"]))

    # --- vector dims limits + mismatches
    show("vector index 4096 dims", lambda: q("CREATE VECTOR INDEX g4096 IF NOT EXISTS FOR (n:GotchaV) ON n.e OPTIONS {indexConfig: {`vector.dimensions`: 4096, `vector.similarity_function`: 'cosine'}}"))
    show("vector index 4097 dims", lambda: q("CREATE VECTOR INDEX g4097 IF NOT EXISTS FOR (n:GotchaV2) ON n.e OPTIONS {indexConfig: {`vector.dimensions`: 4097, `vector.similarity_function`: 'cosine'}}"))
    q("DROP INDEX g4096 IF EXISTS"); q("DROP INDEX g4097 IF EXISTS")
    show("query vector wrong dims", lambda: q("CALL db.index.vector.queryNodes('ctx_vec', 3, $v) YIELD node RETURN node.uid", v=[0.1] * 10))
    show("zero vector with cosine", lambda: q("CALL db.index.vector.queryNodes('ctx_vec', 3, $v) YIELD node RETURN node.uid", v=[0.0] * 384))
    show("node with wrong-dim embedding (silently not indexed?)", lambda: q(
        "CREATE (n:Ctx:Searchable:GotchaTmp {uid:'gotcha:x', ns:'gotcha', embedding: $v}) WITH n "
        "CALL db.index.vector.queryNodes('ctx_vec', 1000, $q) YIELD node WHERE node.uid = 'gotcha:x' RETURN count(*) AS found",
        v=[0.1] * 10, q=[0.1] * 384))
    q("MATCH (n:GotchaTmp) DETACH DELETE n")
    show("unknown index name", lambda: q("CALL db.index.vector.queryNodes('nope', 3, $v)", v=[0.1] * 384))

    # --- index population: query a brand-new index over 20k nodes before it is ONLINE
    rnd = random.Random(1)
    for i in range(0, 20000, 2000):
        q("UNWIND range($a, $b) AS i CREATE (:GotchaPop {i: i, e: $vs[i - $a]})", a=i, b=i + 1999,
          vs=[[rnd.random() for _ in range(384)] for _ in range(2000)])
    q("CREATE VECTOR INDEX gpop IF NOT EXISTS FOR (n:GotchaPop) ON n.e OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}")
    show("state right after CREATE", lambda: q("SHOW INDEXES YIELD name, state, populationPercent WHERE name='gpop' RETURN state, populationPercent"))
    show("query while POPULATING", lambda: q("CALL db.index.vector.queryNodes('gpop', 3, $v) YIELD node RETURN count(*) AS n", v=[0.5] * 384))
    t = time.perf_counter(); q("CALL db.awaitIndex('gpop', 300)")
    print(f"[awaitIndex gpop 20k x 384d] {time.perf_counter() - t:.2f}s")
    show("query after ONLINE", lambda: q("CALL db.index.vector.queryNodes('gpop', 3, $v) YIELD node RETURN count(*) AS n", v=[0.5] * 384))
    q("DROP INDEX gpop IF EXISTS")
    with d.session() as s:
        s.run("MATCH (n:GotchaPop) CALL (n) { DELETE n } IN TRANSACTIONS OF 5000 ROWS").consume()

    # --- fulltext visibility right after commit (eventually_consistent=false) and analyzer list
    q("CREATE (:Ctx:Searchable:GotchaTmp {uid:'gotcha:ft', ns:'gotcha', name:'zyxwv nostro'})")
    show("fulltext sees write immediately", lambda: q("CALL db.index.fulltext.queryNodes('ctx_text', 'zyxwv') YIELD node RETURN count(*) AS n"))
    q("MATCH (n:GotchaTmp) DETACH DELETE n")
    show("analyzers available", lambda: len(q("CALL db.index.fulltext.listAvailableAnalyzers() YIELD analyzer RETURN analyzer")))
    show("english analyzer stems 'breaks'->'break'", lambda: q(
        "CALL db.index.fulltext.queryNodes('ctx_text', 'breaks') YIELD node WHERE node.ns='prism' RETURN count(*) AS n"))
    show("raw lucene: AND OR ( ) : \"", lambda: q("CALL db.index.fulltext.queryNodes('ctx_text', $x)", x='AND OR ( ) : "'))
    show("raw lucene: field injection 'uid:*'", lambda: q("CALL db.index.fulltext.queryNodes('ctx_text', 'name:nostro') YIELD node RETURN count(*) AS n"))
    show("raw lucene: leading wildcard '*stro'", lambda: q("CALL db.index.fulltext.queryNodes('ctx_text', '*stro') YIELD node RETURN count(*) AS n"))

    # --- server hung (docker pause): pooled connection vs new driver
    q("RETURN 1")   # make sure a pooled connection exists
    subprocess.run(["docker", "pause", C], check=True, capture_output=True)
    try:
        show("paused: pooled driver, no timeouts set (15s watchdog)", lambda: with_deadline(lambda: q("RETURN 1 AS x"), 15))
        def fresh():
            with GraphDatabase.driver(URI, auth=AUTH, connection_timeout=3.0, connection_acquisition_timeout=5.0) as fd:
                fd.verify_connectivity()
        show("paused: fresh driver connection_timeout=3", lambda: with_deadline(fresh, 20))
        def txto():
            with GraphDatabase.driver(URI, auth=AUTH, connection_timeout=3.0) as fd:
                fd.execute_query(neo4j.Query("RETURN 1", timeout=2.0))
        show("paused: fresh driver + Query(timeout=2)", lambda: with_deadline(txto, 20))
    finally:
        subprocess.run(["docker", "unpause", C], check=True, capture_output=True)
    time.sleep(0.5)
    show("after unpause: old pooled driver", lambda: with_deadline(lambda: q("RETURN 1 AS x"), 30))
    d.close()

    # --- readiness after restart: poll HTTP 7474 and Bolt separately
    t0 = time.perf_counter()
    subprocess.run(["docker", "restart", C], check=True, capture_output=True)
    t_cmd = time.perf_counter() - t0
    http_ok = bolt_ok = None
    while (http_ok is None or bolt_ok is None) and time.perf_counter() - t0 < 120:
        el = time.perf_counter() - t0
        if http_ok is None:
            try:
                urllib.request.urlopen("http://127.0.0.1:17475/", timeout=1).read(); http_ok = el
            except Exception:  # noqa: BLE001
                pass
        if bolt_ok is None:
            try:
                with GraphDatabase.driver(URI, auth=AUTH, connection_timeout=1) as bd:
                    bd.verify_connectivity(); bd.execute_query("RETURN 1"); bolt_ok = el
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.2)
    print(f"[restart readiness] docker restart returned {t_cmd:.1f}s; HTTP 7474 ok at {http_ok:.1f}s; "
          f"Bolt RETURN 1 ok at {bolt_ok:.1f}s (from restart start)")
    with GraphDatabase.driver(URI, auth=AUTH) as bd:
        t = time.perf_counter(); bd.execute_query("CALL db.awaitIndexes(120)")
        print(f"[after restart] awaitIndexes {time.perf_counter() - t:.2f}s;",
              [dict(r) for r in bd.execute_query("SHOW INDEXES YIELD name, state WHERE name STARTS WITH 'ctx'").records])


if __name__ == "__main__":
    main()
