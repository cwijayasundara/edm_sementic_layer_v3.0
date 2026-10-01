"""Hung server (docker pause) with the ASYNC driver: asyncio.wait_for bounds the call; pool recovers after unpause."""
import asyncio, subprocess, time
import neo4j
from neo4j import AsyncGraphDatabase, GraphDatabase
from schema import URI, AUTH

def stem_check():
    with GraphDatabase.driver(URI, auth=AUTH, notifications_min_severity="OFF") as d:
        for w in ("break", "breaks", "break*", "brake~"):
            n = d.execute_query("CALL db.index.fulltext.queryNodes('ctx_text', $q) YIELD node WHERE node.ns='prism' AND node:BusinessTerm RETURN collect(node.name) AS n", q=w).records[0]["n"]
            print(f"[standard-no-stop-words] {w!r}: {n}")

async def main():
    async with AsyncGraphDatabase.driver(URI, auth=AUTH, connection_timeout=3.0, connection_acquisition_timeout=5.0,
                                         liveness_check_timeout=1.0, max_connection_pool_size=10) as ad:
        await ad.execute_query("RETURN 1")
        subprocess.run(["docker", "pause", "prism-neo4j-spike"], check=True)
        try:
            t = time.perf_counter()
            try:
                await asyncio.wait_for(ad.execute_query("RETURN 1"), timeout=2.0)
            except Exception as e:
                print(f"[async paused, pooled conn, wait_for 2s] {type(e).__name__} after {time.perf_counter()-t:.2f}s")
        finally:
            subprocess.run(["docker", "unpause", "prism-neo4j-spike"], check=True)
        t = time.perf_counter()
        try:
            r = await asyncio.wait_for(ad.execute_query("RETURN 1 AS x"), timeout=10)
            print(f"[async after unpause] ok {r.records[0]['x']} in {time.perf_counter()-t:.2f}s")
        except Exception as e:
            print(f"[async after unpause] {type(e).__name__}: {e}")

stem_check()
asyncio.run(main())
