"""The test session never loads into, reloads or deletes the configured (real) graph namespace: `context_graph` loads
the dedicated TEST_GRAPH_NS."""
import pytest

from prism.config import Settings
from prism.graph.loader import check_ns, production_ns
from tests.graph_ns import REAL_GRAPH_NS, TEST_GRAPH_NS, load_test_graph


def test_the_session_graph_namespace_is_the_dedicated_test_one():
    assert check_ns(TEST_GRAPH_NS) == TEST_GRAPH_NS != REAL_GRAPH_NS == production_ns() == Settings().graph_ns


def test_load_test_graph_refuses_the_real_namespace():
    with pytest.raises(ValueError, match="real graph namespace"):
        load_test_graph(None, None, ns=REAL_GRAPH_NS)


def _fingerprint(driver, ns: str) -> dict:
    (r,) = driver.execute_query(
        "OPTIONAL MATCH (n:Ctx {ns: $ns}) WITH count(n) AS nodes, max(n.loaded_version) AS version, "
        "count(CASE WHEN n.origin = 'history' THEN 1 END) AS history "
        "RETURN nodes, version, history, COUNT { MATCH (:Ctx {ns: $ns})-[r]->() } AS rels", ns=ns).records
    return r.data()


@pytest.mark.neo4j
def test_loading_the_test_graph_leaves_the_real_namespace_untouched(neo4j_driver, graph_embedder):
    before = _fingerprint(neo4j_driver, REAL_GRAPH_NS)
    if not before["nodes"]:
        pytest.skip(f"no real graph in namespace {REAL_GRAPH_NS!r} to compare against")
    report = load_test_graph(neo4j_driver, graph_embedder)
    assert report.nodes > 0
    assert _fingerprint(neo4j_driver, REAL_GRAPH_NS) == before
