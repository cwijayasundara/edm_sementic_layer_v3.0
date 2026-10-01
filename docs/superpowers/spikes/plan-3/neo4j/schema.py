"""Schema (constraints + indexes) for the Prism context graph. Idempotent (IF NOT EXISTS)."""
from __future__ import annotations

import os

from neo4j import GraphDatabase

URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:17688")
AUTH = (os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "spike-password-123"))
DIM = 384

SCHEMA = [
    # Every node carries :Ctx {uid}; MERGE (n:Ctx {uid:$uid}) is then an index seek.
    "CREATE CONSTRAINT ctx_uid IF NOT EXISTS FOR (n:Ctx) REQUIRE n.uid IS UNIQUE",
    # Natural-key constraints must include ns, or namespaced test loads collide (composite uniqueness works in CE).
    "CREATE CONSTRAINT role_ns_id IF NOT EXISTS FOR (r:Role) REQUIRE (r.ns, r.persona_id) IS UNIQUE",
    "CREATE INDEX metric_id IF NOT EXISTS FOR (m:Metric) ON (m.id)",
    "CREATE INDEX ctx_version IF NOT EXISTS FOR (n:Ctx) ON (n.loaded_version)",
    # One vector index over a shared label (5.x vector indexes are single-label).
    # quantization is ON by default in 5.26 (vector-2.0 provider) and distorts scores of sparse/hand-crafted vectors;
    # off = exact float32 scores (graph is small). Re-evaluate with real fastembed vectors.
    f"""CREATE VECTOR INDEX ctx_vec IF NOT EXISTS FOR (n:Searchable) ON n.embedding
        OPTIONS {{indexConfig: {{`vector.dimensions`: {DIM}, `vector.similarity_function`: 'cosine',
                                `vector.quantization.enabled`: false, `vector.hnsw.m`: 16,
                                `vector.hnsw.ef_construction`: 100}}}}""",
    # Fulltext (Lucene BM25). 'standard-no-stop-words' keeps words like 'on', 'no' (stop words hurt 'nostro no').
    """CREATE FULLTEXT INDEX ctx_text IF NOT EXISTS FOR (n:Searchable) ON EACH [n.name, n.description, n.synonyms]
        OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-no-stop-words', `fulltext.eventually_consistent`: false}}""",
]


def create_schema(driver, timeout_s: int = 60) -> None:
    for stmt in SCHEMA:
        driver.execute_query(stmt)
    driver.execute_query("CALL db.awaitIndexes($t)", t=timeout_s)


def drop_schema(driver) -> None:
    for name in ("ctx_vec", "ctx_text", "metric_id", "ctx_version"):
        driver.execute_query(f"DROP INDEX {name} IF EXISTS")
    for name in ("ctx_uid", "role_ns_id"):
        driver.execute_query(f"DROP CONSTRAINT {name} IF EXISTS")


if __name__ == "__main__":
    with GraphDatabase.driver(URI, auth=AUTH) as d:
        create_schema(d)
        for r in d.execute_query("SHOW INDEXES YIELD name, type, state, labelsOrTypes, properties").records:
            print(dict(r))
