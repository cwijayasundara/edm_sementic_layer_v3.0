"""Gateway policy: role scope, metrics-only, sensitive / required dimensions, grain, row scope. Expected access comes
from tests/db/test_rbac.py::expected() (written independently of the policy code), never from allowed_scopes."""
import time

import pytest

from prism.gateway.errors import GatewayError
from prism.gateway.policy import MetricPlan, Policy
from prism.mcp.results import UserFacingError
from prism.security.personas import PERSONAS, claims_for
from tests.db.test_rbac import expected
from tests.gateway.fakes import registry_catalog
from tests.graph_ns import TEST_GRAPH_NS

REQUIRED_ARGS = {"open_break_amount": ["ccy"]}


@pytest.fixture(scope="module")
def policy(fake_catalog) -> Policy:
    return Policy(fake_catalog)


def _permitted(persona: str, metric) -> bool:
    return all(expected(persona, *t.split(".", 1)) != "none" for t in metric.tables)


def _code(fn, *a, **k) -> GatewayError:
    with pytest.raises(GatewayError) as info:
        fn(*a, **k)
    assert isinstance(info.value, UserFacingError)
    return info.value


def test_matrix_covers_5_personas_x_22_metrics(fake_catalog):
    assert len(fake_catalog.metrics) == 22 and len(PERSONAS) == 5


@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_policy_matrix(policy, fake_catalog, persona):
    claims = claims_for(persona)
    failures = []
    for mid, m in sorted(fake_catalog.metrics.items()):
        dims = REQUIRED_ARGS.get(mid, [])
        if _permitted(persona, m):
            try:
                plan = policy.check_metric(claims, mid, dims, {})
            except GatewayError as e:
                failures.append(f"{mid}: expected allowed, got {e.code}")
                continue
            assert plan == MetricPlan(source=m.source, tool="run_metric", metric_id=mid, unit=m.unit,
                                      arguments={"metric_id": mid, "dimensions": dims, "filters": {}})
        else:
            try:
                policy.check_metric(claims, mid, dims, {})
                failures.append(f"{mid}: expected not_permitted, got a plan")
            except GatewayError as e:
                if e.code != "not_permitted":
                    failures.append(f"{mid}: expected not_permitted, got {e.code}")
    assert not failures, "\n".join(failures)


def test_matrix_is_not_trivial(fake_catalog):
    allowed = {p: sum(_permitted(p, m) for m in fake_catalog.metrics.values()) for p in PERSONAS}
    assert allowed["head_data"] == allowed["bi_analyst"] == 22
    assert 0 < allowed["steward"] < 22 and 0 < allowed["cash_ops_emea"] < 22 and 0 < allowed["invest_ops_growth"] < 22


def test_unreadable_metric_answers_exactly_like_an_unknown_one(policy):
    claims = claims_for("cash_ops_emea")
    hidden = _code(policy.check_metric, claims, "price_conflicts", [], {})
    unknown = _code(policy.check_metric, claims, "no_such_metric", [], {})
    assert (hidden.code, type(hidden)) == (unknown.code, type(unknown)) == ("not_permitted", GatewayError)
    assert str(hidden).replace("price_conflicts", "X") == str(unknown).replace("no_such_metric", "X")
    for word in ("marketmaster", "refmaster", "assetrecon", "price", "exists", "unknown"):
        assert word not in str(hidden).replace("price_conflicts", "")
    # nothing else about the hidden metric leaks either: dimensions / filters on it are never inspected
    again = _code(policy.check_metric, claims, "price_conflicts", ["vendor_id", "nope"], {"zzz": 1})
    assert str(again) == str(hidden) and again.code == "not_permitted"


@pytest.mark.parametrize("bad", ["", "X" * 5000, "Open_Breaks", "open breaks", "a;drop", 7, None, ["open_breaks"],
                                 "été"])
def test_malformed_metric_ids_are_refused_without_echoing_them(policy, bad):
    e = _code(policy.check_metric, claims_for("head_data"), bad, [], {})
    assert e.code in ("unknown_metric", "not_permitted") and len(str(e)) < 200
    if isinstance(bad, str) and len(bad) > 100:
        assert bad not in str(e)


def test_missing_scope_claims_deny(policy):
    for claims in ({"sub": "x"}, {"sub": "x", "scopes": None}, {"sub": "x", "scopes": "cashrecon"}, None, "claims"):
        assert _code(policy.check_metric, claims, "open_breaks", [], {}).code == "not_permitted"


# ------------------------------------------------------------------------------------- row scope (fail closed)
def test_dataset_scope_without_the_tables_row_dimension_is_denied(policy):
    claims = {**claims_for("cash_ops_emea"), "rows": {"source_type": ["bank"]}}          # region missing
    hidden = _code(policy.check_metric, claims, "open_breaks", [], {})
    unknown = _code(policy.check_metric, claims, "nope_metric", [], {})
    assert hidden.code == "not_permitted" and str(hidden).replace("open_breaks", "X") == \
        str(unknown).replace("nope_metric", "X")
    policy.check_metric(claims, "late_feeds", [], {})                                     # feedhub: source_type ok
    assert _code(policy.check_metric, {**claims, "rows": {"region": []}}, "open_breaks", [], {}).code == \
        "not_permitted"
    assert _code(policy.check_metric, {**claims, "rows": None}, "open_breaks", [], {}).code == "not_permitted"
    policy.check_metric({**claims, "rows": {"region": ["*"]}}, "open_breaks", [], {})


# ---------------------------------------------------------------------------------- dimensions and filters
@pytest.mark.parametrize("dims", ["ccy", "region", ("region", 1), [1], ["region", "region"], {"region": 1},
                                  [["region"]], ["x"] * 21])
def test_dimensions_must_be_a_list_of_unique_strings(policy, dims):
    assert _code(policy.check_metric, claims_for("head_data"), "open_breaks", dims, {}).code == "invalid_request"


def test_unknown_dimension_and_filter_are_invalid_requests(policy):
    head = claims_for("head_data")
    e = _code(policy.check_metric, head, "open_breaks", ["nope"], {})
    assert e.code == "invalid_request" and "nope" in str(e)
    e = _code(policy.check_metric, head, "open_breaks", [], {"nope": 1})
    assert e.code == "invalid_request" and "nope" in str(e)
    for f in (["region"], "region", {1: "x"}, {f"f{i}": 1 for i in range(21)}):
        assert _code(policy.check_metric, head, "open_breaks", [], f).code == "invalid_request"
    assert policy.check_metric(head, "open_breaks", [], None).arguments["filters"] == {}


def test_plan_carries_only_catalog_names_and_passes_filter_values_through(policy):
    plan = policy.check_metric(claims_for("head_data"), "open_breaks", ["region"], {"region": ["EMEA"]}, limit=10)
    assert plan.arguments == {"metric_id": "open_breaks", "dimensions": ["region"], "filters": {"region": ["EMEA"]},
                              "limit": 10}
    assert plan.audit_plan() == {"metric_ids": ["open_breaks"], "dimensions": ["region"]}
    for bad in (0, 1001, "5", True, 1.5):
        assert _code(policy.check_metric, claims_for("head_data"), "open_breaks", [], {}, limit=bad).code == \
            "invalid_request"


def test_required_dimension(policy):
    for persona in ("head_data", "bi_analyst", "cash_ops_emea"):
        e = _code(policy.check_metric, claims_for(persona), "open_break_amount", ["region"], {})
        assert e.code == "missing_required_dimension" and "ccy" in str(e)
        policy.check_metric(claims_for(persona), "open_break_amount", ["region", "ccy"], {})


# ------------------------------------------------------------------------------------------- metrics-only
@pytest.mark.parametrize("metric", ["manual_matches", "auto_match_rate"])
def test_sensitive_dimension_rejected_for_metrics_only_group_by_and_filter(policy, metric):
    bi = claims_for("bi_analyst")
    assert _code(policy.check_metric, bi, metric, ["matched_by"], {}).code == "sensitive_dimension"
    assert _code(policy.check_metric, bi, metric, [], {"matched_by": "OP1"}).code == "sensitive_dimension"
    assert _code(policy.check_metric, bi, metric, [], {"matched_by": {"in": ["OP1"]}}).code == "sensitive_dimension"
    policy.check_metric(bi, metric, [], {})
    policy.check_metric(claims_for("head_data"), metric, ["matched_by"], {"matched_by": "OP1"})


def test_missing_metrics_only_claim_counts_as_metrics_only(policy):
    claims = {k: v for k, v in claims_for("head_data").items() if k != "metrics_only"}
    assert _code(policy.check_metric, claims, "manual_matches", ["matched_by"], {}).code == "sensitive_dimension"
    assert _code(policy.check_query, claims, "cashrecon").code == "metrics_only"
    for bad in ("false", 0, None, "no"):  # only a real False opens free-form access
        assert _code(policy.check_query, {**claims, "metrics_only": bad}, "cashrecon").code == "metrics_only"


def test_metrics_only_callers_cannot_query_sources(policy):
    for source in ("cashrecon", "refmaster", "nope", ""):
        assert _code(policy.check_query, claims_for("bi_analyst"), source).code == "metrics_only"


def test_query_source_scope(policy):
    policy.check_query(claims_for("cash_ops_emea"), "cashrecon")
    policy.check_query(claims_for("invest_ops_growth"), "refmaster")       # table-level scope
    hidden = _code(policy.check_query, claims_for("cash_ops_emea"), "marketmaster")
    unknown = _code(policy.check_query, claims_for("cash_ops_emea"), "nosuchsrc")
    assert hidden.code == unknown.code == "not_permitted"
    assert str(hidden).replace("marketmaster", "X") == str(unknown).replace("nosuchsrc", "X")
    for bad in (None, 3, ["cashrecon"], "x" * 500):
        e = _code(policy.check_query, claims_for("head_data"), bad)
        assert e.code == "not_permitted" and len(str(e)) < 200


def test_query_source_scope_needs_the_source_or_a_dotted_child_scope(policy):
    """A scope that merely shares a prefix with the source name grants nothing."""
    free = {"sub": "x", "metrics_only": False}
    for scopes in (["cashrecon2"], ["cashreconx.breaks"], ["cash"], ["cashrecon_breaks"]):
        assert _code(policy.check_query, {**free, "scopes": scopes}, "cashrecon").code == "not_permitted", scopes
    policy.check_query({**free, "scopes": ["cashrecon.breaks"]}, "cashrecon")
    policy.check_query({**free, "scopes": ["cashrecon"]}, "cashrecon")


# ------------------------------------------------------------------------------------------------- grain
@pytest.mark.parametrize("dims, filters", [
    (["portfolio_id", "nav_date"], {}),
    (["nav_date", "portfolio_id"], {}),
    (["portfolio_id"], {"nav_date": "2026-09-30"}),
    (["nav_date"], {"portfolio_id": "PF003"}),
    (["nav_date"], {"portfolio_id": {"in": ["PF003"]}}),
    (["portfolio_id"], {"nav_date": {"between": ["2026-09-30", "2026-09-30"]}}),
    ([], {"portfolio_id": "PF003", "nav_date": "2026-09-30"}),
])
def test_grain_too_fine_for_metrics_only(policy, dims, filters):
    e = _code(policy.check_metric, claims_for("bi_analyst"), "nav_break_bps_max", dims, filters)
    assert e.code == "grain_too_fine"
    policy.check_metric(claims_for("head_data"), "nav_break_bps_max", dims, filters)      # full callers may


FINE = {mid: m.fine_grain for mid, m in registry_catalog().metrics.items() if m.fine_grain}


def test_every_entity_by_date_metric_is_grain_protected():
    assert {"recon_unmatched_items", "avg_feed_latency_min", "open_position_exceptions", "position_exceptions",
            "nav_breaches_above_5bps", "nav_break_bps_max", "price_conflicts"} <= set(FINE)


@pytest.mark.parametrize("metric", sorted(FINE))
def test_grain_too_fine_for_metrics_only_on_every_fine_metric(policy, fake_catalog, metric):
    m = fake_catalog.metrics[metric]
    a, b = m.fine_grain[0], m.fine_grain[1]
    bi, head = claims_for("bi_analyst"), claims_for("head_data")
    req = list(m.required)
    attempts = [([a, b], {})]
    if b in m.filters:
        attempts.append(([a], {b: "2026-09-30"}))
    if a in m.filters:
        attempts.append(([b], {a: "X1"}))
    for dims, filters in attempts:
        assert _code(policy.check_metric, bi, metric, [*req, *dims], filters).code == "grain_too_fine", dims
        policy.check_metric(head, metric, [*req, *dims], filters)
    policy.check_metric(bi, metric, [*req, a], {})
    policy.check_metric(bi, metric, [*req, b], {})


def test_coarser_grain_allowed_for_metrics_only(policy):
    bi = claims_for("bi_analyst")
    for dims, filters in ((["portfolio_id"], {}), (["nav_date"], {}), (["nav_date"], {"fund_group": "Growth"}), ([], {})):
        policy.check_metric(bi, "nav_break_bps_max", dims, filters)


def test_catalog_swap_is_seen_by_the_policy():
    cat = registry_catalog()
    p = Policy(cat)
    p.check_metric(claims_for("head_data"), "open_breaks", [], {})
    smaller = type(cat)(version=2, metrics=type(cat.metrics)({k: v for k, v in cat.metrics.items()
                                                              if k != "open_breaks"}), roles=cat.roles)
    p.catalog = smaller
    assert _code(p.check_metric, claims_for("head_data"), "open_breaks", [], {}).code == "not_permitted"


def test_expired_claims_are_not_this_layers_concern_but_scopes_are(policy):
    claims = claims_for("head_data", now=int(time.time()) - 10_000)
    assert policy.check_metric(claims, "open_breaks", [], {}).source == "cashrecon"


@pytest.mark.neo4j
def test_registry_catalog_equals_the_graph_catalog(neo4j_driver, context_graph, fake_catalog):
    from prism.graph.catalog import load_catalog

    real = load_catalog(neo4j_driver, TEST_GRAPH_NS)
    assert dict(real.metrics) == dict(fake_catalog.metrics)
