import base64
import hashlib
import hmac
import json
import time

import psycopg
import pytest
from pydantic import SecretStr

from prism.config import LOGICAL_DBS, Settings
from prism.db.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def migrated():
    settings = Settings(db_prefix="testmig_")  # separate prefix: must not wipe the session-seeded test_ DBs
    try:
        psycopg.connect(settings.dsn("postgres", admin=True), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc}). Start it with: make db", pytrace=False)
    migrate(settings)
    return settings


def test_every_table_forces_rls_and_has_a_read_policy(migrated):
    for db in LOGICAL_DBS:
        with psycopg.connect(migrated.dsn(db, admin=True)) as conn:
            rows = conn.execute(
                """SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity,
                          (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE c.relkind = 'r' AND n.nspname IN ('public', 'private')"""
            ).fetchall()
            assert rows, f"{db} has no tables"
            for ns, rel, rls, force, policies in rows:
                assert rls and force, f"{db}.{ns}.{rel} lacks forced RLS"
                assert policies == 1, f"{db}.{ns}.{rel} has {policies} policies"


def test_runtime_roles_cannot_bypass_rls(migrated):
    with psycopg.connect(migrated.dsn("postgres", admin=True)) as conn:
        rows = conn.execute(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)",
            (["bi_reader", "prism_view_owner", migrated.pg_svc_user],),
        ).fetchall()
    assert len(rows) == 3
    assert all(not sup and not bypass for _, sup, bypass in rows)


def test_hmac_key_is_stored_and_hidden(migrated):
    with psycopg.connect(migrated.dsn("cashrecon", admin=True)) as conn:
        assert conn.execute("SELECT k FROM prism_sec.hmac_key").fetchone()[0] == migrated.ctx_hmac_key.get_secret_value()
        granted = conn.execute(
            "SELECT has_table_privilege('bi_reader', 'prism_sec.hmac_key', 'SELECT')"
        ).fetchone()[0]
    assert granted is False


# --- behavioural tests of the signed context (helpers are local; prism.db.session arrives in Task 10) ---


def _ctx(key: str, claims: dict, exp_offset: int = 300) -> str:
    payload = base64.b64encode(json.dumps({**claims, "exp": int(time.time()) + exp_offset}).encode()).decode()
    return payload + "." + hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


@pytest.fixture()
def seeded(migrated):
    with psycopg.connect(migrated.dsn("cashrecon", admin=True)) as conn:
        conn.execute("DELETE FROM private.cash_accounts")
        conn.execute(
            "INSERT INTO private.cash_accounts VALUES ('A1','LE1','SRC1','BICXGB2L','GB29NWBK60161331926819','GBP','EMEA'),"
            "('A2','LE2','SRC2','BICXUS33','US1234567890','USD','AMER')"
        )
    return migrated


def _query(settings, ctx, query="SELECT account_id FROM public.cash_accounts ORDER BY 1", inject=None):
    with psycopg.connect(settings.dsn("cashrecon", admin=True)) as conn:
        if inject:
            conn.execute(inject)
        conn.execute("SET LOCAL ROLE bi_reader")
        if ctx is not None:
            conn.execute("SELECT set_config('app.ctx', %s, true)", (ctx,))
        rows = conn.execute(query).fetchall()
        conn.rollback()
        return rows


def test_no_context_sees_nothing(seeded):
    assert _query(seeded, None, "SELECT account_id FROM public.cash_accounts") == []


def test_forged_signature_is_rejected(seeded):
    good = _ctx(seeded.ctx_hmac_key.get_secret_value(), {"scopes": ["cashrecon"], "rows": {"region": ["*"]}})
    forged = good.rsplit(".", 1)[0] + "." + "0" * 64
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _query(seeded, forged)
    wrong_key = _ctx("x" * 40, {"scopes": ["cashrecon"], "rows": {"region": ["*"]}})
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _query(seeded, wrong_key)


def test_expired_context_is_rejected(seeded):
    ctx = _ctx(seeded.ctx_hmac_key.get_secret_value(), {"scopes": ["cashrecon"], "rows": {"region": ["*"]}}, exp_offset=-10)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _query(seeded, ctx)


def test_wildcard_scope_and_pii_masking(seeded):
    base = {"scopes": ["cashrecon"], "rows": {"region": ["*"]}}
    view = "SELECT account_id, nostro_no FROM public.cash_accounts ORDER BY 1"
    masked = _query(seeded, _ctx(seeded.ctx_hmac_key.get_secret_value(), base), view)
    assert masked == [("A1", "****6819"), ("A2", "****7890")]
    full = _query(seeded, _ctx(seeded.ctx_hmac_key.get_secret_value(), {**base, "scopes": ["cashrecon", "pii:read"]}), view)
    assert full == [("A1", "GB29NWBK60161331926819"), ("A2", "US1234567890")]


def test_row_dimension_filters(seeded):
    emea = _ctx(seeded.ctx_hmac_key.get_secret_value(), {"scopes": ["cashrecon"], "rows": {"region": ["EMEA"]}})
    assert _query(seeded, emea) == [("A1",)]
    amer_only_other = _ctx(seeded.ctx_hmac_key.get_secret_value(), {"scopes": ["cashrecon"], "rows": {"region": ["APAC"]}})
    assert _query(seeded, amer_only_other) == []


def test_missing_key_fails_closed(seeded):
    unsigned = base64.b64encode(
        json.dumps({"scopes": ["cashrecon", "pii:read"], "rows": {"region": ["*"]}, "exp": int(time.time()) + 300}).encode()
    ).decode() + ".deadbeef"
    with psycopg.connect(seeded.dsn("cashrecon", admin=True)) as conn:
        conn.execute("DELETE FROM prism_sec.hmac_key")
        conn.execute("SET LOCAL ROLE bi_reader")
        conn.execute("SELECT set_config('app.ctx', %s, true)", (unsigned,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT account_id FROM public.cash_accounts").fetchall()
        conn.rollback()
    with psycopg.connect(seeded.dsn("cashrecon", admin=True)) as conn:  # rollback restored the key
        assert conn.execute("SELECT count(*) FROM prism_sec.hmac_key").fetchone()[0] == 1


def test_short_hmac_key_is_refused():
    with pytest.raises(ValueError):
        Settings(db_prefix="testmig_", ctx_hmac_key="short-key1")   # Settings refuses it first
    unchecked = Settings().model_copy(update={"db_prefix": "testmig_", "ctx_hmac_key": SecretStr("short-key1")})
    with pytest.raises(ValueError):
        migrate(unchecked)                                           # and migrate's own check still holds
