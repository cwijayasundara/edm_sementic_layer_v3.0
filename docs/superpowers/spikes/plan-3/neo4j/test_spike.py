import json
import time

import neo4j
import pytest
from neo4j import AsyncGraphDatabase

from catalog import load_catalog
from loader import anchor_vec, counts, fake_embed, load
from prism.security.personas import PERSONAS
from retrieval import asearch_context, context_pack, join_paths, lucene_query, pack_size, search_context
from schema import AUTH, URI

FOREIGN_TO_CASH = ("refmaster", "marketmaster", "assetrecon")
QUESTIONS = [
    "security price conflicts by issuer country",
    "how many open breaks on nostro accounts in EMEA by currency",
    "which custodian feeds were late yesterday",
    "open position exceptions market value for Growth funds",
    "data quality exceptions on bonds by rule",
]


def scopes(pid):
    return PERSONAS[pid].scopes


def pack_for(driver, q, pid, **kw):
    p = PERSONAS[pid]
    return context_pack(driver, q, p.scopes, metrics_only=p.metrics_only, **kw)


# ---------------------------------------------------------------------------------------- Q1 features
def test_indexes_and_constraints_online(driver, graph):
    idx = {r["name"]: dict(r) for r in driver.execute_query(
        "SHOW INDEXES YIELD name, type, state, options").records}
    assert idx["ctx_vec"]["type"] == "VECTOR" and idx["ctx_vec"]["state"] == "ONLINE"
    assert idx["ctx_vec"]["options"]["indexConfig"]["vector.dimensions"] == 384
    assert idx["ctx_vec"]["options"]["indexConfig"]["vector.similarity_function"] == "COSINE"
    assert idx["ctx_text"]["type"] == "FULLTEXT" and idx["ctx_text"]["state"] == "ONLINE"
    cons = {r["name"]: r["type"] for r in driver.execute_query("SHOW CONSTRAINTS YIELD name, type").records}
    assert cons["ctx_uid"] == "UNIQUENESS"
    with pytest.raises(neo4j.exceptions.ConstraintError):
        driver.execute_query("CREATE (:Ctx {uid: 'metric:open_breaks'})")


# ---------------------------------------------------------------------------------------- Q2 loader
def test_real_inputs_loaded(driver, graph):
    c = counts(driver)
    assert c["by_label"]["Metric"] == 22
    assert c["by_label"]["BusinessTerm"] == 25 and c["by_label"]["Concept"] == 10
    assert c["by_label"]["Question"] == 6 and c["by_label"]["Role"] == 5 and c["by_label"]["Table"] == 38
    n = driver.execute_query("MATCH (:Column)-[r:SAME_KEY_AS]->(:Column) RETURN count(r) AS n").records[0]["n"]
    assert n == 11


def test_loader_idempotent(driver, graph):
    before = counts(driver)
    st = load(driver, ns="prism")
    after = counts(driver)
    assert before == after
    assert st["stale_nodes_deleted"] == 0 and st["stale_rels_deleted"] == 0


def test_stale_objects_deleted_on_reload(driver, graph, run_ns):
    load(driver, ns=run_ns)
    full = counts(driver, run_ns)
    assert full["by_label"]["Metric"] == 22
    st = load(driver, ns=run_ns, exclude=frozenset({"metric:open_breaks"}))
    after = counts(driver, run_ns)
    assert after["by_label"]["Metric"] == 21
    assert after["by_label"]["Dimension"] == full["by_label"]["Dimension"] - 4       # its 4 dimensions
    assert st["stale_nodes_deleted"] == 5
    # the 'Open break' term survives but its DEFINES edge to the removed metric is gone
    r = driver.execute_query("MATCH (t:BusinessTerm {uid: $u}) RETURN COUNT { (t)-[:DEFINES]->() } AS n",
                             u=f"{run_ns}:term:open_break").records[0]
    assert r["n"] == 0
    assert counts(driver)["by_label"]["Metric"] == 22                              # real namespace untouched


def test_namespace_isolation_vs_global_index(driver, graph, run_ns):
    """Evidence for the isolation recommendation: indexes are global, so a raw vector query sees test nodes;
    the ns filter inside retrieval removes them."""
    load(driver, ns=run_ns)
    raw = driver.execute_query("CALL db.index.vector.queryNodes('ctx_vec', 10, $v) YIELD node RETURN node.ns AS ns",
                               v=anchor_vec(1)).records
    assert {r["ns"] for r in raw} == {"prism", run_ns}                                # pollution in raw index
    hits = search_context(driver, anchor_vec(1), "price conflict", scopes("steward"), 10)
    assert hits and all(":" in h["uid"] and not h["uid"].startswith(run_ns) for h in hits)


# ---------------------------------------------------------------------------------------- Q3 retrieval
def test_anchor_vectors_give_predictable_neighbours(driver, graph):
    hits = search_context(driver, anchor_vec(1), "", scopes("steward"), 5)
    assert {h["uid"] for h in hits[:2]} == {"metric:price_conflicts", "term:price_conflict"}
    assert all(h["text_rank"] is None for h in hits)                                  # vector path only
    hits = search_context(driver, anchor_vec(0), "", scopes("cash_ops_emea"), 5)
    assert {h["uid"] for h in hits[:2]} == {"term:nostro", "column:cashrecon.cash_accounts.nostro_no"}


@pytest.mark.parametrize("q", QUESTIONS + ["refmaster securities isin marketmaster instruments golden copy prices",
                                           "positions held by growth portfolios at the custodian"])
def test_cash_ops_never_sees_foreign_sources(driver, graph, q):
    pack = pack_for(driver, q, "cash_ops_emea")
    body = json.dumps({k: v for k, v in pack.items() if k != "question"})
    for src in FOREIGN_TO_CASH:
        assert src not in body, (src, body)
    assert "metric:price_conflicts" not in body and "Golden copy" not in body


def test_cash_ops_anchor_vector_for_foreign_metric_is_pruned(driver, graph):
    hits = search_context(driver, anchor_vec(1), "price conflicts golden copy vendor", scopes("cash_ops_emea"), 10)
    assert all(h["source"] in (None, "cashrecon", "feedhub") for h in hits)
    assert not any(h["uid"] in ("metric:price_conflicts", "term:price_conflict", "term:golden_copy") for h in hits)


def test_table_level_scopes(driver, graph):
    q = "data quality exceptions on securities by rule and issuer"
    inv = pack_for(driver, q, "invest_ops_growth", k=15)
    stw = pack_for(driver, q, "steward", k=15)
    inv_body = json.dumps(inv)
    assert "open_dq_exceptions" not in inv_body and "refmaster.exceptions" not in inv_body
    assert "refmaster.dq_rules" not in inv_body
    assert "open_dq_exceptions" in json.dumps(stw)
    hits = search_context(driver, fake_embed("securities isin issuer"), "securities isin issuer",
                          scopes("invest_ops_growth"), 15)
    assert any(h["uid"].startswith("column:refmaster.securities.") for h in hits)
    assert all(not h["uid"].startswith(("column:refmaster.accounts", "endpoint:refmaster.exceptions")) for h in hits)


def test_persona_without_matching_scope_gets_empty_pack(driver, graph):
    pack = context_pack(driver, "open breaks by region", ("nosuchsource",))
    assert pack["hits"] == [] and set(pack) == {"question", "hits"}


def test_sensitive_dimensions_excluded_for_metrics_only(driver, graph):
    q = "manual matches by operator and auto match rate"
    bi = pack_for(driver, q, "bi_analyst")
    head = pack_for(driver, q, "head_data")
    bi_m = {m["id"]: m for m in bi["metrics"]}
    head_m = {m["id"]: m for m in head["metrics"]}
    assert "manual_matches" in bi_m and "matched_by" not in bi_m["manual_matches"]["dims"]
    assert "matched_by" in head_m["manual_matches"]["dims"]
    assert "columns" not in bi and "join_paths" not in bi                       # metrics-only: no physical schema


def test_join_path_issuer_country(driver, graph):
    pairs = [("metric:price_conflicts", "column:refmaster.legal_entities.country")]
    (p,) = join_paths(driver, pairs, scopes("steward"))
    assert "refmaster.securities.security_id = marketmaster.instruments.security_id" in p["joins"]
    assert "refmaster.securities.issuer_entity_id = refmaster.legal_entities.entity_id" in p["joins"]
    assert p["nodes"][:2] == ["metric:price_conflicts", "table:marketmaster.price_suspects"]
    assert p["nodes"][-2:] == ["table:refmaster.legal_entities", "column:refmaster.legal_entities.country"]
    assert join_paths(driver, pairs, scopes("cash_ops_emea")) == []


def test_join_path_is_role_pruned_inside_the_pattern(driver, graph):
    pair = [("table:assetrecon.recon_exceptions", "column:refmaster.legal_entities.country")]
    (p,) = join_paths(driver, pair, scopes("invest_ops_growth"))                  # via refmaster.securities
    assert "table:refmaster.securities" in p["nodes"]
    assert join_paths(driver, pair, ("assetrecon", "refmaster.legal_entities")) == []   # securities not readable


def test_lucene_special_characters(driver, graph):
    q = 'price AND OR ( ) : " conflicts NOT \\ ~ * ? [ ] { } ^ / && ||'
    assert lucene_query(q) == "price OR and OR or OR conflicts OR not"
    hits = search_context(driver, fake_embed(q), q, scopes("steward"), 5)
    assert hits
    with pytest.raises(neo4j.exceptions.ClientError) as ei:                     # unsanitised input crashes
        driver.execute_query("CALL db.index.fulltext.queryNodes('ctx_text', $q)", q='AND OR ( ) : "')
    assert "ProcedureCallFailed" in ei.value.code
    assert search_context(driver, fake_embed("?!"), "?! ( )", scopes("steward"), 5) is not None   # no tokens


def test_fulltext_synonyms_and_snake_case(driver, graph):
    def ft(q):
        return [r["uid"] for r in driver.execute_query(
            "CALL db.index.fulltext.queryNodes('ctx_text', $q) YIELD node WHERE node.ns = 'prism' "
            "RETURN node.uid AS uid LIMIT 5", q=q).records]
    assert ft('"cash account"')[0] == "term:nostro"                 # synonym list property is indexed
    assert "term:nostro" in ft("NOSTRO")                            # analyzer lower-cases
    assert "column:cashrecon.cash_accounts.nostro_no" in ft("nostro")  # snake_case split via description text
    assert "column:cashrecon.cash_accounts.nostro_no" not in ft("name:nostro")  # 'nostro_no' is ONE token in name


@pytest.mark.parametrize("pid", ["steward", "cash_ops_emea", "invest_ops_growth", "bi_analyst", "head_data"])
def test_pack_size_budget(driver, graph, pid):
    for q in QUESTIONS:
        size = pack_size(pack_for(driver, q, pid))
        assert size["est_tokens"] <= 3000, (q, size)


def test_question_history_filtered_by_used_objects(driver, graph):
    hits = search_context(driver, fake_embed("Security price conflicts by issuer country"),
                          "Security price conflicts by issuer country", scopes("invest_ops_growth"), 10)
    assert "question:6" not in {h["uid"] for h in hits}           # USED marketmaster objects
    hits = search_context(driver, fake_embed("Security price conflicts by issuer country"),
                          "Security price conflicts by issuer country", scopes("steward"), 10)
    assert hits[0]["uid"] == "question:6"


# ---------------------------------------------------------------------------------------- Q4 catalog
def test_catalog_single_query(driver, graph):
    c = load_catalog(driver)
    assert len(c["metrics"]) == 22 and set(c["roles"]) == set(PERSONAS)
    m = c["metrics"]["manual_matches"]
    assert m["sensitive"] == ["matched_by"] and m["server"] == "cashrecon-mcp" and m["tables"] == ["cashrecon.match_groups"]
    assert c["metrics"]["price_conflicts"]["endpoint"] == "prices_conflicts_summary"
    assert c["elapsed_ms"] < 100


# ---------------------------------------------------------------------------------------- async + failure modes
async def test_async_driver_search(graph):
    async with AsyncGraphDatabase.driver(URI, auth=AUTH, notifications_min_severity="OFF") as ad:
        hits = await asearch_context(ad, anchor_vec(1), "price conflict", scopes("steward"), 5)
        assert hits[0]["uid"] in ("metric:price_conflicts", "term:price_conflict")


def test_unreachable_server_fails_fast():
    t = time.perf_counter()
    with neo4j.GraphDatabase.driver("bolt://127.0.0.1:17999", auth=AUTH, connection_timeout=2.0) as d:
        with pytest.raises(neo4j.exceptions.ServiceUnavailable):
            d.verify_connectivity()
    assert time.perf_counter() - t < 3


def test_wrong_vector_dimension_is_rejected(driver, graph):
    with pytest.raises(neo4j.exceptions.ClientError) as ei:
        driver.execute_query("CALL db.index.vector.queryNodes('ctx_vec', 5, $v)", v=[0.1] * 10)
    assert "dimension" in ei.value.message.lower()
