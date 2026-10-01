import pytest

pytestmark = pytest.mark.neo4j


def test_driver_runs_query(neo4j_driver):
    records, _, _ = neo4j_driver.execute_query("RETURN 1 AS one")
    assert records[0]["one"] == 1


def test_vector_and_fulltext_procedures_available(neo4j_driver):
    records, _, _ = neo4j_driver.execute_query("SHOW PROCEDURES YIELD name RETURN name")
    names = {r["name"] for r in records}
    assert "db.index.vector.queryNodes" in names
    assert "db.index.fulltext.queryNodes" in names


def test_server_version(neo4j_driver):
    records, _, _ = neo4j_driver.execute_query(
        "CALL dbms.components() YIELD versions RETURN versions[0] AS v"
    )
    assert records[0]["v"].startswith("5.26")
