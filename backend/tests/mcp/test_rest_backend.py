import asyncio

import httpx
import pytest

from prism.mcp.rest_backend import (
    MAX_INFLIGHT_PER_PRINCIPAL,
    MAX_RESPONSE_BYTES,
    RestBackend,
    load_rest_config,
)
from prism.mcp import rest_backend as rb
from prism.mcp.results import SourceError
from prism.security.personas import claims_for
from prism.sources.marketmaster_api.app import create_app as create_marketmaster_api
from prism.sources.refmaster_api.app import create_app as create_refmaster_api

pytestmark = pytest.mark.db


@pytest.fixture
async def refmaster(seeded):
    api = create_refmaster_api(seeded)
    b = RestBackend("refmaster", seeded, base_url="http://api", transport=httpx.ASGITransport(app=api))
    yield b
    await b.aclose()
    await api.state.dbs.close()


@pytest.fixture
async def marketmaster(seeded):
    api = create_marketmaster_api(seeded)
    b = RestBackend("marketmaster", seeded, base_url="http://api", transport=httpx.ASGITransport(app=api))
    yield b
    await b.aclose()
    await api.state.dbs.close()


async def rm(b, persona, metric_id, **kw):
    args = dict(dimensions=[], filters={}, time_range=None, order_by=None, limit=100)
    args.update(kw)
    return await b.run_metric(claims_for(persona), metric_id=metric_id, **args)


def test_registry_files_load_and_reference_real_endpoints():
    for source in ("refmaster", "marketmaster"):
        endpoints, metrics = load_rest_config(source)
        assert endpoints and metrics
        assert all(m.endpoint in endpoints for m in metrics.values())


async def test_price_conflict_heat_map_story(marketmaster):
    r = await rm(marketmaster, "steward", "price_conflicts", dimensions=["vendor_id", "asset_class"],
                 time_range={"last_business_days": 5})
    top = r.rows[0]
    assert (top["vendor_id"], top["asset_class"]) == ("V_A", "Corp bond")
    assert r.window == {"from": "2026-09-24", "to": "2026-09-30"} and r.unit == "conflicts"
    assert r.rows == sorted(r.rows, key=lambda x: -x["value"])


async def test_metric_without_dimensions_is_a_total(marketmaster):
    grouped = await rm(marketmaster, "steward", "price_conflicts", dimensions=["vendor_id"])
    total = await rm(marketmaster, "steward", "price_conflicts")
    assert total.rows == [{"value": sum(x["value"] for x in grouped.rows)}]


async def test_refmaster_metrics_and_filters(refmaster):
    all_open = await rm(refmaster, "steward", "open_dq_exceptions", dimensions=["domain"])
    assert all_open.rows and all(r["value"] >= 0 for r in all_open.rows)
    eq = await rm(refmaster, "steward", "dq_exceptions_total", dimensions=["asset_class"],
                  filters={"asset_class": "Equity"})
    assert {r["asset_class"] for r in eq.rows} == {"Equity"}


async def test_row_scoping_flows_through_the_api(refmaster):
    claims = claims_for("steward")
    claims["rows"] = {"asset_class": ["Equity"]}
    scoped = await refmaster.query(claims, {"endpoint_id": "securities", "params": {"limit": 500}})
    assert scoped.row_count > 0
    ai = scoped.columns.index("asset_class")
    assert {r[ai] for r in scoped.rows} == {"Equity"}


async def test_forbidden_is_an_error_not_empty(refmaster, marketmaster):
    with pytest.raises(SourceError, match="denied|not entitled"):
        await rm(refmaster, "cash_ops_emea", "open_dq_exceptions")
    with pytest.raises(SourceError, match="denied|not entitled"):
        await refmaster.query(claims_for("cash_ops_emea"), {"endpoint_id": "securities", "params": {}})
    with pytest.raises(SourceError, match="denied|not entitled"):
        await marketmaster.query(claims_for("invest_ops_growth"), {"endpoint_id": "prices_suspects", "params": {}})


async def test_query_endpoint_validation(refmaster):
    with pytest.raises(SourceError, match="valid: "):
        await refmaster.query(claims_for("steward"), {"endpoint_id": "nope", "params": {}})
    for params in ({"bogus": 1}, {"limit": 501}, {"limit": "5; DROP"}, {"from": "not-a-date"}):
        with pytest.raises(SourceError):
            await refmaster.query(claims_for("steward"), {"endpoint_id": "corporate_actions", "params": params})
    with pytest.raises(SourceError, match="security_id"):
        await refmaster.query(claims_for("steward"), {"endpoint_id": "security", "params": {}})
    with pytest.raises(SourceError, match="group_by"):
        await refmaster.query(claims_for("steward"),
                              {"endpoint_id": "exceptions_summary", "params": {"group_by": ["assignee"]}})
    with pytest.raises(SourceError, match="endpoint_id"):
        await refmaster.query(claims_for("steward"), {"sql": "select 1"})


async def test_query_endpoint_happy_paths(refmaster, marketmaster):
    r = await refmaster.query(claims_for("steward"),
                              {"endpoint_id": "securities", "params": {"asset_class": "Corp bond", "limit": 5}})
    assert r.row_count == 5 and "isin" in r.columns
    one = await refmaster.query(claims_for("steward"), {"endpoint_id": "security", "params": {"security_id": "SEC000001"}})
    assert one.row_count == 1
    sec = "SEC000001"
    ts = await marketmaster.query(claims_for("steward"), {"endpoint_id": "timeseries", "params": {"security_id": sec}})
    assert ts.row_count >= 20 and ts.columns[0] == "price_date"
    gc = await marketmaster.query(claims_for("steward"), {"endpoint_id": "golden_copy", "params": {"security_id": sec}})
    assert gc.row_count == 1 and gc.columns == ["golden", "quotes"]


async def test_describe_lists_endpoints_and_metrics(marketmaster):
    d = await marketmaster.describe(claims_for("steward"))
    assert d.kind == "rest" and {o["name"] for o in d.objects} >= {"prices_conflicts_summary", "golden_copy"}
    assert [m["id"] for m in d.metrics] == ["price_conflicts", "price_suspects"]
    nothing = await marketmaster.describe(claims_for("cash_ops_emea"))
    assert nothing.metrics == [] and nothing.objects == []


async def test_api_down_is_a_clear_error(seeded):
    b = RestBackend("refmaster", seeded, base_url="http://127.0.0.1:9")  # nothing listens on port 9
    try:
        with pytest.raises(SourceError, match="unavailable"):
            await rm(b, "steward", "open_dq_exceptions")
    finally:
        await b.aclose()


async def test_downstream_token_is_freshly_minted_and_audience_bound(seeded):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"rows": []})

    b = RestBackend("refmaster", seeded, base_url="http://api", transport=httpx.MockTransport(handler))
    try:
        await rm(b, "steward", "open_dq_exceptions", dimensions=["domain"])
    finally:
        await b.aclose()
    from prism.security.tokens import verify

    claims = verify(captured["auth"].removeprefix("Bearer "), "refmaster-api", seeded.jwt_secret.get_secret_value())
    assert claims["sub"] == "steward" and claims["aud"] == "refmaster-api" and claims["exp"] - claims["iat"] <= 60


def _mock_backend(seeded, handler, source="refmaster"):
    return RestBackend(source, seeded, base_url="http://api", transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("bad", ["SEC000001/../../exceptions", "a?debug=1", "a b", "%2e%2e", "..", "a" * 65, "a#b", "a/b", ""])
async def test_path_parameter_values_are_validated_and_no_request_is_sent(seeded, bad):
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    b = _mock_backend(seeded, handler)
    try:
        with pytest.raises(SourceError):
            await b.query(claims_for("steward"), {"endpoint_id": "security", "params": {"security_id": bad}})
    finally:
        await b.aclose()
    assert seen == []


async def test_valid_path_parameter_reaches_the_registry_path(seeded):
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"a": 1})

    b = _mock_backend(seeded, handler)
    try:
        await b.query(claims_for("steward"), {"endpoint_id": "security", "params": {"security_id": "SEC000001"}})
    finally:
        await b.aclose()
    assert seen == ["/api/v1/securities/SEC000001"]


async def test_inflight_cap_per_principal(seeded):
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        await gate.wait()
        return httpx.Response(200, json={"items": []})

    b = _mock_backend(seeded, handler)
    req = {"endpoint_id": "securities", "params": {}}
    try:
        tasks = [asyncio.create_task(b.query(claims_for("steward"), req)) for _ in range(MAX_INFLIGHT_PER_PRINCIPAL)]
        await asyncio.sleep(0.05)
        with pytest.raises(SourceError, match="too many concurrent"):
            await b.query(claims_for("steward"), req)
        other = asyncio.create_task(b.query(claims_for("head_data"), req))  # different sub: not counted
        await asyncio.sleep(0.05)
        gate.set()
        await asyncio.gather(*tasks, other)  # raises if any (incl. the other principal's) call was rejected
        assert b._inflight == {}
        assert (await b.query(claims_for("steward"), req)).row_count == 0
    finally:
        gate.set()
        await b.aclose()


async def test_missing_sub_is_rejected(seeded):
    b = _mock_backend(seeded, lambda r: httpx.Response(200, json={}))
    try:
        for claims in ({**claims_for("steward"), "sub": ""}, {k: v for k, v in claims_for("steward").items() if k != "sub"}):
            with pytest.raises(SourceError, match="invalid principal claims"):
                await b.describe(claims)
    finally:
        await b.aclose()


async def test_oversized_response_body_is_refused(seeded):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"items": "' + b"x" * (MAX_RESPONSE_BYTES + 10) + b'"}')

    b = _mock_backend(seeded, handler)
    try:
        with pytest.raises(SourceError, match="size limit"):
            await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
    finally:
        await b.aclose()


async def test_oversized_content_length_is_refused_early(seeded):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-length": str(MAX_RESPONSE_BYTES + 1)}, content=b"{}")

    b = _mock_backend(seeded, handler)
    try:
        with pytest.raises(SourceError, match="size limit"):
            await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
    finally:
        await b.aclose()


@pytest.mark.parametrize("body", [b"<html>nope</html>", b"[1, 2]", b'"str"'])
async def test_non_json_or_non_object_body_is_an_error(seeded, body):
    b = _mock_backend(seeded, lambda r: httpx.Response(200, content=body))
    try:
        with pytest.raises(SourceError, match="unexpected response"):
            await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
    finally:
        await b.aclose()


async def test_object_without_expected_key_is_an_error(seeded):
    b = _mock_backend(seeded, lambda r: httpx.Response(200, json={"other": 1}))
    try:
        with pytest.raises(SourceError, match="unexpected response"):
            await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
    finally:
        await b.aclose()


async def test_aclose_is_idempotent_and_later_calls_fail_cleanly(seeded):
    b = _mock_backend(seeded, lambda r: httpx.Response(200, json={"items": []}))
    await b.aclose()
    await b.aclose()
    with pytest.raises(SourceError, match="backend is closed"):
        await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
    with pytest.raises(SourceError, match="backend is closed"):
        await b.describe(claims_for("steward"))


# ---------------------------------------------------------------- fix round 1
async def _no_request_backend(seeded):
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [], "rows": []})

    return _mock_backend(seeded, handler), seen


@pytest.mark.parametrize("params", [{"q": "\ud800"}, {"q": "a\x00b"}, {"offset": 10**30}, {"q": "x" * 201},
                                    {"limit": True}])
async def test_bad_caller_values_are_source_errors_with_no_request(seeded, params):
    b, seen = await _no_request_backend(seeded)
    try:
        with pytest.raises(SourceError):
            await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": params})
    finally:
        await b.aclose()
    assert seen == []


async def test_str_list_is_deduplicated_capped_and_enum_checked(seeded):
    got = []

    async def handler(request: httpx.Request) -> httpx.Response:
        got.append(request.url.params.get_list("group_by"))
        return httpx.Response(200, json={"rows": []})

    b = _mock_backend(seeded, handler)
    try:
        await b.query(claims_for("steward"), {"endpoint_id": "exceptions_summary",
                                              "params": {"group_by": ["domain"] * 5000}})
        assert got == [["domain"]]
        with pytest.raises(SourceError, match="group_by"):
            await b.query(claims_for("steward"), {"endpoint_id": "exceptions_summary",
                                                  "params": {"group_by": ["domain"] * 5000 + ["bad"]}})
        with pytest.raises(SourceError, match="group_by"):
            await b.query(claims_for("steward"), {"endpoint_id": "exceptions_summary",
                                                  "params": {"group_by": ["\ud800"]}})
    finally:
        await b.aclose()
    assert len(got) == 1


async def test_invalid_url_and_unicode_errors_are_translated(seeded):
    for exc in (httpx.InvalidURL("secret-url"), UnicodeEncodeError("utf-8", "x", 0, 1, "bad")):
        def handler(request, exc=exc):
            raise exc

        b = _mock_backend(seeded, handler)
        try:
            with pytest.raises(SourceError, match="unavailable") as e:
                await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
            assert "secret-url" not in str(e.value)
        finally:
            await b.aclose()


async def test_non_entitled_caller_learns_no_catalog(refmaster):
    for call in (
        lambda: rm(refmaster, "cash_ops_emea", "zzz"),
        lambda: refmaster.query(claims_for("cash_ops_emea"), {"endpoint_id": "zzz", "params": {}}),
        lambda: refmaster.query(claims_for("cash_ops_emea"), {"params": {}}),
    ):
        with pytest.raises(SourceError) as e:
            await call()
        msg = str(e.value)
        assert msg == "not entitled to the refmaster dataset"


async def test_unknown_ids_list_only_readable_ones(refmaster):
    claims = {**claims_for("steward"), "scopes": ["refmaster.securities"]}
    with pytest.raises(SourceError) as e:
        await refmaster.query(claims, {"endpoint_id": "zzz", "params": {}})
    assert "['security', 'securities']" in str(e.value) or "['securities', 'security']" in str(e.value)
    assert "exceptions" not in str(e.value)
    with pytest.raises(SourceError) as e:
        await refmaster.query(claims, {"params": {}})
    assert "exceptions" not in str(e.value) and "securities" in str(e.value)
    with pytest.raises(SourceError) as e:
        await refmaster.run_metric(claims, metric_id="zzz", dimensions=[], filters={}, time_range=None,
                                   order_by=None, limit=10)
    assert "open_dq_exceptions" not in str(e.value)


async def test_truncated_reflects_the_api_default_page(refmaster):
    dflt = await refmaster.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
    assert dflt.row_count == 100 and dflt.truncated is True
    full = await refmaster.query(claims_for("steward"), {"endpoint_id": "securities", "params": {"limit": 500}})
    assert full.row_count == 240 and full.truncated is False


async def test_overall_deadline_covers_a_slow_body(seeded, monkeypatch):
    monkeypatch.setattr(rb, "TOTAL_DEADLINE_S", 0.5)

    async def body():
        for _ in range(20):
            await asyncio.sleep(0.2)
            yield b" "

    b = _mock_backend(seeded, lambda r: httpx.Response(200, content=body()))
    try:
        with pytest.raises(SourceError, match="timed out"):
            await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
        assert b._inflight == {}
    finally:
        await b.aclose()


async def test_deeply_nested_json_is_an_unexpected_response(seeded):
    b = _mock_backend(seeded, lambda r: httpx.Response(200, content=b"[" * 200_000 + b"]" * 200_000))
    try:
        with pytest.raises(SourceError, match="unexpected response"):
            await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
    finally:
        await b.aclose()


@pytest.mark.parametrize("rows", [[{"domain": "a", "open_count": "many"}], [{"domain": "a"}],
                                  [{"domain": "a", "open_count": {"x": 1}}], [{"domain": "a", "open_count": True}]])
async def test_odd_downstream_metric_rows_are_unexpected_response(seeded, rows):
    b = _mock_backend(seeded, lambda r: httpx.Response(200, json={"rows": rows}))
    try:
        with pytest.raises(SourceError, match="unexpected response"):
            await rm(b, "steward", "open_dq_exceptions", dimensions=["domain"])
    finally:
        await b.aclose()


@pytest.mark.parametrize("value", [20260901, "20260901", "2026-9-1", "2026-09-01T00:00:00", "2026-13-01",
                                   __import__("datetime").datetime(2026, 9, 1), None])
async def test_date_params_are_strict(seeded, value):
    b, seen = await _no_request_backend(seeded)
    try:
        with pytest.raises(SourceError, match="ISO date"):
            await b.query(claims_for("steward"), {"endpoint_id": "exceptions", "params": {"from": value}})
    finally:
        await b.aclose()
    assert seen == []


async def test_date_objects_and_iso_strings_are_accepted(seeded):
    got = []

    async def handler(request: httpx.Request) -> httpx.Response:
        got.append(request.url.params["from"])
        return httpx.Response(200, json={"items": []})

    b = _mock_backend(seeded, handler)
    try:
        for v in ("2026-09-01", __import__("datetime").date(2026, 9, 1)):
            await b.query(claims_for("steward"), {"endpoint_id": "exceptions", "params": {"from": v}})
    finally:
        await b.aclose()
    assert got == ["2026-09-01"] * 2


async def test_order_by_and_dimension_validation(refmaster):
    for order_by in ("--domain", "-", "domain; x", "asset_class", "domain "):
        with pytest.raises(SourceError, match="valid:"):
            await rm(refmaster, "steward", "open_dq_exceptions", dimensions=["domain"], order_by=order_by)
    ok = await rm(refmaster, "steward", "open_dq_exceptions", dimensions=["domain"], order_by="-domain")
    assert ok.rows
    with pytest.raises(SourceError, match="unique"):
        await rm(refmaster, "steward", "open_dq_exceptions", dimensions=["domain", "domain"])
    with pytest.raises(SourceError, match="unique"):
        await rm(refmaster, "steward", "open_dq_exceptions", dimensions=[1])


async def test_query_columns_are_the_union_over_all_rows(seeded):
    items = [{"a": 1}, {"a": 2, "b": 3}, {"c": 4}]
    b = _mock_backend(seeded, lambda r: httpx.Response(200, json={"items": items}))
    try:
        r = await b.query(claims_for("steward"), {"endpoint_id": "securities", "params": {}})
    finally:
        await b.aclose()
    assert r.columns == ["a", "b", "c"]
    assert r.rows == [[1, None, None], [2, 3, None], [None, None, 4]]


async def test_only_the_forwarded_claims_are_minted(seeded):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["authorization"]
        return httpx.Response(200, json={"rows": []})

    b = _mock_backend(seeded, handler)
    claims = {**claims_for("steward"), "email": "x@y", "aud": "evil", "exp": 1, "admin": True}
    try:
        await b.run_metric(claims, metric_id="open_dq_exceptions", dimensions=["domain"], filters={},
                           time_range=None, order_by=None, limit=10)
    finally:
        await b.aclose()
    from prism.security.tokens import verify

    minted = verify(captured["auth"].removeprefix("Bearer "), "refmaster-api", seeded.jwt_secret.get_secret_value())
    assert set(minted) == {"aud", "exp", "iat", "metrics_only", "roles", "rows", "scopes", "sub"}
    assert minted["aud"] == "refmaster-api" and minted["sub"] == "steward"


async def test_run_metric_reports_truncation(marketmaster):
    full = await rm(marketmaster, "steward", "price_conflicts", dimensions=["vendor_id"], limit=1000)
    n = full.row_count
    assert n > 1 and full.truncated is False
    cut = await rm(marketmaster, "steward", "price_conflicts", dimensions=["vendor_id"], limit=1)
    assert cut.row_count == 1 and cut.truncated is True and cut.rows == full.rows[:1]
    exact = await rm(marketmaster, "steward", "price_conflicts", dimensions=["vendor_id"], limit=n)
    assert exact.row_count == n and exact.truncated is False
    total = await rm(marketmaster, "steward", "price_conflicts", limit=1)
    assert total.row_count == 1 and total.truncated is False


def test_rest_sensitive_dimensions_must_be_dimensions():
    base = {"id": "m", "endpoint": "e", "value_field": "v", "description": "d", "unit": "u",
            "dimensions": {"domain": "domain"}, "filters": {}}
    assert rb.EndpointMetric.model_validate(base).sensitive_dimensions == []
    assert rb.EndpointMetric.model_validate({**base, "sensitive_dimensions": ["domain"]}).sensitive_dimensions
    with pytest.raises(ValueError, match="sensitive_dimensions"):
        rb.EndpointMetric.model_validate({**base, "sensitive_dimensions": ["rule_id"]})


async def test_rest_sensitive_dimensions_are_blocked_for_metrics_only_principals(refmaster):
    m = refmaster._metrics["open_dq_exceptions"]
    refmaster._metrics["open_dq_exceptions"] = m.model_copy(update={"sensitive_dimensions": ["rule_id"]})
    with pytest.raises(SourceError, match="not available to metrics-only principals"):
        await rm(refmaster, "bi_analyst", "open_dq_exceptions", dimensions=["rule_id"])
    ok = await rm(refmaster, "head_data", "open_dq_exceptions", dimensions=["rule_id"])
    assert ok.row_count >= 1
    ok = await rm(refmaster, "bi_analyst", "open_dq_exceptions", dimensions=["domain"])
    assert ok.row_count >= 1
    d = await refmaster.describe(claims_for("bi_analyst"))
    by_id = {x["id"]: x for x in d.metrics}
    assert by_id["open_dq_exceptions"]["sensitive_dimensions"] == ["rule_id"]
    assert by_id["dq_exceptions_total"]["sensitive_dimensions"] == []
    assert all(x["required_dimensions"] == [] for x in d.metrics)


async def test_describe_notes_do_not_advertise_query_to_metrics_only_principals(refmaster):
    bi = await refmaster.describe(claims_for("bi_analyst"))
    assert bi.notes == ["Use run_metric for governed measures; free-form query is not available to this principal.",
                        "A metric's sensitive_dimensions can be neither grouped by nor filtered on by this principal."]
    steward = await refmaster.describe(claims_for("steward"))
    assert steward.notes == ["Use run_metric for governed measures; query accepts {'endpoint_id', 'params'}."]


async def test_rest_sensitive_dimensions_cannot_be_filtered_on_by_metrics_only_principals(refmaster):
    m = refmaster._metrics["open_dq_exceptions"]
    refmaster._metrics["open_dq_exceptions"] = m.model_copy(update={"sensitive_dimensions": ["domain"]})
    with pytest.raises(SourceError, match=r"^filter\(s\) \['domain'\] are not available to metrics-only"):
        await rm(refmaster, "bi_analyst", "open_dq_exceptions", filters={"domain": "security"})
    ok = await rm(refmaster, "head_data", "open_dq_exceptions", filters={"domain": "security"})
    assert ok.row_count == 1
    ok = await rm(refmaster, "bi_analyst", "open_dq_exceptions", filters={"asset_class": "Equity"})
    assert ok.row_count == 1


@pytest.mark.parametrize("kw, msg", [
    ({"filters": [("domain", "security")]}, "filters must be an object"),
    ({"filters": (("domain", "x"),)}, "filters must be an object"),
    ({"filters": "domain"}, "filters must be an object"),
    ({"filters": {1: "x"}}, "filters must be an object"),
    ({"dimensions": "domain"}, "dimensions must be a list of unique strings"),
    ({"dimensions": ("domain",)}, "dimensions must be a list of unique strings"),
])
async def test_rest_malformed_dimensions_or_filters_fail_closed_on_direct_calls(refmaster, kw, msg):
    m = refmaster._metrics["open_dq_exceptions"]
    refmaster._metrics["open_dq_exceptions"] = m.model_copy(update={"sensitive_dimensions": ["domain"]})
    with pytest.raises(SourceError, match=f"^{msg}$"):
        await rm(refmaster, "bi_analyst", "open_dq_exceptions", **kw)


def test_rest_fine_grain_dimensions_must_be_dimensions():
    base = {"id": "m", "endpoint": "e", "value_field": "v", "description": "d", "unit": "u",
            "dimensions": {"vendor_id": "vendor_id", "price_date": "price_date"}, "filters": {}}
    assert rb.EndpointMetric.model_validate(base).fine_grain_dimensions == []
    ok = rb.EndpointMetric.model_validate({**base, "fine_grain_dimensions": ["vendor_id", "price_date"]})
    assert ok.fine_grain_dimensions == ["vendor_id", "price_date"]
    with pytest.raises(ValueError, match="fine_grain_dimensions"):
        rb.EndpointMetric.model_validate({**base, "fine_grain_dimensions": ["rule_id"]})


def test_rest_filter_may_not_alias_a_sensitive_or_fine_dimension():
    base = {"id": "m", "endpoint": "e", "value_field": "v", "description": "d", "unit": "u",
            "dimensions": {"vendor": "vendor_id", "price_date": "price_date"},
            "fine_grain_dimensions": ["vendor", "price_date"]}
    with pytest.raises(ValueError, match="exposes fine dimension"):
        rb.EndpointMetric.model_validate({**base, "filters": {"v": {"param": "vendor_id"}}})
    with pytest.raises(ValueError, match="exposes sensitive dimension"):
        rb.EndpointMetric.model_validate({**base, "fine_grain_dimensions": [], "sensitive_dimensions": ["vendor"],
                                          "filters": {"v": {"param": "vendor_id"}}})
    rb.EndpointMetric.model_validate({**base, "filters": {"vendor": {"param": "vendor_id"}}})


async def test_incident_hops_through_rest_metrics(refmaster, marketmaster):
    from prism.sim.universe import SimConfig, build_universe
    inc = build_universe(SimConfig.small()).stories.incident
    ca = await rm(refmaster, "steward", "pending_corporate_actions", dimensions=["security_id", "action_type"],
                  filters={"issuer_entity_id": inc.issuer_entity_id})
    assert ca.rows == [{"security_id": inc.security_id, "action_type": "split", "value": 1}]
    dq = await rm(refmaster, "steward", "open_dq_exceptions", dimensions=["record_ref"],
                  filters={"record_ref": inc.security_id})
    assert dq.rows and dq.rows[0]["value"] >= 1
    ps = await rm(marketmaster, "steward", "price_suspects", dimensions=["security_id", "kind"],
                  filters={"status": "accepted"})
    assert ps.rows == [{"security_id": inc.security_id, "kind": "spike", "value": 1}]
    with pytest.raises(SourceError, match="unknown dimension"):
        await rm(marketmaster, "steward", "price_suspects", dimensions=["deviation_pct"])


async def test_rest_filters_take_one_value(refmaster):
    with pytest.raises(SourceError, match="single value"):
        await rm(refmaster, "steward", "pending_corporate_actions", filters={"security_id": ["SEC000001", "SEC000002"]})
