"""Gateway fixtures. fake_catalog is built straight from the source registries (no Neo4j) and equals
load_catalog()'s metrics (asserted against the real graph in test_policy.py when Neo4j is up)."""
import pytest

from prism.graph.catalog import Catalog
from tests.gateway.fakes import registry_catalog


@pytest.fixture(scope="session")
def fake_catalog() -> Catalog:
    return registry_catalog()
