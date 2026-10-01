"""Per-transaction security context: sign claims, then run queries as bi_reader inside a read-only transaction."""
import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from prism.config import Settings


def sign_ctx(claims: dict, key: str) -> str:
    payload = base64.b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()).decode()
    signature = hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def ctx_from_claims(claims: dict, key: str) -> str:
    return sign_ctx(
        {"sub": claims["sub"], "scopes": claims.get("scopes", []), "rows": claims.get("rows", {}),
         "exp": claims["exp"]},
        key,
    )


def prepare_statements(ctx: str | None, timeout_ms: int) -> list[tuple[str, tuple | None]]:
    """Must run first inside a fresh transaction."""
    return [
        ("SET TRANSACTION READ ONLY", None),
        ("SET LOCAL ROLE bi_reader", None),
        ("SELECT set_config('app.ctx', %s, true)", (ctx or "",)),
        ("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),)),
    ]


@contextmanager
def scoped_sync(settings: Settings, logical_db: str, ctx: str | None) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.dsn(logical_db), row_factory=dict_row) as conn:
        with conn.transaction():
            for statement, args in prepare_statements(ctx, settings.statement_timeout_ms):
                conn.execute(statement, args)
            yield conn
