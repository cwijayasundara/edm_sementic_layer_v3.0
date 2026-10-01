"""Role-filtered hybrid retrieval and the context pack, with real embeddings on the session graph (ns `prism_test`, loaded once
per session by `context_graph`). Tests that need extra graph content load their own `scratch_ns`."""
import asyncio
import json
import logging
import socket
import statistics
import threading
import time
from pathlib import Path

import pytest
import yaml
from neo4j import AsyncGraphDatabase, GraphDatabase

from prism.config import Settings
from prism.graph.knowledge import load_knowledge
from prism.graph.loader import load, uid_for
from prism.graph.model import Graph, build_graph, visible
from prism.graph.retrieval import (GRAPH_ERROR_MESSAGE, KIND_LIMITS, PACK_KEYS, GraphError, GraphUnavailable,
                                   acontext_pack, arun_read, asearch_context, context_pack, empty_pack, expand,
                                   fetch_size, gate, gate_params, lucene_query, pack_tokens, run_read, search_context,
                                   trim_pack)
from prism.security.personas import PERSONAS, claims_for
from tests.graph_ns import TEST_GRAPH_NS

EVAL = Path(__file__).with_name("eval_questions.yaml")
FOREIGN_TO_CASH_OPS = ("refmaster", "marketmaster", "assetrecon")
ISSUER_Q = "security price conflicts by issuer country"
GATE_CASES = [(p, claims_for(p)) for p in sorted(PERSONAS)] + [
    ("feedhub-only", {"scopes": ["feedhub"]}),
    ("feedhub-metrics-only", {"scopes": ["feedhub"], "metrics_only": True}),
    ("securities-table", {"scopes": ["refmaster.securities"]}),
    ("legal-entities+assetrecon", {"scopes": ["assetrecon", "refmaster.legal_entities"]}),
    ("pii-only", {"scopes": ["pii:read"]}),
    ("none", {"scopes": []}),
]


@pytest.fixture(scope="module")
def graph(graph_embedder):
    return build_graph(graph_embedder)


@pytest.fixture(scope="module")
def pack(neo4j_driver, graph_embedder, context_graph):
    def _pack(question: str, who, **kw) -> dict:
        claims = claims_for(who) if isinstance(who, str) else who
        return context_pack(question, claims, driver=neo4j_driver, embedder=graph_embedder, **{"ns": TEST_GRAPH_NS, **kw})
    return _pack


@pytest.fixture(scope="module")
def search(neo4j_driver, graph_embedder, context_graph):
    def _search(question: str, who, **kw):
        claims = claims_for(who) if isinstance(who, str) else who
        return search_context(neo4j_driver, graph_embedder.embed_query(question), question, claims["scopes"],
                              metrics_only=bool(claims.get("metrics_only")), **{"ns": TEST_GRAPH_NS, **kw})
    return _search


def _body(p: dict) -> str:
    return json.dumps(p)


# ------------------------------------------------------------------------------------------- pure helpers
def test_lucene_query_never_passes_raw_syntax():
    q = 'price AND OR ( ) : " conflicts NOT \\ ~ * ? [ ] { } ^ / && || price'
    assert lucene_query(q) == "price OR and OR or OR conflicts OR not"
    assert lucene_query("") == lucene_query(None) == lucene_query("?! ( ) a") == ""
    assert lucene_query("Open_Breaks ÉTÉ") == "open_breaks OR été"
    many = " ".join(f"w{i}" for i in range(5000))
    assert lucene_query(many).count(" OR ") == 63                       # capped below Lucene's clause limit


def test_fetch_size_over_fetches_within_bounds():
    assert fetch_size(1) == 100 and fetch_size(8) == 200 and fetch_size(100) == 1000


def test_gate_params_always_append_public_scope():
    assert gate_params(["cashrecon"], False, "prism")["scopes"] == ["cashrecon", "*"]
    assert gate_params([], True, "prism") == {"scopes": ["*"], "metrics_only": True, "ns": "prism"}
    with pytest.raises(ValueError):
        gate_params([], False, "Bad NS")
    with pytest.raises(ValueError):
        gate("n) OR true //")


def test_trim_pack_drops_lowest_ranked_first():
    big = "x" * 400
    p = {"metrics": [{"id": f"m{i}", "d": big} for i in range(5)],
         "terms": [{"term": f"t{i}", "d": big} for i in range(3)],
         "concepts": [], "columns": [{"table": f"c{i}", "d": big} for i in range(20)],
         "examples": [{"question": "q", "d": big}], "join_paths": []}
    out = trim_pack(p, max_tokens=1000)
    assert pack_tokens(out) <= 1000 and set(out) == set(PACK_KEYS)
    assert out["metrics"][0]["id"] == "m0"                                 # the top metric survives
    assert len(out["columns"]) <= len(out["metrics"]) + 1                  # the long low-ranked list went first
    assert [c["table"] for c in out["columns"]] == [f"c{i}" for i in range(len(out["columns"]))]
    assert trim_pack(empty_pack()) == empty_pack()


# ------------------------------------------------------------------------------------------- the gate
@pytest.mark.neo4j
@pytest.mark.parametrize("claims", [c for _, c in GATE_CASES], ids=[i for i, _ in GATE_CASES])
def test_cypher_gate_agrees_with_python_twin_on_every_node(neo4j_driver, context_graph, graph, claims):
    params = gate_params(claims["scopes"], bool(claims.get("metrics_only")), TEST_GRAPH_NS)
    rows = neo4j_driver.execute_query(f"MATCH (n:Ctx {{ns: $ns}}) WHERE {gate('n')} RETURN n.local_uid AS u",
                                      params).records
    got = {r["u"] for r in rows}
    assert got == {u for u in graph.nodes if visible(graph, claims, u)}
    assert not any(u.startswith("role:") for u in got)                     # M6: roles are gateway-only


def _referenced(p: dict, questions: dict[str, str]) -> set[str]:
    """Every graph object a pack names, as local uids."""
    out = set()
    for m in p["metrics"]:
        out.add(f"metric:{m['id']}")
        if m.get("endpoint"):
            out.add(f"endpoint:{m['source']}.{m['endpoint']}")
        out |= {f"dim:{m['id']}.{d}" for d in m.get("dimensions", [])}
        out |= {f"table:{t}" for t in m.get("tables", [])}
    for t in p["terms"]:
        out |= {f"term:{n.casefold()}" for n in [t["term"], *t.get("broader", [])]}
    for c in p["concepts"]:
        out |= {f"concept:{c['concept']}", *c.get("implemented_by", []), *c.get("keys", [])}
        out |= {f"concept:{r.split(' ', 1)[1]}" for r in c.get("related", [])}
    for t in p["columns"]:
        src = t["table"].split(".")[0]
        out.add(f"table:{t['table']}")
        out |= {f"column:{t['table']}.{c.split(':')[0]}" for c in t["columns"]}
        out |= {f"endpoint:{src}.{e}" for e in t.get("endpoints", [])}
    for e in p["examples"]:
        out.add(questions[e["question"]])
    for j in p["join_paths"]:
        out |= {f"metric:{j['from']}", j["to"], *(f"table:{t}" for t in j["tables"])}
        out |= {f"column:{side.strip()}" for on in j["on"] for side in on.split(" = ")}
    return out


@pytest.mark.neo4j
@pytest.mark.parametrize("claims", [c for _, c in GATE_CASES], ids=[i for i, _ in GATE_CASES])
def test_expand_regates_every_hop(neo4j_driver, context_graph, graph, claims):
    """M3: inject EVERY searchable node (readable or not) as a hit; everything the pack names must be visible."""
    hits = [uid_for(TEST_GRAPH_NS, u) for u, n in graph.nodes.items() if "Searchable" in n["labels"]]
    p = expand(neo4j_driver, hits, claims["scopes"], metrics_only=bool(claims.get("metrics_only")), ns=TEST_GRAPH_NS,
               max_metrics=100, max_paths=10)
    questions = {n["props"]["text"]: u for u, n in graph.nodes.items() if "Question" in n["labels"]}
    named = _referenced(p, questions)
    leaked = {u for u in named if u not in graph.nodes or not visible(graph, claims, u)}
    assert not leaked
    if claims["scopes"] in ([], ["pii:read"]):
        assert not p["metrics"] and not p["columns"] and not p["concepts"]


@pytest.mark.neo4j
def test_readable_term_never_vouches_for_unreadable_links(neo4j_driver, context_graph, pack):
    """term:custodian is readable through feedhub (it defines concept DataSource, implemented by feedhub.sources and
    assetrecon.custodians, and tags assetrecon.custodians.custodian_id): a feedhub-only caller gets the term, never
    the assetrecon objects behind it."""
    feedhub = {"scopes": ["feedhub"]}
    p = expand(neo4j_driver, [uid_for(TEST_GRAPH_NS, "term:custodian")], feedhub["scopes"], ns=TEST_GRAPH_NS)
    assert [t["term"] for t in p["terms"]] == ["custodian"]
    (concept,) = p["concepts"]
    assert concept["concept"] == "DataSource" and concept["implemented_by"] == ["table:feedhub.sources"]
    assert "assetrecon" not in _body(p)
    for q in ("which custodian sent this file", "custodian bank depository safekeeping agent"):
        assert "assetrecon" not in _body(pack(q, feedhub))
    full = expand(neo4j_driver, [uid_for(TEST_GRAPH_NS, "term:custodian")], ["feedhub", "assetrecon"], ns=TEST_GRAPH_NS)
    assert "table:assetrecon.custodians" in full["concepts"][0]["implemented_by"]   # the control: it exists


@pytest.mark.neo4j
def test_synthetic_hops_are_gated(neo4j_driver, scratch_ns):
    """Hops the real data never exercises with mixed scopes: a broader term, a metric the term DEFINES, a column's
    table and endpoint, and a concept relation, each pointing at an object the caller cannot read."""
    g = Graph()
    g.node("term:a", ["BusinessTerm", "Searchable"], name="a", allowed_scopes=["feedhub"])
    g.node("metric:x_secret", ["Metric", "Searchable"], name="x_secret", id="x_secret", source="assetrecon",
           kind="count", mcp_tool="run_metric", dims=[], allowed_scopes=["assetrecon"])
    g.node("term:b", ["BusinessTerm", "Searchable"], name="b", allowed_scopes=["assetrecon"])
    g.node("concept:A", ["Concept", "Searchable"], name="A", allowed_scopes=["feedhub"])
    g.node("concept:B", ["Concept", "Searchable"], name="B", allowed_scopes=["assetrecon"])
    g.node("table:feedhub.t", ["Table"], name="t", source="feedhub", qualified_name="feedhub.t",
           allowed_scopes=["assetrecon"])
    g.node("column:feedhub.t.c", ["Column", "Searchable"], name="c", table="t", source="feedhub", type="text",
           allowed_scopes=["feedhub"])
    g.node("endpoint:feedhub.e", ["Endpoint"], name="e", endpoint_id="e", source="feedhub",
           allowed_scopes=["assetrecon"])
    g.edge("term:a", "BROADER", "term:b")
    g.edge("term:a", "DEFINES", "metric:x_secret")
    g.edge("concept:A", "RELATES_TO", "concept:B")
    g.edge("table:feedhub.t", "HAS_COLUMN", "column:feedhub.t.c")
    g.edge("endpoint:feedhub.e", "BACKED_BY", "table:feedhub.t")
    load(neo4j_driver, None, scratch_ns, graph=g, schema=False)
    hits = [f"{scratch_ns}:{u}" for u in ("term:a", "concept:A", "column:feedhub.t.c")]
    p = expand(neo4j_driver, hits, ["feedhub"], ns=scratch_ns)
    assert p["terms"] == [{"term": "a"}] and p["concepts"] == [{"concept": "A"}] and p["columns"] == []
    assert p["metrics"] == [] and "x_secret" not in _body(p)
    full = expand(neo4j_driver, hits, ["feedhub", "assetrecon"], ns=scratch_ns)  # the control
    assert [m["id"] for m in full["metrics"]] == ["x_secret"]
    assert full["terms"][0]["broader"] == ["b"] and full["concepts"][0]["related"] == ["RELATES_TO B"]
    assert full["columns"] == [{"table": "feedhub.t", "columns": ["c:text"], "endpoints": ["e"]}]


# ------------------------------------------------------------------------------------------- personas
@pytest.mark.neo4j
@pytest.mark.parametrize("q", [ISSUER_Q, "refmaster securities isin marketmaster instruments golden prices",
                               "positions held by growth portfolios at the custodian",
                               "Are price conflicts up this week, and which vendor and asset class is driving it?"])
def test_cash_ops_never_sees_foreign_sources(pack, q):
    body = _body(pack(q, "cash_ops_emea"))
    for src in FOREIGN_TO_CASH_OPS:
        assert src not in body, (src, body)
    assert "price_conflicts" not in body


@pytest.mark.neo4j
@pytest.mark.parametrize("persona", ["steward", "head_data"])
def test_issuer_country_join_path(pack, persona):
    p = pack(ISSUER_Q, persona)
    assert p["metrics"][0]["id"] == "price_conflicts" and p["metrics"][0]["source"] == "marketmaster"
    paths = [j for j in p["join_paths"] if j["from"] == "price_conflicts" and "refmaster.legal_entities" in j["tables"]]
    assert paths, p["join_paths"]
    j = paths[0]
    chain = ["marketmaster.price_suspects", "marketmaster.instruments", "refmaster.securities",
             "refmaster.legal_entities"]
    assert [t for t in j["tables"] if t in chain] == chain
    # a join condition is symmetric; REFERENCES and SAME_KEY_AS can tie on the shortest path in either direction
    on = {frozenset(c.split(" = ")) for c in j["on"]}
    assert {"refmaster.securities.security_id", "marketmaster.instruments.security_id"} in on
    assert {"refmaster.securities.issuer_entity_id", "refmaster.legal_entities.entity_id"} in on


@pytest.mark.neo4j
def test_table_level_scopes(pack, search):
    q = "data quality exceptions on securities by rule and issuer"
    inv = _body(pack(q, "invest_ops_growth"))
    assert "open_dq_exceptions" not in inv and "refmaster.exceptions" not in inv and "refmaster.dq_rules" not in inv
    assert "open_dq_exceptions" in _body(pack(q, "steward"))
    only = {"scopes": ["refmaster.securities"]}
    hits = search("securities isin issuer", only)
    assert any(h.local_uid.startswith("column:refmaster.securities.") for h in hits)
    assert all(h.source in (None, "refmaster") for h in hits)
    tables = {t["table"] for t in pack("securities isin issuer asset class", only)["columns"]}
    assert tables == {"refmaster.securities"}


@pytest.mark.neo4j
@pytest.mark.parametrize("claims", [{"scopes": ["nosuchsource"]}, {"scopes": []}, {"scopes": ["pii:read"]}, {}])
def test_persona_without_matching_scope_gets_empty_pack(pack, claims):
    assert pack("open breaks by region", claims) == empty_pack()


@pytest.mark.neo4j
@pytest.mark.parametrize("q", ["manual matches by operator and auto match rate", "who matched cash items by hand",
                               ISSUER_Q, "positions held by growth portfolios at the custodian"])
def test_metrics_only_pack_has_no_physical_schema_or_sensitive_dimensions(pack, q):
    bi = pack(q, "bi_analyst")
    assert bi["columns"] == [] and bi["join_paths"] == []
    assert all("tables" not in m and "sensitive" not in m for m in bi["metrics"])
    assert all("endpoint" not in m and "time" not in m for m in bi["metrics"])
    assert all(not c.get("implemented_by") and not c.get("keys") for c in bi["concepts"])
    assert "matched_by" not in _body(bi)
    head = pack(q, "head_data")
    if any(m["id"] == "manual_matches" for m in head["metrics"]):
        assert "matched_by" in _body(head)                                  # the control: it exists


@pytest.mark.neo4j
@pytest.mark.parametrize("q", ['price AND OR ( ) : " conflicts NOT \\ ~ * ? [ ] { } ^ / && ||', "", "   ",
                               "open breaks " * 4200, "💸 open breaks 🔥 by region 🇬🇧", "\x00\x1f nul",
                               "*:*", "name:nostro~2 AND region:EMEA^5"])
@pytest.mark.parametrize("persona", ["steward", "cash_ops_emea", "bi_analyst"])
def test_hostile_or_odd_input_never_raises(pack, q, persona):
    p = pack(q, persona)
    assert set(p) == set(PACK_KEYS) and pack_tokens(p) <= 3000


@pytest.mark.neo4j
def test_question_visible_only_when_every_used_object_is(search):
    q = "Are price conflicts up this week, and which vendor and asset class is driving it?"
    assert [h for h in search(q, "steward") if h.kind == "Question"][0].name == q
    assert q not in {h.name for h in search(q, "cash_ops_emea")}
    assert q not in {h.name for h in search(q, "invest_ops_growth")}


@pytest.mark.neo4j
def test_cross_source_question_needs_every_source(neo4j_driver, graph_embedder, scratch_ns):
    """M7: a question whose execution used a cashrecon AND a feedhub metric is visible to exactly the callers who can
    read both (union prefilter + the gate's USED check), not to nobody and not to a feedhub-only caller."""
    k = load_knowledge()
    text = "Do late bank feeds explain the open cash breaks per region?"
    k.history.append(type(k.history[0])(question=text, metrics=["open_breaks", "late_feeds"],
                                        plan="run_metric(open_breaks) and run_metric(late_feeds); combine",
                                        status="verified"))
    g = build_graph(graph_embedder, knowledge=k)
    load(neo4j_driver, graph_embedder, scratch_ns, graph=g)
    vec = graph_embedder.embed_query(text)
    expected = {"cash_ops_emea": True, "head_data": True, "bi_analyst": True, "steward": False,
                "invest_ops_growth": False}
    for persona, want in expected.items():
        c = claims_for(persona)
        hits = search_context(neo4j_driver, vec, text, c["scopes"], metrics_only=c["metrics_only"], ns=scratch_ns)
        assert (text in {h.name for h in hits if h.kind == "Question"}) is want, persona
        examples = context_pack(text, c, driver=neo4j_driver, qvec=vec, ns=scratch_ns)["examples"]
        assert (text in {e["question"] for e in examples}) is want, persona
    assert "feedhub" in claims_for("invest_ops_growth")["scopes"]           # the union prefilter alone would pass


@pytest.mark.neo4j
def test_hits_are_typed_and_capped(search):
    hits = search("open breaks by region and late feeds", "head_data")
    kinds = [h.kind for h in hits]
    assert kinds == sorted(kinds, key=list(KIND_LIMITS).index)            # grouped in kind order
    for kind, n in KIND_LIMITS.items():
        assert kinds.count(kind) <= n
    assert kinds.count("Metric") == 5
    assert "Column" not in {h.kind for h in search("open breaks by region", "bi_analyst")}


# ------------------------------------------------------------------------------------------- quality and speed
def _uid(target: str) -> str:
    """Eval targets spell terms by display name; term identity is the casefolded name (Task 3 ruling)."""
    kind, _, name = target.partition(":")
    return f"term:{name.casefold()}" if kind == "term" else target


def _eval_questions() -> list[dict]:
    """The embedding spike's 60 labelled questions, unchanged (ported from docs/superpowers/spikes/plan-3/embed)."""
    return yaml.safe_load(EVAL.read_text())["questions"]


def test_eval_set_is_complete_and_targets_exist(graph):
    qs = _eval_questions()
    assert len(qs) == 60
    missing = {t for q in qs for t in q["targets"] if _uid(t) not in graph.nodes}
    assert not missing


@pytest.mark.neo4j
@pytest.mark.parametrize("layer", ["search", "pack"])
def test_typed_metric_recall(search, pack, layer):
    """Over the hits, and over what the agent actually receives (expand + budget trimming)."""
    r3 = r5 = n = 0
    misses = []
    for q in _eval_questions():
        targets = [t for t in q["targets"] if t.startswith("metric:")]
        if not targets:
            continue
        if layer == "search":
            ranked = [h.local_uid for h in search(q["q"], "head_data") if h.kind == "Metric"]
        else:
            ranked = [f"metric:{m['id']}" for m in pack(q["q"], "head_data")["metrics"]]
        first = min((ranked.index(t) + 1 for t in targets if t in ranked), default=99)
        n, r3, r5 = n + 1, r3 + (first <= 3), r5 + (first <= 5)
        if first > 3:
            misses.append((first, q["q"], ranked))
    print(f"{layer}: typed metric recall over {n} questions: R@3={r3 / n:.3f} R@5={r5 / n:.3f}")
    assert r3 / n >= 0.95, misses
    assert r5 / n >= 0.98, misses


@pytest.mark.neo4j
def test_pack_latency(pack):
    qs = [q["q"] for q in _eval_questions()[:30]]
    pack(qs[0], "head_data")                                               # warm-up (query plans, model)
    times = []
    for q in qs:
        t = time.perf_counter()
        pack(q, "head_data")
        times.append(1000 * (time.perf_counter() - t))
    p95 = statistics.quantiles(times, n=20)[-1]
    assert p95 < 100, (p95, sorted(times)[-5:])


@pytest.mark.neo4j
@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_pack_budget(pack, persona):
    for q in [x["q"] for x in _eval_questions()[::6]] + [ISSUER_Q]:
        p = pack(q, persona)
        assert set(p) == set(PACK_KEYS) and pack_tokens(p) <= 3000, q


# ------------------------------------------------------------------------------------------- async and failure modes
def _settings_auth() -> tuple[str, tuple[str, str]]:
    s = Settings()
    return s.neo4j_uri, s.neo4j_auth()


@pytest.mark.neo4j
async def test_async_search_and_pack_match_sync(neo4j_driver, graph_embedder, context_graph):
    uri, auth = _settings_auth()
    claims = claims_for("steward")
    vec = graph_embedder.embed_query(ISSUER_Q)
    sync = search_context(neo4j_driver, vec, ISSUER_Q, claims["scopes"], ns=TEST_GRAPH_NS)
    async with AsyncGraphDatabase.driver(uri, auth=auth, notifications_min_severity="OFF") as ad:
        hits = await asearch_context(ad, vec, ISSUER_Q, claims["scopes"], ns=TEST_GRAPH_NS)
        p = await acontext_pack(ISSUER_Q, claims, driver=ad, embedder=graph_embedder, ns=TEST_GRAPH_NS)
    assert [h.local_uid for h in hits] == [h.local_uid for h in sync]
    assert p == context_pack(ISSUER_Q, claims, driver=neo4j_driver, qvec=vec, ns=TEST_GRAPH_NS)


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def black_hole():
    """A TCP port that accepts connections and never answers (a hung server)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    conns, stop = [], threading.Event()

    def accept():
        srv.settimeout(0.1)
        while not stop.is_set():
            try:
                conns.append(srv.accept()[0])
            except OSError:
                pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    t.join()
    for c in conns:
        c.close()
    srv.close()


async def _assert_async_unavailable(port: int, timeout_s: float) -> None:
    async with AsyncGraphDatabase.driver(f"bolt://127.0.0.1:{port}", auth=("neo4j", "x"),
                                         connection_timeout=30) as ad:
        t = time.perf_counter()
        with pytest.raises(GraphUnavailable):
            await asearch_context(ad, [0.0] * 384, "open breaks", ["cashrecon"], timeout_s=timeout_s)
        assert time.perf_counter() - t < timeout_s + 1.0


async def test_async_closed_port_is_graph_unavailable():
    await _assert_async_unavailable(_closed_port(), 2.0)


async def test_async_hung_server_is_graph_unavailable_within_timeout(black_hole):
    await _assert_async_unavailable(black_hole, 0.5)
    await asyncio.sleep(0)


BAD_CYPHER = "MATCH (n WHERE RETURN n"


def _assert_generic_error(exc_info, caplog) -> None:
    assert str(exc_info.value) == GRAPH_ERROR_MESSAGE
    assert exc_info.value.__cause__ is None and exc_info.value.__context__ is None
    assert "Invalid input" not in str(exc_info.value) and "Neo.ClientError" not in str(exc_info.value)
    assert any("Neo.ClientError" in r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG)


@pytest.mark.neo4j
def test_sync_statement_error_is_generic(neo4j_driver, caplog):
    caplog.set_level(logging.DEBUG, logger="prism.graph.retrieval")
    with pytest.raises(GraphError) as exc_info:
        run_read(neo4j_driver, BAD_CYPHER, {}, 5.0)
    _assert_generic_error(exc_info, caplog)


@pytest.mark.neo4j
async def test_async_statement_error_is_generic(caplog):
    caplog.set_level(logging.DEBUG, logger="prism.graph.retrieval")
    uri, auth = _settings_auth()
    async with AsyncGraphDatabase.driver(uri, auth=auth, notifications_min_severity="OFF") as ad:
        with pytest.raises(GraphError) as exc_info:
            await arun_read(ad, BAD_CYPHER, {}, 5.0)
    _assert_generic_error(exc_info, caplog)


def test_sync_closed_port_is_graph_unavailable():
    with GraphDatabase.driver(f"bolt://127.0.0.1:{_closed_port()}", auth=("neo4j", "x"),
                              connection_timeout=2) as d:
        t = time.perf_counter()
        with pytest.raises(GraphUnavailable):
            context_pack("open breaks", claims_for("head_data"), driver=d, qvec=[0.0] * 384, timeout_s=2)
        assert time.perf_counter() - t < 3
