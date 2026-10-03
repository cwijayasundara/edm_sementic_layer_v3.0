"""Idempotent, versioned migrations for the `app` database (seed marker, audit log, query log).

Unlike the source databases, `app` is never dropped: resets re-create the platforms around it, and
the gateway runs `migrate_app` at startup. Every step here is safe to repeat.

Grants are declarative: each run first revokes everything the app role holds on the app database, its
`app`/`public` schemas and their tables/sequences/functions (and its role memberships, both ways), then
grants exactly CONNECT, USAGE on `app`, SELECT/INSERT on
the app tables, DELETE on `app.saved_dashboards` and UPDATE of `app.query_log.verified` only. A stale
UPDATE/DELETE/TRUNCATE grant or membership therefore disappears on the next run. The app role name may
not be a reserved/source role, a superuser or a database owner (checked before anything is altered).

Known limits (documented, not fixed here):
- the app role is cluster-wide: one `prism_app` serves every `db_prefix` on the same cluster, so its
  password is shared across prefixes (the last migrate_app run sets it);
- the schema migrations are serialised by an advisory lock, but the cluster-level role/database steps
  are not: two concurrent *first* runs can still collide on them, so start the seeder and the gateway
  one after the other;
- only grants made by the migrating admin (or the bootstrap superuser) are revoked; a grant issued by
  another grantor with GRANT OPTION is not.
"""
import psycopg
from psycopg import errors, sql

from prism.config import APP_DB, LOGICAL_DBS, RESERVED_ROLES, Settings

_LOCK_KEY = 0x70726973  # pg_advisory_xact_lock key serialising concurrent app migrations ("pris")

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, """
        CREATE TABLE IF NOT EXISTS public.seed_info (
          id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
          seed int NOT NULL,
          as_of date NOT NULL,
          profile text NOT NULL,
          seeded_at timestamptz NOT NULL DEFAULT now()
        );
    """),
    (2, """
        CREATE TABLE IF NOT EXISTS app.audit (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          ts timestamptz NOT NULL DEFAULT now(),
          sub text,
          persona text,
          tool text,
          source text,
          metric_id text,
          dimensions text[],
          status text,
          rows bigint,
          bytes bigint,
          truncated boolean,
          ms double precision,
          question_hash char(64),
          result_handle text,
          error_code text
        );
        CREATE INDEX IF NOT EXISTS audit_sub_ts ON app.audit (sub, ts DESC);
        CREATE TABLE IF NOT EXISTS app.query_log (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          ts timestamptz NOT NULL DEFAULT now(),
          sub text NOT NULL,
          persona text,
          question_hash char(64) NOT NULL,
          question text,
          plan text,
          handles text[],
          metric_ids text[],
          verified boolean NOT NULL DEFAULT false,
          status text
        );
        CREATE INDEX IF NOT EXISTS query_log_sub_ts ON app.query_log (sub, ts DESC);
    """),
    (3, """
        CREATE TABLE IF NOT EXISTS app.agent_runs (
          id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
          ts timestamptz NOT NULL DEFAULT now(),
          run_id text NOT NULL,
          sub text NOT NULL,
          question_hash char(64) NOT NULL,
          path text,
          models text[],
          input_tokens int NOT NULL DEFAULT 0,
          output_tokens int NOT NULL DEFAULT 0,
          cache_read_input_tokens int NOT NULL DEFAULT 0,
          llm_turns int NOT NULL DEFAULT 0,
          tool_calls int NOT NULL DEFAULT 0,
          tool_latency_ms double precision NOT NULL DEFAULT 0,
          cost_usd numeric(12,6) NOT NULL DEFAULT 0,
          status text NOT NULL,
          error_code text
        );
        CREATE INDEX IF NOT EXISTS agent_runs_sub_ts ON app.agent_runs (sub, ts DESC);
    """),
    (4, """
        CREATE TABLE IF NOT EXISTS app.saved_dashboards (
          id uuid PRIMARY KEY,
          sub text NOT NULL,
          title text NOT NULL,
          items jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS saved_dashboards_sub ON app.saved_dashboards (sub, created_at DESC);
    """),
    (5, """
        ALTER TABLE app.query_log ADD COLUMN IF NOT EXISTS record_id uuid;
        ALTER TABLE app.query_log ADD COLUMN IF NOT EXISTS metric_backed boolean NOT NULL DEFAULT false;
        -- every verified row so far was agent-claimed (metric-backed), never human-confirmed
        UPDATE app.query_log SET metric_backed = verified, verified = false WHERE record_id IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS query_log_record_id ON app.query_log (record_id);
    """),
    (6, """
        ALTER TABLE public.seed_info ADD COLUMN IF NOT EXISTS incident jsonb;
    """),
)

APP_TABLES = ("audit", "query_log", "agent_runs", "saved_dashboards")
DELETABLE_TABLES = ("saved_dashboards",)   # user-owned rows; everything else stays append-only


def check_app_role_name(conn: psycopg.Connection, settings: Settings) -> None:
    """Refuse an app role name that is reserved, a superuser or owns a database, before anything is altered."""
    role = settings.pg_app_user
    if role in RESERVED_ROLES | {settings.pg_admin_user, settings.pg_svc_user}:
        raise ValueError(f"pg_app_user {role!r} is a reserved or source role")
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s AND rolsuper", (role,)).fetchone():
        raise ValueError(f"pg_app_user {role!r} is a superuser")
    owned = conn.execute("SELECT datname FROM pg_database WHERE pg_get_userbyid(datdba) = %s ORDER BY 1",
                         (role,)).fetchall()
    if owned:
        raise ValueError(f"pg_app_user {role!r} owns database(s) {[d for (d,) in owned]}")


def ensure_app_role(conn: psycopg.Connection, settings: Settings) -> None:
    """The app role logs in, but holds no attributes, memberships or source-database rights."""
    check_app_role_name(conn, settings)
    role = settings.pg_app_user
    ident = sql.Identifier(role)
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
        try:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(ident))
        except errors.DuplicateObject:  # a concurrent migration created it first
            pass
    conn.execute(sql.SQL("ALTER ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION "
                         "PASSWORD {}").format(ident, sql.Literal(settings.pg_app_password.get_secret_value())))
    _strip_memberships(conn, role)


def _strip_memberships(conn: psycopg.Connection, role: str) -> None:
    """Revoke every membership the role holds and every role that is a member of it (PG16: per grantor)."""
    rows = conn.execute(
        """SELECT r.rolname, u.rolname, g.rolname FROM pg_auth_members m
           JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member
           JOIN pg_roles g ON g.oid = m.grantor
           WHERE u.rolname = %s OR r.rolname = %s""", (role, role)).fetchall()
    for granted, member, grantor in rows:
        conn.execute(sql.SQL("REVOKE {} FROM {} GRANTED BY {}").format(
            sql.Identifier(granted), sql.Identifier(member), sql.Identifier(grantor)))


def ensure_app_database(conn: psycopg.Connection, settings: Settings) -> None:
    """Create `app` if missing (never drop it) and keep PUBLIC out of it on every run."""
    name = settings.dbname(APP_DB)
    if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except errors.DuplicateDatabase:
            pass
    ident, role = sql.Identifier(name), sql.Identifier(settings.pg_app_user)
    conn.execute(sql.SQL("REVOKE CONNECT, TEMP ON DATABASE {} FROM PUBLIC").format(ident))
    # declarative: no CREATE/TEMP on app, no rights at all on the source databases that exist
    sources = [settings.dbname(db) for db in LOGICAL_DBS]
    for (existing,) in conn.execute("SELECT datname FROM pg_database WHERE datname = ANY(%s)", (sources,)):
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(sql.Identifier(existing), role))
    conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(ident, role))
    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(ident, role))


def _grant_statements(app_user: str) -> list[sql.Composable]:
    role = sql.Identifier(app_user)
    tables = sql.SQL(", ").join(sql.Identifier("app", t) for t in APP_TABLES)
    revoke_all = [
        sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM {}").format(sql.SQL(kind), sql.Identifier(schema), role)
        for schema in ("app", "public") for kind in ("TABLES", "SEQUENCES", "FUNCTIONS")
    ]
    return [
        sql.SQL("REVOKE ALL ON SCHEMA app FROM PUBLIC"),
        sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA app FROM PUBLIC"),
        sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"),
        # declarative: drop whatever the role holds (incl. column grants), then grant exactly what it needs
        *revoke_all,
        sql.SQL("REVOKE ALL ON SCHEMA app, public FROM {}").format(role),
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON TABLES FROM {}").format(role),
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA app REVOKE ALL ON SEQUENCES FROM {}").format(role),
        sql.SQL("GRANT USAGE ON SCHEMA app TO {}").format(role),
        # append-only: the gateway records and reads history, it never edits or deletes it
        sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(tables, role),
        # saved dashboards are the user's own rows: they may delete them (never update)
        *[sql.SQL("GRANT DELETE ON {} TO {}").format(sql.Identifier("app", t), role) for t in DELETABLE_TABLES],
        # human confirmation flips exactly one column of the caller's own row (prism.gateway.audit.confirm_answer)
        sql.SQL("GRANT UPDATE (verified) ON app.query_log TO {}").format(role),
    ]


def apply_migrations(conn: psycopg.Connection) -> list[int]:
    """Apply pending versions in one transaction under an advisory lock; return the versions applied."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_KEY,))
    conn.execute("CREATE SCHEMA IF NOT EXISTS app")
    conn.execute("""CREATE TABLE IF NOT EXISTS app.schema_migrations (
                      version int PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())""")
    done = {v for (v,) in conn.execute("SELECT version FROM app.schema_migrations").fetchall()}
    applied = []
    for version, ddl in MIGRATIONS:
        if version in done:
            continue
        conn.execute(ddl)
        conn.execute("INSERT INTO app.schema_migrations (version) VALUES (%s)", (version,))
        applied.append(version)
    return applied


def migrate_app(settings: Settings) -> list[int]:
    """Bring the `app` database up to date. Idempotent; returns the migration versions it applied."""
    with psycopg.connect(settings.dsn("postgres", admin=True), autocommit=True) as conn:
        ensure_app_role(conn, settings)
        ensure_app_database(conn, settings)
    with psycopg.connect(settings.dsn(APP_DB, admin=True)) as conn, conn.transaction():
        applied = apply_migrations(conn)
        for statement in _grant_statements(settings.pg_app_user):
            conn.execute(statement)
    return applied
