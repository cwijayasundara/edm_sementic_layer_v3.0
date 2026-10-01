"""The gateway catalog (one query) and the Python / SQL / graph agreement on who may read what."""
import time

import pytest
from neo4j import AsyncGraphDatabase

from prism.config import LOGICAL_DBS, Settings
from prism.db.policies import ROW_SCOPES
from prism.db.session import ctx_from_claims, scoped_sync, sign_ctx
from prism.graph.catalog import Catalog, CatalogError, aload_catalog, load_catalog, row_values
from prism.graph.loader import load
from prism.graph.model import GRAPH_SCHEMA_VERSION, Graph
from prism.graph.knowledge import REST_SOURCES
from prism.graph.model import readers
from prism.graph.retrieval import gate, gate_params
from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
from prism.mcp.rest_backend import load_rest_config
from prism.security.access import can
from prism.security.personas import ALL_SOURCES, PERSONAS, claims_for
from tests.graph_ns import TEST_GRAPH_NS

pytestmark = pytest.mark.neo4j


@pytest.fixture(scope="module")
def catalog(neo4j_driver, context_graph) -> Catalog:
    return load_catalog(neo4j_driver, TEST_GRAPH_NS)


def _registry() -> dict[str, dict]:
    """What the gateway will expect, straight from the source registries (independent of the graph)."""
    out = {}
    for m in load_metrics(DEFAULT_METRICS_DIR).values():
        out[m.id] = {"source": m.source, "kind": "sql", "endpoint": None, "dimensions": tuple(sorted(m.dimensions)),
                     "required": tuple(sorted(m.required_dimensions)), "sensitive": tuple(sorted(m.sensitive_dimensions)),
                     "fine_grain": tuple(sorted(m.fine_grain_dimensions)), "filters": tuple(sorted(m.filters)),
                     "tables": tuple(sorted(f"{m.source}.{t}" for t in m.tables)),
                     "allowed_scopes": tuple(readers(m.source, sorted(m.tables)))}
    for s in REST_SOURCES:
        endpoints, metrics = load_rest_config(s)
        for m in metrics.values():
            table = endpoints[m.endpoint].table
            out[m.id] = {"source": s, "kind": "rest", "endpoint": m.endpoint, "dimensions": tuple(sorted(m.dimensions)),
                         "required": (), "sensitive": tuple(sorted(m.sensitive_dimensions)),
                         "fine_grain": tuple(sorted(m.fine_grain_dimensions)), "filters": tuple(sorted(m.filters)),
                         "tables": (f"{s}.{table}",), "allowed_scopes": tuple(readers(s, [table]))}
    return out


def test_one_query_returns_22_metrics_and_5_roles(neo4j_driver, catalog, context_graph):
    assert len(catalog.metrics) == 22 and set(catalog.roles) == set(PERSONAS)
    assert catalog.version == context_graph.version or catalog.version > context_graph.version
    t = time.perf_counter()
    load_catalog(neo4j_driver, TEST_GRAPH_NS)
    assert time.perf_counter() - t < 0.5


def test_catalog_matches_the_gateway_contract(catalog):
    reg = _registry()
    assert set(catalog.metrics) == set(reg)
    for mid, want in reg.items():
        m = catalog.metrics[mid]
        got = {k: getattr(m, k) for k in want}
        got["tables"] = tuple(sorted(got["tables"]))
        assert got == want, mid
        assert m.server == f"{m.source}-mcp" and m.tool == "run_metric"
    mm = catalog.metrics["manual_matches"]
    assert mm.sensitive == ("matched_by",) and mm.allowed_scopes == ("cashrecon", "cashrecon.match_groups")
    assert catalog.metrics["open_break_amount"].required == ("ccy",)
    assert catalog.metrics["nav_break_bps_max"].fine_grain == ("nav_date", "portfolio_id")
    assert "fund_group" in catalog.metrics["nav_break_bps_max"].filters
    assert catalog.metrics["price_conflicts"].endpoint == "prices_conflicts_summary"
    assert catalog.metrics["price_conflicts"].fine_grain == ("price_date", "vendor_id")
    assert catalog.metrics["recon_unmatched_items"].fine_grain == ("business_date", "portfolio_id")


def test_roles_and_row_scopes(catalog):
    for pid, p in PERSONAS.items():
        r = catalog.roles[pid]
        assert r.metrics_only == p.metrics_only and r.scopes == p.scopes
        assert {g.scope for g in r.grants} == {s for s in p.scopes if s != "pii:read"}
        assert dict(r.row_scope) == {k: tuple(v) for k, v in p.rows.items()}
    inv = catalog.roles["invest_ops_growth"]
    assert [g.object for g in inv.grants] == ["source:assetrecon", "source:feedhub", "table:refmaster.legal_entities",
                                              "table:refmaster.securities"]
    assert inv.row_values("fund_group") == ("Growth",)
    assert inv.row_values("region") == ()                                   # missing dimension = deny
    assert catalog.roles["steward"].row_values("region") == ()
    assert row_values({}, "region") == () and row_values({"region": ["*"]}, "region") == ("*",)


async def test_async_catalog_equals_sync(catalog):
    s = Settings()
    async with AsyncGraphDatabase.driver(s.neo4j_uri, auth=s.neo4j_auth(),
                                         notifications_min_severity="OFF") as ad:
        assert await aload_catalog(ad, TEST_GRAPH_NS) == catalog


def test_every_rls_table_with_a_granted_dimension_is_covered(catalog):
    """A role granted a table whose RLS dimension is missing from its row scope would see zero rows (deny); the demo
    personas never rely on that, so the graph never advertises a table a persona cannot get rows from."""
    for pid, r in catalog.roles.items():
        for g in r.grants:
            kind, _, name = g.object.partition(":")
            source, _, table = name.partition(".")
            for t in [table] if table else list(ROW_SCOPES[source]):
                scope = ROW_SCOPES[source][t]
                if scope is not None:
                    assert r.row_values(scope[0]), (pid, source, t, scope[0])


# ------------------------------------------------------------------------------------------- can() agreement
def test_graph_tables_are_exactly_the_rls_tables(neo4j_driver, context_graph):
    rows = neo4j_driver.execute_query("MATCH (t:Table {ns: 'prism_test'}) RETURN t.source AS s, t.name AS t").records
    assert {(r["s"], r["t"]) for r in rows} == {(db, t) for db in LOGICAL_DBS for t in ROW_SCOPES[db]}
    assert set(LOGICAL_DBS) == set(ALL_SOURCES)


def _graph_visible_tables(driver, claims) -> set[tuple[str, str]]:
    params = gate_params(claims["scopes"], False, TEST_GRAPH_NS)
    rows = driver.execute_query(f"MATCH (t:Table {{ns: $ns}}) WHERE {gate('t')} RETURN t.source AS s, t.name AS t",
                                params).records
    return {(r["s"], r["t"]) for r in rows}


def _granted_tables(driver, persona) -> set[tuple[str, str]]:
    rows = driver.execute_query(
        "MATCH (:Role {ns: 'prism_test', persona_id: $p})-[:CAN_READ]->(o) "
        "MATCH (t:Table {ns: 'prism_test'}) WHERE o = t OR (o:Source AND (o)-[:HAS_TABLE]->(t)) "
        "RETURN t.source AS s, t.name AS t", p=persona).records
    return {(r["s"], r["t"]) for r in rows}


@pytest.mark.db
@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_python_sql_and_graph_can_agree(seeded, neo4j_driver, context_graph, persona):
    claims = claims_for(persona)
    ctx = ctx_from_claims(claims, seeded.ctx_hmac_key.get_secret_value())
    every = {(db, t) for db in LOGICAL_DBS for t in ROW_SCOPES[db]}
    python = {(db, t) for db, t in every if can(claims, db, t)}
    sql_ok = set()
    for db in LOGICAL_DBS:
        with scoped_sync(seeded, db, ctx) as conn:
            for t in ROW_SCOPES[db]:
                if conn.execute("SELECT prism_sec.can(%s, %s) AS ok", (db, t)).fetchone()["ok"]:
                    sql_ok.add((db, t))
    assert python == sql_ok
    assert _graph_visible_tables(neo4j_driver, claims) == python
    assert _granted_tables(neo4j_driver, persona) == python


@pytest.mark.db
def test_missing_row_dimension_denies_rows_in_sql(seeded):
    """The rule the catalog's row_values mirrors: dataset scope without the table's RLS dimension = no rows."""
    claims = {"sub": "t", "scopes": ["cashrecon"], "rows": {}, "exp": int(time.time()) + 60}
    with scoped_sync(seeded, "cashrecon", sign_ctx(claims, seeded.ctx_hmac_key.get_secret_value())) as conn:
        assert conn.execute("SELECT count(*) AS n FROM breaks").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM match_rules").fetchone()["n"] > 0   # no RLS dimension


# ------------------------------------------------------------------------------------------- stale graph
def _metric_graph(**over) -> Graph:
    props = dict(id="m1", name="m1", source="cashrecon", kind="sql", mcp_server="cashrecon-mcp", mcp_tool="run_metric",
                 unit="breaks", dimensions=["portfolio_id", "business_date"], required_dimensions=[],
                 sensitive_dimensions=[], fine_grain_dimensions=["business_date", "portfolio_id"], filters=[],
                 allowed_scopes=["cashrecon"], schema_version=GRAPH_SCHEMA_VERSION)
    props.update(over)
    g = Graph()
    g.node("metric:m1", ["Metric"], **props)
    return g


def test_current_graph_loads(neo4j_driver, scratch_ns):
    load(neo4j_driver, None, scratch_ns, graph=_metric_graph(), schema=False)
    m = load_catalog(neo4j_driver, scratch_ns).metrics["m1"]
    assert m.fine_grain == ("business_date", "portfolio_id") and m.sensitive == ()
    load(neo4j_driver, None, scratch_ns, graph=_metric_graph(fine_grain_dimensions=[]), schema=False)
    assert load_catalog(neo4j_driver, scratch_ns).metrics["m1"].fine_grain == ()           # explicit [] is fine


@pytest.mark.parametrize("over", [{"fine_grain_dimensions": None}, {"sensitive_dimensions": None},
                                  {"required_dimensions": None}, {"schema_version": None},
                                  {"schema_version": GRAPH_SCHEMA_VERSION - 1}])
def test_a_stale_graph_is_refused_not_read_as_unrestricted(neo4j_driver, scratch_ns, over):
    """A graph loaded before a policy property existed must not read as 'no restriction' (fail closed)."""
    load(neo4j_driver, None, scratch_ns, graph=_metric_graph(**over), schema=False)
    with pytest.raises(CatalogError, match="reload the context graph"):
        load_catalog(neo4j_driver, scratch_ns)
