"""Databases created by the test suite (prefixes testapp_, testappnew_, testmig_, testhist_, testhistcli_) are dropped
at session end and by `make clean-test-dbs`; nothing else is ever matched (the session-seeded test_* databases, the
real ones, other prefixes)."""
import re
import uuid
from pathlib import Path

import psycopg
import pytest

from prism.config import Settings
from tests.db_cleanup import TEST_DB_PATTERN, drop_test_databases, is_test_db

MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"


@pytest.mark.parametrize("name", ["testapp_app", "testapp_cashrecon", "testappnew_app", "testmig_feedhub",
                                  "testhist_app", "testhistcli_app"])
def test_test_databases_match(name):
    assert is_test_db(name)


@pytest.mark.parametrize("name", ["app", "cashrecon", "postgres", "template1", "test_app", "test_cashrecon",
                                  "testappx_app", "testapp", "xtestapp_app", "testhist", "TESTAPP_app", "testmigapp",
                                  "nothere_app"])
def test_other_databases_never_match(name):
    assert not is_test_db(name)


def test_make_clean_test_dbs_uses_the_same_pattern_on_the_project_postgres_only():
    text = MAKEFILE.read_text()
    (recipe,) = re.findall(r"^clean-test-dbs:.*\n((?:\t.*\n)+)", text, re.MULTILINE)
    assert f"TEST_DB_REGEX = {TEST_DB_PATTERN}" in text and "$(TEST_DB_REGEX)" in recipe
    assert "docker compose exec -T postgres" in recipe and "WITH (FORCE)" in recipe


@pytest.mark.db
def test_drop_test_databases_drops_only_test_prefixed_databases():
    settings = Settings()
    probe = f"testhist_probe_{uuid.uuid4().hex[:8]}"
    try:
        with psycopg.connect(settings.dsn("postgres", admin=True), autocommit=True, connect_timeout=3) as conn:
            conn.execute(f'CREATE DATABASE "{probe}"')
            before = {r[0] for r in conn.execute("SELECT datname FROM pg_database")}
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc}). Start it with: make db", pytrace=False)
    dropped = drop_test_databases(settings, startswith=probe)   # this test's own database only: others are in use
    assert dropped == [probe]
    with psycopg.connect(settings.dsn("postgres", admin=True)) as conn:
        after = {r[0] for r in conn.execute("SELECT datname FROM pg_database")}
    assert after == before - {probe}
    with pytest.raises(ValueError, match="test database prefix"):
        drop_test_databases(settings, startswith="test_")                # never the session-seeded databases
