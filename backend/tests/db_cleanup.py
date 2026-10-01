"""Databases the test suite creates under its own prefixes, and how they are dropped (session teardown in
tests/conftest.py; `make clean-test-dbs` runs the same pattern through the project's compose Postgres). The
session-seeded test_* databases and every other database never match."""
import re

import psycopg
from psycopg import sql

TEST_DB_PREFIXES = ("testapp", "testappnew", "testmig", "testhist", "testhistcli")
TEST_DB_PATTERN = "^(" + "|".join(TEST_DB_PREFIXES) + ")_"
_TEST_DB = re.compile(TEST_DB_PATTERN)


def is_test_db(name: str) -> bool:
    return bool(_TEST_DB.match(name))


def drop_test_databases(settings, *, startswith: str | None = None) -> list[str]:
    """Drop every database whose name matches TEST_DB_PATTERN (and starts with `startswith`, itself a test-database
    prefix); returns the names dropped."""
    if startswith is not None and not is_test_db(startswith):
        raise ValueError(f"{startswith!r} is not a test database prefix")
    with psycopg.connect(settings.dsn("postgres", admin=True), autocommit=True, connect_timeout=3) as conn:
        names = [r[0] for r in conn.execute("SELECT datname FROM pg_database WHERE datname ~ %s ORDER BY 1",
                                            (TEST_DB_PATTERN,))
                 if is_test_db(r[0]) and (startswith is None or r[0].startswith(startswith))]
        for name in names:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
    return names
