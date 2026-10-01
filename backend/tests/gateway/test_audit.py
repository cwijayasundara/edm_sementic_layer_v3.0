"""AuditWriter: whitelisted, bound, never raises; query_log honours store_questions; audit lines reach the log."""
import hashlib
import hmac
import io
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from prism.config import APP_DB, Settings
from prism.db.app_migrate import migrate_app
from prism.gateway.audit import (
    AUDIT_DEGRADED_FOR_S,
    AUDIT_FIELDS,
    MAX_LIST_LEN,
    MAX_PLAN_LEN,
    AuditWriter,
    configure_audit_logging,
    open_audit_pool,
    question_hash,
)

pytestmark = pytest.mark.db

PREFIX = "testapp_"


def _qh(question: str) -> str:
    """The hash the default writer stores: HMAC keyed by the configured audit key."""
    return question_hash(question, Settings().audit_hmac_key.get_secret_value())


@pytest.fixture(scope="module")
def app_settings() -> Settings:
    settings = Settings(db_prefix=PREFIX)
    try:
        psycopg.connect(settings.dsn("postgres", admin=True), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc}). Start it with: make db", pytrace=False)
    migrate_app(settings)
    return settings


@pytest.fixture
async def pool(app_settings):
    pool = await open_audit_pool(app_settings)
    yield pool
    await pool.close()


def _audit_rows(settings: Settings, sub: str) -> list[dict]:
    with psycopg.connect(settings.dsn(APP_DB, admin=True)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT row_to_json(a) FROM app.audit a WHERE sub = %s ORDER BY id", (sub,)).fetchall()]


def _query_rows(settings: Settings, sub: str) -> list[dict]:
    with psycopg.connect(settings.dsn(APP_DB, admin=True)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT row_to_json(q) FROM app.query_log q WHERE sub = %s ORDER BY id", (sub,)).fetchall()]


def test_audit_fields_match_the_interface():
    assert AUDIT_FIELDS == ("ts", "sub", "persona", "tool", "source", "metric_id", "dimensions", "status", "rows",
                            "bytes", "truncated", "ms", "question_hash", "result_handle", "error_code")


def test_question_hash_is_hmac_sha256_keyed_by_a_server_secret():
    key = "k" * 40
    q = "How many open breaks?"
    assert question_hash(q, key) == hmac.new(key.encode(), q.encode(), hashlib.sha256).hexdigest()
    assert question_hash(q, key) != hashlib.sha256(q.encode()).hexdigest()  # not a plain, guessable digest
    assert question_hash(q, key) != question_hash(q, "j" * 40)


def test_question_hash_refuses_a_short_or_missing_key():
    for key in ("", "short", None):
        with pytest.raises(ValueError):
            question_hash("q", key)


async def test_write_stores_one_row_with_the_event_fields(pool, app_settings):
    sub = f"u-{uuid.uuid4().hex}"
    writer = AuditWriter(pool)
    await writer.write({"sub": sub, "persona": "cash_ops_emea", "tool": "run_metric", "source": "cashrecon",
                        "metric_id": "open_breaks", "dimensions": ["region", "currency"], "status": "ok",
                        "rows": 12, "bytes": 3456, "truncated": False, "ms": 41.5,
                        "question_hash": _qh("q"), "result_handle": "h_abc123", "error_code": None})
    assert writer.dropped == 0 and not writer.degraded()
    [row] = _audit_rows(app_settings, sub)
    assert row["persona"] == "cash_ops_emea" and row["tool"] == "run_metric" and row["source"] == "cashrecon"
    assert row["metric_id"] == "open_breaks" and row["dimensions"] == ["region", "currency"]
    assert (row["rows"], row["bytes"], row["truncated"], row["ms"]) == (12, 3456, False, 41.5)
    assert row["question_hash"] == _qh("q") and row["result_handle"] == "h_abc123"
    assert row["ts"]


async def test_audit_row_holds_no_query_text_values_or_tokens(pool, app_settings):
    sub = f"u-{uuid.uuid4().hex}"
    question = "Which accounts like ZZSECRETACCT9 have breaks over 1234567.89?"
    token = "eyJhbGciOiJIUzI1NiJ9.SECRETPAYLOAD.SECRETSIG"
    writer = AuditWriter(pool)
    await writer.write({
        "sub": sub, "tool": "run_metric", "source": "cashrecon", "metric_id": "open_breaks",
        "dimensions": ["account_id"], "status": "error", "rows": 1,
        "question": question,  # raw question: only its hash may be stored
        "token": token, "authorization": f"Bearer {token}",
        "filters": {"account_id": "ZZSECRETACCT9"}, "sample_rows": [{"account_id": "ZZSECRETACCT9", "amt": 1234567.89}],
        "sql": "SELECT * FROM breaks WHERE account_id = 'ZZSECRETACCT9'",
        "error_code": "source failed: account ZZSECRETACCT9 not found",  # free text is not a code
        "result_handle": "h_1 ZZSECRETACCT9",
    })
    [row] = _audit_rows(app_settings, sub)
    stored = json.dumps(row)
    for leaked in ("ZZSECRETACCT9", "1234567.89", "SECRETPAYLOAD", "SECRETSIG", "Bearer", "Which accounts", "SELECT"):
        assert leaked not in stored, leaked
    assert row["question_hash"] == _qh(question)
    assert row["error_code"] == "invalid"
    assert row["result_handle"] is None
    assert set(row) == {"id", *AUDIT_FIELDS}


async def test_non_numeric_counts_and_bad_identifiers_are_not_stored(pool, app_settings):
    sub = f"u-{uuid.uuid4().hex}"
    await AuditWriter(pool).write({"sub": sub, "tool": "run_metric", "rows": [{"v": "ZZROWVALUE"}], "bytes": "ZZBYTES",
                                   "metric_id": "open breaks ZZFREE TEXT", "dimensions": ["region", "ZZ bad dim"],
                                   "question_hash": "not-a-hash ZZTEXT", "truncated": "yes"})
    [row] = _audit_rows(app_settings, sub)
    assert "ZZ" not in json.dumps(row)
    assert row["rows"] is None and row["bytes"] is None and row["metric_id"] is None
    assert row["dimensions"] == ["region"] and row["question_hash"] is None and row["truncated"] is None


async def test_event_fields_are_bound_parameters_not_interpolated(pool, app_settings):
    sub = f"x'); DROP TABLE app.audit; -- {uuid.uuid4().hex}"
    question = "o'brien\"; DELETE FROM app.query_log; --"
    plan = {"metric_ids": ["open_breaks", "'); UPDATE app.audit SET status = 'pwned'; --"], "dimensions": ["region"]}
    writer = AuditWriter(pool)
    await writer.write({"sub": sub, "persona": "o'brien", "tool": "describe", "status": "ok"})
    await writer.log_query({"sub": sub, "question": question, "plan": plan, "verified": True})
    assert writer.dropped == 0
    [row] = _audit_rows(app_settings, sub)
    assert row["sub"] == sub and row["persona"] is None  # not an identifier: not stored
    [logged] = _query_rows(app_settings, sub)
    assert (logged["sub"], logged["question"]) == (sub, question)
    assert json.loads(logged["plan"]) == {"metric_ids": ["open_breaks"], "dimensions": ["region"]}
    with psycopg.connect(app_settings.dsn(APP_DB, admin=True)) as conn:
        assert conn.execute("SELECT to_regclass('app.audit')").fetchone()[0] is not None
        assert conn.execute("SELECT count(*) FROM app.audit WHERE status = 'pwned'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM app.query_log").fetchone()[0] > 0


def test_insert_statements_have_no_interpolated_values():
    from prism.gateway import audit

    for statement in (audit._INSERT_AUDIT, audit._INSERT_QUERY, audit._HISTORY):
        assert isinstance(statement, str) and "{" not in statement and "'" not in statement
    assert audit._INSERT_AUDIT.count("%(") == len(AUDIT_FIELDS) - 1  # ts is always the server's now()
    assert "%(ts)s" not in audit._INSERT_AUDIT and "now()" in audit._INSERT_AUDIT


async def test_write_with_postgres_down_does_not_raise_and_counts_a_drop(caplog):
    down = Settings(pg_host="127.0.0.1", pg_port=1)
    pool = await open_audit_pool(down)
    try:
        writer = AuditWriter(pool, timeout_s=1.0)
        started = time.monotonic()
        with caplog.at_level(logging.INFO, logger="prism.gateway.audit"):
            await writer.write({"sub": "s", "tool": "run_metric", "status": "ok", "token": "SECRETTOKEN"})
            stored = await writer.log_query({"sub": "s", "question": "SECRETQUESTION", "verified": True})
        assert stored is False  # a dropped insert is reported, so record_answer never claims it was recorded
        assert time.monotonic() - started < 5
        assert writer.dropped == 2 and writer.degraded()                  # a recent drop: /healthz says degraded
        assert not writer.degraded(now=time.monotonic() + AUDIT_DEGRADED_FOR_S + 1)   # and recovers once it is old
        assert "SECRETTOKEN" not in caplog.text and "SECRETQUESTION" not in caplog.text
        assert "audit_dropped" in caplog.text
    finally:
        await pool.close()


async def test_log_query_stores_question_when_allowed(pool, app_settings, monkeypatch):
    monkeypatch.delenv("PRISM_STORE_QUESTIONS", raising=False)
    sub = f"u-{uuid.uuid4().hex}"
    writer = AuditWriter(pool)  # store_questions defaults to True (demo)
    assert Settings().store_questions is True
    assert await writer.log_query({"sub": sub, "persona": "bi_analyst", "question": "How many open breaks?",
                            "plan": {"metric_ids": ["open_breaks"], "dimensions": ["region"]}, "handles": ["h_1", "h_2"],
                            "metric_ids": ["open_breaks"], "verified": True, "status": "ok", "token": "SECRETTOKEN"}) is True
    [row] = _query_rows(app_settings, sub)
    assert row["question"] == "How many open breaks?"
    assert row["question_hash"] == _qh("How many open breaks?")
    assert row["handles"] == ["h_1", "h_2"] and row["metric_ids"] == ["open_breaks"] and row["verified"] is True
    assert json.loads(row["plan"]) == {"metric_ids": ["open_breaks"], "dimensions": ["region"]}
    assert "SECRETTOKEN" not in json.dumps(row)


async def test_log_query_keeps_only_the_hash_when_questions_are_not_stored(pool, app_settings):
    sub = f"u-{uuid.uuid4().hex}"
    writer = AuditWriter(pool, store_questions=False)
    await writer.log_query({"sub": sub, "question": "How many open breaks?", "verified": False,
                            "plan": {"metric_ids": ["open_breaks"], "dimensions": ["region"]}})
    [row] = _query_rows(app_settings, sub)
    assert row["question"] is None and row["plan"] is None  # the plan is question-derived: same rule
    assert row["question_hash"] == _qh("How many open breaks?")


async def test_store_questions_setting_governs_the_default_writer(pool, app_settings, monkeypatch):
    monkeypatch.setenv("PRISM_STORE_QUESTIONS", "false")
    sub = f"u-{uuid.uuid4().hex}"
    await AuditWriter(pool).log_query({"sub": sub, "question": "How many open breaks?", "verified": True})
    [row] = _query_rows(app_settings, sub)
    assert row["question"] is None
    assert row["question_hash"] == _qh("How many open breaks?")


async def test_query_history_is_per_sub_newest_first_and_bounded(pool):
    sub, other = f"u-{uuid.uuid4().hex}", f"u-{uuid.uuid4().hex}"
    writer = AuditWriter(pool)
    for i in range(3):
        await writer.log_query({"sub": sub, "question": f"q{i}", "verified": True})
    await writer.log_query({"sub": other, "question": "not mine", "verified": True})
    history = await writer.query_history(sub, limit=2)
    assert [h["question"] for h in history] == ["q2", "q1"]
    assert all("sub" not in h for h in history)
    assert len(await writer.query_history(sub, limit=10_000)) == 3
    assert await writer.query_history("x' OR '1'='1", limit=5) == []


@pytest.fixture
def audit_loggers(monkeypatch):
    """Snapshot and restore the audit loggers so the explicit config does not leak into other tests."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])  # prove the lines arrive without any root handler
    for name in ("prism.mcp.audit", "prism.gateway.audit"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "handlers", list(logger.handlers))
        monkeypatch.setattr(logger, "propagate", logger.propagate)
        monkeypatch.setattr(logger, "level", logger.level)
    yield


def test_configure_audit_logging_replaces_the_handler_for_a_new_stream(audit_loggers):
    first, second = io.StringIO(), io.StringIO()
    configure_audit_logging(first)
    configure_audit_logging(second)
    for name in ("prism.mcp.audit", "prism.gateway.audit"):
        logger = logging.getLogger(name)
        assert len([h for h in logger.handlers if getattr(h, "_prism_audit", False)]) == 1
        logger.info("ZZLINE %s", name)
    assert "ZZLINE" not in first.getvalue()
    assert second.getvalue().count("ZZLINE") == 2


def test_configure_audit_logging_attaches_one_handler_without_root(audit_loggers):
    stream = io.StringIO()
    configure_audit_logging(stream)
    configure_audit_logging(stream)  # idempotent
    for name in ("prism.mcp.audit", "prism.gateway.audit"):
        logger = logging.getLogger(name)
        ours = [h for h in logger.handlers if getattr(h, "_prism_audit", False)]
        assert len(ours) == 1
        assert logger.isEnabledFor(logging.INFO)
        assert logger.propagate is False  # no duplicate lines when a root handler also exists
        logger.info('{"event": "tool_call", "tool": "%s"}', name)
    out = stream.getvalue()
    assert "prism.mcp.audit" in out and "prism.gateway.audit" in out
    assert out.count("tool_call") == 2


def test_source_server_entrypoints_configure_audit_logging(monkeypatch):
    from prism.mcp import servers

    calls = []
    monkeypatch.setattr(servers, "configure_audit_logging", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(servers, "create_app", lambda source: (None, f"app:{source}"))
    assert servers.cashrecon_app() == "app:cashrecon"
    assert calls == [1]


SECRET_EVENT_MARKERS = ("ZZQUESTION", "ZZPLANSECRET", "SECRETPAYLOAD", "SECRETSIG", "Bearer", "ZZFILTERVALUE",
                        "ZZROWVALUE", "ZZSQL", "ZZERRTEXT")


def _secret_laden_event(sub: str) -> dict:
    token = "eyJhbGciOiJIUzI1NiJ9.SECRETPAYLOAD.SECRETSIG"
    return {
        "sub": sub, "persona": "cash_ops_emea", "tool": "run_metric", "source": "cashrecon",
        "metric_id": "open_breaks", "dimensions": ["region"], "status": "error", "rows": 3,
        "question": "Which ZZQUESTION accounts have breaks?",
        "plan": "run_metric open_breaks where account_id = ZZPLANSECRET",
        "token": token, "authorization": f"Bearer {token}",
        "filters": {"account_id": "ZZFILTERVALUE"}, "filter_values": ["ZZFILTERVALUE"],
        "sample_rows": [{"account_id": "ZZROWVALUE"}], "sql": "SELECT ZZSQL FROM breaks",
        "error_code": "failed ZZERRTEXT",
    }


async def test_audit_log_line_carries_no_question_plan_token_or_values(pool, app_settings, audit_loggers):
    stream = io.StringIO()
    configure_audit_logging(stream)
    sub = f"u-{uuid.uuid4().hex}"
    await AuditWriter(pool).write(_secret_laden_event(sub))
    out = stream.getvalue()
    assert "gateway_call" in out and "open_breaks" in out and sub in out  # the line really was captured
    for leaked in SECRET_EVENT_MARKERS:
        assert leaked not in out, leaked
    [row] = _audit_rows(app_settings, sub)
    stored = json.dumps(row)
    for leaked in SECRET_EVENT_MARKERS:
        assert leaked not in stored, leaked


async def test_caller_supplied_ts_is_ignored(pool, app_settings):
    sub = f"u-{uuid.uuid4().hex}"
    await AuditWriter(pool).write({"sub": sub, "tool": "describe", "status": "ok",
                                   "ts": datetime(2000, 1, 1, tzinfo=UTC)})
    [row] = _audit_rows(app_settings, sub)
    stored = datetime.fromisoformat(row["ts"])
    assert abs(datetime.now(UTC) - stored) < timedelta(minutes=5)


async def test_writer_uses_the_configured_or_given_hmac_key(pool, app_settings):
    sub = f"u-{uuid.uuid4().hex}"
    key = "x" * 48
    writer = AuditWriter(pool, hmac_key=key)
    await writer.write({"sub": sub, "tool": "describe", "status": "ok", "question": "q?"})
    await writer.log_query({"sub": sub, "question": "q?", "verified": True})
    [row] = _audit_rows(app_settings, sub)
    [logged] = _query_rows(app_settings, sub)
    assert row["question_hash"] == logged["question_hash"] == question_hash("q?", key)


@pytest.mark.parametrize("plan", [
    "run_metric open_breaks by region",                      # free text
    ["open_breaks"],                                         # not the structured form
    {"metric_ids": "open_breaks"},                           # not a list
    {"steps": [{"tool": "run_metric", "metric_id": "open_breaks"}]},  # unknown shape
    {"metric_ids": ["ZZ bad"], "dimensions": ["Region!"]},   # nothing name-like survives
])
async def test_unstructured_plans_are_not_stored(pool, app_settings, plan):
    sub = f"u-{uuid.uuid4().hex}"
    await AuditWriter(pool, store_questions=True).log_query({"sub": sub, "question": "q", "plan": plan})
    [row] = _query_rows(app_settings, sub)
    assert row["plan"] is None


async def test_structured_plan_keeps_only_names(pool, app_settings):
    sub = f"u-{uuid.uuid4().hex}"
    plan = {"metric_ids": ["open_breaks", "x'; DROP", "Open_Breaks", "open_breaks\n", 7, "aged_open_breaks"],
            "dimensions": ["region", "account_id = 'ZZFILTERVALUE'", "currency"],
            "filters": {"account_id": "ZZFILTERVALUE"}, "question": "ZZQUESTION"}
    await AuditWriter(pool, store_questions=True).log_query({"sub": sub, "question": "q", "plan": plan})
    [row] = _query_rows(app_settings, sub)
    assert json.loads(row["plan"]) == {"metric_ids": ["open_breaks", "aged_open_breaks"],
                                       "dimensions": ["region", "currency"]}
    assert "ZZ" not in row["plan"]


async def test_structured_plan_is_capped(pool, app_settings):
    sub, big = f"u-{uuid.uuid4().hex}", f"u-{uuid.uuid4().hex}"
    writer = AuditWriter(pool, store_questions=True)
    await writer.log_query({"sub": sub, "question": "q",
                            "plan": {"metric_ids": [f"m{i}" for i in range(500)], "dimensions": []}})
    [row] = _query_rows(app_settings, sub)
    assert len(json.loads(row["plan"])["metric_ids"]) == MAX_LIST_LEN
    long_name = "m" + "x" * 62
    await writer.log_query({"sub": big, "question": "q",
                            "plan": {"metric_ids": [long_name] * 500, "dimensions": [long_name] * 500}})
    [row] = _query_rows(app_settings, big)
    assert row["plan"] is None or len(row["plan"]) <= MAX_PLAN_LEN


async def test_over_long_or_empty_sub_is_not_stored_or_truncated(pool, app_settings):
    prefix = (uuid.uuid4().hex * 8)[:256]  # unique per run: a 256-char sub that a longer one must not become
    writer = AuditWriter(pool)
    before = writer.dropped
    await writer.log_query({"sub": prefix + "-a", "question": "q", "verified": True})
    await writer.log_query({"sub": "", "question": "q", "verified": True})
    assert writer.dropped == before + 2
    assert _query_rows(app_settings, prefix) == []  # never truncated onto another caller's sub
    tool = f"t{uuid.uuid4().hex[:20]}"
    await writer.write({"sub": prefix + "-b", "tool": tool, "status": "ok"})
    with psycopg.connect(app_settings.dsn(APP_DB, admin=True)) as conn:
        [(stored_sub,)] = conn.execute("SELECT sub FROM app.audit WHERE tool = %s", (tool,)).fetchall()
    assert stored_sub is None


@pytest.mark.parametrize("sub", ["", None, 42, "u" * 257])
async def test_query_history_rejects_an_invalid_sub(pool, sub):
    with pytest.raises(ValueError):
        await AuditWriter(pool).query_history(sub)


@pytest.mark.parametrize("limit", [None, "abc", object(), float("nan")])
async def test_query_history_rejects_an_invalid_limit(pool, limit):
    with pytest.raises(ValueError):
        await AuditWriter(pool).query_history("u-1", limit=limit)
