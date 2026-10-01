"""RefMaster EDM mock REST API (domain-model style), secured by audience-bound JWT + Postgres RLS."""
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from psycopg import sql

from prism.config import Settings
from prism.sources.common import Databases, bearer_claims, check_range, fetch, page, require_table, select, where

DB = "refmaster"
AUDIENCE = "refmaster-api"

SECURITY_COLS = ("security_id", "isin", "cusip", "sedol", "ticker", "name", "asset_class", "sub_class", "ccy",
                 "issuer_entity_id", "country", "status", "valid_from", "valid_to")
ENTITY_COLS = ("entity_id", "lei", "name", "country", "region", "sector", "parent_entity_id", "status")
ACCOUNT_COLS = ("account_id", "product_id", "name", "account_type", "owner_entity_id", "region", "lifecycle_state")
CA_COLS = ("ca_id", "security_id", "event_type", "ex_date", "pay_date", "ratio", "status")
EXC_COLS = ("exc_id", "rule_id", "domain", "record_ref", "asset_class", "status", "assignee", "opened_at", "closed_at")
DD_COLS = ("domain", "attribute", "definition", "owner", "source", "lineage")


def _claims(request: Request) -> dict:
    return bearer_claims(request, AUDIENCE)


Claims = Annotated[dict, Depends(_claims)]
Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]
FromDate = Annotated[date | None, Query(alias="from")]
ToDate = Annotated[date | None, Query(alias="to")]
ExceptionGroup = Literal["domain", "asset_class", "status", "rule_id"]

router = APIRouter()


@router.get("/securities")
async def list_securities(request: Request, claims: Claims, asset_class: str | None = None, ccy: str | None = None,
                          country: str | None = None, status: str | None = None, q: str | None = None,
                          limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "securities")
    cond, params = where([("asset_class", "eq", asset_class), ("ccy", "eq", ccy), ("country", "eq", country),
                          ("status", "eq", status), ("name", "ilike", q)])
    rows = await fetch(request, DB, claims, select("securities", SECURITY_COLS, cond, ("security_id",), limit, offset), params)
    return page(rows, limit, offset)


@router.get("/securities/{security_id}")
async def get_security(request: Request, claims: Claims, security_id: str):
    require_table(claims, DB, "securities")
    cond, params = where([("security_id", "eq", security_id)])
    rows = await fetch(request, DB, claims, select("securities", SECURITY_COLS, cond, ("security_id",), 1, 0), params)
    if not rows:
        raise HTTPException(404, "security not found")
    return rows[0]


@router.get("/entities")
async def list_entities(request: Request, claims: Claims, country: str | None = None, sector: str | None = None,
                        q: str | None = None, limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "legal_entities")
    cond, params = where([("country", "eq", country), ("sector", "eq", sector), ("name", "ilike", q)])
    rows = await fetch(request, DB, claims, select("legal_entities", ENTITY_COLS, cond, ("entity_id",), limit, offset), params)
    return page(rows, limit, offset)


@router.get("/accounts")
async def list_accounts(request: Request, claims: Claims, region: str | None = None,
                        lifecycle_state: str | None = None, product_id: str | None = None,
                        limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "accounts")
    cond, params = where([("region", "eq", region), ("lifecycle_state", "eq", lifecycle_state),
                          ("product_id", "eq", product_id)])
    rows = await fetch(request, DB, claims, select("accounts", ACCOUNT_COLS, cond, ("account_id",), limit, offset), params)
    return page(rows, limit, offset)


@router.get("/corporate-actions")
async def list_corporate_actions(request: Request, claims: Claims, security_id: str | None = None,
                                 event_type: str | None = None, status: str | None = None,
                                 date_from: FromDate = None, date_to: ToDate = None,
                                 limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "corporate_actions")
    check_range(date_from, date_to)
    cond, params = where([("security_id", "eq", security_id), ("event_type", "eq", event_type),
                          ("status", "eq", status), ("ex_date", "gte", date_from), ("ex_date", "lte", date_to)])
    rows = await fetch(request, DB, claims, select("corporate_actions", CA_COLS, cond, ("ex_date", "ca_id"), limit, offset), params)
    return page(rows, limit, offset)


def _exception_filters(domain, asset_class, status, date_from, date_to):
    return [("domain", "eq", domain), ("asset_class", "eq", asset_class), ("status", "eq", status),
            ("opened_at", "gte", date_from),
            ("opened_at", "lt", date_to + timedelta(days=1) if date_to else None)]


@router.get("/exceptions")
async def list_exceptions(request: Request, claims: Claims, domain: str | None = None,
                          asset_class: str | None = None, status: str | None = None,
                          date_from: FromDate = None, date_to: ToDate = None,
                          limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "exceptions")
    check_range(date_from, date_to)
    cond, params = where(_exception_filters(domain, asset_class, status, date_from, date_to))
    rows = await fetch(request, DB, claims, select("exceptions", EXC_COLS, cond, ("opened_at", "exc_id"), limit, offset), params)
    return page(rows, limit, offset)


@router.get("/exceptions/summary")
async def exceptions_summary(request: Request, claims: Claims,
                             group_by: Annotated[list[ExceptionGroup], Query()] = ["domain"],
                             domain: str | None = None, asset_class: str | None = None,
                             date_from: FromDate = None, date_to: ToDate = None):
    require_table(claims, DB, "exceptions")
    check_range(date_from, date_to)
    groups = list(dict.fromkeys(group_by))
    cond, params = where(_exception_filters(domain, asset_class, None, date_from, date_to))
    cols = sql.SQL(", ").join(map(sql.Identifier, groups))
    query = sql.SQL(
        "SELECT {g}, count(*) FILTER (WHERE status <> 'closed') AS open_count, count(*) AS total "
        "FROM exceptions{w} GROUP BY {g} ORDER BY total DESC, {g}"
    ).format(g=cols, w=cond)
    return {"group_by": groups, "rows": await fetch(request, DB, claims, query, params)}


@router.get("/data-dictionary")
async def data_dictionary(request: Request, claims: Claims, domain: str | None = None):
    require_table(claims, DB, "data_dictionary")
    cond, params = where([("domain", "eq", domain)])
    rows = await fetch(request, DB, claims, select("data_dictionary", DD_COLS, cond, ("domain", "attribute"), 500, 0), params)
    return page(rows, 500, 0)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.dbs.close()

    app = FastAPI(title="RefMaster EDM API", version="1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.dbs = Databases(settings)
    app.include_router(router, prefix="/api/v1")
    return app
