import time

import psycopg
import pytest
from pydantic import SecretStr
from psycopg import sql

from prism.config import Settings
from prism.db.session import ctx_from_claims, scoped_sync
from prism.sim.seed import is_seeded, project_all
from prism.sim.universe import SimConfig, build_universe

pytestmark = pytest.mark.db


def test_seed_loads_every_projected_row(seeded):
    u = build_universe(SimConfig.small())
    for db, tables in project_all(u).items():   # projectors + the incident pass's appended rows
        with psycopg.connect(seeded.dsn(db, admin=True)) as conn:
            for name, table in tables.items():
                ident = sql.Identifier(*name.split("."))
                count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(ident)).fetchone()[0]
                assert count == len(table.rows), f"{db}.{name}"


def test_seed_info_records_the_incident(seeded):
    from prism.config import APP_DB
    from prism.sim.incident import incident_record
    want = incident_record(build_universe(SimConfig.small()))
    with psycopg.connect(seeded.dsn(APP_DB, admin=True)) as conn:
        got = conn.execute("SELECT incident FROM seed_info").fetchone()[0]
    assert got == want


def test_is_seeded(seeded):
    assert is_seeded(seeded)
    assert not is_seeded(Settings(db_prefix="nothere_"))


def test_is_seeded_false_when_ctx_key_changes(seeded):
    assert not is_seeded(seeded.model_copy(update={"ctx_hmac_key": SecretStr("a-different-key-0123456789abcdef0123")}))


def test_signed_context_grants_rows_and_missing_context_grants_none(seeded):
    claims = {"sub": "t", "scopes": ["cashrecon"], "rows": {"region": ["*"]}, "exp": int(time.time()) + 60}
    with scoped_sync(seeded, "cashrecon", ctx_from_claims(claims, seeded.ctx_hmac_key.get_secret_value())) as conn:
        assert conn.execute("SELECT count(*) AS n FROM breaks").fetchone()["n"] > 0
    with scoped_sync(seeded, "cashrecon", None) as conn:
        assert conn.execute("SELECT count(*) AS n FROM breaks").fetchone()["n"] == 0
