"""Bulk-load generated tables with COPY (as the admin role, which owns the tables)."""
import psycopg
from psycopg import sql

from prism.config import Settings
from prism.sim.model import TableData


def write_tables(settings: Settings, logical_db: str, tables: dict[str, TableData]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with psycopg.connect(settings.dsn(logical_db, admin=True)) as conn, conn.cursor() as cur:
        for name, table in tables.items():
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(*name.split(".")), sql.SQL(", ").join(map(sql.Identifier, table.columns))
            )
            with cur.copy(statement) as copy:
                for row in table.rows:
                    copy.write_row(row)
            counts[name] = len(table.rows)
    return counts
