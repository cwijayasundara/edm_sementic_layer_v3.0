"""Q1: prove each Neo4j 5.26 Community feature by running it (isolated :Q1 nodes, removed at the end)."""
import asyncio
import time

import neo4j
from neo4j import AsyncGraphDatabase, GraphDatabase

from schema import AUTH, URI, create_schema


def run(d, cypher_, **p):
    recs = d.execute_query(cypher_, p).records
    return [dict(r) for r in recs]


def main():
    with GraphDatabase.driver(URI, auth=AUTH) as d:
        print("driver", neo4j.__version__, "server", d.get_server_info().agent)
        print(run(d, "CALL dbms.components() YIELD name, versions, edition"))
        create_schema(d)
        run(d, "MATCH (n:Q1) DETACH DELETE n")
        # dynamic labels (5.26)
        try:
            run(d, "MERGE (n:Q1:Ctx {uid:'q1:dyn'}) SET n:$($lbl) RETURN labels(n) AS l", lbl="Metric")
            print("dynamic label SET n:$($lbl):", run(d, "MATCH (n {uid:'q1:dyn'}) RETURN labels(n) AS l"))
        except neo4j.exceptions.Neo4jError as e:
            print("dynamic labels FAILED:", e.code, e.message[:200])
        # vector
        v1 = [1.0] + [0.0] * 383
        v2 = [0.9, 0.1] + [0.0] * 382
        run(d, "UNWIND $rows AS r MERGE (n:Q1:Ctx:Searchable {uid:r.uid}) SET n.name=r.name, n.embedding=r.v, "
               "n.synonyms=r.syn, n.description='q1 smoke'",
            rows=[{"uid": "q1:a", "name": "Nostro account", "v": v1, "syn": ["cash account", "bank account"]},
                  {"uid": "q1:b", "name": "Price conflict", "v": v2, "syn": ["price-source conflict"]}])
        # immediately after write, before awaitIndexes
        print("vector right after write:", run(d, "CALL db.index.vector.queryNodes('ctx_vec', 2, $v) YIELD node, score "
                                                "WHERE node:Q1 RETURN node.uid AS uid, score", v=v1))
        run(d, "CALL db.awaitIndexes(60)")
        print("vector:", run(d, "CALL db.index.vector.queryNodes('ctx_vec', 2, $v) YIELD node, score "
                                "RETURN node.uid AS uid, score", v=v1))
        print("fulltext list prop (synonyms):", run(d, "CALL db.index.fulltext.queryNodes('ctx_text', $q) YIELD node, score "
                                                       "WHERE node:Q1 RETURN node.uid AS uid, score", q='"cash account"'))
        print("fulltext case-insens NOSTRO:", run(d, "CALL db.index.fulltext.queryNodes('ctx_text', 'NOSTRO') YIELD node, score "
                                                    "WHERE node:Q1 RETURN node.uid AS uid, score"))
        print("fulltext with limit option:", run(d, "CALL db.index.fulltext.queryNodes('ctx_text', 'price', {limit: 5}) "
                                                   "YIELD node, score WHERE node:Q1 RETURN node.uid AS uid, score"))
        # unique constraint violation
        try:
            run(d, "CREATE (:Ctx:Q1 {uid:'q1:a'})")
        except neo4j.exceptions.ConstraintError as e:
            print("IS UNIQUE enforced ->", type(e).__name__, e.code)
        # paths
        run(d, "MERGE (a:Q1:Ctx {uid:'q1:p1'}) MERGE (b:Q1:Ctx {uid:'q1:p2'}) MERGE (c:Q1:Ctx {uid:'q1:p3'}) "
               "MERGE (d:Q1:Ctx {uid:'q1:p4'}) MERGE (a)-[:SAME_KEY_AS]->(b) MERGE (b)-[:REFERENCES]->(c) "
               "MERGE (a)-[:REFERENCES]->(d) MERGE (d)-[:REFERENCES]->(c)")
        print("shortestPath():", run(d, "MATCH (a {uid:'q1:p1'}), (c {uid:'q1:p3'}) MATCH p=shortestPath((a)-[:SAME_KEY_AS|REFERENCES*..6]-(c)) "
                                      "RETURN [n IN nodes(p) | n.uid] AS path"))
        print("SHORTEST 1 + QPP:", run(d, "MATCH p = SHORTEST 1 (a:Q1 {uid:'q1:p1'})(()-[:SAME_KEY_AS|REFERENCES]-(x) WHERE x.uid <> 'q1:p2'){1,6}(c:Q1 {uid:'q1:p3'}) "
                                        "RETURN [n IN nodes(p) | n.uid] AS path"))
        print("ALL SHORTEST:", run(d, "MATCH p = ALL SHORTEST (a:Q1 {uid:'q1:p1'})-[:SAME_KEY_AS|REFERENCES]-{1,6}(c:Q1 {uid:'q1:p3'}) "
                                    "RETURN [n IN nodes(p) | n.uid] AS path"))
        run(d, "MATCH (n:Q1) DETACH DELETE n")

    async def amain():
        async with AsyncGraphDatabase.driver(URI, auth=AUTH, max_connection_pool_size=20,
                                             connection_acquisition_timeout=5.0, connection_timeout=3.0) as ad:
            await ad.verify_connectivity()
            t = time.perf_counter()
            res = await asyncio.gather(*[ad.execute_query("RETURN $i AS i", i=i, routing_="r") for i in range(200)])
            print(f"async 200 concurrent execute_query: {1000*(time.perf_counter()-t):.1f} ms, all ok={len(res)==200}")
            async with ad.session(database="neo4j") as s:
                r = await s.execute_read(lambda tx: _one(tx))
                print("async session.execute_read:", r)

    async def _one(tx):
        res = await tx.run("RETURN 1 AS x")
        return (await res.single())["x"]

    asyncio.run(amain())


if __name__ == "__main__":
    main()
