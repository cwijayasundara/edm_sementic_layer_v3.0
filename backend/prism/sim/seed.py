"""Create every platform database and load it from one shared universe."""
import psycopg
from psycopg.types.json import Jsonb

from prism.config import APP_DB, Settings
from prism.db.migrate import migrate
from prism.sim.incident import apply_incident, incident_record
from prism.sim.model import TableData
from prism.sim.project_assetrecon import project_assetrecon
from prism.sim.project_cashrecon import project_cashrecon
from prism.sim.project_feedhub import project_feedhub
from prism.sim.project_marketmaster import project_marketmaster
from prism.sim.project_refmaster import project_refmaster
from prism.sim.universe import SimConfig, build_universe
from prism.sim.writer import write_tables

PROJECTORS = {
    "refmaster": project_refmaster,
    "marketmaster": project_marketmaster,
    "cashrecon": project_cashrecon,
    "assetrecon": project_assetrecon,
    "feedhub": project_feedhub,
}


def project_all(universe) -> dict[str, dict[str, TableData]]:
    """Every platform's tables, then the cross-system incident pass over all of them (M9 spec §2.5)."""
    tables = {db: project(universe) for db, project in PROJECTORS.items()}
    apply_incident(universe, tables)
    return tables


def seed_all(settings: Settings, cfg: SimConfig) -> dict[str, dict[str, int]]:
    """Re-create the source databases and load them. `app` survives; its seed marker is written last."""
    migrate(settings)  # clears the seed marker before dropping anything
    universe = build_universe(cfg)
    counts = {db: write_tables(settings, db, tables) for db, tables in project_all(universe).items()}
    with psycopg.connect(settings.dsn(APP_DB, admin=True)) as conn:
        conn.execute("""INSERT INTO seed_info (seed, as_of, profile, incident) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET seed = EXCLUDED.seed, as_of = EXCLUDED.as_of,
                          profile = EXCLUDED.profile, incident = EXCLUDED.incident, seeded_at = now()""",
                     (cfg.seed, cfg.as_of, cfg.profile, Jsonb(incident_record(universe))))
    return counts


def _database_exists(conn: psycopg.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def is_seeded(settings: Settings) -> bool:
    """True only if seeding completed AND the databases trust the currently configured context key.

    Returns False only on a definite "not seeded" signal (database missing, seed marker absent, key
    mismatch). Any connection or other database error PROPAGATES: an unreachable or misbehaving server
    must never be mistaken for "not seeded", because callers answer "not seeded" by dropping databases.
    """
    with psycopg.connect(settings.dsn("postgres", admin=True), connect_timeout=3) as conn:
        app_exists = _database_exists(conn, settings.dbname(APP_DB))
        refmaster_exists = _database_exists(conn, settings.dbname("refmaster"))
    if not app_exists:
        return False
    with psycopg.connect(settings.dsn(APP_DB, admin=True), connect_timeout=3) as conn:
        if conn.execute("SELECT to_regclass('public.seed_info')").fetchone()[0] is None:
            return False
        if conn.execute("SELECT 1 FROM seed_info").fetchone() is None:
            return False
    if not refmaster_exists:
        return False
    with psycopg.connect(settings.dsn("refmaster", admin=True), connect_timeout=3) as conn:
        if conn.execute("SELECT to_regclass('prism_sec.hmac_key')").fetchone()[0] is None:
            return False
        row = conn.execute("SELECT k FROM prism_sec.hmac_key").fetchone()
        return row is not None and row[0] == settings.ctx_hmac_key.get_secret_value()
