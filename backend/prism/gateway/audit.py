"""Gateway audit trail (`app.audit`) and query history (`app.query_log`).

Every event is reduced to a fixed whitelist of typed fields before it is logged or stored: identifiers
must look like identifiers, counts must be integers, error codes must be codes. Anything else (row
values, filters, SQL, tokens, free-text messages, the raw question) is dropped; the question survives
only as an HMAC-SHA256 keyed by the server secret `audit_hmac_key` (not guessable by hashing candidate
questions). The audit timestamp is always the database's now(); a caller-supplied `ts` is ignored.

A query-log `plan` is never free text: only the structured form {"metric_ids": [...], "dimensions": [...]}
is accepted, and only names matching the metric/dimension name pattern survive. The plan is derived from
the question, so it is stored only when `store_questions` is on. A query-log row carries a gateway-made `record_id`
and `metric_backed`; `confirm_answer` is the only update, and it sets `verified` on the caller's own metric-backed row. Writes never raise into the caller: a
failure is logged and counted in `dropped`.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import sys
import time
from typing import IO, Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from prism.config import APP_DB, Settings

audit_log = logging.getLogger("prism.gateway.audit")
AUDIT_LOGGERS = ("prism.mcp.audit", "prism.gateway.audit")

AUDIT_FIELDS = ("ts", "sub", "persona", "tool", "source", "metric_id", "dimensions", "status", "rows",
                "bytes", "truncated", "ms", "question_hash", "result_handle", "error_code")
QUERY_LOG_FIELDS = ("sub", "persona", "question_hash", "question", "plan", "handles", "metric_ids",
                    "verified", "status", "record_id", "metric_backed")
RECORD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
HISTORY_MAX = 100
MAX_SUB_LEN = 256
MAX_PLAN_LEN = 4000
MAX_QUESTION_LEN = 4000
MAX_LIST_LEN = 64

_IDENT = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_HANDLE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HASH = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"[a-z][a-z0-9_]{0,62}")  # metric ids and dimension names (prism.mcp.metrics._IDENT)
PLAN_KEYS = ("metric_ids", "dimensions")
MIN_KEY_LEN = 32
AUDIT_DEGRADED_FOR_S = 300.0   # a dropped row keeps /healthz's audit field 'degraded' this long
INVALID_CODE = "invalid"

# Column lists are fixed constants; every value is a bound parameter.
_INSERT_AUDIT = (
    "INSERT INTO app.audit (ts, sub, persona, tool, source, metric_id, dimensions, status, rows, bytes, "
    "truncated, ms, question_hash, result_handle, error_code) "
    "VALUES (now(), %(sub)s, %(persona)s, %(tool)s, %(source)s, %(metric_id)s, "
    "%(dimensions)s, %(status)s, %(rows)s, %(bytes)s, %(truncated)s, %(ms)s, %(question_hash)s, "
    "%(result_handle)s, %(error_code)s)"
)
_INSERT_QUERY = (
    "INSERT INTO app.query_log (sub, persona, question_hash, question, plan, handles, metric_ids, verified, status, "
    "record_id, metric_backed) "
    "VALUES (%(sub)s, %(persona)s, %(question_hash)s, %(question)s, %(plan)s, %(handles)s, %(metric_ids)s, "
    "%(verified)s, %(status)s, %(record_id)s, %(metric_backed)s)"
)
# the only UPDATE the app role may run (column grant on `verified`): the caller's own metric-backed, ok row
_CONFIRM = (
    "UPDATE app.query_log SET verified = true "
    "WHERE record_id = %s AND sub = %s AND metric_backed AND status = 'ok' RETURNING id"
)
_HISTORY = (
    "SELECT ts, persona, question, question_hash, plan, handles, metric_ids, verified, status "
    "FROM app.query_log WHERE sub = %s ORDER BY ts DESC, id DESC LIMIT %s"
)


class AuditUnavailable(Exception):
    """The app database could not serve a history read (writes never raise; they count drops)."""


def question_hash(question: str, key: str) -> str:
    """HMAC-SHA256 of the question under the server's audit key (hex)."""
    if not isinstance(key, str) or len(key) < MIN_KEY_LEN:
        raise ValueError(f"the question-hash key must be a string of at least {MIN_KEY_LEN} characters")
    return hmac.new(key.encode(), question.encode(), hashlib.sha256).hexdigest()


def _match(pattern: re.Pattern, value: Any) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _idents(values: Any, pattern: re.Pattern = _IDENT) -> list[str] | None:
    if not isinstance(values, (list, tuple)):
        return None
    return [v for v in values[:MAX_LIST_LEN] if _match(pattern, v)]


CALLER_ID = re.compile(r"[A-Za-z0-9_.:@+-]{1,256}")   # what a verified gateway token's `sub` may contain


def valid_caller_id(value: Any) -> bool:
    """A gateway caller id: 1..256 characters of [A-Za-z0-9_.:@+-] (no spaces, control characters or NUL, which
    would break the audit insert or a log line). Stricter than valid_sub, which the audit writer applies."""
    return isinstance(value, str) and CALLER_ID.fullmatch(value) is not None


def valid_sub(value: Any) -> bool:
    """A caller id is a non-empty string of at most MAX_SUB_LEN characters (never truncated onto another)."""
    return isinstance(value, str) and 0 < len(value) <= MAX_SUB_LEN


def _sub(value: Any) -> str | None:
    return value if valid_sub(value) else None


def _hash_of(event: dict, key: str) -> str | None:
    if isinstance(event.get("question"), str):
        return question_hash(event["question"], key)
    return _match(_HASH, event.get("question_hash"))


def sanitize_plan(plan: Any) -> str | None:
    """The stored plan: compact JSON of the structured form, names only, capped; anything else is None."""
    if not isinstance(plan, dict) or not any(k in plan for k in PLAN_KEYS):
        return None
    clean = {}
    for key in PLAN_KEYS:
        names = plan.get(key, [])
        if not isinstance(names, (list, tuple)):
            return None
        clean[key] = list(dict.fromkeys(n for n in names if _match(_NAME, n)))[:MAX_LIST_LEN]
    if not any(clean.values()):
        return None
    text = json.dumps(clean, separators=(",", ":"))
    return text if len(text) <= MAX_PLAN_LEN else None  # never cut JSON in half


def sanitize_audit_event(event: dict, *, key: str) -> dict[str, Any]:
    """The audit row for an event: whitelisted, typed fields only (`ts` is the database's now())."""
    code = event.get("error_code")
    ms = event.get("ms")
    return {
        "sub": _sub(event.get("sub")),
        "persona": _match(_IDENT, event.get("persona")),
        "tool": _match(_IDENT, event.get("tool")),
        "source": _match(_IDENT, event.get("source")),
        "metric_id": _match(_IDENT, event.get("metric_id")),
        "dimensions": _idents(event.get("dimensions")),
        "status": _match(_IDENT, event.get("status")),
        "rows": _count(event.get("rows")),
        "bytes": _count(event.get("bytes")),
        "truncated": event.get("truncated") if isinstance(event.get("truncated"), bool) else None,
        "ms": float(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None,
        "question_hash": _hash_of(event, key),
        "result_handle": _match(_HANDLE, event.get("result_handle")),
        "error_code": None if code is None else (_match(_CODE, code) or INVALID_CODE),
    }


def sanitize_query_event(event: dict, *, store_questions: bool, key: str) -> dict[str, Any]:
    """The query_log row for an event; the raw question and its plan are kept only when `store_questions`."""
    question = event.get("question")
    return {
        "sub": _sub(event.get("sub")),
        "persona": _match(_IDENT, event.get("persona")),
        "question_hash": _hash_of(event, key),
        "question": question[:MAX_QUESTION_LEN] if store_questions and isinstance(question, str) else None,
        "plan": sanitize_plan(event.get("plan")) if store_questions else None,
        "handles": _idents(event.get("handles"), _HANDLE),
        "metric_ids": _idents(event.get("metric_ids")),
        "verified": event.get("verified") is True,
        "status": _match(_IDENT, event.get("status")),
        "record_id": _match(RECORD_ID, event.get("record_id")),
        "metric_backed": event.get("metric_backed") is True,
    }


class AuditWriter:
    """Writes audit and query-log rows through an async pool; never raises from a write."""

    def __init__(self, pool: AsyncConnectionPool, *, store_questions: bool | None = None,
                 hmac_key: str | None = None, timeout_s: float = 2.0):
        self._pool = pool
        # None -> Settings (PRISM_STORE_QUESTIONS / PRISM_AUDIT_HMAC_KEY), so the bare constructor follows config
        settings = Settings() if store_questions is None or hmac_key is None else None
        self._store_questions = settings.store_questions if store_questions is None else store_questions
        self._key = settings.audit_hmac_key.get_secret_value() if hmac_key is None else hmac_key
        question_hash("", self._key)  # refuse a short key now, not on every write
        self._timeout_s = timeout_s
        self.dropped = 0
        self._last_drop: float | None = None   # time.monotonic() of the latest drop (/healthz: audit degraded)

    async def _execute(self, statement: str, params: dict[str, Any]) -> None:
        async with asyncio.timeout(self._timeout_s):
            async with self._pool.connection(timeout=self._timeout_s) as conn:
                await conn.execute(statement, params)

    def degraded(self, now: float | None = None) -> bool:
        """True when a row was dropped within the last AUDIT_DEGRADED_FOR_S seconds (no count is exposed)."""
        now = time.monotonic() if now is None else now
        return self._last_drop is not None and now - self._last_drop <= AUDIT_DEGRADED_FOR_S

    def _drop(self, kind: str, exc: BaseException | None) -> None:
        self.dropped += 1
        self._last_drop = time.monotonic()
        # exception type only: driver messages can echo connection details or parameter values
        audit_log.warning(json.dumps({"event": "audit_dropped", "kind": kind, "dropped": self.dropped,
                                      "error_type": type(exc).__name__ if exc else "invalid_event"}))

    async def write(self, event: dict) -> None:
        """Record one gateway call in app.audit (and the audit log). Never raises, except on cancellation."""
        try:
            row = sanitize_audit_event(event, key=self._key)
            audit_log.info(json.dumps({"event": "gateway_call", **row}, default=str))
            await self._execute(_INSERT_AUDIT, row)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - auditing must never break the caller's request
            self._drop("audit", exc)

    async def log_query(self, event: dict) -> bool:
        """Record an answered question in app.query_log: True when the row was inserted, False when it was dropped
        (invalid event or database failure, counted in `dropped`). Never raises, except on cancellation."""
        try:
            row = sanitize_query_event(event, store_questions=self._store_questions, key=self._key)
            if row["sub"] is None or row["question_hash"] is None:
                self._drop("query_log", None)
                return False
            await self._execute(_INSERT_QUERY, row)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - history capture must never break the caller's request
            self._drop("query_log", exc)
            return False

    async def confirm_answer(self, sub: str, record_id: str) -> bool:
        """Human confirmation: set verified on the caller's own metric-backed, ok row. True when such a row exists
        (confirming twice is fine), False for anything else (unknown id, another caller's row, not metric-backed,
        not ok, invalid input). Raises AuditUnavailable when the database fails: unlike log_query, the caller must
        be able to tell "no" from "try again"."""
        if not valid_sub(sub) or _match(RECORD_ID, record_id) is None:
            return False
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._pool.connection(timeout=self._timeout_s) as conn:
                    cur = await conn.execute(_CONFIRM, (record_id, sub))
                    return await cur.fetchone() is not None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AuditUnavailable(f"confirm unavailable ({type(exc).__name__})") from None

    async def query_history(self, sub: str, limit: int = 20) -> list[dict[str, Any]]:
        """This caller's recent questions, newest first (never another caller's).

        Raises ValueError for an invalid `sub` or `limit` and AuditUnavailable when the database fails."""
        if not valid_sub(sub):
            raise ValueError(f"sub must be a non-empty string of at most {MAX_SUB_LEN} characters")
        try:
            limit = max(1, min(int(limit), HISTORY_MAX))
        except (TypeError, ValueError, OverflowError):
            raise ValueError("limit must be an integer") from None
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._pool.connection(timeout=self._timeout_s) as conn:
                    cur = conn.cursor(row_factory=dict_row)
                    await cur.execute(_HISTORY, (sub, limit))
                    return await cur.fetchall()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise AuditUnavailable(f"query history unavailable ({type(exc).__name__})") from None


async def open_audit_pool(settings: Settings, *, max_size: int = 4) -> AsyncConnectionPool:
    """An opened pool for the app role. Does not wait for a connection: Postgres may be down at startup."""
    pool = AsyncConnectionPool(
        settings.app_dsn(APP_DB, connect_timeout=3),
        min_size=1, max_size=max_size, open=False,
        check=AsyncConnectionPool.check_connection, name="prism-audit",
    )
    await pool.open(wait=False)
    return pool


def configure_audit_logging(stream: IO[str] | None = None) -> None:
    """Attach exactly one explicit INFO handler to each audit logger, writing to `stream` (default stderr).

    Audit lines then reach the log whether or not anything configured the root logger. Idempotent:
    calling again with the same stream changes nothing; calling with a different stream replaces our
    handler (handlers added by others are left alone). `propagate` is set to False so a root handler
    does not print every audit line twice -- consequently root-level handlers (and pytest's caplog) do
    not see audit lines; capture them by passing a stream here.
    """
    target = stream or sys.stderr
    for name in AUDIT_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        ours = [h for h in logger.handlers if getattr(h, "_prism_audit", False)]
        if len(ours) == 1 and getattr(ours[0], "stream", None) is target:
            continue
        for old in ours:
            logger.removeHandler(old)
        handler = logging.StreamHandler(target)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._prism_audit = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
