"""combine(): DuckDB over the caller's own handles only. Every hostile statement is run through the real combine()
(sqlglot guard + hardened in-memory DuckDB); the engine hardening is also tested on its own, guard bypassed."""
import time

import duckdb
import pytest

from prism.gateway.combine import MAX_ROWS, combine, hardened_connection
from prism.gateway.errors import GatewayError
from prism.gateway.results import ResultStore

AMOUNT = "amount (transaction currency)"


@pytest.fixture
def store():
    return ResultStore()


@pytest.fixture
def h(store):
    """Alice's handles: amounts per ccy/region, break counts per region, a base-currency amount, a free-form result."""
    amt = store.put("alice", ["region", "ccy", "value"],
                    [["EMEA", "EUR", 100.0], ["EMEA", "USD", 50.0], ["APAC", "USD", 7.5], ["APAC", "JPY", 9000.0]],
                    {"units": {"value": AMOUNT}, "source": "cashrecon", "metric_id": "open_break_amount"})
    cnt = store.put("alice", ["region", "value"], [["EMEA", 3], ["APAC", 2]],
                    {"units": {"value": "breaks"}, "source": "cashrecon", "metric_id": "open_breaks"})
    base = store.put("alice", ["portfolio_id", "value"], [["PF1", 10.0], ["PF2", 5.0]],
                     {"units": {"value": "amount (base currency)"}})
    raw = store.put("alice", ["ccy", "amount", "n"], [["EUR", 1.0, 1], ["USD", 2.0, 2]], {})   # no units known
    return {"amt": amt, "cnt": cnt, "base": base, "raw": raw}


def run(store, h, sql, **names):
    names = names or {"amt": h["amt"], "cnt": h["cnt"]}
    out = combine(store, "alice", sql, names)
    return store.get("alice", out)


def code_of(store, h, sql, names=None, sub="alice"):
    with pytest.raises(GatewayError) as info:
        combine(store, sub, sql, names or {"amt": h["amt"], "cnt": h["cnt"], "base": h["base"], "raw": h["raw"]})
    return info.value.code


# ------------------------------------------------------------------------------------------------- happy path
def test_join_of_two_handles(store, h):
    r = run(store, h, "SELECT c.region, a.ccy, sum(a.value) AS amount, max(c.value) AS breaks FROM amt a "
                      "JOIN cnt c ON a.region = c.region GROUP BY c.region, a.ccy ORDER BY c.region, a.ccy")
    assert r.columns == ("region", "ccy", "amount", "breaks")
    assert r.rows == (("APAC", "JPY", 9000.0, 2), ("APAC", "USD", 7.5, 2), ("EMEA", "EUR", 100.0, 3),
                      ("EMEA", "USD", 50.0, 3))
    assert r.meta["truncated"] is False and r.meta["partial"] is False and r.meta["source"] == "combine"


def test_result_is_a_handle_of_the_same_sub_only(store, h):
    out = combine(store, "alice", "SELECT region FROM cnt", {"cnt": h["cnt"]})
    with pytest.raises(GatewayError) as info:
        store.get("bob", out)
    assert info.value.code == "unknown_handle"


def test_another_subs_handle_is_unknown(store, h):
    assert code_of(store, h, "SELECT * FROM cnt", {"cnt": h["cnt"]}, sub="bob") == "unknown_handle"
    assert code_of(store, h, "SELECT * FROM x", {"x": "r_000000000000"}) == "unknown_handle"


def test_trailing_semicolon_comments_and_case_are_fine(store, h):
    r = run(store, h, "-- hi\nSELECT /* x */ REGION, Value FROM CNT ORDER BY 1;  ", cnt=h["cnt"])
    assert r.rows == (("APAC", 2), ("EMEA", 3))


def test_cte_and_union_allowed(store, h):
    r = run(store, h, "WITH e AS (SELECT region FROM cnt WHERE region = 'EMEA') SELECT region FROM e "
                      "UNION ALL SELECT region FROM cnt ORDER BY region", cnt=h["cnt"])
    assert r.rows == (("APAC",), ("EMEA",), ("EMEA",))


def test_row_cap_and_partial_flags(store, h):
    big = store.put("alice", ["n"], [[i] for i in range(200)])
    r = run(store, h, "SELECT x.n, y.n AS m FROM big x, big y ORDER BY x.n, m", big=big)
    assert len(r.rows) == MAX_ROWS == 10_000 and r.meta["truncated"] is True
    assert r.rows[0] == (0, 0) and r.rows[-1] == (49, 199)
    cut = store.put("alice", ["n"], [[1]], {"truncated": True})
    r = run(store, h, "SELECT n FROM cut", cut=cut)
    assert r.meta["partial"] is True and r.meta["truncated"] is True


def test_mixed_value_types_load_as_text(store, h):
    odd = store.put("alice", ["v", "Dup", "dup", "nothing", "big"],
                    [[1, "a", "b", None, 2**70], ["x", "c", "d", None, 1], [True, "e", "f", None, 2]])
    r = run(store, h, "SELECT v, dup, dup_2, nothing, big FROM odd ORDER BY v", odd=odd)
    assert r.columns == ("v", "dup", "dup_2", "nothing", "big")
    assert {row[0] for row in r.rows} == {"1", "x", "true"}


# ------------------------------------------------------------------------------------------ hostile corpus
HOSTILE = [
    "SELECT 1; SELECT 2",
    "SELECT * FROM cnt; DROP TABLE cnt",
    "SELECT * FROM cnt;;SELECT 1",
    "ATTACH ':memory:' AS x",
    "ATTACH '/tmp/x.db' AS x",
    "DETACH x",
    "COPY cnt TO '/tmp/out.csv'",
    "COPY (SELECT * FROM cnt) TO '/tmp/out.parquet' (FORMAT parquet)",
    "INSTALL httpfs",
    "LOAD httpfs",
    "FORCE INSTALL httpfs",
    "PRAGMA database_list",
    "PRAGMA enable_profiling",
    "SET enable_external_access = true",
    "RESET lock_configuration",
    "SET VARIABLE x = 1",
    "CREATE TABLE z AS SELECT 1",
    "CREATE MACRO m(x) AS x",
    "INSERT INTO cnt VALUES ('X', 1)",
    "UPDATE cnt SET value = 0",
    "DELETE FROM cnt",
    "DROP TABLE cnt",
    "EXPORT DATABASE '/tmp/x'",
    "CALL pragma_version()",
    "DESCRIBE cnt",
    "SHOW TABLES",
    "SUMMARIZE cnt",
    "EXPLAIN SELECT 1",
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM \"/etc/passwd\"",
    "SELECT * FROM 'cnt.csv'",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_csv_auto('/etc/passwd')",
    "SELECT * FROM read_parquet('/tmp/x.parquet')",
    "SELECT * FROM read_json('/etc/passwd')",
    "SELECT * FROM read_text('/etc/passwd')",
    "SELECT * FROM read_blob('/etc/passwd')",
    "SELECT read_text('/etc/passwd')",
    "SELECT * FROM glob('/etc/*')",
    "SELECT * FROM \"read_csv\"('/etc/passwd')",
    "SELECT * FROM READ_CSV('/etc/passwd')",
    "SELECT * FROM range(10)",
    "SELECT * FROM generate_series(1, 10)",
    "SELECT * FROM query('SELECT 1')",
    "SELECT * FROM query_table('cnt')",
    "SELECT * FROM duckdb_settings()",
    "SELECT * FROM duckdb_tables",
    "SELECT * FROM duckdb_tables()",
    "SELECT * FROM information_schema.tables",
    "SELECT * FROM main.cnt",
    "SELECT * FROM memory.main.cnt",
    "SELECT * FROM system.main.duckdb_settings",
    "SELECT * FROM pg_catalog.pg_tables",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM pragma_database_list",
    "SELECT current_setting('enable_external_access')",
    "SELECT getenv('HOME')",
    "SELECT getvariable('x')",
    "SELECT version()",
    "SELECT * FROM cnt, LATERAL read_csv('/etc/passwd')",
    "SELECT * FROM (SELECT * FROM read_csv('/etc/passwd'))",
    "WITH x AS (SELECT * FROM read_csv('/etc/passwd')) SELECT * FROM x",
    "WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT * FROM t",
    "WITH x AS MATERIALIZED (SELECT * FROM glob('*')) SELECT * FROM x",
    "SELECT * FROM cnt WHERE region IN (SELECT * FROM read_text('/etc/passwd'))",
    "SELECT (SELECT content FROM read_text('/etc/passwd'))",
    "SELECT * FROM cnt UNION SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM unnest([1, 2])",
    "SELECT * FROM (VALUES (1), (2)) t(x)",
    "SELECT list_transform([1], x -> x + 1)",
    "SELECT COLUMNS('.*') FROM cnt",
    "SELECT * FROM cnt PIVOT (sum(value) FOR region IN ('EMEA'))",
    "SELECT $1",
    "SELECT ?",
    "SELECT * INTO z FROM cnt",
    "SELECT region FROM cnt; ATTACH 'x' AS y",
    "SELECT region FROM cnt -- ; ATTACH 'x'\n; ATTACH 'x' AS y",
    "SELECT ';' AS s; ATTACH 'x' AS y",
    "/* ; */ ATTACH 'x' AS y",
    "SELECT $$;$$ AS s; ATTACH 'x' AS y",
    "SELECT * FROM cnt; ATTACH 'x' AS y",                    # Greek question mark
    "SELECT * FROM ｃｎｔ",                                          # full-width identifier
    "SELECT * FROM cnt\x00; ATTACH 'x' AS y",
    "SELECT * FROM \"cnt\"\"; ATTACH 'x' AS y; --\"",
    "SELECT * FROM nope",
    "SELECT nope FROM cnt",
    "SELECT cnt FROM cnt",                                           # whole-row struct reference
    "SELECT s['value'] FROM (SELECT cnt AS s FROM cnt)",
    "BEGIN TRANSACTION",
    "CHECKPOINT",
    "USE memory",
    "",
    "   ;  ",
    "-- only a comment",
]


@pytest.mark.parametrize("sql", HOSTILE)
def test_hostile_sql_is_rejected(store, h, sql):
    assert code_of(store, h, sql) in ("invalid_sql", "sql_not_allowed")


@pytest.mark.parametrize("sql", [None, 5, ["SELECT 1"], "SELECT 1 " + " " * 20_001])
def test_sql_must_be_a_bounded_string(store, h, sql):
    assert code_of(store, h, sql) == "invalid_sql"


@pytest.mark.parametrize("names", [{}, {"Cnt": "x"}, {"1x": "x"}, {"a b": "x"}, {"duckdb_x": "x"},
                                   {"information_schema": "x"}, {"main": "x"}, {"pg_x": "x"}, {"x": 5},
                                   {f"t{i}": "x" for i in range(9)}, ["cnt"], None, {"sqlite_master": "x"}])
def test_handle_aliases_are_validated(store, h, names):
    with pytest.raises(GatewayError) as info:
        combine(store, "alice", "SELECT 1", names)
    assert info.value.code in ("invalid_request", "unknown_handle")


def test_engine_is_hardened_even_without_the_guard():
    conn = hardened_connection()
    try:
        for stmt in ("ATTACH '/tmp/prism_x.db' AS x", "COPY (SELECT 1) TO '/tmp/prism_x.csv'", "INSTALL httpfs",
                     "LOAD httpfs", "SELECT * FROM read_csv('/etc/passwd')", "SELECT * FROM read_text('/etc/passwd')",
                     "SELECT * FROM glob('/etc/*')", "SET enable_external_access = true",
                     "RESET enable_external_access", "SET lock_configuration = false", "SET threads = 64",
                     "SET memory_limit = '64GB'", "SET temp_directory = '/tmp'", "EXPORT DATABASE '/tmp/prism_x'"):
            with pytest.raises(duckdb.Error):
                conn.execute(stmt)
    finally:
        conn.close()


def test_cross_join_bomb_times_out_within_about_6_seconds(store, h):
    n = store.put("alice", ["n"], [[i] for i in range(3000)])
    t = time.perf_counter()
    with pytest.raises(GatewayError) as info:
        combine(store, "alice", "SELECT sum(a.n * b.n * c.n) FROM n a, n b, n c", {"n": n})
    assert info.value.code == "combine_timeout"
    assert time.perf_counter() - t < 6.5


def test_runtime_errors_are_user_facing_and_short(store, h):
    with pytest.raises(GatewayError) as info:
        combine(store, "alice", "SELECT CAST(region AS INTEGER) FROM cnt", {"cnt": h["cnt"]})
    assert info.value.code == "combine_failed" and len(str(info.value)) < 400


# ----------------------------------------------------------------------------------------------- currency
ALLOWED_CCY = [
    "SELECT ccy, sum(value) FROM amt GROUP BY ccy",
    "SELECT region, ccy, sum(value) AS t FROM amt GROUP BY region, ccy",
    "SELECT ccy, sum(value) FROM amt GROUP BY 1",
    "SELECT ccy, sum(value) FROM amt GROUP BY ALL",
    "SELECT ccy, region, value FROM amt",
    "SELECT ccy, value * 2 AS doubled FROM amt",
    "SELECT count(*), count(value), count(DISTINCT value) FROM amt",
    "SELECT region, ccy, sum(value) OVER (PARTITION BY ccy) FROM amt",
    "WITH t AS (SELECT ccy AS c, value AS v FROM amt) SELECT c, sum(v) FROM t GROUP BY c",
    "SELECT ccy, sum(t) FROM (SELECT ccy, region, sum(value) AS t FROM amt GROUP BY ccy, region) GROUP BY ccy",
    "SELECT a.ccy, sum(a.value), sum(c.value) FROM amt a JOIN cnt c USING (region) GROUP BY a.ccy",
    "SELECT sum(value) FROM base",                                  # base-currency amount, no ccy column
    "SELECT region, sum(value) FROM cnt GROUP BY region",           # counts are not amounts
    "SELECT ccy, sum(amount), sum(n) FROM raw GROUP BY ccy",
    "SELECT ccy, sum(value) FILTER (WHERE region = 'EMEA') FROM amt GROUP BY ccy",
]

MIXING = [
    "SELECT sum(value) FROM amt",
    "SELECT region, sum(value) FROM amt GROUP BY region",
    "SELECT region, avg(value) FROM amt GROUP BY region",
    "SELECT max(value) FROM amt",
    "SELECT min(value) FROM amt",
    "SELECT array_agg(value) FROM amt",
    "SELECT list(value) FROM amt",
    "SELECT string_agg(CAST(value AS VARCHAR), ',') FROM amt",
    "SELECT sum(value) OVER () FROM amt",
    "SELECT sum(value) OVER (PARTITION BY region) FROM amt",
    "SELECT region, sum(value) OVER (PARTITION BY ccy) FROM amt",          # per-currency, but unlabelled
    "SELECT sum(value) FROM amt GROUP BY ccy",                             # per-currency, but unlabelled
    "SELECT ccy, sum(value) FROM amt GROUP BY ROLLUP (ccy)",
    "SELECT ccy, sum(value) FROM amt GROUP BY CUBE (ccy)",
    "SELECT ccy, sum(value) FROM amt GROUP BY GROUPING SETS ((ccy), ())",
    "SELECT region, sum(value) FROM amt GROUP BY ALL",
    "WITH t AS (SELECT value AS v FROM amt) SELECT sum(v) FROM t",
    "WITH t(v) AS (SELECT value FROM amt) SELECT sum(v) FROM t",
    "SELECT sum(v) FROM (SELECT value * 1 AS v FROM amt)",
    "SELECT sum(t) FROM (SELECT ccy, sum(value) AS t FROM amt GROUP BY ccy)",
    "SELECT ccy, sum(v) FROM (SELECT 'EUR' AS ccy, value AS v FROM amt) GROUP BY ccy",
    "SELECT c, sum(value) FROM (SELECT upper(ccy) AS c, value FROM amt) GROUP BY c",
    "SELECT r, sum(value) FROM (SELECT region AS r, value FROM amt) GROUP BY r",
    "SELECT c.region, sum(a.value) FROM amt a JOIN cnt c USING (region) GROUP BY c.region",
    "SELECT b.ccy, sum(a.value) FROM amt a, raw b GROUP BY b.ccy",
    "SELECT (SELECT sum(value) FROM amt) AS s",
    "SELECT region FROM cnt WHERE value > (SELECT avg(value) FROM amt)",
    "SELECT sum(amount) FROM raw",
    "SELECT sum(n) FROM raw",
    "SELECT ccy, sum(value) FROM amt UNION ALL SELECT 'ALL', sum(value) FROM amt GROUP BY ccy",
    "SELECT x.ccy, sum(y.value) FROM amt x JOIN amt y ON x.region = y.region GROUP BY x.ccy",
]


@pytest.mark.parametrize("sql", ALLOWED_CCY)
def test_currency_safe_aggregates_are_allowed(store, h, sql):
    combine(store, "alice", sql, {"amt": h["amt"], "cnt": h["cnt"], "base": h["base"], "raw": h["raw"]})


@pytest.mark.parametrize("sql", MIXING)
def test_currency_mixing_is_rejected(store, h, sql):
    # list/array/string aggregates are outside the function allow-list altogether
    want = "sql_not_allowed" if sql.split("(")[0].endswith(("array_agg", "list", "string_agg")) else "currency_mixing"
    assert code_of(store, h, sql) == want


# ------------------------------------------------------------------------- currency across chained combines
@pytest.mark.parametrize("step1, step2", [
    ("SELECT region, value FROM amt", "SELECT sum(value) FROM t"),                          # ccy dropped
    ("SELECT region, value FROM amt", "SELECT region, sum(value) FROM t GROUP BY region"),
    ("SELECT ccy AS c, value FROM amt", "SELECT c, sum(value) FROM t GROUP BY c"),          # renamed: still genuine
    ("SELECT 'EUR' AS ccy, value FROM amt", "SELECT ccy, sum(value) FROM t GROUP BY ccy"),  # fake literal ccy
    ("SELECT upper(ccy) AS ccy, value FROM amt", "SELECT ccy, sum(value) FROM t GROUP BY ccy"),
    ("SELECT x.ccy, y.value FROM amt x JOIN amt y ON x.region = y.region",                 # ccy of another instance
     "SELECT ccy, sum(value) FROM t GROUP BY ccy"),
    ("SELECT ccy, value AS v FROM amt", "SELECT sum(v) FROM t"),
    ("SELECT ccy, value, value AS VALUE2 FROM amt", "SELECT sum(value2) FROM t"),
])
def test_currency_rule_survives_a_two_step_combine(store, h, step1, step2):
    t = combine(store, "alice", step1, {"amt": h["amt"]})
    if "AS c," in step1:   # a renamed genuine ccy is still the genuine ccy
        combine(store, "alice", step2, {"t": t})
        return
    assert code_of(store, h, step2, {"t": t}) == "currency_mixing"


def test_two_step_combine_with_genuine_ccy_is_allowed(store, h):
    t = combine(store, "alice", "SELECT ccy, region, value FROM amt WHERE region = 'EMEA'", {"amt": h["amt"]})
    r = run(store, h, "SELECT ccy, sum(value) AS s FROM t GROUP BY ccy ORDER BY ccy", t=t)
    assert r.rows == (("EUR", 100.0), ("USD", 50.0))
    t2 = combine(store, "alice", "SELECT ccy, sum(value) AS s FROM amt GROUP BY ccy", {"amt": h["amt"]})
    assert code_of(store, h, "SELECT sum(s) FROM t2", {"t2": t2}) == "currency_mixing"
    run(store, h, "SELECT ccy, max(s) FROM t2 GROUP BY ccy", t2=t2)
    counts = combine(store, "alice", "SELECT region, value FROM cnt", {"cnt": h["cnt"]})
    run(store, h, "SELECT sum(value) FROM c", c=counts)                                 # no amounts: unrestricted


@pytest.mark.parametrize("sql", ["SELECT " + "(" * 3000 + "1" + ")" * 3000,
                                 "SELECT * FROM " + "(SELECT * FROM " * 800 + "cnt" + ")" * 800,
                                 "SELECT " + " + ".join(["1"] * 5000)])
def test_absurd_nesting_is_refused_not_crashed(store, h, sql):
    assert code_of(store, h, sql) in ("invalid_sql", "sql_not_allowed", "combine_failed")


# ----------------------------------------------------------------------- security review regressions (C1/C2/I3)
SELF_JOINED_CTE = "WITH t AS (SELECT ccy, value FROM amt) "


@pytest.mark.parametrize("sql", [
    # C1: one CTE referenced twice must give two distinct table instances (amount of y, ccy of x)
    SELF_JOINED_CTE + "SELECT x.ccy, sum(y.value) AS s FROM t x CROSS JOIN t y GROUP BY x.ccy",
    SELF_JOINED_CTE + "SELECT y.ccy, sum(x.value) AS s FROM t x JOIN t y ON x.value > 0 GROUP BY y.ccy",
    "WITH t AS (SELECT ccy, value FROM amt), u AS (SELECT * FROM t) "
    "SELECT a.ccy, sum(b.value) FROM u a, u b GROUP BY a.ccy",
    # same alias in two set-operation branches is two instances (branch 1 labels y's amount with x's ccy)
    SELF_JOINED_CTE + "SELECT c, sum(v) FROM (SELECT x.ccy AS c, y.value AS v FROM t x, t y "
    "UNION ALL SELECT y.ccy AS c, y.value AS v FROM t y) GROUP BY c",
    # C2: COALESCE of two different instances' ccy is not the genuine ccy of either
    "SELECT coalesce(b.ccy, a.ccy) AS ccy, sum(a.value) s FROM amt a LEFT JOIN "
    "(SELECT * FROM amt WHERE ccy = 'JPY') b ON a.region = b.region GROUP BY coalesce(b.ccy, a.ccy)",
    "SELECT coalesce(b.ccy, a.ccy) AS ccy, sum(a.value) s FROM amt a LEFT JOIN amt b "
    "ON a.region = b.region AND b.ccy = 'JPY' GROUP BY coalesce(b.ccy, a.ccy)",
    "SELECT CASE WHEN b.ccy IS NULL THEN a.ccy ELSE b.ccy END AS ccy, sum(a.value) FROM amt a "
    "LEFT JOIN amt b ON a.region = b.region AND b.ccy = 'JPY' GROUP BY 1",
    "SELECT if(b.ccy IS NULL, a.ccy, b.ccy) AS ccy, sum(a.value) FROM amt a "
    "LEFT JOIN amt b ON a.region = b.region AND b.ccy = 'JPY' GROUP BY 1",
    "SELECT nullif(a.ccy, 'EUR') AS ccy, sum(a.value) FROM amt a GROUP BY 1",
    "SELECT coalesce(ccy, 'USD') AS ccy, sum(value) FROM amt GROUP BY 1",
])
def test_review_currency_bypasses_are_refused(store, h, sql):
    assert code_of(store, h, sql) == "currency_mixing"


@pytest.mark.parametrize("sql", [
    SELF_JOINED_CTE + "SELECT y.ccy, x.value FROM t x CROSS JOIN t y",
    "SELECT coalesce(b.ccy, a.ccy) AS ccy, a.value FROM amt a LEFT JOIN amt b ON a.region = b.region",
    "SELECT a.ccy, a.value + b.value AS v FROM amt a JOIN amt b USING (region)",   # row-level cross-ccy arithmetic
])
def test_forged_ccy_lineage_is_not_recorded(store, h, sql):
    out = combine(store, "alice", sql, {"amt": h["amt"]})
    assert store.get("alice", out).meta["lineage"]["ccy"] is None
    assert code_of(store, h, "SELECT ccy, sum(value) FROM t GROUP BY ccy",
                   {"t": out}) in ("currency_mixing", "invalid_sql")


def test_self_joined_cte_with_matching_instance_is_still_allowed(store, h):
    r = run(store, h, "WITH t AS (SELECT ccy, region, value FROM amt) SELECT x.ccy, sum(x.value) AS s FROM t x "
                      "JOIN t y ON x.ccy = y.ccy AND x.region = 'EMEA' AND y.region = 'EMEA' GROUP BY x.ccy "
                      "ORDER BY x.ccy", amt=h["amt"])
    assert r.rows == (("EUR", 100.0), ("USD", 50.0))
    assert r.meta["lineage"]["amount"] == ["s"] and r.meta["lineage"]["ccy"] == "ccy"


@pytest.mark.parametrize("sql, rows", [
    ("SELECT region, ccy FROM amt WHERE ccy = 'EUR' OR ccy = 'JPY' ORDER BY ccy", (("EMEA", "EUR"), ("APAC", "JPY"))),
    ("SELECT region, ccy FROM amt WHERE region = 'EMEA' AND NOT ccy = 'EUR'", (("EMEA", "USD"),)),
    ("SELECT region, ccy FROM amt WHERE (ccy = 'USD' AND region = 'APAC') OR ccy = 'JPY' ORDER BY ccy",
     (("APAC", "JPY"), ("APAC", "USD"))),
    ("SELECT a.ccy, sum(a.value) FROM amt a JOIN amt b ON a.region = b.region AND a.ccy = b.ccy "
     "WHERE NOT (a.ccy = 'JPY' OR a.ccy = 'EUR') GROUP BY a.ccy", (("USD", 57.5),)),
    ("SELECT ccy, CASE WHEN value > 10 AND region = 'EMEA' OR ccy = 'JPY' THEN 'big' ELSE 'small' END AS k "
     "FROM amt ORDER BY ccy, k", (("EUR", "big"), ("JPY", "big"), ("USD", "big"), ("USD", "small"))),
    ("SELECT ccy, sum(value) FILTER (WHERE region = 'EMEA' OR region = 'APAC') FROM amt "
     "WHERE ccy = 'EUR' GROUP BY ccy", (("EUR", 100.0),)),
])
def test_and_or_not_are_allowed(store, h, sql, rows):
    assert run(store, h, sql, amt=h["amt"]).rows == rows


@pytest.mark.parametrize("sql", ["SELECT length(repeat('x', 500000000))", "SELECT lpad(region, 500000000, 'x') FROM cnt",
                                 "SELECT rpad(region, 500000000, 'x') FROM cnt"])
def test_string_blowup_functions_are_not_allowed(store, h, sql):
    assert code_of(store, h, sql) == "sql_not_allowed"


def test_amounts_through_a_union_keep_their_genuine_ccy(store, h):
    r = run(store, h, "SELECT c, sum(v) AS s FROM (SELECT ccy AS c, value AS v FROM amt WHERE region = 'EMEA' "
                      "UNION ALL SELECT ccy, value FROM amt WHERE region = 'APAC') GROUP BY c ORDER BY c", amt=h["amt"])
    assert r.rows == (("EUR", 100.0), ("JPY", 9000.0), ("USD", 57.5))
    out = combine(store, "alice", "SELECT ccy, value FROM amt WHERE region = 'EMEA' UNION ALL "
                                  "SELECT ccy, value FROM amt WHERE region = 'APAC'", {"amt": h["amt"]})
    assert store.get("alice", out).meta["lineage"]["ccy"] == "ccy"


@pytest.mark.parametrize("sql", [
    "SELECT c, sum(v) s FROM (SELECT ccy AS c, region AS r, value AS v FROM amt UNION ALL BY NAME "
    "SELECT ccy AS r, region AS c, value AS v FROM amt) GROUP BY c",
    "SELECT c, sum(v) s FROM (SELECT ccy AS c, value AS v FROM amt UNION ALL BY NAME "
    "SELECT value AS v, region AS c FROM amt) GROUP BY c",
])
def test_union_by_name_matches_columns_by_name(store, h, sql):
    assert code_of(store, h, sql) == "currency_mixing"


def test_union_by_name_with_genuine_ccy_is_allowed(store, h):
    r = run(store, h, "SELECT c, sum(v) AS s FROM (SELECT ccy AS c, value AS v FROM amt WHERE region = 'EMEA' "
                      "UNION ALL BY NAME SELECT value AS v, ccy AS c FROM amt WHERE region = 'APAC') "
                      "GROUP BY c ORDER BY c", amt=h["amt"])
    assert r.rows == (("EUR", 100.0), ("JPY", 9000.0), ("USD", 57.5))
