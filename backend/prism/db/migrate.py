"""(Re)create roles, databases, schemas, security functions, RLS policies and grants."""
from pathlib import Path

import psycopg
from psycopg import sql

from prism.config import APP_DB, LOGICAL_DBS, Settings
from prism.db.app_migrate import migrate_app
from prism.db.policies import policy_statements

DDL_DIR = Path(__file__).parent / "ddl"


def _admin(settings: Settings, logical: str, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(settings.dsn(logical, admin=True), autocommit=autocommit)


def ensure_roles(conn: psycopg.Connection, settings: Settings) -> None:
    if settings.pg_svc_user == settings.pg_admin_user:
        raise ValueError("pg_svc_user must differ from pg_admin_user (the service role must not be the admin)")
    for role, login in (("bi_reader", False), ("prism_view_owner", False), (settings.pg_svc_user, True)):
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} {} NOBYPASSRLS").format(
                sql.Identifier(role), sql.SQL("LOGIN" if login else "NOLOGIN")))
        # Enforced on every run so pre-existing roles cannot keep elevated attributes.
        conn.execute(sql.SQL("ALTER ROLE {} NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
    conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
        sql.Identifier(settings.pg_svc_user), sql.Literal(settings.pg_svc_password.get_secret_value())))
    conn.execute(sql.SQL("GRANT bi_reader TO {}").format(sql.Identifier(settings.pg_svc_user)))


def recreate_databases(conn: psycopg.Connection, settings: Settings) -> None:
    """Drop and re-create the source databases. Never `app`: it holds the audit and query history."""
    for logical in LOGICAL_DBS:
        name = sql.Identifier(settings.dbname(logical))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(name))
        conn.execute(sql.SQL("REVOKE CONNECT, TEMP ON DATABASE {} FROM PUBLIC").format(name))


def grant_statements(db_name: str, svc_user: str) -> list[sql.Composable]:
    return [
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db_name), sql.Identifier(svc_user)),
        sql.SQL("GRANT USAGE ON SCHEMA public TO bi_reader"),
        sql.SQL("GRANT USAGE ON SCHEMA prism_sec TO bi_reader, prism_view_owner"),
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO bi_reader"),
        sql.SQL("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA prism_sec FROM PUBLIC"),
        sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA prism_sec TO bi_reader, prism_view_owner"),
    ]


def migrate(settings: Settings) -> None:
    if len(settings.ctx_hmac_key.get_secret_value()) < 32:
        raise ValueError("ctx_hmac_key must be at least 32 characters")
    with _admin(settings, "postgres", autocommit=True) as conn:
        ensure_roles(conn, settings)
    migrate_app(settings)
    with _admin(settings, APP_DB) as conn:  # invalidate the seed marker BEFORE the sources go away
        conn.execute("DELETE FROM seed_info")
    with _admin(settings, "postgres", autocommit=True) as conn:
        recreate_databases(conn, settings)
    security_sql = (DDL_DIR / "security.sql").read_text()
    for logical in LOGICAL_DBS:
        with _admin(settings, logical) as conn:
            conn.execute(security_sql)
            conn.execute("INSERT INTO prism_sec.hmac_key (k) VALUES (%s)", (settings.ctx_hmac_key.get_secret_value(),))
            conn.execute((DDL_DIR / f"{logical}.sql").read_text())
            for statement in policy_statements(logical):
                conn.execute(statement)
            for statement in grant_statements(settings.dbname(logical), settings.pg_svc_user):
                conn.execute(statement)
