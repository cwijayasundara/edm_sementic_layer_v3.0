import uuid

import psycopg
import pytest
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable

from prism.config import MODELS_DIR, Settings
from prism.graph.embedder import Embedder, EmbedderError
from prism.graph.loader import delete_ns
from prism.sim.seed import seed_all
from prism.sim.universe import SimConfig

from tests.db_cleanup import drop_test_databases
from tests.graph_ns import TEST_GRAPH_NS, load_test_graph


@pytest.fixture(scope="session", autouse=True)
def _drop_test_databases_at_session_end():
    """The db / gateway / history tests create testapp_*, testappnew_*, testmig_*, testhist_* and testhistcli_*
    databases; drop them (and only them) when the session ends. Postgres down: nothing to drop."""
    yield
    try:
        drop_test_databases(Settings())
    except psycopg.OperationalError:
        pass


@pytest.fixture(scope="session")
def seeded() -> Settings:
    settings = Settings(db_prefix="test_")
    try:
        psycopg.connect(settings.dsn("postgres", admin=True), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc}). Start it with: make db", pytrace=False)
    seed_all(settings, SimConfig.small())
    return settings


@pytest.fixture(scope="session")
def neo4j_driver():
    settings = Settings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=settings.neo4j_auth(),
        connection_timeout=settings.neo4j_timeout_s,
        notifications_min_severity="OFF",
    )
    try:
        driver.verify_connectivity()
    except (ServiceUnavailable, AuthError) as exc:
        driver.close()
        pytest.skip(f"Neo4j not reachable: run `make db` ({type(exc).__name__}: {exc})")
    yield driver
    driver.close()


@pytest.fixture(scope="session")
def graph_embedder() -> Embedder:
    """The local embedding model, loaded once (offline; `make models` caches it)."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HF_HUB_OFFLINE", "1")
        try:
            embedder = Embedder(cache_dir=MODELS_DIR).load()
        except EmbedderError as exc:
            pytest.skip(f"embedding model unavailable: run `make models` ({exc})")
    return embedder


@pytest.fixture(scope="session")
def context_graph(neo4j_driver, graph_embedder):
    """The context graph, loaded once per session into TEST_GRAPH_NS (never the real namespace); later graph and
    gateway tests read it. Only that namespace is deleted at session end."""
    report = load_test_graph(neo4j_driver, graph_embedder)
    yield report
    delete_ns(neo4j_driver, TEST_GRAPH_NS)   # refuses the configured (real) namespace


@pytest.fixture
def scratch_ns(neo4j_driver):
    """A private namespace for tests that mutate the graph; deleted in teardown."""
    ns = "t" + uuid.uuid4().hex[:10]
    yield ns
    delete_ns(neo4j_driver, ns)
