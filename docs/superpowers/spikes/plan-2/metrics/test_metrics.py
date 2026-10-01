"""Governed-metrics spike tests: (a) compile-level unit tests, (b) real-data tests as bi_reader via signed ctx."""
import base64
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import psycopg
import pytest

from metrics import Metric, MetricError, compile_metric, last_business_days, load_metrics, run_metric

REPO = Path("/Users/chamindawijayasundara/Documents/learning_101/edm_sementic_layer_v2.0")
AS_OF = date(2026, 9, 30)
METRICS = load_metrics(Path(__file__).parent / "metrics")


# ------------------------------------------------------------------------ copied from prism/db/session.py
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
    return [
        ("SET TRANSACTION READ ONLY", None),
        ("SET LOCAL ROLE bi_reader", None),
        ("SELECT set_config('app.ctx', %s, true)", (ctx or "",)),
        ("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),)),
    ]


# ------------------------------------------------------------------ copied from prism/security/personas.py
ALL_SOURCES = ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
ALL_ROWS = {"asset_class": ("*",), "region": ("*",), "fund_group": ("*",), "source_type": ("*",)}
PERSONAS = {
    "steward": (("refmaster", "marketmaster"), {"asset_class": ("*",)}),
    "cash_ops_emea": (("cashrecon", "feedhub"), {"region": ("EMEA",), "source_type": ("bank",)}),
    "invest_ops_growth": (("assetrecon", "feedhub", "refmaster.securities", "refmaster.legal_entities"),
                          {"fund_group": ("Growth",), "source_type": ("custodian",), "asset_class": ("*",)}),
    "bi_analyst": (ALL_SOURCES, ALL_ROWS),
    "head_data": ((*ALL_SOURCES, "pii:read"), ALL_ROWS),
}


def claims_for(persona_id: str, ttl_s: int = 300) -> dict:
    scopes, rows = PERSONAS[persona_id]
    return {"sub": persona_id, "roles": [persona_id], "scopes": list(scopes),
            "rows": {d: list(v) for d, v in rows.items()}, "exp": int(time.time()) + ttl_s}


# ------------------------------------------------------------------------------------ env / connection
def _env() -> dict[str, str]:
    out = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


ENV = _env()
CTX_KEY = ENV["PRISM_CTX_HMAC_KEY"]  # must come from .env (generated key), never the config default


@contextmanager
def scoped(persona: str, db: str):
    """Same as prism.db.session.scoped_sync, connecting as the non-admin service user prism_svc."""
    dsn = (f"host=localhost port={ENV.get('PRISM_PG_PORT', '5434')} dbname={db} user=prism_svc "
           f"password={ENV.get('PRISM_PG_SVC_PASSWORD', 'prism_svc_dev')}")
    ctx = ctx_from_claims(claims_for(persona), CTX_KEY)
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            for statement, args in prepare_statements(ctx, 5000):
                conn.execute(statement, args)
            yield conn


def run(persona: str, metric_id: str, **kw) -> list[dict]:
    m = METRICS[metric_id]
    with scoped(persona, m.source) as conn:
        return run_metric(conn, m, as_of=AS_OF, **kw)


# ======================================================================================= (a) compile
def sql_text(q) -> str:
    return q.as_string(None)


def test_all_six_metrics_load():
    assert set(METRICS) == {"open_breaks", "aged_open_breaks", "late_feeds", "position_exceptions",
                            "nav_break_bps_max", "auto_match_rate"}


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
def test_injection_in_values_is_inert(evil):
    q, params = compile_metric(METRICS["open_breaks"], dimensions=["region"],
                               filters={"region": evil, "ccy": [evil]}, as_of=AS_OF)
    text = sql_text(q)
    assert evil not in text and "DROP" not in text and "1=1" not in text
    assert params[:2] == [evil, [evil]]
    assert run("head_data", "open_breaks", filters={"region": evil}) == [{"value": 0}]


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
])
def test_definition_validator_rejects(over, msg):
    with pytest.raises(ValueError, match=msg):
        Metric.model_validate(_base(**over))


def test_definition_allows_trusted_join():
    m = Metric.model_validate(_base(
        source="feedhub", **{"from": "feed_deliveries d JOIN sources s ON s.source_id = d.source_id"},
        expr="count(*)", dimensions={"country": "s.country"}))
    assert "JOIN sources s" in sql_text(compile_metric(m, dimensions=["country"], as_of=AS_OF)[0])


# ===================================================================================== (b) real data
def test_run_metric_refuses_wrong_db_and_unscoped_conn():
    with scoped("head_data", "feedhub") as conn, pytest.raises(MetricError, match="must run on database"):
        run_metric(conn, METRICS["open_breaks"], as_of=AS_OF)
    dsn = (f"host=localhost port={ENV.get('PRISM_PG_PORT', '5434')} dbname=cashrecon user=prism_svc "
           f"password={ENV.get('PRISM_PG_SVC_PASSWORD', 'prism_svc_dev')}")
    with psycopg.connect(dsn) as conn, pytest.raises(MetricError, match="bi_reader"):
        run_metric(conn, METRICS["open_breaks"], as_of=AS_OF)


def test_open_breaks_row_level_security_by_persona():
    head = {r["region"]: r["value"] for r in run("head_data", "open_breaks", dimensions=["region"])}
    emea = {r["region"]: r["value"] for r in run("cash_ops_emea", "open_breaks", dimensions=["region"])}
    assert set(head) == {"AMER", "APAC", "EMEA"}
    assert set(emea) == {"EMEA"} and emea["EMEA"] == head["EMEA"] > 0


def test_cash_ops_emea_sees_only_bank_feeds():
    rows = run("cash_ops_emea", "late_feeds", dimensions=["source_type"])
    assert rows and {r["source_type"] for r in rows} == {"bank"}
    head = {r["source_type"] for r in run("head_data", "late_feeds", dimensions=["source_type"])}
    assert len(head) > 1


def test_persona_without_dataset_gets_nothing():
    assert run("steward", "open_breaks", dimensions=["region"]) == []
    assert run("steward", "open_breaks") == [{"value": 0}]
    assert run("steward", "auto_match_rate") == [{"value": None}]


def test_aged_open_breaks_usd_concentrated():
    rows = run("head_data", "aged_open_breaks", dimensions=["legal_entity_id"], filters={"ccy": "USD"})
    total = sum(r["value"] for r in rows)
    share = rows[0]["value"] / total
    print(f"aged USD: top={rows[0]} total={total} share={share:.3f}")
    assert share >= 0.7


def test_position_exceptions_spike_on_late_portfolios():
    recent = run("head_data", "position_exceptions", dimensions=["portfolio_id"],
                 time_range={"last_business_days": 6})
    late = [r["portfolio_id"] for r in recent[:3]]
    per_day = sum(r["value"] for r in recent[:3]) / 6
    base = run("head_data", "position_exceptions", filters={"portfolio_id": late},
               time_range={"from": "2026-09-03", "to": "2026-09-22"})[0]["value"] / 14
    print(f"late portfolios {late}: {per_day:.2f}/day recent vs {base:.2f}/day baseline")
    assert set(late) == {"PF001", "PF002", "PF005"}
    assert 18 <= per_day <= 22 and 1.0 <= base <= 1.6


def test_late_feeds_concentrated_on_one_source():
    rows = run("head_data", "late_feeds", dimensions=["source_id"], time_range={"last_business_days": 6})
    total = sum(r["value"] for r in rows)
    print(f"late feeds 6d: top={rows[0]} runner_up={rows[1]} share={rows[0]['value'] / total:.3f}")
    assert rows[0]["source_id"] == "SRC001" and rows[0]["value"] >= 3 * rows[1]["value"]
    ranged = run("head_data", "late_feeds", filters={"business_date": {"gte": "2026-09-23", "lte": "2026-09-30"}})
    assert ranged[0]["value"] == total


def test_nav_break_bps_max_on_pf003_pf009():
    rows = run("head_data", "nav_break_bps_max", dimensions=["portfolio_id", "nav_date"],
               time_range={"last_business_days": 20}, order_by="metric_desc", limit=7)
    top = rows[:6]
    assert {r["portfolio_id"] for r in top} == {"PF003", "PF009"}
    assert {r["nav_date"] for r in top} == {date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)}
    assert all(r["value"] >= 80 for r in top) and rows[6]["value"] <= 2
    others = run("head_data", "nav_break_bps_max", time_range={"last_business_days": 3},
                 filters={"portfolio_id": {"ne": "PF003"}})  # 'ne' operator
    assert others[0]["value"] >= 80  # PF009 still present
    rest = run("head_data", "nav_break_bps_max", dimensions=["portfolio_id"], order_by="metric_desc",
               time_range={"last_business_days": 3})
    assert all(r["value"] <= 2 for r in rest if r["portfolio_id"] not in ("PF003", "PF009"))


def test_auto_match_rate_fraction():
    overall = run("head_data", "auto_match_rate")[0]["value"]
    by_region = run("head_data", "auto_match_rate", dimensions=["region"])
    print(f"auto_match_rate overall={overall:.4f} by_region={by_region}")
    assert 0.85 <= overall <= 1.0
    assert all(0.85 <= r["value"] <= 1.0 for r in by_region)
    by_actor = {r["matched_by"]: r["value"] for r in run("head_data", "auto_match_rate", dimensions=["matched_by"])}
    assert by_actor["system"] == 1.0 and all(v == 0.0 for k, v in by_actor.items() if k != "system")
