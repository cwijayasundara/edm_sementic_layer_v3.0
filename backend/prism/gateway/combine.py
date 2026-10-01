"""combine(): run one guarded SELECT over the caller's own result handles in a fresh, hardened, in-memory DuckDB.

Defence in depth: the static guard (combine_guard) decides what may run; the engine is locked down anyway (no
external access, no extension autoload/install, no temp spill directory, bounded threads and memory, configuration
locked) so a guard bypass still cannot reach files, the network or other databases. Tables are created only from
the handles the caller passed (resolved through the ResultStore with the caller's `sub`). Bounds: 10,000 output
rows (truncated flag), 5 s wall clock via conn.interrupt() from a timer. The output is stored as a new handle of
the same sub; `partial` marks a result built from a truncated input.
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from typing import Any

import duckdb

from prism.gateway.combine_guard import CCY, TableInfo, check_sql
from prism.gateway.errors import GatewayError
from prism.gateway.results import ResultStore, StoredResult

MAX_ROWS = 10_000
TIMEOUT_S = 5.0
MAX_HANDLES = 8
MAX_SQL_CHARS = 20_000
ALIAS = re.compile(r"[a-z_][a-z0-9_]{0,62}")
RESERVED = ("duckdb_", "pragma_", "sqlite_", "pg_", "information_schema", "main", "memory", "system", "temp")
ENGINE_SETTINGS = (
    "SET temp_directory = ''",
    "SET threads = 2",
    "SET memory_limit = '256MB'",
    "SET autoinstall_known_extensions = false",
    "SET autoload_known_extensions = false",
    "SET enable_external_access = false",
    "SET lock_configuration = true",
)
_INT64 = (-(2**63), 2**63 - 1)


def hardened_connection() -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(":memory:")
    try:
        for statement in ENGINE_SETTINGS:
            conn.execute(statement)
    except BaseException:
        conn.close()
        raise
    return conn


def _invalid(message: str) -> GatewayError:
    return GatewayError("invalid_request", message)


def _inputs(store: ResultStore, sub: str, handles: Any) -> dict[str, StoredResult]:
    if not isinstance(handles, Mapping) or not 1 <= len(handles) <= MAX_HANDLES:
        raise _invalid(f"handles must map 1..{MAX_HANDLES} table names to result handles")
    out = {}
    for alias, handle in handles.items():
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias) or alias.startswith(RESERVED):
            raise _invalid("table names must be lower-case identifiers (not duckdb_/pg_/sqlite_/pragma_/system names)")
        out[alias] = store.get(sub, handle)  # another sub's handle is simply unknown
    return out


def _column_names(columns: tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    names = []
    for i, c in enumerate(columns):
        base = c.strip().lower() or f"col_{i + 1}"
        name, n = base, 1
        while name in seen:
            n += 1
            name = f"{base}_{n}"
        seen.add(name)
        names.append(name)
    return names


def _duck_type(values: list[Any]) -> str:
    vals = [v for v in values if v is not None]
    if not vals:
        return "VARCHAR"
    if all(isinstance(v, bool) for v in vals):
        return "BOOLEAN"
    if any(isinstance(v, bool) for v in vals):
        return "VARCHAR"
    if all(isinstance(v, int) and _INT64[0] <= v <= _INT64[1] for v in vals):
        return "BIGINT"
    if all(isinstance(v, (int, float)) and not (isinstance(v, int) and not _INT64[0] <= v <= _INT64[1])
           for v in vals):
        return "DOUBLE"
    return "VARCHAR"


def _cell(value: Any, typ: str) -> Any:
    if value is None or typ != "VARCHAR":
        return float(value) if typ == "DOUBLE" and value is not None else value
    return value if isinstance(value, str) else json.dumps(value)


def _table(entry: StoredResult) -> tuple[TableInfo, list[str], list[dict]]:
    names = _column_names(entry.columns)
    types = {n: _duck_type([r[i] for r in entry.rows]) for i, n in enumerate(names)}
    numeric = {n for n in names if types[n] in ("BIGINT", "DOUBLE")}
    lineage = entry.meta.get("lineage")
    if isinstance(lineage, Mapping):  # a combine output: amounts and genuine ccy come from its recorded lineage
        if lineage.get("unresolved"):
            amount_cols, ccy_col = numeric, None
        else:
            marked = set(lineage.get("amount") or ())
            amount_cols = {names[i] for i, c in enumerate(entry.columns) if c in marked}
            ccy_col = next((names[i] for i, c in enumerate(entry.columns) if c == lineage.get("ccy")), None)
        payload = [{n: _cell(r[i], types[n]) for i, n in enumerate(names)} for r in entry.rows]
        return TableInfo(columns=types, amount=frozenset(amount_cols), ccy=ccy_col), names, payload
    units_in = entry.meta.get("units") or {}
    units = {names[i]: units_in[c] for i, c in enumerate(entry.columns) if c in units_in}
    has_ccy = CCY in names
    amount: set[str] = set()
    if has_ccy:
        for n in names:
            unit = units.get(n)
            if n == CCY:
                continue
            if isinstance(unit, str):
                if "amount" in unit.lower() or "currency" in unit.lower():
                    amount.add(n)
            elif types[n] in ("BIGINT", "DOUBLE"):
                amount.add(n)   # unknown unit next to a ccy column: treat as an amount (fail closed)
    payload = [{n: _cell(r[i], types[n]) for i, n in enumerate(names)} for r in entry.rows]
    return TableInfo(columns=types, amount=frozenset(amount), ccy=CCY if has_ccy else None), names, payload


def _load(conn: duckdb.DuckDBPyConnection, alias: str, info: TableInfo, payload: list[dict]) -> None:
    spec = json.dumps([info.columns])
    cols = ", ".join(f'CAST(NULL AS {t}) AS "{n.replace(chr(34), chr(34) * 2)}"' for n, t in info.columns.items())
    conn.execute(f'CREATE TABLE "{alias}" AS SELECT {cols} WHERE false')
    if payload:
        conn.execute(f'INSERT INTO "{alias}" SELECT unnest(from_json(?::JSON, ?), recursive := true)',
                     [json.dumps(payload), spec])


def _lineage_meta(checked, columns: list[str]) -> dict[str, Any]:
    """What the next combine needs to keep enforcing the currency rule on this output (fail closed when ambiguous)."""
    lower = [c.lower() for c in columns]
    known = set(checked.amount) | ({checked.ccy} if checked.ccy else set())
    if checked.unresolved or len(set(lower)) != len(lower) or (known and not known <= set(lower)):
        return {"unresolved": bool(known or checked.unresolved), "amount": [], "ccy": None}
    return {"unresolved": False, "amount": [c for c in columns if c.lower() in checked.amount],
            "ccy": next((c for c in columns if c.lower() == checked.ccy), None)}


def _interrupt(conn: duckdb.DuckDBPyConnection) -> None:
    try:
        conn.interrupt()
    except Exception:  # noqa: BLE001 - the connection may already be closed; nothing to stop then
        pass


def combine(store: ResultStore, sub: str, sql: Any, handles: Any, *, timeout_s: float = TIMEOUT_S,
            max_rows: int = MAX_ROWS) -> str:
    """Run `sql` over the caller's handles (table name -> handle) and return a new handle of the same sub."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_CHARS or "\x00" in sql:
        raise GatewayError("invalid_sql", f"sql must be a non-empty string of at most {MAX_SQL_CHARS} characters")
    inputs = _inputs(store, sub, handles)
    tables = {alias: _table(entry) for alias, entry in inputs.items()}
    checked = check_sql(sql, {alias: t[0] for alias, t in tables.items()})
    conn = hardened_connection()
    timer = None
    try:
        for alias, (info, _, payload) in tables.items():
            _load(conn, alias, info, payload)
        timer = threading.Timer(timeout_s, _interrupt, (conn,))
        timer.daemon = True
        timer.start()
        try:
            cur = conn.execute(checked.sql)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(max_rows + 1)
        except duckdb.InterruptException:
            raise GatewayError("combine_timeout", f"combine exceeded {timeout_s:g} s; narrow the query") from None
        except duckdb.Error as e:
            # the engine's text can quote cell values: only its error class (a fixed DuckDB name) is shown
            raise GatewayError("combine_failed", f"combine failed ({type(e).__name__}): check table and column "
                                                 f"names, types and casts") from None
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        conn.close()
    truncated = len(rows) > max_rows
    partial = any(bool(e.meta.get("truncated") or e.meta.get("partial")) for e in inputs.values())
    meta = {"source": "combine", "truncated": truncated or partial, "partial": partial, "units": {},
            "inputs": sorted(e.handle for e in inputs.values()), "lineage": _lineage_meta(checked, columns)}
    return store.put(sub, columns, [list(r) for r in rows[:max_rows]], meta)


__all__ = ["MAX_ROWS", "TIMEOUT_S", "combine", "hardened_connection"]
