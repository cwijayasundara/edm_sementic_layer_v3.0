"""Hardening / differential / fuzz tests for sql_guard, executed through the production path.

Production path: service user (NOT admin) -> one transaction with SET TRANSACTION READ ONLY,
SET LOCAL ROLE bi_reader, statement_timeout=2s, search_path=public, app.ctx cleared ->
cur.execute(safe_sql, prepare=True). Only the seeded ``test_*`` databases are touched.
"""

from __future__ import annotations

import random
import time
from collections import Counter

import psycopg
import pytest
from test_sql_guard import ALLOWED, POSITIVE

from prism.db.session import prepare_statements
from prism.mcp.sql_guard import (
    _DENY_FUNC_PREFIXES,
    _DENY_FUNC_SUBSTRINGS,
    _DENY_FUNCS,
    SqlGuardError,
    validate_select,
)

MAX_ROWS = 50
FORBIDDEN_IN_ERRORS = ("private", "prism_sec", "pg_catalog", "information_schema", "hmac")
DBS = ("cashrecon", "assetrecon", "feedhub")


@pytest.fixture(scope="module")
def svc_conns(seeded):
    cs = {db: psycopg.connect(seeded.dsn(db)) for db in DBS}
    yield cs
    for c in cs.values():
        c.close()


def _func_denied(name: str) -> bool:
    n = name.lower()
    return n in _DENY_FUNCS or n.startswith(_DENY_FUNC_PREFIXES) or any(x in n for x in _DENY_FUNC_SUBSTRINGS)


_VIEW_CACHE: dict[int, frozenset[str]] = {}


def _view_expansions(conn) -> frozenset[str]:
    """private.<v> for every masking view public.<v> in this database (cached per connection)."""
    key = id(conn)
    if key not in _VIEW_CACHE:
        with conn.cursor() as cur:
            cur.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relkind = 'v'")
            _VIEW_CACHE[key] = frozenset(f"private.{r[0]}" for r in cur.fetchall())
    return _VIEW_CACHE[key]


def plan_violations(node, view_expansions: frozenset[str] = frozenset()) -> list[str]:
    """Walk an EXPLAIN (FORMAT JSON) plan: every relation must be public.<allowed>, no denied functions.

    The plan shows the tables BEHIND views. The masking views in public (public.cash_accounts, ...) legitimately
    expand to private.<same name>; those exact expansions are passed in as ``view_expansions`` (derived from the
    database's own view list in the fixture). Default is strict: nothing outside public.<allowed> passes.
    """
    found: list[str] = []
    if isinstance(node, dict):
        rel = node.get("Relation Name")
        if rel is not None:
            schema = node.get("Schema")
            if f"{schema}.{rel}" in view_expansions:
                pass
            elif schema != "public" or f"public.{rel}" not in ALLOWED:
                found.append(f"relation {schema}.{rel}")
        fn = node.get("Function Name")
        if fn is not None and _func_denied(fn):
            found.append(f"function {fn}")
        for v in node.values():
            found += plan_violations(v, view_expansions)
    elif isinstance(node, list):
        for v in node:
            found += plan_violations(v, view_expansions)
    return found


class Outcome:
    """Result of one production-path execution plus the oracle signals."""

    def __init__(self, result, elapsed: float, plan: list[str], explain_error: psycopg.Error | None):
        self.result, self.elapsed, self.plan, self.explain_error = result, elapsed, plan, explain_error

    def flags(self) -> list[str]:
        out = [f"plan: {v}" for v in self.plan]
        for label, err in (("exec", self.result), ("explain", self.explain_error)):
            if isinstance(err, psycopg.Error) and err.sqlstate == "42501":
                out.append(f"{label} SQLSTATE 42501 (insufficient privilege): {err}")
        if isinstance(self.result, psycopg.Error):
            if self.result.sqlstate == "57014":
                out.append("SQLSTATE 57014 (canceled/timeout)")
            if _is_leak(self.result):
                out.append(f"leak-looking error: {self.result}")
        if self.elapsed > 0.5:
            out.append(f"slow: {self.elapsed:.2f}s")
        return out


def run_prod_full(conns, db: str, safe_sql: str) -> Outcome:
    """Production path + EXPLAIN plan oracle. EXPLAIN (no ANALYZE) does not execute the statement."""
    conn = conns[db]
    plan: list[str] = []
    explain_error = None
    result = None
    elapsed = 0.0
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                # Same preamble as production (prism.db.session); the extra search_path pin is what
                # Task 7's SqlBackend adds in its own scoped(), so tests and production cannot drift.
                for statement, args in prepare_statements(ctx="", timeout_ms=2000):
                    cur.execute(statement, args)
                cur.execute("SET LOCAL search_path = public")
                try:
                    with conn.transaction():  # savepoint: a failing EXPLAIN must not poison the txn
                        cur.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + safe_sql)
                        plan = plan_violations(cur.fetchone()[0], _view_expansions(conn))
                except psycopg.Error as e:
                    explain_error = e
                t = time.perf_counter()
                try:
                    cur.execute(safe_sql, prepare=True)
                    cols = [d.name for d in cur.description]
                    result = (cur.fetchall(), cols)
                finally:
                    elapsed = time.perf_counter() - t
    except psycopg.Error as e:
        result = e
    finally:
        if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            conn.rollback()
    return Outcome(result, elapsed, plan, explain_error)


def run_prod(conns, db: str, safe_sql: str):
    """Return (rows, column_names) or the psycopg exception, via the production path."""
    return run_prod_full(conns, db, safe_sql).result


def _is_leak(err: psycopg.Error) -> bool:
    """A forbidden object was actually reached (permission denied / relation lookup), as opposed to
    Postgres merely echoing a bogus column qualifier ("missing FROM-clause entry for table ...")."""
    msg = str(err).lower()
    if not any(w in msg for w in FORBIDDEN_IN_ERRORS):
        return False
    return not msg.lstrip().startswith("missing from-clause entry")


def _validate(sql: str) -> str:
    return validate_select(sql, allowed_tables=ALLOWED, max_rows=MAX_ROWS)


def test_prod_path_sanity(svc_conns):
    r = run_prod(svc_conns, "cashrecon", _validate("SELECT 1 AS one"))
    assert r == ([(1,)], ["one"])
    # and the DB really does block the base table for this role
    r = run_prod(svc_conns, "cashrecon", "SELECT * FROM private.cash_accounts")
    assert isinstance(r, psycopg.Error)


def test_plan_oracle_flags_forbidden_relations_and_functions():
    bad_catalog = {"Plan": {"Node Type": "Seq Scan", "Relation Name": "pg_roles", "Schema": "pg_catalog"}}
    bad_private = [{"Plan": {"Plans": [{"Relation Name": "cash_accounts", "Schema": "private"}]}}]
    good = [{"Plan": {"Node Type": "Seq Scan", "Relation Name": "breaks", "Schema": "public"}}]
    bad_func = {"Plan": {"Node Type": "Function Scan", "Function Name": "pg_ls_dir"}}
    assert plan_violations(bad_catalog)
    assert plan_violations(bad_private)
    assert plan_violations(bad_func)
    assert plan_violations(good) == []
    # a view expansion is only tolerated when explicitly declared
    assert plan_violations(bad_private, frozenset({"private.cash_accounts"})) == []
    assert plan_violations(bad_catalog, frozenset({"private.cash_accounts"}))
    assert plan_violations({"Plan": {"Node Type": "Function Scan", "Function Name": "generate_series"}}) == []


def test_oracle_classification_flags_sleep_and_permission_errors(svc_conns):
    slow = run_prod_full(svc_conns, "cashrecon", "SELECT pg_sleep(0.7)")  # bypasses validate_select
    assert any(f.startswith("slow") for f in slow.flags()), slow.flags()
    denied = run_prod_full(svc_conns, "cashrecon", "SELECT * FROM private.cash_accounts")
    assert any("42501" in f for f in denied.flags()), denied.flags()
    assert any("private" in f for f in denied.plan) or denied.explain_error is not None
    clean = run_prod_full(svc_conns, "cashrecon", _validate("SELECT count(*) FROM breaks"))
    assert clean.flags() == [], clean.flags()


# --------------------------------------------------------------------------------------
# B1: backslash / E-string differentials
# --------------------------------------------------------------------------------------
REJECT = "REJECT"
SAFE_IF_ACCEPTED = "SAFE"  # syntax-error in Postgres; if the guard accepts it the DB must not run it
NL = "\n"

B1_CASES: list[tuple[str, object]] = [
    (r"SELECT 'a\b' AS x", "a\\b"),
    (r"SELECT length('\s+') AS x", 3),
    (r"SELECT regexp_replace('a  b', '\s+', '_', 'g') AS x", "a_b"),
    (r"SELECT 'a\nb' AS x", "a\\nb"),
    (r"SELECT '%\_%' AS x", "%\\_%"),
    ("SELECT 'a\\" + NL + "b' AS x", "a\\\nb"),
    (r"SELECT 'a\ b' AS x", "a\\ b"),
    (r'SELECT ' + "'a" + "\\" + '"b' + "' AS x", 'a\\"b'),
    (r"SELECT 'a\$$b' AS x", "a\\$$b"),
    (r"SELECT $$a\$$ AS x", "a\\"),  # dollar-quoted: value is a single backslash after 'a'
    (r"SELECT $$a\b$$ AS x", "a\\b"),
    (r"SELECT 1 AS x -- trailing \ backslash", 1),
    (r"SELECT 1 /* \ */ AS x", 1),
    (r"SELECT '\' AS x", REJECT),
    (r"SELECT '\\' AS x", REJECT),
    (r"SELECT '\' || (SELECT count(*) FROM private.cash_accounts) || '\' AS x", REJECT),
    (r"SELECT $$a\$$ || (SELECT count(*) FROM private.cash_accounts) || $$\$$ AS x", REJECT),
    (r"SELECT 1 AS x -- \'", REJECT),
    ("SELECT 1 AS \"a\\\"", SAFE_IF_ACCEPTED),
    ("SELECT 1 \\", REJECT),
    ("SELECT 1 AS x \\" + NL, REJECT),
    ("SELECT e'x' AS x", REJECT),
    ("SELECT E'x' AS x", REJECT),
    ("SELECT E  'x' AS x", REJECT),
    ("SELECT e" + NL + "'x' AS x", REJECT),
    ("SELECT e\t'x' AS x", REJECT),
    ("SELECT E\u00a0'x' AS x", SAFE_IF_ACCEPTED),
    ("SELECT E\u2009'x' AS x", SAFE_IF_ACCEPTED),
    ("SELECT E\u3000'x' AS x", SAFE_IF_ACCEPTED),
    ("SELECT \uff25'x' AS x", SAFE_IF_ACCEPTED),
    ("SELECT \uff45'x' AS x", SAFE_IF_ACCEPTED),
    (r"WITH c AS (SELECT E'a\\' AS x) SELECT * FROM c", REJECT),
    (r"SELECT (SELECT E'\\' || ' , (SELECT 1) --') AS x", REJECT),
    (r"SELECT 1 WHERE 'a' LIKE '%\_%' ESCAPE '\'", REJECT),
    (r"SELECT 1 WHERE 'a' SIMILAR TO '%\_%' ESCAPE '\'", REJECT),
    (r"SELECT 1 WHERE 'a' LIKE 'a\\' ESCAPE '!'", REJECT),
    (r"SELECT $t$a\$t$ AS x", "a\\"),  # tagged dollar quote: accept-and-verify or reject
]

# Relaxed-rule cases that MUST be accepted (over-rejection is a regression) and return the literal value.
B1_MUST_ACCEPT = [
    (r"SELECT 'a\b' AS x", "a\\b"),
    (r"SELECT length('\s+') AS x", 3),
    (r"SELECT regexp_replace('a  b', '\s+', '_', 'g') AS x", "a_b"),
    (r"SELECT $$a\$$ AS x", "a\\"),
    ('SELECT 1 AS "a\\"', 1),
]


def _assert_clean(out: Outcome, sql: str) -> None:
    assert out.flags() == [], (sql, out.flags())


@pytest.mark.parametrize(("sql", "expected"), B1_MUST_ACCEPT, ids=[repr(c[0])[:60] for c in B1_MUST_ACCEPT])
def test_b1_must_accept(svc_conns, sql, expected):
    safe = _validate(sql)  # must NOT raise SqlGuardError
    out = run_prod_full(svc_conns, "cashrecon", safe)
    _assert_clean(out, sql)
    assert not isinstance(out.result, psycopg.Error), (sql, safe, out.result)
    assert out.result[0][0][0] == expected, (sql, safe, out.result)


@pytest.mark.parametrize(("sql", "expected"), B1_CASES, ids=[repr(c[0])[:60] for c in B1_CASES])
def test_b1_backslash_differentials(svc_conns, sql, expected):
    if expected == REJECT:
        with pytest.raises(SqlGuardError):
            _validate(sql)
        return
    try:
        safe = _validate(sql)
    except SqlGuardError:
        return  # rejection is always acceptable for the SAFE_IF_ACCEPTED class and literal cases
    out = run_prod_full(svc_conns, "cashrecon", safe)
    r = out.result
    if expected == SAFE_IF_ACCEPTED:
        assert not out.plan and not (isinstance(r, psycopg.Error) and _is_leak(r)), (sql, r, out.plan)
        return
    _assert_clean(out, sql)
    assert not isinstance(r, psycopg.Error), (sql, safe, r)
    assert r[0][0][0] == expected, (sql, safe, r)


def test_b1_quoted_identifier_backslash_column_name(svc_conns):
    safe = _validate('SELECT 1 AS "a\\"')
    r = run_prod(svc_conns, "cashrecon", safe)
    assert r == ([(1,)], ["a\\"])


# --------------------------------------------------------------------------------------
# B2: identity functions in every form
# --------------------------------------------------------------------------------------
IDENTITY = [
    "current_user", "session_user", "system_user", "current_database", "current_catalog",
    "inet_client_port", "current_query", "current_schema", "current_schemas", "current_role", "user",
]
FORMS = {
    "bare": "SELECT {n}",
    "call": "SELECT {n}()",
    "upper": "SELECT {N}",
    "upper_call": "SELECT {N}()",
    "quoted": 'SELECT "{n}"',
    "scalar_subquery": "SELECT (SELECT {n})",
    "cte": "WITH c AS (SELECT {n} AS v) SELECT * FROM c",
    "order_by": "SELECT 1 AS a ORDER BY {n}",
    "where": "SELECT 1 WHERE {n} IS NOT NULL",
    "case": "SELECT CASE WHEN 1 = 1 THEN {n} END",
    "values": "SELECT * FROM (VALUES ({n})) AS t(v)",
    "func_arg": "SELECT lower({n}::text)",
    "lateral": "SELECT * FROM (SELECT 1) a, LATERAL (SELECT {n} AS v) b",
    "group_by": "SELECT count(*) FROM breaks GROUP BY {n}",
    "having": "SELECT count(*) FROM breaks HAVING {n} IS NOT NULL",
    "join_on": "SELECT 1 FROM breaks a JOIN breaks b ON {n} IS NOT NULL",
    "union_branch": "SELECT 1 UNION SELECT {n}",
    "in_list": "SELECT 1 WHERE 1 IN (1, {n})",
    "partition_by": "SELECT row_number() OVER (PARTITION BY {n}) FROM breaks",
    "quoted_call": 'SELECT "{n}"()',
}


@pytest.mark.parametrize("form", list(FORMS))
@pytest.mark.parametrize("name", IDENTITY)
def test_b2_identity_functions_rejected_in_every_form(name, form):
    sql = FORMS[form].format(n=name, N=name.upper())
    with pytest.raises(SqlGuardError):
        _validate(sql)


def test_b2_quoted_form_outcome_is_guard_rejection():
    # Recorded outcome: a quoted "current_user" (a column reference in Postgres) is rejected
    # by the guard itself, not left to fail at the database.
    with pytest.raises(SqlGuardError):
        _validate('SELECT "current_user"')


# --------------------------------------------------------------------------------------
# B3: deterministic fuzz
# --------------------------------------------------------------------------------------
# Expected benign error classes for accepted-then-failed mutations (syntax / type / name errors only;
# never privileges 42501 or timeouts 57014).
BENIGN_SQLSTATES = {
    "42601", "42703", "42883", "42804", "42P01", "42P10", "42803", "42809", "22P02", "22007", "22008",
    "22012", "22023", "22003", "0A000", "42725", "42702", "42P18", "42846", "42P20",
}

POOL = [
    "SELECT", "FROM", "WHERE", "UNION", "UNION ALL", "WITH", "AS", "JOIN", "LATERAL", "LIMIT 1",
    ";", "--", "/* x */", "/*", "*/", "$$", "$t$", "'", '"', "\\", "\\'", "'\\'", "E'x'", "e'\\\\'",
    "\u00a0", "\u2009", "\uff25", "\uff33\uff25\uff2c\uff25\uff23\uff34", "\u0131", "\u212a",
    "(", ")", "pg_sleep(1)", "private.cash_accounts", "information_schema.tables",
    "pg_catalog.pg_roles", "date_bin('1 day', now(), 'x')", "current_user", "date_part('epoch', now())",
    "date_bin('15 minutes', now(), 'CASE WHEN PG_SLEEP(3) IS NULL THEN CURRENT_TIMESTAMP ELSE CURRENT_TIMESTAMP END')",
    "OPERATOR(private.+)", "0o17", "U&'d'", " user ", "ts_stat(", "query_to_xml(", "lo_import(",
    "getpgusername()", "date_part(", "1_000", "0x1F", "OPERATOR(pg_catalog.+)", "::regclass", "set_config('a','b',false)", ",", ".", "*", "\n", "\t",
]


def _tokens(sql: str) -> list[str]:
    # split on ASCII space only, so NBSP/thin-space/tab/newline survive inside tokens
    return [t for t in sql.replace("(", " ( ").replace(")", " ) ").replace(",", " , ").split(" ") if t]


def _mutate(rng: random.Random, sql: str) -> str:
    for _ in range(rng.randint(1, 3)):
        toks = _tokens(sql)
        if not toks:
            break
        i = rng.randrange(len(toks))
        op = rng.randrange(6)
        if op == 0:
            toks.insert(i, rng.choice(POOL))
        elif op == 1:
            del toks[i]
        elif op == 2:
            toks.insert(i, toks[i])
        elif op == 3 and len(toks) > 1:
            j = rng.randrange(len(toks))
            toks[i], toks[j] = toks[j], toks[i]
        elif op == 4:
            toks[i] = toks[i] + rng.choice(POOL)
        else:
            toks[i] = rng.choice(POOL)
        sql = " ".join(toks)
    return sql[:4000]


@pytest.mark.slow
def test_b3_deterministic_fuzz(svc_conns):
    rng = random.Random(20260930)
    seeds = [(db, sql) for db, sql in POSITIVE if len(sql) < 600][:25]
    assert len(seeds) >= 20
    total = accepted = rejected = db_errors = 0
    sqlstates: Counter[str] = Counter()
    offenders: list[str] = []
    flagged: list[str] = []
    started = time.perf_counter()
    while total < 1800:
        db, seed = seeds[total % len(seeds)]
        sql = _mutate(rng, seed)
        total += 1
        try:
            safe = _validate(sql)
        except SqlGuardError:
            rejected += 1
            continue
        except BaseException as e:  # noqa: BLE001 - the point of the test
            offenders.append(f"{type(e).__name__}: {sql!r}")
            continue
        assert isinstance(safe, str)
        accepted += 1
        out = run_prod_full(svc_conns, db, safe)
        for f in out.flags():
            flagged.append(f"{f} :: {sql!r}")
        if out.explain_error is not None and out.explain_error.sqlstate == "42501":
            flagged.append(f"EXPLAIN 42501 :: {sql!r}")
        if isinstance(out.result, psycopg.Error):
            db_errors += 1
            sqlstates[str(out.result.sqlstate)] += 1
            if out.result.sqlstate not in BENIGN_SQLSTATES:
                flagged.append(f"unexpected SQLSTATE {out.result.sqlstate}: {out.result} :: {sql!r}")
    print(
        f"\nFUZZ total={total} accepted={accepted} rejected={rejected} "
        f"accepted_then_db_error={db_errors} sqlstates={dict(sqlstates)} "
        f"seconds={time.perf_counter() - started:.1f}"
    )
    assert total >= 1500
    assert not offenders, "non-SqlGuardError exceptions:\n" + "\n".join(offenders[:10])
    assert not flagged, "oracle flags on accepted statements:\n" + "\n".join(flagged[:10])
