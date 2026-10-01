"""Test isolation strategy (see report): ONE Neo4j container per test session; the real graph (ns='prism') is loaded
once per session; tests that mutate load into their own namespace ns='t<runid>' (uid prefix + :SpikeRun label) and
delete it in teardown. Every retrieval query filters n.ns = $ns, so namespaced test nodes never leak into results
even though vector/fulltext indexes are global."""
import uuid

import pytest
from neo4j import GraphDatabase

from loader import delete_ns, load
from schema import AUTH, URI, create_schema


@pytest.fixture(scope="session")
def driver():
    d = GraphDatabase.driver(URI, auth=AUTH, notifications_min_severity="OFF", max_connection_pool_size=20,
                             connection_timeout=3.0, connection_acquisition_timeout=5.0)
    d.verify_connectivity()
    create_schema(d)
    d.execute_query("MATCH (n:SpikeRun) DETACH DELETE n")   # leftovers of crashed runs
    yield d
    d.close()


@pytest.fixture(scope="session")
def graph(driver):
    stats = load(driver, ns="prism")
    return stats


@pytest.fixture
def run_ns(driver):
    ns = "t" + uuid.uuid4().hex[:8]
    yield ns
    delete_ns(driver, ns)
