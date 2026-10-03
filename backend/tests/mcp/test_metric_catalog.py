from contextlib import contextmanager
from datetime import date

import psycopg
import pytest

from prism.db.session import ctx_from_claims, prepare_statements
from prism.mcp.metrics import DEFAULT_METRICS_DIR, ALLOWED_TABLES, compile_metric, load_metrics, run_metric
from prism.mcp.rest_backend import REST_DIR, load_rest_config
from prism.security.personas import claims_for

pytestmark = pytest.mark.db
METRICS = load_metrics(DEFAULT_METRICS_DIR)
AS_OF = date(2026, 9, 30)
EXPECTED = {
    "open_breaks", "aged_open_breaks", "open_break_amount", "avg_break_age", "break_resolution_rate",
    "manual_matches", "auto_match_rate", "position_exceptions", "open_position_exceptions", "exception_mv_abs",
    "recon_unmatched_items", "clean_recon_run_rate", "nav_break_bps_max", "nav_breaches_above_5bps",
    "late_feeds", "missing_or_failed_deliveries", "feed_on_time_rate", "avg_feed_latency_min",
    "open_support_tickets", "funds",
}


def with_required(m, dims):
    """The requested dimensions plus any the metric requires (e.g. ccy for amounts)."""
    return [*dims, *[d for d in m.required_dimensions if d not in dims]]


@contextmanager
def scoped(settings, persona, db):
    ctx = ctx_from_claims(claims_for(persona), settings.ctx_hmac_key.get_secret_value())
    with psycopg.connect(settings.dsn(db)) as conn:
        with conn.transaction():
            for statement, args in prepare_statements(ctx, 5000):
                conn.execute(statement, args)
            yield conn


def test_catalog_is_complete_and_well_formed():
    assert set(METRICS) == EXPECTED
    for m in METRICS.values():
        assert m.source in ALLOWED_TABLES and m.description and m.unit
        assert m.dimensions, f"{m.id} has no dimensions"


@pytest.mark.parametrize("metric_id", sorted(EXPECTED))
def test_every_metric_compiles_with_every_dimension(metric_id):
    m = METRICS[metric_id]
    compile_metric(m, dimensions=with_required(m, []), as_of=AS_OF)
    for d in m.dimensions:
        compile_metric(m, dimensions=with_required(m, [d]), as_of=AS_OF)
    compile_metric(m, dimensions=with_required(m, list(m.dimensions)[:2]), as_of=AS_OF)


@pytest.mark.parametrize("metric_id", sorted(EXPECTED))
def test_every_metric_runs_and_returns_data_as_head_data(seeded, metric_id):
    m = METRICS[metric_id]
    with scoped(seeded, "head_data", m.source) as conn:
        rows = run_metric(conn, m, db_prefix=seeded.db_prefix, as_of=AS_OF, dimensions=with_required(m, [list(m.dimensions)[0]]),
                          limit=1000)
    assert rows, f"{metric_id} returned no rows on the seeded data"
    assert all("value" in r for r in rows)
    assert any(r["value"] not in (None, 0) for r in rows), f"{metric_id} is all zero/null"


@pytest.mark.parametrize("metric_id", sorted(EXPECTED))
def test_every_metric_returns_nothing_without_the_dataset(seeded, metric_id):
    m = METRICS[metric_id]
    with scoped(seeded, "steward", m.source) as conn:  # steward has refmaster+marketmaster only
        rows = run_metric(conn, m, db_prefix=seeded.db_prefix, as_of=AS_OF,
                          dimensions=with_required(m, [list(m.dimensions)[0]]))
    assert rows == []


def test_open_break_amount_requires_ccy():
    m = METRICS["open_break_amount"]
    assert m.required_dimensions == ["ccy"] and "currenc" in m.description
    assert [x for x in METRICS.values() if x.required_dimensions] == [m]


def test_metric_ids_are_unique_across_every_source():
    ids = [m.id for m in load_metrics(DEFAULT_METRICS_DIR).values()]
    rest_files = sorted(REST_DIR.glob("*.yaml"))
    assert {p.stem for p in rest_files} == {"refmaster", "marketmaster"}
    for p in rest_files:
        ids += list(load_rest_config(p.stem)[1])
    assert len(ids) == len(set(ids)), sorted(i for i in set(ids) if ids.count(i) > 1)
    assert len(ids) == len(EXPECTED) + 5


def test_overlapping_metrics_say_how_they_relate():
    d = {i: m.description for i, m in METRICS.items()}
    assert "superset of missing_or_failed_deliveries" in d["late_feeds"]
    assert "subset of late_feeds" in d["missing_or_failed_deliveries"]
    assert "LATE deliveries only" in d["avg_feed_latency_min"]
    assert "complement of late_feeds" in d["feed_on_time_rate"]
    for mid in ("open_breaks", "aged_open_breaks", "open_break_amount", "avg_break_age"):
        assert "open = status <> 'closed'" in d[mid], mid
    for mid in ("open_position_exceptions", "open_support_tickets"):
        assert "open = status = 'open'" in d[mid], mid
    assert "open_breaks with age_days > 5" in d["aged_open_breaks"]
    for mid in ("position_exceptions", "open_position_exceptions", "exception_mv_abs"):
        assert "between internal and custodian positions" in d[mid], mid
    assert "all statuses" in d["exception_mv_abs"]
    rest = load_rest_config("refmaster")[1]
    for mid in ("open_dq_exceptions", "dq_exceptions_total"):
        assert "data-quality rule exceptions (reference-data validation)" in rest[mid].description, mid
    assert "not closed" in rest["open_dq_exceptions"].description


def test_incident_hops_through_sql_metrics(seeded):
    from prism.sim.keys import fund_entity_id
    from prism.sim.universe import SimConfig, build_universe
    u = build_universe(SimConfig.small())
    inc = u.stories.incident
    fe = fund_entity_id(u.cfg.n_entities, next(k for k, p in enumerate(u.portfolios)
                                               if p.portfolio_id == inc.anchor_portfolio_id))

    def run(db, metric_id, dims, filters):
        with scoped(seeded, "head_data", db) as conn:
            return run_metric(conn, METRICS[metric_id], db_prefix=seeded.db_prefix, as_of=AS_OF, dimensions=dims,
                              filters=filters)

    assert run("assetrecon", "funds", ["fund_entity_id", "custodian_source_id"],
               {"portfolio_id": inc.anchor_portfolio_id}) == \
        [{"fund_entity_id": fe, "custodian_source_id": inc.source_id, "value": 1}]
    assert run("assetrecon", "position_exceptions", ["security_id"],
               {"portfolio_id": inc.anchor_portfolio_id, "cause_code": "corporate_action"}) == \
        [{"security_id": inc.security_id, "value": 2}]
    assert run("feedhub", "late_feeds", ["source_id"], {"feed_type": "corporate_actions"}) == \
        [{"source_id": inc.source_id, "value": 2}]
    breaks = run("cashrecon", "open_breaks", ["legal_entity_id", "bank_source_id"], {"break_type": "cash_in_lieu"})
    assert [(r["legal_entity_id"], r["value"]) for r in breaks] == [(fe, 1)]
