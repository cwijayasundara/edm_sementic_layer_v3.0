import json
import time

import pytest
from psycopg_pool import PoolTimeout

from prism.mcp.base import DescribeResult
from prism.mcp.metrics import MetricError
from prism.mcp.results import SourceError
from prism.mcp.sql_backend import MAX_INFLIGHT_PER_PRINCIPAL, MAX_RESULT_BYTES, SqlBackend
from prism.mcp.sql_guard import SqlGuardError
from prism.security.personas import claims_for

pytestmark = pytest.mark.db


@pytest.fixture
async def cash(seeded):
    b = SqlBackend("cashrecon", seeded)
    yield b
    await b.aclose()


async def rm(backend, persona, metric_id, **kw):
    args = dict(dimensions=[], filters={}, time_range=None, order_by=None, limit=100)
    args.update(kw)
    return await backend.run_metric(claims_for(persona), metric_id=metric_id, **args)


async def test_run_metric_respects_row_level_security(cash):
    head = await rm(cash, "head_data", "open_breaks", dimensions=["region"])
    emea = await rm(cash, "cash_ops_emea", "open_breaks", dimensions=["region"])
    assert len({r["region"] for r in head.rows}) >= 2
    assert {r["region"] for r in emea.rows} == {"EMEA"}
    assert head.source == "cashrecon" and head.metric_id == "open_breaks" and head.unit == "breaks"


async def test_run_metric_window_is_reported(cash):
    r = await rm(cash, "head_data", "open_breaks", time_range={"last_business_days": 5})
    assert r.window == {"from": "2026-09-24", "to": "2026-09-30"}


async def test_unknown_metric_lists_valid_ids(cash):
    with pytest.raises(SourceError) as e:
        await rm(cash, "head_data", "nope")
    assert "open_breaks" in str(e.value)


async def test_persona_without_the_dataset_gets_an_explicit_error(cash):
    with pytest.raises(SourceError, match="not entitled"):
        await rm(cash, "steward", "open_breaks")


async def test_bad_dimension_is_a_metric_error(cash):
    with pytest.raises(MetricError):
        await rm(cash, "head_data", "open_breaks", dimensions=["desk"])


async def test_injection_in_values_is_inert(cash):
    r = await rm(cash, "head_data", "open_breaks", filters={"region": "x'; DROP TABLE breaks; --"})
    assert r.rows == [{"value": 0}]


async def test_query_masks_account_numbers_without_pii_scope(cash):
    sql = {"sql": "SELECT nostro_no FROM cash_accounts ORDER BY account_id"}
    emea = await cash.query(claims_for("cash_ops_emea"), sql)
    head = await cash.query(claims_for("head_data"), sql)
    assert emea.rows and all(str(r[0]).startswith("****") for r in emea.rows)
    assert head.rows and not any(str(r[0]).startswith("****") for r in head.rows)
    assert emea.columns == ["nostro_no"]


async def test_query_is_row_scoped(cash):
    q = {"sql": "SELECT DISTINCT region FROM breaks"}
    assert {r[0] for r in (await cash.query(claims_for("cash_ops_emea"), q)).rows} == {"EMEA"}
    assert len({r[0] for r in (await cash.query(claims_for("head_data"), q)).rows}) >= 2


@pytest.mark.parametrize("sql", [
    "SELECT * FROM private.cash_accounts",
    "SELECT * FROM breaks; SELECT 1",
    "SELECT set_config('app.ctx', '', false)",
    r"SELECT 'a\' AS x",
    "DELETE FROM breaks",
])
async def test_query_rejections(cash, sql):
    with pytest.raises(SqlGuardError):
        await cash.query(claims_for("head_data"), {"sql": sql})


async def test_query_requires_a_sql_string_and_an_entitled_table(cash):
    with pytest.raises(SourceError, match="'sql'"):
        await cash.query(claims_for("head_data"), {"endpoint_id": "x"})
    with pytest.raises(SourceError, match="not entitled"):
        await cash.query(claims_for("steward"), {"sql": "SELECT * FROM breaks"})


async def test_query_table_entitlement_is_per_table(seeded):
    b = SqlBackend("feedhub", seeded)
    try:
        claims = claims_for("cash_ops_emea")
        claims["scopes"] = ["feedhub.sources"]  # table-level scope only
        ok = await b.query(claims, {"sql": "SELECT count(*) AS n FROM sources"})
        assert ok.row_count == 1
        with pytest.raises(SqlGuardError, match="table not allowed"):
            await b.query(claims, {"sql": "SELECT * FROM feeds"})
    finally:
        await b.aclose()


async def test_query_row_cap_and_truncation(seeded):
    b = SqlBackend("cashrecon", seeded.model_copy(update={"mcp_query_max_rows": 7}))
    try:
        r = await b.query(claims_for("head_data"), {"sql": "SELECT g FROM generate_series(1, 100000) AS g"})
        assert r.row_count == 7 and r.truncated is True and len(r.rows) == 7
    finally:
        await b.aclose()


async def test_query_timeout(seeded):
    b = SqlBackend("cashrecon", seeded.model_copy(update={"statement_timeout_ms": 200}))
    try:
        with pytest.raises(SourceError, match="timed out"):
            await b.query(claims_for("head_data"), {"sql": "SELECT count(*) FROM generate_series(1, 400000000)"})
    finally:
        await b.aclose()


async def test_query_byte_budget(cash):
    r = await cash.query(claims_for("head_data"),
                         {"sql": "SELECT repeat('x', 400000) AS big FROM generate_series(1, 50)"})
    assert r.truncated is True and len(r.rows) <= 3
    assert len(json.dumps(r.rows)) <= MAX_RESULT_BYTES


async def test_describe_lists_only_entitled_objects(cash):
    emea = await cash.describe(claims_for("cash_ops_emea"))
    assert isinstance(emea, DescribeResult) and emea.kind == "sql"
    names = {o["name"] for o in emea.objects}
    assert {"breaks", "cash_accounts"} <= names and "private.cash_accounts" not in names
    cols = {c["name"] for o in emea.objects if o["name"] == "cash_accounts" for c in o["columns"]}
    assert "nostro_no" in cols
    assert {m["id"] for m in emea.metrics} >= {"open_breaks", "aged_open_breaks", "auto_match_rate"}
    none = await cash.describe(claims_for("steward"))
    assert none.metrics == [] and none.objects == []


async def test_no_context_leak_between_pooled_calls(cash):
    seen = []
    for persona in ["head_data", "cash_ops_emea"] * 6:
        r = await rm(cash, persona, "open_breaks", dimensions=["region"])
        seen.append((persona, {x["region"] for x in r.rows}))
    for persona, regions in seen:
        assert (regions == {"EMEA"}) if persona == "cash_ops_emea" else (len(regions) >= 2)


async def test_scoped_section_pins_role_readonly_and_search_path(cash):
    with cash.scoped(claims_for("head_data")) as conn:
        role, ro, sp = conn.execute(
            "SELECT current_user::text, current_setting('transaction_read_only'), current_setting('search_path')"
        ).fetchone()
    assert (role, ro, sp) == ("bi_reader", "on", "public")
    with cash.scoped(claims_for("head_data")) as conn:
        ctx, same = conn.execute(
            "SELECT current_setting('app.ctx', true), current_setting('statement_timeout')::interval = %s * interval '1 millisecond'",
            (cash._settings.statement_timeout_ms,)).fetchone()
    assert ctx and same


def _assert_raw_connection_clean(backend):
    with backend._get_pool().connection() as conn:
        user, ctx, ro, timeout, cursors = conn.execute(
            "SELECT current_user::text, current_setting('app.ctx', true), current_setting('transaction_read_only'),"
            " current_setting('statement_timeout'), (SELECT count(*) FROM pg_cursors)").fetchone()
        default = conn.execute("SHOW statement_timeout").fetchone()[0]
    assert user != "bi_reader" and not ctx and ro == "off" and cursors == 0
    assert timeout == default and timeout != f"{backend._settings.statement_timeout_ms}ms"


async def test_no_state_survives_on_the_pooled_connection(seeded):
    b = SqlBackend("cashrecon", seeded.model_copy(update={"statement_timeout_ms": 700}))
    try:
        await rm(b, "head_data", "open_breaks")
        _assert_raw_connection_clean(b)
        with pytest.raises(SqlGuardError):
            await b.query(claims_for("head_data"), {"sql": "DELETE FROM breaks"})
        with pytest.raises(SourceError):
            await b.query(claims_for("head_data"), {"sql": "SELECT 1/0"})
        _assert_raw_connection_clean(b)
        with pytest.raises(SourceError, match="timed out"):
            await b.query(claims_for("head_data"), {"sql": "SELECT count(*) FROM generate_series(1, 400000000)"})
        _assert_raw_connection_clean(b)
    finally:
        await b.aclose()


async def test_cumulative_deadline_applies_across_fetches(seeded):
    b = SqlBackend("cashrecon", seeded.model_copy(update={"statement_timeout_ms": 1000, "mcp_query_max_rows": 1000}))
    try:
        start = time.monotonic()
        with pytest.raises(SourceError, match="timed out"):
            await b.query(claims_for("head_data"), {
                "sql": "SELECT g, (SELECT count(*) FROM generate_series(1, 500000 + g*0)) FROM generate_series(1,500) g"})
        assert time.monotonic() - start < 3
    finally:
        await b.aclose()


async def test_single_oversized_row_is_refused(cash):
    with pytest.raises(SourceError, match="size limit"):
        await cash.query(claims_for("head_data"), {"sql": "SELECT repeat('x', 20000000) AS big"})


async def test_truncated_flag_is_exact(seeded):
    b = SqlBackend("cashrecon", seeded.model_copy(update={"mcp_query_max_rows": 7}))
    try:
        exact = await b.query(claims_for("head_data"), {"sql": "SELECT g FROM generate_series(1, 7) AS g"})
        assert exact.row_count == 7 and exact.truncated is False
        more = await b.query(claims_for("head_data"), {"sql": "SELECT g FROM generate_series(1, 8) AS g"})
        assert more.row_count == 7 and more.truncated is True
    finally:
        await b.aclose()


async def test_inflight_limiter(cash):
    a, other = claims_for("head_data"), claims_for("cash_ops_emea")
    from contextlib import ExitStack
    with ExitStack() as stack:
        for _ in range(MAX_INFLIGHT_PER_PRINCIPAL):
            stack.enter_context(cash._limit_inflight(a))
        with pytest.raises(SourceError, match="too many concurrent"):
            with cash._limit_inflight(a):
                pass
        with cash._limit_inflight(other):
            pass
    with cash._limit_inflight(a):  # released afterwards
        pass
    assert cash._inflight == {}
    with pytest.raises(SourceError, match="invalid principal"):
        with cash._limit_inflight({"scopes": []}):
            pass


async def test_pool_timeout_maps_to_busy(cash, monkeypatch):
    pool = cash._get_pool()

    def boom(*a, **k):
        raise PoolTimeout("no connection")

    monkeypatch.setattr(pool, "connection", boom)
    with pytest.raises(SourceError, match="busy"):
        await rm(cash, "head_data", "open_breaks")


async def test_metric_requires_every_table_scope(seeded):
    b = SqlBackend("feedhub", seeded)
    try:
        claims = claims_for("cash_ops_emea")
        claims["scopes"] = ["feedhub.sources"]
        for metric_id in ("late_feeds", "open_support_tickets"):
            with pytest.raises(SourceError, match="not entitled to feedhub"):
                await b.run_metric(claims, metric_id=metric_id, dimensions=[], filters={}, time_range=None,
                                   order_by=None, limit=10)
        listed = {m["id"] for m in (await b.describe(claims)).metrics}
        assert "late_feeds" not in listed and "open_support_tickets" not in listed
        full = claims_for("head_data")
        assert {"late_feeds"} <= {m["id"] for m in (await b.describe(full)).metrics}
    finally:
        await b.aclose()


async def test_non_entitled_caller_learns_no_metric_ids(cash):
    with pytest.raises(SourceError, match="not entitled") as e:
        await rm(cash, "steward", "nope")
    assert "open_breaks" not in str(e.value)


async def test_bad_claims_and_closed_backend(seeded):
    b = SqlBackend("cashrecon", seeded)
    with pytest.raises(SourceError, match="invalid principal claims"):
        await b.query({"scopes": ["cashrecon"]}, {"sql": "SELECT 1"})
    await b.query(claims_for("head_data"), {"sql": "SELECT 1 FROM breaks LIMIT 1"})
    await b.aclose()
    with pytest.raises(SourceError, match="closed"):
        await b.query(claims_for("head_data"), {"sql": "SELECT 1 FROM breaks LIMIT 1"})


async def test_run_metric_reports_truncation(cash):
    full = await rm(cash, "head_data", "open_breaks", dimensions=["legal_entity_id"], limit=1000)
    n = full.row_count
    assert n > 2 and full.truncated is False
    cut = await rm(cash, "head_data", "open_breaks", dimensions=["legal_entity_id"], limit=2)
    assert cut.row_count == 2 and len(cut.rows) == 2 and cut.truncated is True
    assert cut.rows == full.rows[:2]
    exact = await rm(cash, "head_data", "open_breaks", dimensions=["legal_entity_id"], limit=n)
    assert exact.row_count == n and exact.truncated is False
    one_short = await rm(cash, "head_data", "open_breaks", dimensions=["legal_entity_id"], limit=n - 1)
    assert one_short.row_count == n - 1 and one_short.truncated is True
    total = await rm(cash, "head_data", "open_breaks", limit=1)
    assert total.row_count == 1 and total.truncated is False


async def test_sensitive_dimensions_are_blocked_for_metrics_only_principals(cash):
    with pytest.raises(SourceError, match=r"dimension\(s\) \['matched_by'\] are not available to metrics-only"):
        await rm(cash, "bi_analyst", "manual_matches", dimensions=["matched_by"])
    with pytest.raises(SourceError, match="metrics-only"):
        await rm(cash, "bi_analyst", "manual_matches", dimensions=["region", "matched_by"])
    no_flag = {k: v for k, v in claims_for("head_data").items() if k != "metrics_only"}  # fail closed
    with pytest.raises(SourceError, match="metrics-only"):
        await cash.run_metric(no_flag, metric_id="manual_matches", dimensions=["matched_by"], filters={},
                              time_range=None, order_by=None, limit=10)
    head = await rm(cash, "head_data", "manual_matches", dimensions=["matched_by"])
    assert head.row_count >= 2 and all(r["matched_by"] for r in head.rows)
    by_region = await rm(cash, "bi_analyst", "manual_matches", dimensions=["region"])
    assert by_region.row_count >= 2 and "matched_by" not in by_region.rows[0]
    total = await rm(cash, "bi_analyst", "manual_matches")
    assert total.row_count == 1


async def test_describe_exposes_sensitive_dimensions(cash):
    d = await cash.describe(claims_for("bi_analyst"))
    by_id = {m["id"]: m for m in d.metrics}
    assert by_id["manual_matches"]["sensitive_dimensions"] == ["matched_by"]
    assert by_id["open_breaks"]["sensitive_dimensions"] == []


async def test_required_dimension_over_the_backend_and_describe(cash):
    with pytest.raises(MetricError, match=r"requires dimension\(s\) \['ccy'\]"):
        await rm(cash, "head_data", "open_break_amount")
    r = await rm(cash, "head_data", "open_break_amount", dimensions=["ccy"])
    assert r.row_count >= 2 and len({row["ccy"] for row in r.rows}) == r.row_count
    d = await cash.describe(claims_for("head_data"))
    by_id = {m["id"]: m for m in d.metrics}
    assert by_id["open_break_amount"]["required_dimensions"] == ["ccy"]
    assert by_id["open_breaks"]["required_dimensions"] == []


async def test_unknown_metric_lists_only_readable_metric_ids(seeded):
    b = SqlBackend("feedhub", seeded)
    try:
        claims = {**claims_for("head_data"), "scopes": ["feedhub.support_tickets"]}
        with pytest.raises(SourceError) as e:
            await b.run_metric(claims, metric_id="zzz", dimensions=[], filters={}, time_range=None,
                               order_by=None, limit=10)
        msg = str(e.value)
        assert "open_support_tickets" in msg
        assert "late_feeds" not in msg and "feed_on_time_rate" not in msg
    finally:
        await b.aclose()


async def test_describe_notes_do_not_advertise_query_to_metrics_only_principals(cash):
    bi = await cash.describe(claims_for("bi_analyst"))
    assert bi.notes == ["Use run_metric for governed measures; free-form query is not available to this principal.",
                        "A metric's sensitive_dimensions can be neither grouped by nor filtered on by this principal."]
    head = await cash.describe(claims_for("head_data"))
    assert head.notes == ["Use run_metric for governed measures; query accepts a single SELECT."]


@pytest.mark.parametrize("metric_id", ["manual_matches", "auto_match_rate"])
async def test_sensitive_dimensions_cannot_be_filtered_on_by_metrics_only_principals(cash, metric_id):
    for flt in ({"matched_by": "ops.emea01"}, {"matched_by": ["ops.emea01", "ops.amer02"]},
                {"region": "EMEA", "matched_by": {"ne": "system"}}):
        with pytest.raises(SourceError, match=r"^filter\(s\) \['matched_by'\] are not available to metrics-only"):
            await rm(cash, "bi_analyst", metric_id, filters=flt)
    no_flag = {k: v for k, v in claims_for("head_data").items() if k != "metrics_only"}  # fail closed
    with pytest.raises(SourceError, match="metrics-only"):
        await cash.run_metric(no_flag, metric_id=metric_id, dimensions=[], filters={"matched_by": "ops.emea01"},
                              time_range=None, order_by=None, limit=10)
    head = await rm(cash, "head_data", metric_id, filters={"matched_by": "ops.emea01"})
    assert head.row_count == 1 and head.rows[0]["value"] is not None
    by_region = await rm(cash, "bi_analyst", metric_id, filters={"region": "EMEA"})
    assert by_region.row_count == 1


@pytest.mark.parametrize("kw, msg", [
    ({"filters": [("matched_by", "ops.emea01")]}, "filters must be an object"),
    ({"filters": (("matched_by", "x"),)}, "filters must be an object"),
    ({"filters": "matched_by"}, "filters must be an object"),
    ({"filters": {1: "x"}}, "filters must be an object"),
    ({"dimensions": "matched_by"}, "dimensions must be a list of strings"),
    ({"dimensions": ("matched_by",)}, "dimensions must be a list of strings"),
    ({"dimensions": [None]}, "dimensions must be a list of strings"),
])
@pytest.mark.parametrize("persona", ["bi_analyst", "head_data"])
async def test_malformed_dimensions_or_filters_fail_closed_on_direct_calls(cash, kw, msg, persona):
    with pytest.raises(SourceError, match=f"^{msg}$"):
        await rm(cash, persona, "manual_matches", **kw)


async def test_well_formed_dict_filter_still_works_for_head_data(cash):
    r = await rm(cash, "head_data", "manual_matches", filters={"matched_by": "ops.emea01"})
    assert r.row_count == 1 and r.rows[0]["value"] >= 1
