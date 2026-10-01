import pytest
from pydantic import SecretStr
from httpx import ASGITransport, AsyncClient

from prism.sources.refmaster_api.app import AUDIENCE, create_app

pytestmark = pytest.mark.db


@pytest.fixture
async def client(seeded):
    app = create_app(seeded)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await app.state.dbs.close()


async def test_steward_lists_corporate_bonds(client, headers_for):
    r = await client.get("/api/v1/securities", params={"asset_class": "Corp bond", "limit": 50},
                         headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 200
    body = r.json()
    assert 0 < body["count"] <= 50
    assert {item["asset_class"] for item in body["items"]} == {"Corp bond"}


async def test_persona_without_dataset_gets_403(client, headers_for):
    r = await client.get("/api/v1/securities", headers=headers_for("cash_ops_emea", AUDIENCE))
    assert r.status_code == 403


async def test_table_level_scope_is_honoured(client, headers_for):
    h = headers_for("invest_ops_growth", AUDIENCE)
    assert (await client.get("/api/v1/securities", headers=h)).status_code == 200
    assert (await client.get("/api/v1/exceptions", headers=h)).status_code == 403


async def test_missing_expired_and_wrong_audience_tokens_are_401(client, headers_for):
    assert (await client.get("/api/v1/securities")).status_code == 401
    expired = headers_for("steward", AUDIENCE, ttl_s=-10)
    assert (await client.get("/api/v1/securities", headers=expired)).status_code == 401
    wrong = headers_for("steward", "marketmaster-api")
    assert (await client.get("/api/v1/securities", headers=wrong)).status_code == 401


async def test_unknown_security_is_404(client, headers_for):
    r = await client.get("/api/v1/securities/SEC999999", headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 404


async def test_search_text_with_sql_metacharacters_is_literal(client, headers_for):
    h = headers_for("steward", AUDIENCE)
    r = await client.get("/api/v1/securities", params={"q": "O'Br%_"}, headers=h)
    assert r.status_code == 200 and r.json()["count"] == 0
    total = (await client.get("/api/v1/securities", params={"limit": 500}, headers=h)).json()["count"]
    r = await client.get("/api/v1/securities", params={"q": "%", "limit": 500}, headers=h)
    assert r.status_code == 200
    body = r.json()
    # '%' matched literally (coupon names like "4.500%"), not as a wildcard that would return every row
    assert 0 < body["count"] < total
    assert all("%" in item["name"] for item in body["items"])
    for probe in ("_", "\\"):
        r = await client.get("/api/v1/securities", params={"q": probe, "limit": 500}, headers=h)
        assert r.status_code == 200 and r.json()["count"] == 0


async def test_inverted_date_range_is_422(client, headers_for):
    r = await client.get("/api/v1/corporate-actions", params={"from": "2026-09-30", "to": "2026-01-01"},
                         headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 422


async def test_exceptions_summary_groups(client, headers_for):
    r = await client.get("/api/v1/exceptions/summary", params=[("group_by", "domain"), ("group_by", "status")],
                         headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert rows and {row["domain"] for row in rows} <= {"security", "price", "entity", "corporate_action"}
    assert all(row["open_count"] <= row["total"] for row in rows)
    bad = await client.get("/api/v1/exceptions/summary", params={"group_by": "assignee"},
                           headers=headers_for("steward", AUDIENCE))
    assert bad.status_code == 422


async def _restricted_headers(seeded):
    from prism.security.personas import claims_for
    from prism.security.tokens import mint
    claims = claims_for("steward")
    claims["rows"] = {"asset_class": ["Equity"]}
    return {"Authorization": f"Bearer {mint(claims, AUDIENCE, seeded.jwt_secret.get_secret_value(), ttl_s=300)}"}


async def test_row_scoping_through_the_api(client, headers_for, seeded):
    rh = await _restricted_headers(seeded)
    uh = headers_for("steward", AUDIENCE)
    r = await client.get("/api/v1/securities", params={"limit": 500}, headers=rh)
    items = r.json()["items"]
    assert r.status_code == 200 and items and {i["asset_class"] for i in items} == {"Equity"}
    full = await client.get("/api/v1/securities", params={"limit": 500}, headers=uh)
    assert full.json()["count"] > len(items)
    p = {"group_by": "asset_class"}
    rs = (await client.get("/api/v1/exceptions/summary", params=p, headers=rh)).json()["rows"]
    us = (await client.get("/api/v1/exceptions/summary", params=p, headers=uh)).json()["rows"]
    assert {row["asset_class"] for row in rs} <= {"Equity"}
    assert sum(r_["total"] for r_ in rs) < sum(u["total"] for u in us)


async def test_db_policy_denial_maps_to_403(seeded, headers_for):
    app = create_app(seeded.model_copy(update={"ctx_hmac_key": SecretStr("x" * 40)}))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            r = await c.get("/api/v1/securities", headers=headers_for("steward", AUDIENCE))
            assert r.status_code == 403
    finally:
        await app.state.dbs.close()


@pytest.mark.parametrize("path", ["/api/v1/entities", "/api/v1/accounts", "/api/v1/data-dictionary"])
async def test_other_endpoints_smoke(client, headers_for, path):
    r = await client.get(path, headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 200 and r.json()["count"] > 0


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
async def test_invalid_paging_is_422(client, headers_for, params):
    r = await client.get("/api/v1/securities", params=params, headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 422


@pytest.mark.parametrize("path", ["/api/v1/exceptions", "/api/v1/exceptions/summary"])
async def test_inverted_range_is_422_on_exceptions(client, headers_for, path):
    r = await client.get(path, params={"from": "2026-09-30", "to": "2026-01-01"},
                         headers=headers_for("steward", AUDIENCE))
    assert r.status_code == 422


async def test_401_challenge_and_case_insensitive_scheme(client, headers_for):
    r = await client.get("/api/v1/securities")
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    token = headers_for("steward", AUDIENCE)["Authorization"].removeprefix("Bearer ")
    ok = await client.get("/api/v1/securities", headers={"Authorization": f"bearer {token}"})
    assert ok.status_code == 200
    bad = await client.get("/api/v1/securities", headers={"Authorization": "Bearer "})
    assert bad.status_code == 401


@pytest.mark.parametrize("path,key", [("/api/v1/exceptions", "exc_id"), ("/api/v1/corporate-actions", "ca_id")])
async def test_pagination_is_stable(client, headers_for, path, key):
    h = headers_for("steward", AUDIENCE)
    ids = {}
    for name, params in {"a": {"limit": 50, "offset": 0}, "b": {"limit": 50, "offset": 50},
                         "all": {"limit": 100}}.items():
        r = await client.get(path, params=params, headers=h)
        assert r.status_code == 200
        ids[name] = [i[key] for i in r.json()["items"]]
    assert not set(ids["a"]) & set(ids["b"])
    assert ids["a"] + ids["b"] == ids["all"]
