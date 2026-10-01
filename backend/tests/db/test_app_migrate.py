"""The `app` database: idempotent versioned migrations, survival across resets, and role isolation."""
import uuid

import psycopg
import pytest

from prism.config import APP_DB, LOGICAL_DBS, Settings
from prism.db.app_migrate import MIGRATIONS, migrate_app
from prism.db.migrate import migrate
from prism.sim import cli
from prism.sim.seed import is_seeded

pytestmark = pytest.mark.db

PREFIX = "testapp_"  # own prefix: the reset test must not wipe the session-seeded test_ databases


@pytest.fixture(scope="module")
def app_settings() -> Settings:
    settings = Settings(db_prefix=PREFIX)
    try:
        psycopg.connect(settings.dsn("postgres", admin=True), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc}). Start it with: make db", pytrace=False)
    migrate(settings)  # every database (sources + app) exists for the isolation checks
    return settings


def _admin(settings: Settings, logical: str = APP_DB) -> psycopg.Connection:
    return psycopg.connect(settings.dsn(logical, admin=True), autocommit=True)


def _schema_snapshot(settings: Settings) -> tuple:
    with _admin(settings) as conn:
        versions = conn.execute("SELECT version, applied_at FROM app.schema_migrations ORDER BY 1").fetchall()
        columns = conn.execute(
            """SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns
               WHERE table_schema IN ('app', 'public') ORDER BY 1, 2, 3"""
        ).fetchall()
    return versions, columns


def test_migrate_app_twice_is_a_noop(app_settings):
    before = _schema_snapshot(app_settings)
    migrate_app(app_settings)
    after = _schema_snapshot(app_settings)
    assert before == after
    versions = [v for v, _ in after[0]]
    assert versions == [version for version, _ in MIGRATIONS]
    tables = {(s, t) for s, t, _, _ in after[1]}
    assert {("app", "audit"), ("app", "query_log"), ("app", "agent_runs"), ("public", "seed_info")} <= tables


def test_migrate_app_creates_a_missing_database():
    settings = Settings(db_prefix="testappnew_")
    with _admin(settings, "postgres") as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{settings.dbname(APP_DB)}" WITH (FORCE)')
    try:
        migrate_app(settings)
        migrate_app(settings)
        with _admin(settings) as conn:
            assert conn.execute("SELECT count(*) FROM app.schema_migrations").fetchone()[0] == len(MIGRATIONS)
    finally:
        with _admin(settings, "postgres") as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{settings.dbname(APP_DB)}" WITH (FORCE)')


def test_reset_keeps_app_rows(app_settings, monkeypatch):
    sub = f"reset-{uuid.uuid4().hex}"
    with _admin(app_settings) as conn:
        conn.execute("INSERT INTO app.audit (sub, tool, status) VALUES (%s, 'run_metric', 'ok')", (sub,))
        conn.execute("INSERT INTO app.query_log (sub, question_hash) VALUES (%s, %s)", (sub, "0" * 64))
    monkeypatch.setenv("PRISM_DB_PREFIX", PREFIX)
    assert cli.main(["--reset", "--small"]) == 0
    with _admin(app_settings) as conn:
        assert conn.execute("SELECT count(*) FROM app.audit WHERE sub = %s", (sub,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM app.query_log WHERE sub = %s", (sub,)).fetchone()[0] == 1
        assert conn.execute("SELECT profile FROM seed_info").fetchone()[0] == "small"
    assert is_seeded(app_settings)
    # and a second reset in a row works against the surviving app database (seed marker is upserted)
    assert cli.main(["--reset", "--small"]) == 0
    with _admin(app_settings) as conn:
        assert conn.execute("SELECT count(*) FROM app.audit WHERE sub = %s", (sub,)).fetchone()[0] == 1


def test_reset_clears_seed_marker_before_dropping_sources(app_settings, monkeypatch):
    """A reset that dies part-way must not leave the old marker saying "seeded" over half-written data."""
    from prism.sim import seed

    def boom(_universe):
        raise RuntimeError("projection failed")

    with _admin(app_settings) as conn:  # a completed earlier seed
        conn.execute("""INSERT INTO seed_info (seed, as_of, profile) VALUES (42, '2026-09-30', 'small')
                        ON CONFLICT (id) DO NOTHING""")
    assert is_seeded(app_settings)
    monkeypatch.setitem(seed.PROJECTORS, "feedhub", boom)
    with pytest.raises(RuntimeError, match="projection failed"):
        seed.seed_all(app_settings, seed.SimConfig.small())
    assert not is_seeded(app_settings)
    with _admin(app_settings) as conn:
        assert conn.execute("SELECT count(*) FROM seed_info").fetchone()[0] == 0


def test_app_role_is_isolated_from_source_data(app_settings):
    """The app role reaches only app.audit / app.query_log; source roles never reach app."""
    seeded_settings = app_settings
    app_user = seeded_settings.pg_app_user
    assert app_user not in (seeded_settings.pg_svc_user, seeded_settings.pg_admin_user)
    for logical in LOGICAL_DBS:
        with pytest.raises(psycopg.OperationalError, match="permission denied"):
            psycopg.connect(seeded_settings.app_dsn(logical), connect_timeout=3).close()
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        psycopg.connect(seeded_settings.dsn(APP_DB), connect_timeout=3).close()  # prism_svc
    with _admin(seeded_settings, "postgres") as conn:
        app_db = seeded_settings.dbname(APP_DB)
        for role in ("bi_reader", "prism_view_owner", seeded_settings.pg_svc_user):
            assert conn.execute("SELECT has_database_privilege(%s, %s, 'CONNECT')", (role, app_db)).fetchone()[0] is False
        attrs = conn.execute(
            "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname = %s", (app_user,)
        ).fetchone()
        assert attrs == (False, False, False, False)
        member_of = conn.execute(
            """SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
               JOIN pg_roles u ON u.oid = m.member WHERE u.rolname = %s""", (app_user,)
        ).fetchall()
        assert member_of == []
    with psycopg.connect(seeded_settings.app_dsn(), autocommit=True) as conn:
        conn.execute("INSERT INTO app.audit (sub, tool, status) VALUES ('iso', 'describe', 'ok')")
        conn.execute("SELECT count(*) FROM app.audit").fetchone()
        for statement in ("SELECT * FROM public.seed_info", "SELECT * FROM app.schema_migrations",
                          "DELETE FROM app.audit WHERE sub = 'iso'", "UPDATE app.audit SET status = 'x'",
                          "DELETE FROM app.query_log", "CREATE TABLE app.sneaky (x int)"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


def _app_role_rights(settings: Settings) -> dict:
    role, db = settings.pg_app_user, settings.dbname(APP_DB)
    with _admin(settings) as conn:
        def one(query: str, *args) -> object:
            return conn.execute(query, args).fetchone()[0]

        tables = {}
        for table in ("app.audit", "app.query_log", "app.agent_runs", "app.schema_migrations", "public.seed_info"):
            tables[table] = {p for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
                             if one("SELECT has_table_privilege(%s, %s, %s)", role, table, p)}
        return {
            "tables": tables,
            "column_update": one("SELECT has_any_column_privilege(%s, 'app.audit', 'UPDATE')", role),
            "schema_create": one("SELECT has_schema_privilege(%s, 'app', 'CREATE')", role),
            "public_create": one("SELECT has_schema_privilege(%s, 'public', 'CREATE')", role),
            "db_temp": one("SELECT has_database_privilege(%s, %s, 'TEMP')", role, db),
            "db_create": one("SELECT has_database_privilege(%s, %s, 'CREATE')", role, db),
            "member_of": conn.execute(
                """SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
                   WHERE m.member = (SELECT oid FROM pg_roles WHERE rolname = %s)""", (role,)).fetchall(),
            "members": conn.execute(
                """SELECT r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
                   WHERE m.roleid = (SELECT oid FROM pg_roles WHERE rolname = %s)""", (role,)).fetchall(),
        }


EXPECTED_RIGHTS = {
    "tables": {"app.audit": {"SELECT", "INSERT"}, "app.query_log": {"SELECT", "INSERT"},
               "app.agent_runs": {"SELECT", "INSERT"},
               "app.schema_migrations": set(), "public.seed_info": set()},
    "column_update": False, "schema_create": False, "public_create": False, "db_temp": False, "db_create": False,
    "member_of": [], "members": [],
}


def test_migrate_app_heals_stale_grants_and_memberships(app_settings):
    """Grants are declarative: whatever extra rights or memberships the app role picked up are revoked."""
    role = app_settings.pg_app_user
    extra, inheritor = f"stale_{uuid.uuid4().hex[:12]}", f"stale_{uuid.uuid4().hex[:12]}"
    db = app_settings.dbname(APP_DB)
    try:
        with _admin(app_settings, "postgres") as conn:
            conn.execute(f'CREATE ROLE "{extra}" NOLOGIN')
            conn.execute(f'CREATE ROLE "{inheritor}" NOLOGIN')
            conn.execute(f'GRANT "{extra}" TO "{role}"')
            conn.execute(f'GRANT "{role}" TO "{inheritor}"')
            conn.execute(f'GRANT TEMP, CREATE ON DATABASE "{db}" TO "{role}"')
        with _admin(app_settings) as conn:
            conn.execute(f'GRANT DELETE, UPDATE, TRUNCATE, REFERENCES, TRIGGER ON app.audit, app.query_log, app.agent_runs TO "{role}"')
            conn.execute(f'GRANT UPDATE (status) ON app.audit TO "{role}"')
            conn.execute(f'GRANT SELECT, INSERT ON app.schema_migrations, public.seed_info TO "{role}"')
            conn.execute(f'GRANT CREATE ON SCHEMA app, public TO "{role}"')
        assert _app_role_rights(app_settings) != EXPECTED_RIGHTS  # the stale rights really are in place
        migrate_app(app_settings)
        assert _app_role_rights(app_settings) == EXPECTED_RIGHTS
    finally:
        with _admin(app_settings, "postgres") as conn:
            conn.execute(f'DROP ROLE IF EXISTS "{extra}"')
            conn.execute(f'DROP ROLE IF EXISTS "{inheritor}"')
    with psycopg.connect(app_settings.app_dsn(), autocommit=True) as conn:
        for statement in ("DELETE FROM app.audit", "UPDATE app.query_log SET status = 'x'",
                          "TRUNCATE app.audit", "CREATE TABLE app.sneaky (x int)", "CREATE TEMP TABLE t (x int)"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)


@pytest.mark.parametrize("name", ["prism_svc", "bi_reader", "prism_view_owner", "postgres"])
def test_reserved_role_names_are_refused_for_the_app_role(name):
    with pytest.raises(ValueError, match="pg_app_user"):
        Settings(pg_app_user=name)


def test_admin_and_service_users_are_refused_for_the_app_role():
    with pytest.raises(ValueError, match="pg_app_user"):
        Settings(pg_svc_user="svc_x", pg_app_user="svc_x")
    with pytest.raises(ValueError, match="pg_app_user"):
        Settings(pg_admin_user="admin_x", pg_app_user="admin_x")


def test_migrate_app_refuses_a_source_database_owner_or_superuser(app_settings):
    """A role that owns a database (or is a superuser) is never turned into the app role."""
    owner = f"owner_{uuid.uuid4().hex[:12]}"
    with _admin(app_settings, "postgres") as conn:
        conn.execute(f'CREATE ROLE "{owner}" NOLOGIN')
        conn.execute(f'ALTER DATABASE "{app_settings.dbname("refmaster")}" OWNER TO "{owner}"')
    try:
        with pytest.raises(ValueError, match="pg_app_user"):
            migrate_app(app_settings.model_copy(update={"pg_app_user": owner}))
        with _admin(app_settings, "postgres") as conn:  # untouched: still NOLOGIN, still owner
            assert conn.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (owner,)).fetchone()[0] is False
        with pytest.raises(ValueError, match="pg_app_user"):
            migrate_app(app_settings.model_copy(update={"pg_app_user": "postgres"}))  # copy skips validation
    finally:
        with _admin(app_settings, "postgres") as conn:
            conn.execute(f'ALTER DATABASE "{app_settings.dbname("refmaster")}" OWNER TO "{app_settings.pg_admin_user}"')
        with _admin(app_settings) as conn:  # only if a regression granted it anything in app
            conn.execute(f'DROP OWNED BY "{owner}"')
        with _admin(app_settings, "postgres") as conn:
            conn.execute(f'DROP ROLE IF EXISTS "{owner}"')
