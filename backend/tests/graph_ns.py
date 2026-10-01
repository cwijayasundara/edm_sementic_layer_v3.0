"""The test session's graph namespace. The session never loads into, reloads or deletes the configured (real)
namespace (settings.graph_ns): a load there supersedes every older node, distilled history included. `context_graph`
loads TEST_GRAPH_NS instead; as a non-production namespace its uids carry the "prism_test:" prefix (the ctx_uid
constraint is global), so tests read it by passing ns=TEST_GRAPH_NS and compare local_uid, never uid."""
import pytest

from prism.config import Settings
from prism.graph.loader import LoadReport, check_ns, load

TEST_GRAPH_NS = check_ns("prism_test")
REAL_GRAPH_NS = check_ns(Settings().graph_ns)
if REAL_GRAPH_NS == TEST_GRAPH_NS:
    raise pytest.UsageError(f"PRISM_GRAPH_NS is {TEST_GRAPH_NS!r}, the test session's own namespace: point it at the "
                            "real graph namespace (default 'prism') before running the tests")


def load_test_graph(driver, embedder, ns: str = TEST_GRAPH_NS, **kw) -> LoadReport:
    """Load the context graph into the test namespace; refuses the real one."""
    if check_ns(ns) == REAL_GRAPH_NS:
        raise ValueError(f"refusing to load tests into the real graph namespace {ns!r}")
    return load(driver, embedder, ns=ns, **kw)
