import pytest
from httpx import ASGITransport, AsyncClient

from prism.sim.calendar import business_days
from prism.sim.universe import SimConfig, build_universe
from prism.sources.marketmaster_api.app import AUDIENCE, create_app

pytestmark = pytest.mark.db


@pytest.fixture
async def client(seeded):
    app = create_app(seeded)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.dbs.close()


@pytest.fixture(scope="module")
def universe():
    return build_universe(SimConfig.small())


async def test_conflict_heatmap_is_led_by_vendor_a_corp_bonds(client, headers_for, seeded):
    week = business_days(seeded.as_of, 5)
    r = await client.get("/api/v1/prices/conflicts/summary",
                         params=[("from", str(week[0])), ("to", str(week[-1])),
                                 ("group_by", "vendor_id"), ("group_by", "asset_class")],
                         headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 200
    top = r.json()["rows"][0]
    assert (top["vendor_id"], top["asset_class"]) == ("V_A", "Corp bond")


async def test_golden_copy_shows_stale_carry_forward(client, headers_for, universe):
    sid = universe.stories.stale_security_ids[0]
    r = await client.get(f"/api/v1/golden-copy/{sid}", headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 200
    body = r.json()
    assert body["golden"]["rule"] == "carry_forward"
    golden = body["golden"]["value"]
    assert body["quotes"] and all(abs(q["value"] - golden) / golden > 0.03 for q in body["quotes"])


async def test_timeseries_is_complete_and_ordered(client, headers_for, universe):
    sid = universe.securities[0].security_id
    r = await client.get(f"/api/v1/instruments/{sid}/timeseries", headers=headers_for("steward", AUDIENCE))
    dates = [p["price_date"] for p in r.json()["points"]]
    assert r.status_code == 200 and len(dates) == len(universe.days) and dates == sorted(dates)


async def test_unknown_instrument_is_404(client, headers_for):
    h = headers_for("steward", AUDIENCE)
    assert (await client.get("/api/v1/instruments/SEC999999/timeseries", headers=h)).status_code == 404
    assert (await client.get("/api/v1/golden-copy/SEC999999", headers=h)).status_code == 404


async def test_persona_without_dataset_gets_403(client, headers_for):
    r = await client.get("/api/v1/prices/suspects", headers=headers_for("invest_ops_growth", AUDIENCE))
    assert r.status_code == 403


async def test_inverted_range_is_422(client, headers_for):
    r = await client.get("/api/v1/dq/metrics", params={"from": "2026-09-30", "to": "2026-09-01"},
                         headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 422


async def test_401_missing_and_wrong_audience(client, headers_for):
    r = await client.get("/api/v1/instruments")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    r = await client.get("/api/v1/instruments", headers=headers_for("steward", "refmaster-api"))
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"


async def test_row_scoping_through_the_api(client, headers_for, seeded):
    from prism.security.personas import claims_for
    from prism.security.tokens import mint
    claims = claims_for("steward")
    claims["rows"] = {"asset_class": ["Equity"]}
    rh = {"Authorization": f"Bearer {mint(claims, AUDIENCE, seeded.jwt_secret.get_secret_value(), ttl_s=300)}"}
    uh = headers_for("steward", AUDIENCE)
    for path, key in (("/api/v1/prices/suspects", "asset_class"), ("/api/v1/instruments", "asset_class")):
        r = await client.get(path, params={"limit": 500}, headers=rh)
        items = r.json()["items"]
        assert r.status_code == 200 and items and {i[key] for i in items} == {"Equity"}, path
        full = await client.get(path, params={"limit": 500}, headers=uh)
        assert full.json()["count"] > len(items), path


@pytest.mark.parametrize("path", ["/api/v1/vendors", "/api/v1/prices/suspects", "/api/v1/dq/metrics"])
async def test_smoke_endpoints(client, headers_for, path):
    r = await client.get(path, headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 200 and r.json()["count"] > 0


async def test_esg_scores_and_sovereign_404(client, headers_for, universe):
    h = headers_for("steward", AUDIENCE)
    corp = next(e for e in universe.entities if e.sector != "Sovereign")
    r = await client.get(f"/api/v1/esg/{corp.entity_id}", headers=h)
    assert r.status_code == 200 and r.json()["count"] > 0
    sov = next(e for e in universe.entities if e.sector == "Sovereign")
    assert (await client.get(f"/api/v1/esg/{sov.entity_id}", headers=h)).status_code == 404


async def test_limit_501_is_422(client, headers_for):
    r = await client.get("/api/v1/instruments", params={"limit": 501}, headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 422


async def test_suspects_pagination_is_deterministic(client, headers_for):
    h = headers_for("steward", AUDIENCE)
    ids = {}
    for name, params in {"a": {"limit": 20, "offset": 0}, "b": {"limit": 20, "offset": 20}, "all": {"limit": 40}}.items():
        r = await client.get("/api/v1/prices/suspects", params=params, headers=h)
        assert r.status_code == 200
        ids[name] = [i["suspect_id"] for i in r.json()["items"]]
    assert len(ids["all"]) == 40 and not set(ids["a"]) & set(ids["b"])
    assert ids["a"] + ids["b"] == ids["all"]


async def test_suspects_summary_groups_and_filters_by_security(client, headers_for):
    from prism.sim.universe import SimConfig, build_universe
    inc = build_universe(SimConfig.small()).stories.incident
    h = headers_for("steward", AUDIENCE)
    r = await client.get("/api/v1/prices/suspects/summary",
                         params=[("group_by", "security_id"), ("group_by", "kind"), ("group_by", "status"),
                                 ("security_id", inc.security_id)], headers=h)
    rows = r.json()["rows"]
    assert {"security_id": inc.security_id, "kind": "spike", "status": "accepted", "suspects": 1} in rows
    assert {row["security_id"] for row in rows} == {inc.security_id}
    assert (await client.get("/api/v1/prices/suspects/summary", params={"group_by": "deviation_pct"},
                             headers=h)).status_code == 422
