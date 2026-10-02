"""Trace storage: owner-bound, re-gated on read, outside the catalog. Neo4j session graph in ns `prism_test`."""
import pytest
from neo4j import AsyncGraphDatabase

from prism.config import Settings

from prism.graph.catalog import load_catalog
from prism.graph.lineage import lineage
from prism.graph.retrieval import context_pack
from prism.graph.traces import (TraceOwned, aget_trace, amark_confirmed, arecord_trace, get_trace, mark_confirmed,
                                record_trace)
from prism.security.personas import claims_for
from tests.graph_ns import TEST_GRAPH_NS, load_test_graph

NOW, DAY = 1_790_000_000, 86_400


def trace(run_id="a" * 32, sub="steward", **kw):
    return {"run_id": run_id, "sub": sub, "question": "Which vendor drives price conflicts?", "answer": "Vendor A.",
            "path": "metric", "status": "ok", "answered": ["metric:price_conflicts"],
            "steps": [
                {"seq": 0, "parent": None, "kind": "context", "label": "Searched the context graph", "note": "Look up",
                 "considered": ["price_conflicts"], "ms": 12, "status": "ok", "error_code": None,
                 "tool": "search_context", "args_json": '{"question": "q"}', "handle": None, "rows": None,
                 "truncated": None, "touched": []},
                {"seq": 1, "parent": None, "kind": "metric", "label": "Ran metric price_conflicts by vendor_id",
                 "note": None, "considered": [], "ms": 40, "status": "ok", "error_code": None, "tool": "run_metric",
                 "args_json": '{"metric_id": "price_conflicts"}', "handle": "r_aaaaaaaaaaaa", "rows": 4,
                 "truncated": False,
                 "touched": ["metric:price_conflicts", "dim:price_conflicts.vendor_id", "source:marketmaster"]},
                {"seq": 2, "parent": None, "kind": "answer", "label": "Answered", "note": None, "considered": [],
                 "ms": None, "status": "ok", "error_code": None, "tool": None, "args_json": None, "handle": None,
                 "rows": None, "truncated": None, "touched": []},
            ], **kw}


@pytest.fixture
def db(neo4j_driver, context_graph):
    neo4j_driver.execute_query("MATCH (n) WHERE (n:Trace OR n:TraceStep OR n:ToolCall) AND n.ns = $ns DETACH DELETE n",
                               ns=TEST_GRAPH_NS)
    return neo4j_driver


def put(db, t=None, who="steward", now=NOW, retention=7 * DAY):
    return record_trace(db, t or trace(), claims_for(who), ns=TEST_GRAPH_NS, now=now, retention_s=retention,
                        timeout_s=5.0)


def get(db, run_id="a" * 32, who="steward", now=NOW):
    return get_trace(db, run_id, claims_for(who) if isinstance(who, str) else who, ns=TEST_GRAPH_NS, now=now,
                     timeout_s=5.0)


@pytest.mark.neo4j
def test_owner_round_trip_with_links(db):
    assert put(db) == 3
    t = get(db)
    assert (t["run_id"], t["question"], t["answer"], t["confirmed"]) == ("a" * 32, trace()["question"], "Vendor A.", False)
    assert [s["seq"] for s in t["steps"]] == [0, 1, 2]
    step = t["steps"][1]
    assert step["tool"] == "run_metric" and step["rows"] == 4 and step["handle"] == "r_aaaaaaaaaaaa"
    assert {x["id"] for x in step["touched"]} == {"metric:price_conflicts", "dim:price_conflicts.vendor_id",
                                                  "source:marketmaster"}
    assert all(set(x) == {"id", "kind", "label"} for x in step["touched"])
    assert t["steps"][0]["considered"] == ["price_conflicts"] and t["steps"][0]["touched"] == []


@pytest.mark.neo4j
def test_other_callers_and_expired_runs_read_as_missing(db):
    put(db)
    assert get(db, who="head_data") is None
    assert get(db, run_id="b" * 32) is None
    assert get(db, now=NOW + 8 * DAY) is None


@pytest.mark.neo4j
def test_record_refuses_another_callers_run_id_and_replaces_own(db):
    put(db)
    with pytest.raises(TraceOwned):
        put(db, trace(sub="head_data"), who="head_data")
    put(db, trace(answer="Vendor B."))
    assert get(db)["answer"] == "Vendor B."
    assert db.execute_query("MATCH (t:Trace {ns: $ns}) RETURN count(t) AS c", ns=TEST_GRAPH_NS).records[0]["c"] == 1


@pytest.mark.neo4j
def test_links_only_to_visible_catalog_nodes(db):
    t = trace(sub="cash_ops_emea")
    t["steps"][1]["touched"] = ["metric:price_conflicts", "metric:open_breaks", "metric:no_such_metric"]
    put(db, t, who="cash_ops_emea")
    touched = {x["id"] for x in get(db, who="cash_ops_emea")["steps"][1]["touched"]}
    assert touched == {"metric:open_breaks"}   # marketmaster is out of scope; unknown ids link nothing
    stored = db.execute_query("MATCH (:TraceStep {ns: $ns})-[:TOUCHED]->(n) RETURN collect(n.local_uid) AS u",
                              ns=TEST_GRAPH_NS).records[0]["u"]
    assert stored == ["metric:open_breaks"]    # the write-side gate, not just the read re-gate


def answered_with(db):
    return db.execute_query("MATCH (:Trace {ns: $ns})-[:ANSWERED_WITH]->(n) RETURN collect(n.local_uid) AS u",
                            ns=TEST_GRAPH_NS).records[0]["u"]


@pytest.mark.neo4j
def test_answered_with_links_only_visible_metrics(db):
    put(db, trace(sub="cash_ops_emea", answered=["metric:price_conflicts", "metric:no_such_metric"]),
        who="cash_ops_emea")
    assert answered_with(db) == []
    put(db, trace(sub="cash_ops_emea", answered=["metric:price_conflicts", "metric:open_breaks"]), who="cash_ops_emea")
    assert answered_with(db) == ["metric:open_breaks"]


@pytest.mark.neo4j
def test_get_trace_regates_touched_nodes(db):
    put(db)
    narrowed = {**claims_for("steward"), "scopes": ["refmaster"]}
    assert get(db, who=narrowed)["steps"][1]["touched"] == []


@pytest.mark.neo4j
def test_expired_traces_of_the_caller_are_deleted_on_write(db):
    put(db, now=NOW - 8 * DAY)
    put(db, trace(run_id="c" * 32))
    left = db.execute_query("MATCH (t:Trace {ns: $ns}) RETURN collect(t.run_id) AS ids", ns=TEST_GRAPH_NS)
    assert left.records[0]["ids"] == ["c" * 32]
    assert db.execute_query("MATCH (n:TraceStep {ns: $ns}) RETURN count(n) AS c",
                            ns=TEST_GRAPH_NS).records[0]["c"] == 3


@pytest.mark.neo4j
def test_purge_deletes_any_expired_trace_but_never_anothers_live_one(db):
    put(db, trace(run_id="d" * 32, sub="head_data"), who="head_data", now=NOW - 8 * DAY)   # someone else's, expired
    put(db, trace(run_id="a" * 32, sub="head_data"), who="head_data")                       # someone else's, live
    put(db, trace(run_id="e" * 32))                                                          # mine, unexpired
    put(db, trace(run_id="f" * 32), now=NOW - 8 * DAY)                                       # mine, expired
    put(db, trace(run_id="c" * 32))                                                          # the write that purges
    ids = db.execute_query("MATCH (t:Trace {ns: $ns}) RETURN collect(t.run_id) AS ids", ns=TEST_GRAPH_NS)
    assert sorted(ids.records[0]["ids"]) == sorted(["a" * 32, "e" * 32, "c" * 32])


@pytest.mark.neo4j
def test_sub_comes_from_the_claims_not_the_payload(db):
    put(db, trace(sub="head_data"), who="steward")
    assert get(db) is not None
    assert get(db, who="head_data") is None


@pytest.mark.neo4j
async def test_async_twins_round_trip(db):
    s = Settings()
    async with AsyncGraphDatabase.driver(s.neo4j_uri, auth=s.neo4j_auth(), notifications_min_severity="OFF") as ad:
        kw = {"ns": TEST_GRAPH_NS, "now": NOW, "timeout_s": 5.0}
        steward = claims_for("steward")
        assert await arecord_trace(ad, trace(), steward, retention_s=7 * DAY, **kw) == 3
        with pytest.raises(TraceOwned):
            await arecord_trace(ad, trace(sub="head_data"), claims_for("head_data"), retention_s=7 * DAY, **kw)
        t = await aget_trace(ad, "a" * 32, steward, **kw)
        assert t["answer"] == "Vendor A." and t["confirmed"] is False and len(t["steps"]) == 3
        assert await aget_trace(ad, "a" * 32, claims_for("head_data"), **kw) is None
        assert await amark_confirmed(ad, "a" * 32, "head_data", **kw) is False
        assert await amark_confirmed(ad, "a" * 32, "steward", **kw) is True
        assert (await aget_trace(ad, "a" * 32, steward, **kw))["confirmed"] is True


@pytest.mark.neo4j
def test_mark_confirmed_is_owner_only(db):
    put(db)
    assert mark_confirmed(db, "a" * 32, "head_data", ns=TEST_GRAPH_NS, now=NOW, timeout_s=5.0) is False
    assert mark_confirmed(db, "a" * 32, "steward", ns=TEST_GRAPH_NS, now=NOW, timeout_s=5.0) is True
    assert get(db)["confirmed"] is True


@pytest.mark.neo4j
def test_traces_survive_a_graph_load_and_stay_out_of_the_catalog(db, graph_embedder):
    put(db)
    load_test_graph(db, graph_embedder)
    assert get(db)["steps"][1]["touched"]                       # links to still-existing nodes survive
    labels = db.execute_query("MATCH (n:Ctx {ns: $ns}) WHERE n:Trace OR n:TraceStep OR n:ToolCall RETURN count(n) AS c",
                              ns=TEST_GRAPH_NS).records[0]["c"]
    assert labels == 0
    pack = context_pack("Which vendor drives price conflicts?", claims_for("steward"), driver=db,
                        embedder=graph_embedder, ns=TEST_GRAPH_NS)
    assert "Vendor A." not in str(pack) and "TraceStep" not in str(pack)
    g = lineage(db, {"price_conflicts": ["vendor_id"]}, [], claims_for("steward"), ns=TEST_GRAPH_NS)
    assert all(not n["id"].startswith(("trace", "step")) for n in g["nodes"])
    assert "a" * 32 not in str(load_catalog(db, TEST_GRAPH_NS).__dict__)


def test_trace_constraint_is_in_the_schema():
    from prism.graph.schema import SCHEMA
    from prism.graph.traces import TRACE_CONSTRAINT
    assert TRACE_CONSTRAINT in SCHEMA
