"""Constraints and indexes of the context graph (ported from the plan-3 Neo4j spike). Idempotent: IF NOT EXISTS.

Every node carries :Ctx {uid, ns, loaded_version}; MERGE (n:Ctx {uid: $uid}) is then an index seek. Natural-key
constraints include `ns`, so a scratch-namespace load never collides with the real graph (composite uniqueness
works in Community Edition). Searchable nodes share one label, because 5.x vector indexes are single-label.
"""
from __future__ import annotations

DIM = 384
# Bump whenever a policy-bearing property is added to or changes meaning on the nodes the gateway catalog reads
# (Metric). Every Metric node carries `schema_version`; load_catalog refuses a graph written by an older loader
# (it would read a missing restriction as "no restriction"). 2 = fine_grain_dimensions.
GRAPH_SCHEMA_VERSION = 2
VECTOR_INDEX = "ctx_vec"
FULLTEXT_INDEX = "ctx_text"

# (name, label, properties): natural keys, unique per namespace.
NATURAL_KEYS = (
    ("source_ns_name", "Source", ("ns", "name")),
    ("metric_ns_id", "Metric", ("ns", "id")),
    ("term_ns_key", "BusinessTerm", ("ns", "key")),
    ("concept_ns_name", "Concept", ("ns", "name")),
    ("role_ns_id", "Role", ("ns", "persona_id")),
)

SCHEMA = [
    "CREATE CONSTRAINT ctx_uid IF NOT EXISTS FOR (n:Ctx) REQUIRE n.uid IS UNIQUE",
    *(f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE ({', '.join(f'n.{p}' for p in props)}) IS UNIQUE"
      for name, label, props in NATURAL_KEYS),
    "CREATE CONSTRAINT trace_ns_run IF NOT EXISTS FOR (t:Trace) REQUIRE (t.ns, t.run_id) IS UNIQUE",
    "CREATE INDEX ctx_ns_version IF NOT EXISTS FOR (n:Ctx) ON (n.ns, n.loaded_version)",
    # Quantization is on by default in 5.26 and distorts scores; the graph is small, so keep exact float32 vectors.
    f"""CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS FOR (n:Searchable) ON n.embedding
        OPTIONS {{indexConfig: {{`vector.dimensions`: {DIM}, `vector.similarity_function`: 'cosine',
                                `vector.quantization.enabled`: false, `vector.hnsw.m`: 16,
                                `vector.hnsw.ef_construction`: 100}}}}""",
    # 'english' stems ('breaks' -> 'break'); the spike showed 'standard-no-stop-words' does not.
    f"""CREATE FULLTEXT INDEX {FULLTEXT_INDEX} IF NOT EXISTS FOR (n:Searchable) ON EACH [n.name, n.description, n.synonyms]
        OPTIONS {{indexConfig: {{`fulltext.analyzer`: 'english', `fulltext.eventually_consistent`: false}}}}""",
]


def create_schema(driver, timeout_s: int = 60) -> None:
    for stmt in SCHEMA:
        driver.execute_query(stmt)
    driver.execute_query("CALL db.awaitIndexes($t)", t=timeout_s)
