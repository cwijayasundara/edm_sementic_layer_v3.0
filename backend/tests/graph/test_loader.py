"""Context-graph schema, model builder and versioned loader. The graph is loaded once per session into the test
session's configured ns `prism_test` (fixture `context_graph`; never the real ns); tests that mutate load into their own `scratch_ns`."""
import json
import socket

import pytest
from neo4j.exceptions import ConstraintError

from prism.config import Settings
from prism.graph.knowledge import REST_SOURCES, load_knowledge
from prism.graph.loader import counts, delete_ns, load, production_ns, uid_for
from prism.graph.model import Graph, build_graph, can_read, load_ddl, readers, visible
from prism.graph.schema import DIM, NATURAL_KEYS, create_schema
from prism.mcp.client import call_tool
from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
from prism.mcp.rest_backend import load_rest_config
from prism.mcp.servers import MCP_PORTS, create_backend
from prism.security.access import can
from prism.security.personas import ALL_SOURCES, PERSONAS, claims_for
from prism.security.tokens import mint
from tests.graph_ns import TEST_GRAPH_NS, load_test_graph

SEARCHABLE_KINDS = {"BusinessTerm", "Metric", "Column", "Question", "Concept"}


def _sql_ids() -> set[str]:
    return set(load_metrics(DEFAULT_METRICS_DIR))


def _rest_ids() -> set[str]:
    return {m for s in REST_SOURCES for m in load_rest_config(s)[1]}


@pytest.fixture(scope="module")
def graph(graph_embedder):
    return build_graph(graph_embedder)


def _rows(driver, cypher: str, **params) -> list[dict]:
    return [r.data() for r in driver.execute_query(cypher, **params).records]


def _graph_metrics(driver, ns: str | None = None) -> dict[str, dict]:
    return {r["m"]["id"]: r["m"] for r in _rows(driver, "MATCH (m:Metric {ns: $ns}) RETURN m", ns=ns or TEST_GRAPH_NS)}


# ------------------------------------------------------------------------------------------- pure model
def test_ddl_covers_every_table_and_column_referenced_by_knowledge_and_metrics():
    ddl = load_ddl()
    k = load_knowledge()
    refs = [r for c in k.concepts for r in c.implemented_by + c.identified_by]
    refs += [r for t in k.terms for r in t.tags] + [r for g in k.same_key for r in g]
    for ref in refs:
        kind, _, rest = ref.partition(":")
        parts = rest.split(".")
        if kind == "endpoint":
            assert parts[1] in load_rest_config(parts[0])[0], ref
            continue
        assert parts[1] in ddl[parts[0]], ref
        if kind == "column":
            assert parts[2] in dict(ddl[parts[0]][parts[1]].columns), ref
    for m in load_metrics(DEFAULT_METRICS_DIR).values():
        assert m.tables and all(t in ddl[m.source] for t in m.tables), m.id
    for s in REST_SOURCES:
        endpoints, _ = load_rest_config(s)
        assert all(e.table in ddl[s] for e in endpoints.values())


def test_masking_view_exposes_public_name_and_flags_masked_columns():
    cash = load_ddl()["cashrecon"]
    spec = cash["cash_accounts"]  # private.cash_accounts is reached only through the public masking view
    assert spec.masked == {"nostro_no"}
    assert [c for c, _ in spec.columns] == ["account_id", "legal_entity_id", "bank_source_id", "bank_bic",
                                            "nostro_no", "ccy", "region"]
    assert spec.primary_key == ["account_id"]
    assert ("account_id", "cash_accounts", None) in cash["breaks"].references
    assert all(not s.masked for name, s in cash.items() if name != "cash_accounts")


def test_metric_nodes_carry_the_gateway_contract(graph):
    metrics = {m["id"]: m for m in graph.labelled("Metric")}
    assert set(metrics) == _sql_ids() | _rest_ids() and len(metrics) == 22
    m = metrics["manual_matches"]
    assert m["kind"] == "sql" and m["mcp_server"] == "cashrecon-mcp" and m["mcp_tool"] == "run_metric"
    assert m["sensitive_dimensions"] == ["matched_by"] and m["tables"] == ["cashrecon.match_groups"]
    assert m["allowed_scopes"] == ["cashrecon", "cashrecon.match_groups"] and m["definition"]
    assert metrics["open_break_amount"]["required_dimensions"] == ["ccy"]
    p = metrics["price_conflicts"]
    assert p["kind"] == "rest" and p["endpoint_id"] == "prices_conflicts_summary" and p["mcp_server"] == "marketmaster-mcp"
    dims = {d["name"]: d for d in graph.labelled("Dimension") if d["metric"] == "manual_matches"}
    assert dims["matched_by"]["sensitive"] is True and not any(d["sensitive"] for n, d in dims.items() if n != "matched_by")
    nav = metrics["nav_break_bps_max"]
    assert nav["fine_grain_dimensions"] == ["nav_date", "portfolio_id"]
    assert p["fine_grain_dimensions"] == ["price_date", "vendor_id"]                     # REST metrics too
    assert metrics["open_breaks"]["fine_grain_dimensions"] == []
    nav_dims = {d["name"]: d for d in graph.labelled("Dimension") if d["metric"] == "nav_break_bps_max"}
    assert {n: d.get("grain") for n, d in nav_dims.items()} == {"portfolio_id": "fine", "nav_date": "fine"}
    assert all(d.get("grain") is None for d in dims.values())


def test_every_node_has_scopes_and_searchables_have_embeddings(graph):
    for uid, n in graph.nodes.items():
        if "Role" in n["labels"]:                       # gateway-internal: readable by nobody through retrieval
            assert n["props"]["allowed_scopes"] == [], uid
        else:
            assert n["props"]["allowed_scopes"], uid
        kinds = set(n["labels"]) - {"Searchable"}
        assert ("Searchable" in n["labels"]) == bool(kinds & SEARCHABLE_KINDS), uid
        if "Searchable" in n["labels"]:
            p = n["props"]
            assert len(p["embedding"]) == DIM and p["name"] and isinstance(p["synonyms"], list), uid


def _claim_cases() -> list[tuple[str, dict]]:
    """Synthetic claims: every source scope, every `<source>.<table>` scope, unrelated scopes, none, and the personas."""
    cases = [(s, {"scopes": [s]}) for s in ALL_SOURCES]
    cases += [(f"{s}.{t}", {"scopes": [f"{s}.{t}"]}) for s, tables in load_ddl().items() for t in sorted(tables)]
    cases += [("unrelated-table", {"scopes": ["cashrecon.no_such_table"]}),
              ("unknown-source", {"scopes": ["nosuchsource"]}), ("pii", {"scopes": ["pii:read"]}),
              ("none", {"scopes": []})]
    return cases + [(f"persona:{p}", claims_for(p)) for p in sorted(PERSONAS)]


CLAIM_CASES = _claim_cases()
PUBLIC_TERMS = {"sla", "fund administrator", "basis point", "security master", "corporate action", "exception",
                "data quality rule", "data domain", "data steward", "four-eyes approval", "data lineage",
                "stale price", "tolerance", "week over week", "business day"}


@pytest.mark.parametrize("claims", [c for _, c in CLAIM_CASES], ids=[i for i, _ in CLAIM_CASES])
def test_allowed_scopes_agree_with_access_rules(graph, claims):
    """Every node's allowed_scopes, evaluated by can_read, against prism.security.access.can() for one claim set."""
    ddl = load_ddl()
    out: dict[str, list[str]] = {}
    for a, typ, b, _ in graph.edges:
        out.setdefault(f"{a}|{typ}", []).append(b)
    tagged = {}
    for a, typ, b, _ in graph.edges:
        if typ == "TAGGED_WITH":
            tagged.setdefault(b, []).append(a)

    def readable(uid: str) -> bool:
        return can_read(claims, graph.nodes[uid]["props"])

    seen = set()
    data_kinds = ("Table", "Column", "Endpoint", "Field", "Metric", "Dimension")
    for uid, n in graph.nodes.items():   # pass 1: data nodes, straight from can()
        p, kind = n["props"], (set(n["labels"]) - {"Searchable"}).pop()
        if kind not in data_kinds:
            continue
        if kind == "Table":
            tables = [p["name"]]
        elif kind in ("Column", "Endpoint"):
            tables = [p["table"]]
        elif kind == "Field":
            tables = [graph.nodes[f"endpoint:{p['source']}.{p['endpoint_id']}"]["props"]["table"]]
        else:
            metric = p if kind == "Metric" else graph.nodes[f"metric:{p['metric']}"]["props"]
            tables = [t.split(".", 1)[1] for t in metric["tables"]]
        source = p.get("source") or graph.nodes[f"metric:{p['metric']}"]["props"]["source"]
        if len(tables) == 1:
            expected = can(claims, source, tables[0])
        else:  # multi-table objects are readable through the source scope only (fails closed)
            expected = source in claims["scopes"]
            assert not expected or all(can(claims, source, t) for t in tables), uid
        assert readable(uid) == expected, (uid, p["allowed_scopes"])
        seen.add(uid)
    for uid, n in graph.nodes.items():   # pass 2: nodes derived from data nodes
        p, kind = n["props"], (set(n["labels"]) - {"Searchable"}).pop()
        if kind in data_kinds:
            continue
        if kind == "Source":
            expected = any(can(claims, p["name"], t) for t in ddl[p["name"]])
        elif kind == "Concept":
            expected = any(readable(r) for r in out[f"{uid}|IMPLEMENTED_BY"])
        elif kind == "BusinessTerm":
            links = out.get(f"{uid}|DEFINES", []) + tagged.get(uid, [])
            if p["key"] in PUBLIC_TERMS:
                assert p["allowed_scopes"] == ["*"] and not links, uid
                expected = True
            else:
                assert links, uid
                expected = any(readable(r) for r in links)
        elif kind == "Question":   # union prefilter + the gate: visible iff every metric it used is readable
            used = out[f"{out[f'{uid}|ANSWERED_BY'][0]}|USED"]
            assert p["allowed_scopes"] == sorted({s for m in used for s in graph.nodes[m]["props"]["allowed_scopes"]})
            assert visible(graph, claims, uid) == (bool(used) and all(readable(m) for m in used)), uid
            seen.add(uid)
            continue
        elif kind == "Execution":
            (q,) = [a for a, typ, b, _ in graph.edges if typ == "ANSWERED_BY" and b == uid]
            assert p["allowed_scopes"] == graph.nodes[q]["props"]["allowed_scopes"], uid
            assert visible(graph, claims, uid) == visible(graph, claims, q), uid
            seen.add(uid)
            continue
        elif kind == "Role":       # gateway-internal: never visible to retrieval, whatever the scopes
            assert p["allowed_scopes"] == [] and not visible(graph, claims, uid), uid
            expected = False
        else:
            raise AssertionError(f"unchecked node kind {kind}: {uid}")
        assert readable(uid) == expected, (uid, p["allowed_scopes"])
        seen.add(uid)
    assert seen == set(graph.nodes)


def test_only_explicitly_public_terms_are_public(graph):
    public = {u for u, n in graph.nodes.items() if "*" in n["props"]["allowed_scopes"]}
    assert all(graph.nodes[u]["props"]["allowed_scopes"] == ["*"] for u in public)
    assert public == {f"term:{t}" for t in PUBLIC_TERMS}
    roles = {u for u, n in graph.nodes.items() if "Role" in n["labels"]}
    assert roles == {f"role:{p}" for p in PERSONAS}
    assert all(not visible(graph, claims_for(p), r) for p in PERSONAS for r in roles)


def test_unlinked_term_fails_closed(graph_embedder):
    g = build_graph(graph_embedder, exclude=frozenset({"metric:open_breaks"}))
    term = g.nodes["term:open break"]["props"]
    assert term["allowed_scopes"] == []
    assert not can_read(claims_for("head_data"), term) and not can_read({"scopes": ["cashrecon"]}, term)


def test_question_without_used_objects_fails_closed(graph_embedder):
    g = build_graph(graph_embedder, exclude=frozenset({"metric:late_feeds"}))
    (uid,) = [u for u, n in g.nodes.items() if n["props"].get("text") == "Which feeds were late, and is one source "
                                                                            "responsible?"]
    assert g.nodes[uid]["props"]["allowed_scopes"] == []
    assert not visible(g, claims_for("head_data"), uid)


def test_knowledge_scopes(graph):
    nodes = {u: n["props"] for u, n in graph.nodes.items()}
    assert nodes["term:sla"]["allowed_scopes"] == ["*"]                       # not source-bound
    assert nodes["term:nav break"]["name"] == "NAV break"                      # display name authored, key casefolded
    assert "cashrecon" not in nodes["concept:Security"]["allowed_scopes"]
    assert nodes["concept:Security"]["allowed_scopes"] == sorted(
        {"refmaster", "refmaster.securities", "marketmaster", "marketmaster.instruments"})
    cash_ops = claims_for("cash_ops_emea")
    assert not can_read(cash_ops, nodes["term:price conflict"])
    assert can_read(cash_ops, nodes["term:open break"])
    for q in graph.labelled("Question"):
        assert q["allowed_scopes"] and "*" not in q["allowed_scopes"]          # bound to the metrics it used
    assert readers("cashrecon", ["breaks", "match_groups"]) == ["cashrecon"]  # multi-table: source scope only


def test_build_graph_exclude_drops_metric_dimensions_and_edges(graph_embedder, graph):
    g = build_graph(graph_embedder, exclude=frozenset({"metric:open_breaks"}))
    assert "metric:open_breaks" not in g.nodes and not any(u.startswith("dim:open_breaks.") for u in g.nodes)
    assert all("metric:open_breaks" not in (a, b) for a, _, b, _ in g.edges)
    assert len(g.labelled("Metric")) == 21


# ------------------------------------------------------------------------------------------- loader guards
class _NoDriver:
    """Any use fails the test: proves validation happens before the database is touched."""

    def __getattr__(self, name):
        raise AssertionError(f"driver.{name} used")


BAD_NS = ["", "Prism", "a:b", "ns with space", "x}) DETACH DELETE n //", "t\n"]


@pytest.mark.parametrize("ns", BAD_NS)
def test_bad_namespace_is_rejected_before_any_driver_use(ns):
    with pytest.raises(ValueError, match="namespace"):
        uid_for(ns, "x")
    with pytest.raises(ValueError, match="namespace"):
        load(_NoDriver(), None, ns, graph=Graph())
    with pytest.raises(ValueError, match="namespace"):
        counts(_NoDriver(), ns)
    with pytest.raises(ValueError, match="namespace"):
        delete_ns(_NoDriver(), ns)


def test_uid_for_follows_the_configured_namespace(monkeypatch):
    assert uid_for(Settings().graph_ns, "x") == "x"
    assert uid_for("t1", "x") == "t1:x"
    monkeypatch.setenv("PRISM_GRAPH_NS", "alt")
    assert uid_for("alt", "x") == "x" and uid_for("prism", "x") == "prism:x"


def test_delete_ns_refuses_the_configured_namespace(monkeypatch):
    with pytest.raises(ValueError, match="refusing"):
        delete_ns(_NoDriver(), Settings().graph_ns)
    monkeypatch.setenv("PRISM_GRAPH_NS", "tguard")       # never point a real driver at this: it would delete
    with pytest.raises(ValueError, match="refusing"):
        delete_ns(_NoDriver(), "tguard")
    with pytest.raises(AssertionError, match="driver"):  # the explicit override passes the guard
        delete_ns(_NoDriver(), "tguard", allow_production=True)


@pytest.mark.parametrize("label", ["Bad Label", "x", "Ctx`) DETACH DELETE n //", "A-B", ""])
def test_bad_node_label_is_rejected(label):
    with pytest.raises(ValueError, match="label"):
        Graph().node("n1", [label], allowed_scopes=[])
    g = Graph()
    g.nodes["n1"] = {"labels": [label], "props": {"allowed_scopes": []}}  # bypassing Graph.node
    with pytest.raises(ValueError, match="label"):
        load(_NoDriver(), None, "tlabel", graph=g, schema=False)


@pytest.mark.parametrize("typ", ["bad type", "HAS_TABLE]->() DETACH DELETE a //", "has_table", ""])
def test_bad_relationship_type_is_rejected(typ):
    with pytest.raises(ValueError, match="relationship type"):
        Graph().edge("a", typ, "b")
    g = Graph()
    g.node("a", ["Source"], allowed_scopes=[])
    g.node("b", ["Table"], allowed_scopes=[])
    g.edges.append(("a", typ, "b", {}))  # bypassing Graph.edge
    with pytest.raises(ValueError, match="relationship type"):
        load(_NoDriver(), None, "tlabel", graph=g, schema=False)


# ------------------------------------------------------------------------------------------- Neo4j
@pytest.mark.neo4j
def test_schema_is_idempotent_and_configured(neo4j_driver):
    def snapshot():
        idx = {r["name"]: r for r in _rows(neo4j_driver, "SHOW INDEXES YIELD name, type, state, options, "
                                                         "labelsOrTypes, properties")}
        cons = {r["name"]: r for r in _rows(neo4j_driver, "SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, "
                                                          "properties")}
        return idx, cons

    create_schema(neo4j_driver)
    first = snapshot()
    create_schema(neo4j_driver)
    idx, cons = snapshot()
    assert (idx, cons) == first
    vec = idx["ctx_vec"]["options"]["indexConfig"]
    assert idx["ctx_vec"]["state"] == "ONLINE" and idx["ctx_vec"]["labelsOrTypes"] == ["Searchable"]
    assert vec["vector.dimensions"] == DIM and vec["vector.similarity_function"] == "COSINE"
    assert vec["vector.quantization.enabled"] is False
    assert idx["ctx_text"]["options"]["indexConfig"]["fulltext.analyzer"] == "english"
    assert idx["ctx_text"]["properties"] == ["name", "description", "synonyms"]
    assert cons["ctx_uid"]["type"] == "UNIQUENESS"
    for name, label, props in NATURAL_KEYS:
        assert cons[name]["labelsOrTypes"] == [label] and cons[name]["properties"] == list(props)
        assert "ns" in props


@pytest.mark.neo4j
def test_real_graph_loaded(neo4j_driver, context_graph, graph):
    c = counts(neo4j_driver, TEST_GRAPH_NS)
    assert c["nodes"] == len(graph.nodes) == context_graph.nodes
    assert c["rels"] == len(graph.edges) == context_graph.rels
    assert c["by_label"]["Metric"] == 22 and c["by_label"]["Table"] == 38 and c["by_label"]["Role"] == 5
    assert c["by_label"]["Concept"] == 10 and c["by_label"]["Question"] == 12
    bad = _rows(neo4j_driver, "MATCH (n:Ctx {ns: 'prism_test'}) WHERE n.uid IS NULL OR n.loaded_version IS NULL "
                              "OR n.allowed_scopes IS NULL RETURN n.uid AS uid LIMIT 5")
    assert bad == []
    (row,) = _rows(neo4j_driver, "MATCH (c:Column {ns: 'prism_test', local_uid: 'column:cashrecon.cash_accounts.nostro_no'}) "
                                 "RETURN c.masked AS masked, size(c.embedding) AS dim")
    assert row == {"masked": True, "dim": DIM}
    grants = _rows(neo4j_driver, "MATCH (r:Role {ns: 'prism_test', persona_id: 'invest_ops_growth'})-[g:CAN_READ]->(o) "
                                 "RETURN o.local_uid AS o, g.scope AS scope, g.row_scope AS rows ORDER BY o")
    assert [g["o"] for g in grants] == ["source:assetrecon", "source:feedhub", "table:refmaster.legal_entities",
                                        "table:refmaster.securities"]
    assert all('"fund_group": ["Growth"]' in g["rows"] for g in grants)
    (bi,) = _rows(neo4j_driver, "MATCH (r:Role {ns: 'prism_test', persona_id: 'bi_analyst'}) RETURN r.metrics_only AS m")
    assert bi["m"] is True


@pytest.mark.neo4j
def test_metric_id_set_is_the_union_of_sql_and_rest_metrics(neo4j_driver, context_graph):
    ids = set(_graph_metrics(neo4j_driver))
    assert ids == _sql_ids() | _rest_ids() and len(ids) == 22


@pytest.mark.neo4j
def test_load_twice_gives_identical_counts(neo4j_driver, graph_embedder, context_graph, graph):
    before = counts(neo4j_driver, TEST_GRAPH_NS)
    report = load_test_graph(neo4j_driver, graph_embedder, graph=graph)
    assert counts(neo4j_driver, TEST_GRAPH_NS) == before
    assert report.deleted == {"nodes": 0, "rels": 0}
    assert report.version > context_graph.version
    (v,) = _rows(neo4j_driver, "MATCH (n:Ctx {ns: 'prism_test'}) RETURN min(n.loaded_version) AS lo, "
                               "max(n.loaded_version) AS hi")
    assert v["lo"] == v["hi"] == report.version


@pytest.mark.neo4j
def test_stale_objects_are_removed_on_reload(neo4j_driver, graph_embedder, context_graph, graph, scratch_ns):
    prism_before = counts(neo4j_driver, TEST_GRAPH_NS)
    load(neo4j_driver, graph_embedder, scratch_ns, graph=graph)
    full = counts(neo4j_driver, scratch_ns)
    assert full["by_label"]["Metric"] == 22
    smaller = build_graph(graph_embedder, exclude=frozenset({"metric:open_breaks"}))
    report = load(neo4j_driver, graph_embedder, scratch_ns, graph=smaller)
    after = counts(neo4j_driver, scratch_ns)
    n_dims = len([u for u in graph.nodes if u.startswith("dim:open_breaks.")])
    assert n_dims > 0 and report.deleted["nodes"] == 1 + n_dims
    assert after["by_label"]["Metric"] == 21
    assert after["by_label"]["Dimension"] == full["by_label"]["Dimension"] - n_dims
    assert after["nodes"] == full["nodes"] - 1 - n_dims and after["rels"] == len(smaller.edges)
    assert {k: v for k, v in after["by_label"].items() if k not in ("Metric", "Dimension")} == \
        {k: v for k, v in full["by_label"].items() if k not in ("Metric", "Dimension")}
    gone = _rows(neo4j_driver, "MATCH (n:Ctx {uid: $u}) RETURN n", u=uid_for(scratch_ns, "metric:open_breaks"))
    assert gone == []
    (t,) = _rows(neo4j_driver, "MATCH (t:BusinessTerm {uid: $u}) RETURN COUNT { (t)-[:DEFINES]->() } AS n",
                 u=uid_for(scratch_ns, "term:open break"))
    assert t["n"] == 0                                                   # the term survives, its edge does not
    assert counts(neo4j_driver, TEST_GRAPH_NS) == prism_before          # the session graph is untouched


@pytest.mark.neo4j
def test_scoped_namespace_load_never_touches_prism(neo4j_driver, graph_embedder, context_graph, graph):
    def fingerprint():
        (r,) = _rows(neo4j_driver, "MATCH (n:Ctx {ns: $ns}) RETURN count(n) AS n, max(n.loaded_version) AS v, "
                                   "collect(n.uid)[0..5] AS sample", ns=TEST_GRAPH_NS)
        return r, counts(neo4j_driver, TEST_GRAPH_NS)

    before = fingerprint()
    ns = "tscope-" + str(before[0]["v"])
    try:
        report = load(neo4j_driver, graph_embedder, ns, graph=graph)
        assert counts(neo4j_driver, ns)["nodes"] == report.nodes
        assert fingerprint() == before
        uids = _rows(neo4j_driver, "MATCH (n:Ctx {ns: $ns}) WHERE NOT n.uid STARTS WITH $p RETURN n.uid AS u LIMIT 1",
                     ns=ns, p=f"{ns}:")
        assert uids == []
    finally:
        delete_ns(neo4j_driver, ns)
    assert counts(neo4j_driver, ns)["nodes"] == 0 and fingerprint() == before
    with pytest.raises(ValueError):
        delete_ns(neo4j_driver, production_ns())


@pytest.mark.neo4j
def test_load_never_adopts_a_node_of_another_namespace(neo4j_driver, scratch_ns):
    """MERGE keys on {uid, ns}: a same-uid node owned by another namespace is a hard error, not silently taken over."""
    other = scratch_ns + "-other"
    g = Graph()
    g.node("source:x", ["Source"], name="x", allowed_scopes=["x"])
    uid = uid_for(scratch_ns, "source:x")
    neo4j_driver.execute_query("CREATE (:Ctx:Source {uid: $u, ns: $o, name: 'foreign', loaded_version: 1})",
                               u=uid, o=other)
    try:
        with pytest.raises(ConstraintError):
            load(neo4j_driver, None, scratch_ns, graph=g)
        (row,) = _rows(neo4j_driver, "MATCH (n:Ctx {uid: $u}) RETURN n.ns AS ns, n.name AS name", u=uid)
        assert row == {"ns": other, "name": "foreign"}
    finally:
        delete_ns(neo4j_driver, other)


# ------------------------------------------------------------------------------------------- describe consistency
def _assert_describe_matches_graph(source: str, described: list[dict], graph_metrics: dict[str, dict]) -> None:
    mine = {i: m for i, m in graph_metrics.items() if m["source"] == source}
    assert {m["id"] for m in described} == set(mine), source
    for d in described:
        g = mine[d["id"]]
        assert d["dimensions"] == g["dimensions"], d["id"]
        assert d["sensitive_dimensions"] == g["sensitive_dimensions"], d["id"]
        assert d["required_dimensions"] == g["required_dimensions"], d["id"]
        assert d["unit"] == g.get("unit") and d["description"] == g["definition"], d["id"]
        assert [f"{k}:{t}" for k, t in d["filters"].items()] == g["filters"], d["id"]


@pytest.mark.db
@pytest.mark.neo4j
@pytest.mark.parametrize("source", ALL_SOURCES)
async def test_graph_metrics_match_in_process_describe_for_head_data(seeded, neo4j_driver, context_graph, source):
    backend = create_backend(source, seeded)
    try:
        result = await backend.describe(claims_for("head_data"))
    finally:
        await backend.aclose()
    _assert_describe_matches_graph(source, result.metrics, _graph_metrics(neo4j_driver))


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.mark.live
@pytest.mark.neo4j
@pytest.mark.parametrize("source", ALL_SOURCES)
async def test_graph_metrics_match_live_describe_for_head_data(neo4j_driver, context_graph, source):
    port = MCP_PORTS[source]
    if not _port_open(port):
        pytest.skip(f"{source} MCP server not running on 127.0.0.1:{port} (scripts/start_backend.sh)")
    settings = Settings()
    token = mint(claims_for("head_data"), f"{source}-mcp", settings.jwt_secret.get_secret_value(), ttl_s=60)
    result = await call_tool(f"http://127.0.0.1:{port}/mcp", token, "describe")
    assert not result.is_error, result
    _assert_describe_matches_graph(source, result.structured_content["metrics"], _graph_metrics(neo4j_driver))


@pytest.mark.neo4j
def test_cli_counts(neo4j_driver, context_graph, capsys):
    from prism.graph.cli import main

    assert main(["counts", "--ns", TEST_GRAPH_NS]) == 0
    assert json.loads(capsys.readouterr().out) == counts(neo4j_driver, TEST_GRAPH_NS)


def test_cli_reports_unreachable_neo4j(monkeypatch, capsys):
    from prism.graph.cli import main

    monkeypatch.setenv("PRISM_NEO4J_URI", "bolt://127.0.0.1:17999")
    assert main(["counts"]) == 2
    assert "Neo4j not reachable" in capsys.readouterr().err
