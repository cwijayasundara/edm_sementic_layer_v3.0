"""build_lineage: pure assembly of lineage rows into the UI graph (no Neo4j)."""
from prism.graph.lineage import DETAIL_MAX, RESULT_ID, build_lineage


def n(kind, uid, **kw):
    return {"kind": kind, "local_uid": uid, **kw}


M = n("Metric", "metric:open_breaks", name="open_breaks", source="cashrecon", definition="Open cash breaks")
D = n("Dimension", "dim:open_breaks.region", name="region", source=None)
C = n("Column", "column:cashrecon.breaks.region", name="region", table="breaks", source="cashrecon", type="text")
T = n("Table", "table:cashrecon.breaks", name="breaks", qualified_name="cashrecon.breaks", source="cashrecon")
S = n("Source", "source:cashrecon", name="cashrecon")


def test_nodes_and_edges_are_deduplicated_and_labelled():
    rows = [{"a": M, "t": None, "b": None}, {"a": M, "t": "HAS_DIMENSION", "b": D},
            {"a": D, "t": "ON_COLUMN", "b": C}, {"a": T, "t": "HAS_COLUMN", "b": C}, {"a": T, "t": "HAS_COLUMN", "b": C},
            {"a": S, "t": "HAS_TABLE", "b": T}, {"a": S, "t": "PROVIDES", "b": M}]
    g = build_lineage(rows)
    by_id = {x["id"]: x for x in g["nodes"]}
    assert set(by_id) == {"metric:open_breaks", "dim:open_breaks.region", "column:cashrecon.breaks.region",
                          "table:cashrecon.breaks", "source:cashrecon"}
    assert by_id["column:cashrecon.breaks.region"]["label"] == "breaks.region"
    assert by_id["table:cashrecon.breaks"]["label"] == "cashrecon.breaks"
    assert by_id["metric:open_breaks"] == {"id": "metric:open_breaks", "kind": "Metric", "label": "open_breaks",
                                           "source": "cashrecon", "detail": "Open cash breaks"}
    assert len(g["edges"]) == 5 and g["truncated"] is False
    assert {"from": "table:cashrecon.breaks", "to": "column:cashrecon.breaks.region", "type": "HAS_COLUMN"} in g["edges"]


def test_label_falls_back_to_local_uid():
    g = build_lineage([{"a": n("Column", "column:x"), "t": None, "b": None}])
    assert g["nodes"] == [{"id": "column:x", "kind": "Column", "label": "column:x"}]


def test_unknown_kinds_are_dropped_with_their_edges():
    role = n("Role", "role:steward", name="steward")
    g = build_lineage([{"a": role, "t": "CAN_READ", "b": S}])
    assert [x["id"] for x in g["nodes"]] == ["source:cashrecon"] and g["edges"] == []


def test_detail_is_capped():
    long = n("BusinessTerm", "term:break", name="break", definition="x" * 1000)
    (node,) = build_lineage([{"a": long, "t": None, "b": None}])["nodes"]
    assert len(node["detail"]) == DETAIL_MAX


def test_cap_keeps_priority_kinds_and_drops_dangling_edges():
    cols = [n("Column", f"column:c{i:03}", name=f"c{i}") for i in range(200)]
    rows = [{"a": M, "t": None, "b": None}, {"a": S, "t": "PROVIDES", "b": M}] + \
           [{"a": D, "t": "ON_COLUMN", "b": c} for c in cols]
    g = build_lineage(rows, max_nodes=10)
    kinds = [x["kind"] for x in g["nodes"]]
    assert g["truncated"] is True and len(kinds) == 10
    assert kinds[:3] == ["Metric", "Source", "Dimension"]
    ids = {x["id"] for x in g["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])


def test_combined_root_links_to_present_inputs_only():
    g = build_lineage([{"a": M, "t": None, "b": None}],
                      combined_inputs=["metric:open_breaks", "source:feedhub"])
    assert g["nodes"][0] == {"id": RESULT_ID, "kind": "Result", "label": "Combined result"}
    assert g["edges"] == [{"from": RESULT_ID, "to": "metric:open_breaks", "type": "COMBINES"}]


def test_combined_with_no_present_input_has_no_result_root():
    g = build_lineage([], combined_inputs=["metric:open_breaks"])
    assert g["nodes"] == [] and g["edges"] == []
    g = build_lineage([{"a": M, "t": None, "b": None}], combined_inputs=["source:feedhub"])
    assert [x["id"] for x in g["nodes"]] == ["metric:open_breaks"]


def test_dimension_inherits_its_metrics_source():
    g = build_lineage([{"a": M, "t": "HAS_DIMENSION", "b": D}])
    dim = next(x for x in g["nodes"] if x["kind"] == "Dimension")
    assert dim["source"] == "cashrecon"


# ----------------------------------------------------------------------------------------------- Neo4j-backed
import pytest  # noqa: E402

from prism.graph.knowledge import load_knowledge  # noqa: E402
from prism.graph.lineage import lineage  # noqa: E402
from prism.graph.loader import load  # noqa: E402
from prism.graph.model import build_graph, visible  # noqa: E402
from prism.security.personas import PERSONAS, claims_for  # noqa: E402
from tests.graph_ns import TEST_GRAPH_NS  # noqa: E402


@pytest.fixture(scope="module")
def graph(graph_embedder):
    return build_graph(graph_embedder)


@pytest.fixture(scope="module")
def run(neo4j_driver, context_graph):
    def _run(who, plans, sources=(), **kw):
        claims = claims_for(who) if isinstance(who, str) else who
        return lineage(neo4j_driver, plans, sources, claims, ns=TEST_GRAPH_NS, **kw)
    return _run


@pytest.mark.neo4j
def test_lineage_of_a_metric_reaches_source_table_and_dimension_columns(run):
    g = run("steward", {"price_conflicts": ["vendor_id"]})
    kinds = {x["kind"] for x in g["nodes"]}
    assert {"Metric", "Source", "Dimension", "Table"} <= kinds
    ids = {x["id"] for x in g["nodes"]}
    assert "metric:price_conflicts" in ids and "source:marketmaster" in ids
    assert {"from": "source:marketmaster", "to": "metric:price_conflicts", "type": "PROVIDES"} in g["edges"]
    dims = [x for x in g["nodes"] if x["kind"] == "Dimension"]
    assert [d["label"] for d in dims] == ["vendor_id"]          # only the dimensions the result used
    assert all(not x["id"].startswith("prism_test:") for x in g["nodes"])   # local_uid, never uid


@pytest.mark.neo4j
@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_every_lineage_node_is_visible_to_the_caller(run, graph, persona):
    """The Python twin of gate() agrees: nothing in a lineage graph is hidden from that caller elsewhere."""
    claims = claims_for(persona)
    for metric in ("price_conflicts", "open_breaks", "manual_matches", "open_position_exceptions"):
        dims = sorted(graph.nodes[f"metric:{metric}"]["props"].get("dimensions") or [])
        g = run(claims, {metric: dims})
        leaked = [x["id"] for x in g["nodes"] if x["id"] not in graph.nodes or not visible(graph, claims, x["id"])]
        assert leaked == [], (persona, metric, leaked)


@pytest.mark.neo4j
def test_lineage_metrics_only_hides_schema_and_sensitive_dimensions(run):
    g = run("bi_analyst", {"manual_matches": ["matched_by", "region"]})
    kinds = {x["kind"] for x in g["nodes"]}
    assert not kinds & {"Table", "Column", "Field", "Endpoint"}
    assert "matched_by" not in {x["label"] for x in g["nodes"]}
    assert "metric:manual_matches" in {x["id"] for x in g["nodes"]}
    head = run("head_data", {"manual_matches": ["matched_by"]})
    assert "matched_by" in {x["label"] for x in head["nodes"]}   # the control: it exists


@pytest.mark.neo4j
def test_lineage_of_a_foreign_metric_is_empty(run):
    assert run("cash_ops_emea", {"price_conflicts": ["vendor_id"]})["nodes"] == []


@pytest.mark.neo4j
def test_free_form_source_yields_only_its_visible_source(run):
    g = run("head_data", {}, ["cashrecon"])
    assert g["nodes"] == [{"id": "source:cashrecon", "kind": "Source", "label": "cashrecon"}]
    assert run("steward", {}, ["cashrecon"])["nodes"] == []


@pytest.mark.neo4j
def test_questions_are_capped_and_link_to_their_metric(run):
    g = run("steward", {"price_conflicts": []})
    qs = [x for x in g["nodes"] if x["kind"] == "Question"]
    assert 1 <= len(qs) <= 3
    assert all({"from": q["id"], "to": "metric:price_conflicts", "type": "ASKED_ABOUT"} in g["edges"] for q in qs)


@pytest.mark.neo4j
def test_cross_source_question_needs_every_used_object_visible(neo4j_driver, graph_embedder, scratch_ns):
    """A question that used a cashrecon AND a feedhub metric passes the union-scope check for a cashrecon-only
    caller, so only the gate's every-USED-object rule keeps it out of that caller's lineage."""
    k = load_knowledge()
    text = "Do late bank feeds explain the open cash breaks per region?"
    k.history.append(type(k.history[0])(question=text, metrics=["open_breaks", "late_feeds"],
                                        plan="run_metric(open_breaks) and run_metric(late_feeds); combine",
                                        status="verified"))
    load(neo4j_driver, graph_embedder, scratch_ns, graph=build_graph(graph_embedder, knowledge=k))

    def seen(scopes):
        g = lineage(neo4j_driver, {"open_breaks": []}, (), {"scopes": scopes}, ns=scratch_ns)
        return {x["id"] for x in g["nodes"]}, {x["label"] for x in g["nodes"] if x["kind"] == "Question"}

    ids, questions = seen(["cashrecon"])
    assert "metric:open_breaks" in ids                       # not vacuous
    assert text not in questions
    assert text in seen(["cashrecon", "feedhub"])[1]         # positive control
