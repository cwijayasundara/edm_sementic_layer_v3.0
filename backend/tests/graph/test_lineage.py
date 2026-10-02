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
