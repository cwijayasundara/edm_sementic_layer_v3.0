"""MarketMaster EDM mock REST API: vendor prices, golden copy, suspects, DQ metrics, ESG."""
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from psycopg import sql

from prism.config import Settings
from prism.security.access import can
from prism.sources.common import Databases, bearer_claims, check_range, fetch, page, require_table, select, where

DB = "marketmaster"
AUDIENCE = "marketmaster-api"

SUSPECT_COLS = ("suspect_id", "security_id", "vendor_id", "price_date", "kind", "deviation_pct", "status", "asset_class")
INSTRUMENT_COLS = ("security_id", "isin", "name", "asset_class", "ccy")


def _claims(request: Request) -> dict:
    return bearer_claims(request, AUDIENCE)


Claims = Annotated[dict, Depends(_claims)]
Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]
FromDate = Annotated[date | None, Query(alias="from")]
ToDate = Annotated[date | None, Query(alias="to")]
ConflictGroup = Literal["vendor_id", "asset_class", "price_date", "status"]

router = APIRouter()


@router.get("/vendors")
async def list_vendors(request: Request, claims: Claims):
    require_table(claims, DB, "vendors")
    rows = await fetch(request, DB, claims,
                       select("vendors", ("vendor_id", "name", "rank_default"), sql.SQL(""), ("rank_default", "vendor_id"), 50, 0), [])
    return page(rows, 50, 0)


@router.get("/instruments")
async def list_instruments(request: Request, claims: Claims, asset_class: str | None = None, q: str | None = None,
                           limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "instruments")
    cond, params = where([("asset_class", "eq", asset_class), ("name", "ilike", q)])
    rows = await fetch(request, DB, claims, select("instruments", INSTRUMENT_COLS, cond, ("security_id",), limit, offset), params)
    return page(rows, limit, offset)


@router.get("/instruments/{security_id}/timeseries")
async def timeseries(request: Request, claims: Claims, security_id: str,
                     date_from: FromDate = None, date_to: ToDate = None):
    require_table(claims, DB, "golden_prices")
    check_range(date_from, date_to)
    cond, params = where([("security_id", "eq", security_id)])
    if not await fetch(request, DB, claims, select("instruments", ("security_id",), cond, ("security_id",), 1, 0), params):
        raise HTTPException(404, "instrument not found")
    cond, params = where([("security_id", "eq", security_id), ("price_date", "gte", date_from),
                          ("price_date", "lte", date_to)])
    points = await fetch(request, DB, claims,
                         select("golden_prices", ("price_date", "value", "chosen_vendor_id", "rule"), cond,
                                ("price_date",), 500, 0), params)
    return {"security_id": security_id, "points": points}


def _suspect_filters(kind, vendor_id, asset_class, status, date_from, date_to):
    return [("kind", "eq", kind), ("vendor_id", "eq", vendor_id), ("asset_class", "eq", asset_class),
            ("status", "eq", status), ("price_date", "gte", date_from), ("price_date", "lte", date_to)]


@router.get("/prices/conflicts")
async def list_conflicts(request: Request, claims: Claims, vendor_id: str | None = None,
                         asset_class: str | None = None, status: str | None = None,
                         date_from: FromDate = None, date_to: ToDate = None, limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "price_suspects")
    check_range(date_from, date_to)
    cond, params = where(_suspect_filters("conflict", vendor_id, asset_class, status, date_from, date_to))
    rows = await fetch(request, DB, claims, select("price_suspects", SUSPECT_COLS, cond, ("price_date", "suspect_id"), limit, offset), params)
    return page(rows, limit, offset)


@router.get("/prices/conflicts/summary")
async def conflicts_summary(request: Request, claims: Claims,
                            group_by: Annotated[list[ConflictGroup], Query()] = ["vendor_id", "asset_class"],
                            date_from: FromDate = None, date_to: ToDate = None):
    require_table(claims, DB, "price_suspects")
    check_range(date_from, date_to)
    groups = list(dict.fromkeys(group_by))
    cond, params = where(_suspect_filters("conflict", None, None, None, date_from, date_to))
    cols = sql.SQL(", ").join(map(sql.Identifier, groups))
    query = sql.SQL(
        "SELECT {g}, count(*) AS conflicts FROM price_suspects{w} GROUP BY {g} ORDER BY conflicts DESC, {g}"
    ).format(g=cols, w=cond)
    return {"group_by": groups, "rows": await fetch(request, DB, claims, query, params)}


SuspectGroup = Literal["security_id", "kind", "vendor_id", "price_date", "status", "asset_class"]


@router.get("/prices/suspects/summary")
async def suspects_summary(request: Request, claims: Claims,
                           group_by: Annotated[list[SuspectGroup], Query()] = ["kind"],
                           kind: str | None = None, vendor_id: str | None = None, status: str | None = None,
                           security_id: str | None = None, price_date: date | None = None,
                           date_from: FromDate = None, date_to: ToDate = None):
    """Price suspects of every kind (stale, spike, missing, conflict), counted per group."""
    require_table(claims, DB, "price_suspects")
    check_range(date_from, date_to)
    groups = list(dict.fromkeys(group_by))
    cond, params = where(_suspect_filters(kind, vendor_id, None, status, date_from, date_to)
                         + [("security_id", "eq", security_id), ("price_date", "eq", price_date)])
    cols = sql.SQL(", ").join(map(sql.Identifier, groups))
    query = sql.SQL("SELECT {g}, count(*) AS suspects FROM price_suspects{w} GROUP BY {g} ORDER BY suspects DESC, {g}"
                    ).format(g=cols, w=cond)
    return {"group_by": groups, "rows": await fetch(request, DB, claims, query, params)}


@router.get("/prices/suspects")
async def list_suspects(request: Request, claims: Claims, kind: str | None = None, vendor_id: str | None = None,
                        asset_class: str | None = None, status: str | None = None,
                        date_from: FromDate = None, date_to: ToDate = None, limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "price_suspects")
    check_range(date_from, date_to)
    cond, params = where(_suspect_filters(kind, vendor_id, asset_class, status, date_from, date_to))
    rows = await fetch(request, DB, claims, select("price_suspects", SUSPECT_COLS, cond, ("price_date", "suspect_id"), limit, offset), params)
    return page(rows, limit, offset)


@router.get("/golden-copy/{security_id}")
async def golden_copy(request: Request, claims: Claims, security_id: str,
                      on: Annotated[date | None, Query(alias="date")] = None):
    require_table(claims, DB, "golden_prices")
    cond, params = where([("security_id", "eq", security_id), ("price_date", "eq", on)])
    query = sql.SQL("SELECT security_id, price_date, value, chosen_vendor_id, rule, asset_class "
                    "FROM golden_prices{w} ORDER BY price_date DESC LIMIT 1").format(w=cond)
    golden = await fetch(request, DB, claims, query, params)
    if not golden:
        raise HTTPException(404, "no golden price for this instrument and date")
    quotes: list[dict] = []
    if can(claims, DB, "vendor_prices"):
        cond, params = where([("security_id", "eq", security_id), ("price_date", "eq", golden[0]["price_date"])])
        quotes = await fetch(request, DB, claims,
                             select("vendor_prices", ("vendor_id", "value", "received_at"), cond, ("vendor_id",), 50, 0),
                             params)
    return {"golden": golden[0], "quotes": quotes}


@router.get("/dq/metrics")
async def dq_metrics(request: Request, claims: Claims, domain: str | None = None, stage: str | None = None,
                     date_from: FromDate = None, date_to: ToDate = None, limit: Limit = 500, offset: Offset = 0):
    require_table(claims, DB, "dq_stage_metrics")
    check_range(date_from, date_to)
    cond, params = where([("domain", "eq", domain), ("stage", "eq", stage), ("business_date", "gte", date_from),
                          ("business_date", "lte", date_to)])
    rows = await fetch(request, DB, claims,
                       select("dq_stage_metrics", ("business_date", "domain", "stage", "count", "sla_met"), cond,
                              ("business_date", "domain", "stage"), limit, offset), params)
    return page(rows, limit, offset)


@router.get("/esg/{entity_id}")
async def esg(request: Request, claims: Claims, entity_id: str):
    require_table(claims, DB, "esg_scores")
    cond, params = where([("entity_id", "eq", entity_id)])
    rows = await fetch(request, DB, claims,
                       select("esg_scores", ("entity_id", "provider", "as_of", "score"), cond, ("as_of", "provider"), 50, 0), params)
    if not rows:
        raise HTTPException(404, "no ESG scores for this entity")
    return page(rows, 50, 0)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.dbs.close()

    app = FastAPI(title="MarketMaster EDM API", version="1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.dbs = Databases(settings)
    app.include_router(router, prefix="/api/v1")
    return app
