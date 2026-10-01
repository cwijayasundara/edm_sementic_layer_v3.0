"""Shared plumbing for the mock platform REST APIs: auth, entitlement checks, safe SQL building, scoped fetch."""
import asyncio
from datetime import date
from typing import Any

import psycopg
from fastapi import HTTPException, Request
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from prism.config import Settings
from prism.db.session import ctx_from_claims, prepare_statements
from prism.security.access import can
from prism.security.tokens import TokenError, verify

_OPS = {"eq": "=", "gte": ">=", "lte": "<=", "lt": "<"}


class Databases:
    """Lazily opened connection pools (as prism_svc), one per logical database."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pools: dict[str, AsyncConnectionPool] = {}
        self._lock = asyncio.Lock()

    async def pool(self, logical: str) -> AsyncConnectionPool:
        async with self._lock:
            if logical not in self._pools:
                pool = AsyncConnectionPool(self._settings.dsn(logical), min_size=1, max_size=5, open=False)
                try:
                    await pool.open(wait=True, timeout=10)
                except BaseException:
                    try:
                        await pool.close()
                    except Exception:
                        pass
                    raise
                self._pools[logical] = pool
            return self._pools[logical]

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()


def bearer_claims(request: Request, audience: str) -> dict:
    unauthorized = HTTPException(401, "invalid or missing token", headers={"WWW-Authenticate": "Bearer"})
    parts = request.headers.get("authorization", "").split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise unauthorized
    try:
        return verify(parts[1], audience, request.app.state.settings.jwt_secret.get_secret_value())
    except TokenError as exc:
        raise unauthorized from exc


def require_table(claims: dict, db: str, table: str) -> None:
    if not can(claims, db, table):
        raise HTTPException(403, f"not entitled to {db}.{table}")


def check_range(start: date | None, end: date | None) -> None:
    if start and end and start > end:
        raise HTTPException(422, "'from' must be on or before 'to'")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def where(filters: list[tuple[str, str, Any]]) -> tuple[sql.Composable, list]:
    """Column names must be trusted code constants, never request-derived; values are always bound parameters."""
    parts: list[sql.Composable] = []
    params: list = []
    for column, op, value in filters:
        if op != "ilike" and op not in _OPS:
            raise ValueError(f"unsupported filter operator: {op!r}")
        if value is None:
            continue
        if op == "ilike":
            parts.append(sql.SQL("{} ILIKE %s ESCAPE '\\'").format(sql.Identifier(column)))
            params.append(f"%{_escape_like(value)}%")
        else:
            parts.append(sql.SQL("{} {} %s").format(sql.Identifier(column), sql.SQL(_OPS[op])))
            params.append(value)
    if not parts:
        return sql.SQL(""), []
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts), params


def select(table: str, columns: tuple[str, ...], cond: sql.Composable, order_by: str | tuple[str, ...],
           limit: int, offset: int) -> sql.Composed:
    order = (order_by,) if isinstance(order_by, str) else order_by
    return sql.SQL("SELECT {cols} FROM {t}{w} ORDER BY {o} LIMIT {l} OFFSET {off}").format(
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)), t=sql.Identifier(table), w=cond,
        o=sql.SQL(", ").join(map(sql.Identifier, order)), l=sql.Literal(limit), off=sql.Literal(offset),
    )


def page(rows: list[dict], limit: int, offset: int) -> dict:
    return {"items": rows, "count": len(rows), "limit": limit, "offset": offset}


async def fetch(request: Request, db: str, claims: dict, query: sql.Composable, params: list) -> list[dict]:
    settings: Settings = request.app.state.settings
    ctx = ctx_from_claims(claims, settings.ctx_hmac_key.get_secret_value())
    pool = await request.app.state.dbs.pool(db)
    try:
        async with pool.connection() as conn, conn.transaction():
            for statement, args in prepare_statements(ctx, settings.statement_timeout_ms):
                await conn.execute(statement, args)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, params)
                return await cur.fetchall()
    except psycopg.errors.InsufficientPrivilege as exc:
        raise HTTPException(403, "access denied by data policy") from exc
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(504, "query timed out") from exc
