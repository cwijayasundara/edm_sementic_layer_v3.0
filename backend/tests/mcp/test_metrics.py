"""Governed-metrics tests: (a) compile-level unit tests, (b) real-data tests as bi_reader via signed ctx."""
from contextlib import contextmanager
from datetime import date

import psycopg
import pytest

from prism.db.session import ctx_from_claims, prepare_statements
from prism.mcp.metrics import (
    DEFAULT_METRICS_DIR,
    Metric,
    MetricError,
    compile_metric,
    last_business_days,
    load_metrics,
    run_metric,
)
from prism.mcp.results import UserFacingError
from prism.security.personas import claims_for
from prism.sim.universe import SimConfig, build_universe

AS_OF = date(2026, 9, 30)
METRICS = load_metrics(DEFAULT_METRICS_DIR)
STORIES = build_universe(SimConfig.small()).stories


@contextmanager
def scoped(settings, persona: str, db: str):
    ctx = ctx_from_claims(claims_for(persona), settings.ctx_hmac_key.get_secret_value())
    with psycopg.connect(settings.dsn(db)) as conn:
        with conn.transaction():
            for statement, args in prepare_statements(ctx, 5000):
                conn.execute(statement, args)
            yield conn


def run(settings, persona, metric_id, **kw):
    m = METRICS[metric_id]
    with scoped(settings, persona, m.source) as conn:
        return run_metric(conn, m, db_prefix=settings.db_prefix, as_of=AS_OF, **kw)


# ======================================================================================= (a) compile
def sql_text(q) -> str:
    return q.as_string(None)


def test_all_six_metrics_load():
    assert {"open_breaks", "aged_open_breaks", "late_feeds", "position_exceptions",
            "nav_break_bps_max", "auto_match_rate"} <= set(METRICS)


def test_metric_error_is_user_facing():
    assert issubclass(MetricError, UserFacingError) and issubclass(MetricError, ValueError)


def test_values_are_bound_not_interpolated():
    q, params = compile_metric(METRICS["open_breaks"], dimensions=["region", "ccy"],
                               filters={"ccy": ["XYZ", "QQQ"], "legal_entity_id": "LE99999",
                                        "age_days": {"between": [7, 42]}}, as_of=AS_OF)
    text = sql_text(q)
    for v in ("XYZ", "QQQ", "LE99999", "42"):
        assert v not in text
    assert params == [["XYZ", "QQQ"], "LE99999", 7, 42, 100]
    assert "ccy = ANY(%s)" in text and "age_days BETWEEN %s AND %s" in text and text.endswith("LIMIT %s")


def test_gte_lte_and_types_coerced():
    q, params = compile_metric(METRICS["late_feeds"], filters={"business_date": {"gte": "2026-09-01"}},
                               as_of=AS_OF)
    assert params[0] == date(2026, 9, 1) and "business_date >= %s" in sql_text(q)
    with pytest.raises(MetricError, match="bad value"):
        compile_metric(METRICS["late_feeds"], filters={"business_date": "not-a-date"}, as_of=AS_OF)
    with pytest.raises(MetricError, match="bad value"):
        compile_metric(METRICS["open_breaks"], filters={"age_days": "5 OR 1=1"}, as_of=AS_OF)


def test_combined_range_filter():
    q, params = compile_metric(METRICS["open_breaks"], filters={"age_days": {"gte": 3, "lte": 10}}, as_of=AS_OF)
    assert "age_days >= %s AND age_days <= %s" in sql_text(q) and params == [3, 10, 100]
    with pytest.raises(MetricError, match="cannot be combined"):
        compile_metric(METRICS["open_breaks"], filters={"age_days": {"between": [1, 2], "gte": 0}}, as_of=AS_OF)
    with pytest.raises(MetricError, match="valid"):
        compile_metric(METRICS["open_breaks"], filters={"age_days": {}}, as_of=AS_OF)


def test_unknown_names_list_valid():
    m = METRICS["open_breaks"]
    with pytest.raises(MetricError) as e:
        compile_metric(m, dimensions=["desk"], as_of=AS_OF)
    assert "valid: ['break_type', 'ccy', 'legal_entity_id', 'region']" in str(e.value)
    with pytest.raises(MetricError) as e:
        compile_metric(m, filters={"desk": "x"}, as_of=AS_OF)
    assert "'region'" in str(e.value) and "'age_days'" in str(e.value)
    with pytest.raises(MetricError, match="valid"):
        compile_metric(m, filters={"age_days": {"like": 3}}, as_of=AS_OF)
    with pytest.raises(MetricError, match="valid"):
        compile_metric(m, dimensions=["region"], order_by="amount", as_of=AS_OF)


def test_order_by_variants():
    m = METRICS["open_breaks"]
    assert 'ORDER BY "ccy" DESC, "region", "ccy"' in sql_text(
        compile_metric(m, dimensions=["region", "ccy"], order_by="-ccy", as_of=AS_OF)[0])
    assert "ORDER BY value ASC NULLS LAST" in sql_text(
        compile_metric(m, dimensions=["region"], order_by="metric", as_of=AS_OF)[0])


def test_last_business_days_window():
    assert last_business_days(5, date(2026, 9, 30)) == (date(2026, 9, 24), date(2026, 9, 30))
    assert last_business_days(6, date(2026, 9, 30)) == (date(2026, 9, 23), date(2026, 9, 30))
    assert last_business_days(1, date(2026, 10, 4)) == (date(2026, 10, 2), date(2026, 10, 2))   # Sunday
    assert last_business_days(3, date(2026, 10, 3)) == (date(2026, 9, 30), date(2026, 10, 2))   # Saturday
    q, params = compile_metric(METRICS["late_feeds"], time_range={"last_business_days": 5}, as_of=AS_OF)
    assert "business_date BETWEEN %s AND %s" in sql_text(q)
    assert params[-3:-1] == [date(2026, 9, 24), date(2026, 9, 30)]
    for bad in (0, -1, "5", 5.0, True):
        with pytest.raises(MetricError):
            compile_metric(METRICS["late_feeds"], time_range={"last_business_days": bad}, as_of=AS_OF)


def test_time_range_from_to_and_errors():
    _, params = compile_metric(METRICS["late_feeds"], time_range={"from": "2026-09-01", "to": "2026-09-15"},
                               as_of=AS_OF)
    assert params[:2] == [date(2026, 9, 1), date(2026, 9, 15)]
    with pytest.raises(MetricError, match="from must be <= to"):
        compile_metric(METRICS["late_feeds"], time_range={"from": "2026-09-15", "to": "2026-09-01"}, as_of=AS_OF)
    with pytest.raises(MetricError, match="no time_column"):
        compile_metric(METRICS["auto_match_rate"], time_range={"last_business_days": 5}, as_of=AS_OF)
    with pytest.raises(MetricError):
        compile_metric(METRICS["late_feeds"], time_range={"from": "2026-09-01; DROP", "to": "x"}, as_of=AS_OF)


def test_limit_clamped():
    _, params = compile_metric(METRICS["open_breaks"], dimensions=["region"], limit=10_000, as_of=AS_OF)
    assert params[-1] == 1000
    _, params = compile_metric(METRICS["open_breaks"], limit=50_000, max_limit=250, as_of=AS_OF)
    assert params[-1] == 250
    for bad in (0, -5, "10", True):
        with pytest.raises(MetricError):
            compile_metric(METRICS["open_breaks"], limit=bad, as_of=AS_OF)


def test_fetch_extra_adds_one_row_beyond_the_cap():
    _, params = compile_metric(METRICS["open_breaks"], dimensions=["region"], limit=10, fetch_extra=1, as_of=AS_OF)
    assert params[-1] == 11
    _, params = compile_metric(METRICS["open_breaks"], limit=10_000, fetch_extra=1, as_of=AS_OF)
    assert params[-1] == 1001
    _, params = compile_metric(METRICS["open_breaks"], limit=10, as_of=AS_OF)
    assert params[-1] == 10
    for bad in (-1, 2, True, "1"):
        with pytest.raises(MetricError, match="fetch_extra"):
            compile_metric(METRICS["open_breaks"], limit=10, fetch_extra=bad, as_of=AS_OF)


@pytest.mark.parametrize("evil", ["region; DROP TABLE breaks", "' OR 1=1 --", 'region" , (select 1) AS "x'])
def test_injection_in_names_rejected(evil):
    m = METRICS["open_breaks"]
    with pytest.raises(MetricError, match="unknown"):
        compile_metric(m, dimensions=[evil], as_of=AS_OF)
    with pytest.raises(MetricError, match="unknown"):
        compile_metric(m, filters={evil: "x"}, as_of=AS_OF)
    with pytest.raises(MetricError, match="unknown"):
        compile_metric(m, dimensions=["region"], order_by=evil, as_of=AS_OF)


@pytest.mark.parametrize("evil", ["region; DROP TABLE breaks", "' OR 1=1 --"])
def test_injection_in_values_is_inert(seeded, evil):
    q, params = compile_metric(METRICS["open_breaks"], dimensions=["region"],
                               filters={"region": evil, "ccy": [evil]}, as_of=AS_OF)
    text = sql_text(q)
    assert evil not in text and "DROP" not in text and "1=1" not in text
    assert params[:2] == [evil, [evil]]
    assert run(seeded, "head_data", "open_breaks", filters={"region": evil}) == [{"value": 0}]


def _base(**over) -> dict:
    d = {"id": "x", "source": "cashrecon", "description": "d", "type": "count", "expr": "count(*)",
         "from": "breaks"}
    d.update(over)
    return d


@pytest.mark.parametrize("over, msg", [
    ({"from": "private.cash_accounts"}, "forbidden"),
    ({"from": "recon_exceptions"}, "allowed for cashrecon"),
    ({"from": "breaks b JOIN nav_checks n ON n.portfolio_id = b.account_id"}, "allowed for cashrecon"),
    ({"from": "breaks, statements"}, "comma joins"),
    ({"from": "breaks JOIN pg_roles r ON true"}, "forbidden"),
    ({"base_filter": "status <> 'closed'; DROP TABLE breaks"}, "forbidden"),
    ({"base_filter": "region IN (SELECT 1)"}, "forbidden"),
    ({"base_filter": "current_setting('app.ctx') <> ''"}, "forbidden"),
    ({"base_filter": "ccy LIKE 'US%'"}, "forbidden"),
    ({"expr": "sum(amount)"}, "must start with"),
    ({"type": "ratio", "expr": None}, "ratio requires"),
    ({"dimensions": {"Bad Name": "region"}}, "bad dimension name"),
    ({"dimensions": {"value": "region"}}, "bad dimension name"),
    ({"extra_key": 1}, "Extra inputs"),
    ({"base_filter": "region IN (VALUES ('EMEA'))"}, "forbidden"),
    ({"base_filter": "EXISTS (TABLE ledger_entries)"}, "forbidden"),
    ({"base_filter": "query_to_xml('x', true, true, '') IS NOT NULL"}, "forbidden"),
    ({"base_filter": "database_to_xml(true, true, '') IS NOT NULL"}, "forbidden"),
    ({"base_filter": "xpath('/a', region::xml) IS NOT NULL"}, "forbidden"),
    ({"base_filter": "chr(115) = 's'"}, "forbidden"),
    ({"base_filter": "lo_import('x') > 0"}, "forbidden"),
    ({"base_filter": "region IN (SELECT 1 INTERSECT SELECT 2)"}, "forbidden"),
    ({"base_filter": "region <> 'x' EXCEPT y"}, "forbidden"),
    ({"base_filter": "EXISTS (WITH a AS (x) y)"}, "forbidden"),
    ({"base_filter": "LATERAL x"}, "forbidden"),
    ({"base_filter": "current_user = 'x'"}, "forbidden"),
    ({"base_filter": "session_user = 'x'"}, "forbidden"),
    ({"base_filter": "version() <> ''"}, "forbidden"),
    ({"base_filter": "nextval('s') > 0"}, "forbidden"),
    ({"base_filter": "setval('s', 1) > 0"}, "forbidden"),
    ({"base_filter": "region LIKE 'a' UESCAPE '!'"}, "forbidden"),
    ({"base_filter": "region = `x`"}, "forbidden"),
    ({"from": "breaks b JOIN (recon_exceptions r) ON true"}, "not allowed"),
    ({"from": "breaks WHERE true"}, "not allowed"),
    ({"from": "breaks GROUP BY 1"}, "not allowed"),
    ({"from": "breaks ORDER BY 1"}, "not allowed"),
    ({"from": "breaks LIMIT 1"}, "not allowed"),
    ({"from": "breaks HAVING true"}, "not allowed"),
    ({"type": "ratio", "expr": None, "numerator": "true), count(*) FILTER (WHERE true"}, "commas"),
])
def test_definition_validator_rejects(over, msg):
    with pytest.raises(ValueError, match=msg):
        Metric.model_validate(_base(**over))


def test_definition_allows_ordinary_expressions():
    m = Metric.model_validate(_base(
        dimensions={"day": "date_trunc('day', opened_on)",
                    "bucket": "CASE WHEN age_days > 5 THEN 'aged' ELSE 'recent' END"},
        base_filter="status <> 'within_tolerance' AND ccy <> 'tablet'"))
    assert set(m.dimensions) == {"day", "bucket"}


def test_definition_allows_trusted_join():
    m = Metric.model_validate(_base(
        source="feedhub", **{"from": "feed_deliveries d JOIN sources s ON s.source_id = d.source_id"},
        expr="count(*)", dimensions={"country": "s.country"}))
    assert "JOIN sources s" in sql_text(compile_metric(m, dimensions=["country"], as_of=AS_OF)[0])


# ===================================================================================== (b) real data
def test_run_metric_refuses_wrong_db_and_unscoped_conn(seeded):
    with scoped(seeded, "head_data", "feedhub") as conn, pytest.raises(MetricError, match="must run on database"):
        run_metric(conn, METRICS["open_breaks"], db_prefix="test_", as_of=AS_OF)
    with psycopg.connect(seeded.dsn("cashrecon")) as conn, pytest.raises(MetricError, match="bi_reader"):
        run_metric(conn, METRICS["open_breaks"], db_prefix="test_", as_of=AS_OF)


def test_open_breaks_row_level_security_by_persona(seeded):
    head = {r["region"]: r["value"] for r in run(seeded, "head_data", "open_breaks", dimensions=["region"])}
    emea = {r["region"]: r["value"] for r in run(seeded, "cash_ops_emea", "open_breaks", dimensions=["region"])}
    assert set(head) == {"AMER", "APAC", "EMEA"}
    assert set(emea) == {"EMEA"} and emea["EMEA"] == head["EMEA"] > 0


def test_cash_ops_emea_sees_only_bank_feeds(seeded):
    rows = run(seeded, "cash_ops_emea", "late_feeds", dimensions=["source_type"])
    assert rows and {r["source_type"] for r in rows} == {"bank"}
    head = {r["source_type"] for r in run(seeded, "head_data", "late_feeds", dimensions=["source_type"])}
    assert len(head) > 1


def test_persona_without_dataset_gets_nothing(seeded):
    assert run(seeded, "steward", "open_breaks", dimensions=["region"]) == []
    assert run(seeded, "steward", "open_breaks") == [{"value": 0}]
    assert run(seeded, "steward", "auto_match_rate") == [{"value": None}]


def test_aged_open_breaks_usd_concentrated(seeded):
    rows = run(seeded, "head_data", "aged_open_breaks", dimensions=["legal_entity_id"], filters={"ccy": "USD"})
    total = sum(r["value"] for r in rows)
    share = rows[0]["value"] / total
    print(f"aged USD: top={rows[0]} total={total} share={share:.3f}")
    assert rows[0]["legal_entity_id"] == STORIES.usd_break_entity_id
    assert share >= 0.5


def test_position_exceptions_spike_on_late_portfolios(seeded):
    recent = run(seeded, "head_data", "position_exceptions", dimensions=["portfolio_id"],
                 time_range={"last_business_days": 6})
    n_late = len(STORIES.late_portfolio_ids)
    late = [r["portfolio_id"] for r in recent[:n_late]]
    per_day = sum(r["value"] for r in recent[:n_late]) / 6
    lo, hi = date(2026, 9, 3), date(2026, 9, 22)
    n_days = sum(1 for k in range((hi - lo).days + 1) if date.fromordinal(lo.toordinal() + k).weekday() < 5)
    base = run(seeded, "head_data", "position_exceptions", filters={"portfolio_id": late},
               time_range={"from": lo.isoformat(), "to": hi.isoformat()})[0]["value"] / n_days
    print(f"late portfolios {late}: {per_day:.2f}/day recent vs {base:.2f}/day baseline")
    assert set(late) == set(STORIES.late_portfolio_ids)
    assert per_day >= 3 * base


def test_late_feeds_concentrated_on_one_source(seeded):
    rows = run(seeded, "head_data", "late_feeds", dimensions=["source_id"], time_range={"last_business_days": 6})
    total = sum(r["value"] for r in rows)
    print(f"late feeds 6d: top={rows[0]} runner_up={rows[1]} share={rows[0]['value'] / total:.3f}")
    assert rows[0]["source_id"] == STORIES.late_custodian_source_id == "SRC001"
    assert rows[0]["value"] >= 2 * rows[1]["value"]
    ranged = run(seeded, "head_data", "late_feeds", filters={"business_date": {"gte": "2026-09-23", "lte": "2026-09-30"}})
    assert ranged[0]["value"] == total


def test_nav_break_bps_max_on_pf003_pf009(seeded):
    rows = run(seeded, "head_data", "nav_break_bps_max", dimensions=["portfolio_id", "nav_date"],
               time_range={"last_business_days": 20}, order_by="metric_desc", limit=7)
    print(f"nav rows: {[(r['portfolio_id'], str(r['nav_date']), float(r['value'])) for r in rows]}")
    story = {"PF003", "PF009"}
    assert story == set(STORIES.nav_portfolio_ids)
    top = [r for r in rows if r["portfolio_id"] in story]
    assert top and all(r["value"] > 5 for r in top)
    assert all(r["value"] < 3 for r in rows if r["portfolio_id"] not in story)
    assert [r["portfolio_id"] in story for r in rows] == sorted(
        (r["portfolio_id"] in story for r in rows), reverse=True)  # story rows come first
    per_pf = run(seeded, "head_data", "nav_break_bps_max", dimensions=["portfolio_id"], order_by="metric_desc",
                 time_range={"last_business_days": 20})
    print(f"nav per portfolio: {[(r['portfolio_id'], float(r['value'])) for r in per_pf]}")
    assert {r["portfolio_id"] for r in per_pf[:2]} == story
    others = run(seeded, "head_data", "nav_break_bps_max", time_range={"last_business_days": 20},
                 filters={"portfolio_id": {"ne": "PF003"}})  # 'ne' operator
    assert others[0]["value"] > 5  # PF009 still present


def test_auto_match_rate_fraction(seeded):
    overall = run(seeded, "head_data", "auto_match_rate")[0]["value"]
    by_region = run(seeded, "head_data", "auto_match_rate", dimensions=["region"])
    print(f"auto_match_rate overall={overall:.4f} by_region={by_region}")
    assert 0.85 <= overall <= 1.0
    assert all(0.85 <= r["value"] <= 1.0 for r in by_region)
    by_actor = {r["matched_by"]: r["value"] for r in run(seeded, "head_data", "auto_match_rate", dimensions=["matched_by"])}
    assert by_actor["system"] == 1.0 and all(v == 0.0 for k, v in by_actor.items() if k != "system")


# ----------------------------------------------------------------------------- input hardening (compile)
@pytest.mark.parametrize("bad", [float("inf"), float("nan"), 1e400, 5.9, " 5 ", "1_000", "\u0663", "5.5",
                                 "5 OR 1=1", 10**30, "1" * 19, "", True])
def test_int_filter_rejects_non_strict_values(bad):
    with pytest.raises(MetricError, match="bad value"):
        compile_metric(METRICS["open_breaks"], filters={"age_days": bad}, as_of=AS_OF)


@pytest.mark.parametrize("good, expected", [(5, 5), (5.0, 5), ("5", 5), ("-12", -12)])
def test_int_filter_accepts_strict_values(good, expected):
    _, params = compile_metric(METRICS["open_breaks"], filters={"age_days": good}, as_of=AS_OF)
    assert params[0] == expected and type(params[0]) is int


def test_nul_byte_in_string_filter_rejected():
    with pytest.raises(MetricError, match="bad value"):
        compile_metric(METRICS["open_breaks"], filters={"region": "EM\x00EA"}, as_of=AS_OF)
    with pytest.raises(MetricError, match="bad value"):
        compile_metric(METRICS["open_breaks"], filters={"ccy": ["USD", "E\x00"]}, as_of=AS_OF)


@pytest.mark.parametrize("bad", ["region", [["region"]], [None], ["region", None], ("region",), {"region": 1}, 5])
def test_dimensions_must_be_list_of_strings(bad):
    with pytest.raises(MetricError, match="dimensions must be a list of strings"):
        compile_metric(METRICS["open_breaks"], dimensions=bad, as_of=AS_OF)


@pytest.mark.parametrize("dims", [None, ["region"]])
@pytest.mark.parametrize("bad", [5, ["region"], {"a": 1}, b"region"])
def test_order_by_must_be_string(dims, bad):
    with pytest.raises(MetricError, match="order_by must be a string"):
        compile_metric(METRICS["open_breaks"], dimensions=dims, order_by=bad, as_of=AS_OF)


@pytest.mark.parametrize("bad", [0, -1, "10", 1.5, True, None])
def test_max_limit_must_be_positive_int(bad):
    with pytest.raises(MetricError, match="max_limit"):
        compile_metric(METRICS["open_breaks"], max_limit=bad, as_of=AS_OF)


# --------------------------------------------------------------------------- role guards (real RLS path)
def test_result_discarded_when_role_changes_during_execution(seeded, monkeypatch):
    m = METRICS["open_breaks"]
    monkeypatch.setattr("prism.mcp.metrics.compile_metric",
                        lambda metric, **kw: (psycopg.sql.SQL("SELECT set_config('role', 'none', true) AS value"), []))
    with scoped(seeded, "head_data", m.source) as conn, pytest.raises(MetricError, match="role changed"):
        run_metric(conn, m, db_prefix=seeded.db_prefix, as_of=AS_OF)


def test_run_metric_refuses_writable_transaction(seeded):
    m = METRICS["open_breaks"]
    with psycopg.connect(seeded.dsn(m.source)) as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE bi_reader")
            with pytest.raises(MetricError, match="read-only"):
                run_metric(conn, m, db_prefix=seeded.db_prefix, as_of=AS_OF)


def test_metric_tables_property():
    assert METRICS["open_breaks"].tables == {"breaks"}
    m = Metric(**_base(**{"from": "breaks b JOIN Public.Statements s ON s.id = b.id"}))
    assert m.tables == {"breaks", "statements"}
    assert all(not t.startswith("public.") and t == t.lower() for x in METRICS.values() for t in x.tables)


def test_sensitive_dimensions_must_be_dimensions(tmp_path):
    ok = Metric.model_validate(_base(dimensions={"region": "region", "who": "matched_by"},
                                     sensitive_dimensions=["who"]))
    assert ok.sensitive_dimensions == ["who"]
    assert Metric.model_validate(_base()).sensitive_dimensions == []
    with pytest.raises(ValueError, match=r"sensitive_dimensions \['nope'\]"):
        Metric.model_validate(_base(dimensions={"region": "region"}, sensitive_dimensions=["nope"]))
    (tmp_path / "x.yaml").write_text(
        "id: x\nsource: cashrecon\ndescription: d\ntype: count\nexpr: count(*)\nfrom: breaks\n"
        "dimensions: {region: region}\nsensitive_dimensions: [matched_by]\n")
    with pytest.raises(ValueError, match="sensitive_dimensions"):
        load_metrics(tmp_path)


def test_operator_ids_are_sensitive_in_the_catalog():
    assert METRICS["manual_matches"].sensitive_dimensions == ["matched_by"]
    assert METRICS["auto_match_rate"].sensitive_dimensions == ["matched_by"]


def test_required_dimensions_are_enforced_by_the_compiler():
    m = METRICS["open_break_amount"]
    for dims in ([], ["region"], ["legal_entity_id", "break_type"]):
        with pytest.raises(MetricError, match=r"metric open_break_amount requires dimension\(s\) \['ccy'\] "
                                              r"\(its value is not meaningful across them\)"):
            compile_metric(m, dimensions=dims, as_of=AS_OF)
    q, _ = compile_metric(m, dimensions=["ccy"], as_of=AS_OF)
    assert "GROUP BY" in sql_text(q)
    compile_metric(m, dimensions=["region", "ccy"], filters={"ccy": "EUR"}, as_of=AS_OF)


def test_required_dimensions_must_be_dimensions():
    ok = Metric.model_validate(_base(dimensions={"ccy": "ccy"}, required_dimensions=["ccy"]))
    assert ok.required_dimensions == ["ccy"]
    with pytest.raises(ValueError, match=r"required_dimensions \['ccy'\]"):
        Metric.model_validate(_base(dimensions={"region": "region"}, required_dimensions=["ccy"]))


@pytest.mark.parametrize("bad", [[("region", "EMEA")], (("region", "EMEA"),), "region", {1: "x"}, 5])
def test_filters_must_be_an_object_with_string_keys(bad):
    with pytest.raises(MetricError, match="^filters must be an object$"):
        compile_metric(METRICS["open_breaks"], filters=bad, as_of=AS_OF)
    compile_metric(METRICS["open_breaks"], filters=None, as_of=AS_OF)
    compile_metric(METRICS["open_breaks"], filters={}, as_of=AS_OF)


def test_fine_grain_dimensions_must_be_dimensions():
    ok = Metric.model_validate(_base(dimensions={"pf": "portfolio_id", "d": "nav_date"},
                                     fine_grain_dimensions=["pf", "d"]))
    assert ok.fine_grain_dimensions == ["pf", "d"]
    assert Metric.model_validate(_base()).fine_grain_dimensions == []
    with pytest.raises(ValueError, match=r"fine_grain_dimensions \['nope'\]"):
        Metric.model_validate(_base(dimensions={"region": "region"}, fine_grain_dimensions=["nope"]))


def test_nav_break_bps_max_is_fine_grained_by_portfolio_and_date():
    assert sorted(METRICS["nav_break_bps_max"].fine_grain_dimensions) == ["nav_date", "portfolio_id"]


# ------------------------------------------------------------------------------------- grain classification
def _registry_metrics():
    """Every governed metric, SQL and REST: (id, model, time column)."""
    from prism.graph.knowledge import REST_SOURCES
    from prism.mcp.rest_backend import load_rest_config

    out = [(m.id, m, m.time_column) for m in METRICS.values()]
    for s in REST_SOURCES:
        out += [(m.id, m, None) for m in load_rest_config(s)[1].values()]
    return out


def _identifier_dims(m) -> list[str]:
    return sorted(d for d in m.dimensions if d.endswith("_id"))


def _date_dims(m, time_column) -> list[str]:
    return sorted(d for d, expr in m.dimensions.items()
                  if d == time_column or expr == time_column or d.endswith("_date") or expr.endswith("_date"))


def test_every_identifier_by_date_metric_classifies_its_grain():
    """A metric with an identifier dimension (`*_id`) and a date dimension can return one underlying row per group,
    so it must declare `fine_grain_dimensions` (a new metric fails here until classified). An explicit
    `fine_grain_dimensions: []` (with a YAML comment saying why) is the only opt-out."""
    unclassified, incomplete = [], []
    for mid, m, time_column in _registry_metrics():
        ids, dates = _identifier_dims(m), _date_dims(m, time_column)
        if not (ids and dates):
            continue
        if "fine_grain_dimensions" not in m.model_fields_set:
            unclassified.append(mid)
        elif m.fine_grain_dimensions and not set(ids + dates) <= set(m.fine_grain_dimensions):
            incomplete.append((mid, ids + dates, m.fine_grain_dimensions))
    assert not unclassified, f"declare fine_grain_dimensions for {unclassified}"
    assert not incomplete, incomplete


def test_entity_by_date_metrics_are_fine_grained():
    fine = {mid: sorted(m.fine_grain_dimensions) for mid, m, _ in _registry_metrics()}
    for mid in ("recon_unmatched_items", "open_position_exceptions", "position_exceptions",
                "nav_breaches_above_5bps", "nav_break_bps_max"):
        assert fine[mid] == ["nav_date" if "nav" in mid else "business_date", "portfolio_id"], mid
    for mid in ("avg_feed_latency_min", "feed_on_time_rate", "late_feeds", "missing_or_failed_deliveries"):
        assert fine[mid] == ["business_date", "source_id"], mid
    assert fine["price_conflicts"] == ["price_date", "vendor_id"]


@pytest.mark.parametrize("filters, dims, msg", [
    ({"who": {"expr": "matched_by"}}, {"matched_by": "matched_by"}, "sensitive"),
    ({"who": {"expr": " Matched_By "}}, {"matched_by": "matched_by"}, "sensitive"),
    ({"day": {"expr": "nav_date", "type": "date"}}, {"portfolio_id": "portfolio_id", "nav_date": "nav_date"}, "fine"),
])
def test_a_filter_may_not_alias_a_sensitive_or_fine_dimension(filters, dims, msg):
    """The gateway checks filters by name; an alias over the same expression would slip past it."""
    over = {"dimensions": dims, "filters": filters, "sensitive_dimensions": ["matched_by"] if msg == "sensitive" else [],
            "fine_grain_dimensions": ["portfolio_id", "nav_date"] if msg == "fine" else []}
    with pytest.raises(ValueError, match=f"exposes {msg} dimension"):
        Metric.model_validate(_base(**over))
    same_name = {next(iter(dims)) if msg == "sensitive" else "nav_date": next(iter(filters.values()))}
    Metric.model_validate(_base(**{**over, "filters": same_name}))
