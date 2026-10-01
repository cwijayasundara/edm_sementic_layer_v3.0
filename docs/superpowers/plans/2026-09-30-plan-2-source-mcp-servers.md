# Plan 2 — Source MCP Servers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put one MCP server in front of each of the five simulated platforms, all with the **same three-tool contract** (`describe`, `run_metric`, `query`), authenticated with audience-bound JWTs, enforcing the caller's row-level entitlements at the source, with ~22 governed metrics and a guarded free-form query path.

**Architecture:** A generic `build_source_app(backend, settings)` builds an `MCPServer` (streamable HTTP, stateless, JSON responses, SDK token verifier) whose three tools delegate to a `SourceBackend`. `SqlBackend` serves CashRecon / AssetRecon / FeedHub (Postgres, `bi_reader` + signed context, governed-metric compiler, AST SQL guard). `RestBackend` serves RefMaster / MarketMaster by calling the mock REST APIs with a freshly minted, audience-bound token (on-behalf-of; the incoming token is never passed through). Servers listen on 8201–8205.

**Tech Stack:** Python 3.13/3.12, `mcp>=2.2,<3` (v2 API: `MCPServer`, client on `httpx2`), `sqlglot>=30,<31`, `pyyaml`, `psycopg[binary]`+`psycopg-pool`, `httpx`, FastAPI apps from Plan 1, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-30-agentic-data-intelligence-design.md` (§4.3 uniform tool contract, §4.4 the graph will consume `describe()`, §5 security, §6 token efficiency). Carry-forward items: `docs/superpowers/plans/plan-1-carry-forward.md`. **Proven prototypes** (read the ones a task names): `docs/superpowers/spikes/plan-2/` (`README.md` explains them; versions pinned there).

## Global Constraints

- **No client, vendor or real-product names** anywhere (code, YAML, tests, docs, commit messages). Generic names only: RefMaster, MarketMaster, CashRecon, AssetRecon, FeedHub.
- Python ≥ 3.12 (venv is 3.13), managed with **uv**; dependencies pinned as `mcp>=2.2,<3`, `sqlglot>=30,<31`, `pyyaml>=6`, `httpx>=0.28` (moved from the dev group to runtime).
- **`mcp` v2 API only:** `from mcp.server.mcpserver import MCPServer`; there is no `FastMCP`. The MCP client uses `httpx2`.
- Postgres host port **5434**, bound to 127.0.0.1. Ports: APIs 8101/8102 (existing), **MCP servers 8201 refmaster, 8202 marketmaster, 8203 cashrecon, 8204 assetrecon, 8205 feedhub**, reserved for later plans: 8000, 8200, 3000.
- Console scripts cannot import `prism` on this Mac (macOS hidden-flag on the venv `.pth`). Always use the module form from `backend/`: `uv run python -m prism.<module>`. pytest uses `pythonpath = ["."]`. **Do not** create `tests/__init__.py` or `sys.path` hacks; tests live under `backend/tests/mcp/` (no `__init__.py`).
- **Audiences:** MCP servers accept only tokens with `aud == "<source>-mcp"` (e.g. `cashrecon-mcp`); REST backends call their API with `aud == "<source>-api"`. HS256, secret = `Settings.jwt_secret`. Identity/entitlements come only from verified tokens; **no tool accepts identity, roles or scopes as an argument**.
- **Security invariants (spec §5):** SQL always runs as `SET LOCAL ROLE bi_reader` inside a `READ ONLY` transaction with the signed context and `statement_timeout`; the base table `private.cash_accounts` is never reachable (masking view `public.cash_accounts` only); every user-provided value is a bound parameter; free-form SQL passes `validate_select` and is executed with `prepare=True` (extended protocol); metrics-only principals (`claims["metrics_only"]`) cannot call `query`.
- Tool results: structured content is the payload; the text content is a **short summary** (never a copy of the rows).
- Commit trailer, exactly: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Use `git -c user.name=dev -c user.email=cwijayasundara@gmail.com commit`. Never `git add` `.env`, `.superpowers/` or `backend/.venv`.
- Work on branch `plan-2-source-mcp` created from `main`.

## Review Focus

Failure modes the spec implies but the task tests do not exercise directly; each has a named test in the owning task:

1. Wrong-audience, expired or unsigned token on any MCP server → **401** (never a 500, never data). (Task 3 `test_wrong_audience_and_expired_are_401`, Task 9 `test_token_for_another_source_is_401`)
2. `query` with stacked statements, a backslash, or `set_config(...)` → a clear tool error, and nothing executes. (Task 4 corpus; Task 7 `test_query_rejections`)
3. A filter value such as `"x'; DROP TABLE breaks; --"` → inert (bound parameter; zero rows), not an error that leaks SQL. (Task 5 `test_injection_in_values_is_inert`)
4. A persona without the dataset calling `run_metric`/`query` on a REST source → an explicit tool error (the API's 403), not an empty success that looks like "no data". (Task 8 `test_forbidden_is_an_error_not_empty`)
5. The source REST API is down or slow → a clear error within the timeout, not a hang. (Task 8 `test_api_down_is_a_clear_error`)
6. Concurrent calls from different personas never see each other's rows. (Task 7 `test_no_context_leak_between_pooled_calls`, Task 9 `test_concurrent_personas_are_isolated`)
7. A runaway query is cancelled by `statement_timeout` and reported as "timed out". (Task 7 `test_query_timeout`)

---

## File Structure

```
backend/pyproject.toml                       (modify: deps, httpx to runtime)
backend/prism/config.py                      (modify: api urls, mcp settings)
backend/prism/mcp/__init__.py
backend/prism/mcp/results.py                 SourceError, jsonable, has_dataset, forward_claims
backend/prism/mcp/auth.py                    PrismTokenVerifier, current_claims
backend/prism/mcp/client.py                  mcp_client, call_tool (reused by the Plan 3 gateway)
backend/prism/mcp/base.py                    result models, SourceBackend protocol, build_source_app
backend/prism/mcp/sql_guard.py               (from spike)
backend/prism/mcp/metrics.py                 (from spike, edited)
backend/prism/mcp/metrics/*.yaml             19 SQL governed metrics
backend/prism/mcp/sql_backend.py             SqlBackend
backend/prism/mcp/rest/refmaster.yaml        endpoint registry + endpoint-backed metrics
backend/prism/mcp/rest/marketmaster.yaml
backend/prism/mcp/rest_backend.py            RestBackend
backend/prism/mcp/servers.py                 SOURCES, MCP_PORTS, create_app, uvicorn factories
backend/prism/mcp/cli.py                     python -m prism.mcp.cli list|call
backend/tests/mcp/conftest.py                token helpers
backend/tests/mcp/test_results.py
backend/tests/mcp/test_auth.py
backend/tests/mcp/test_base.py
backend/tests/mcp/test_sql_guard.py          (from spike, edited)
backend/tests/mcp/test_metrics.py            (from spike, edited)
backend/tests/mcp/test_metric_catalog.py
backend/tests/mcp/test_sql_backend.py
backend/tests/mcp/test_rest_backend.py
backend/tests/mcp/test_servers_e2e.py
backend/tests/mcp/test_cli.py
backend/Procfile, scripts/start_backend.sh, README.md, Makefile   (modify, Task 10)
```

---

### Task 1: Branch, dependencies, settings

**Files:**
- Modify: `backend/pyproject.toml`, `backend/prism/config.py`
- Create: `backend/prism/mcp/__init__.py` (empty)
- Test: `backend/tests/test_config.py` (append)

**Interfaces:**
- Produces: `Settings.refmaster_api_url: str`, `Settings.marketmaster_api_url: str`, `Settings.mcp_query_max_rows: int`, `Settings.mcp_allowed_hosts: list[str]`.

- [ ] **Step 1: Create the branch**

```bash
git checkout -b plan-2-source-mcp main
```

- [ ] **Step 2: Write the failing test** — append to `backend/tests/test_config.py`

```python
def test_mcp_settings_defaults():
    s = Settings()
    assert s.refmaster_api_url == "http://127.0.0.1:8101"
    assert s.marketmaster_api_url == "http://127.0.0.1:8102"
    assert s.mcp_query_max_rows == 500
    assert "127.0.0.1:*" in s.mcp_allowed_hosts and "localhost:*" in s.mcp_allowed_hosts
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd backend && uv run pytest tests/test_config.py -q`
Expected: FAIL with `AttributeError` (`refmaster_api_url`).

- [ ] **Step 4: Implement**

`backend/prism/config.py` — add inside `Settings` after `statement_timeout_ms`:

```python
    refmaster_api_url: str = "http://127.0.0.1:8101"
    marketmaster_api_url: str = "http://127.0.0.1:8102"
    mcp_query_max_rows: int = 500
    mcp_allowed_hosts: list[str] = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
```

`backend/pyproject.toml` — dependencies list becomes:

```toml
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "psycopg[binary]>=3.2",
  "psycopg-pool>=3.2",
  "pydantic-settings>=2.4",
  "pyjwt>=2.9",
  "honcho>=2.0",
  "mcp>=2.2,<3",
  "sqlglot>=30,<31",
  "pyyaml>=6",
  "httpx>=0.28",
]
```

and the dev group becomes `dev = ["pytest>=8", "pytest-asyncio>=0.24"]`. Create empty `backend/prism/mcp/__init__.py`.

- [ ] **Step 5: Install and run tests**

Run: `cd backend && uv sync && uv run pytest tests/test_config.py -q` → Expected: `4 passed`.
Run: `cd backend && uv run python -c "from mcp.server.mcpserver import MCPServer; import sqlglot, yaml, httpx2; print('ok')"` → Expected: `ok`.

- [ ] **Step 6: Commit**

```bash
git add backend/pyproject.toml backend/uv.lock backend/prism/config.py backend/prism/mcp/__init__.py backend/tests/test_config.py
git commit -m "chore: plan 2 dependencies and settings

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Shared helpers, token verifier, client

**Files:**
- Create: `backend/prism/mcp/results.py`, `backend/prism/mcp/auth.py`, `backend/prism/mcp/client.py`, `backend/tests/mcp/conftest.py`
- Test: `backend/tests/mcp/test_results.py`, `backend/tests/mcp/test_auth.py`

**Interfaces:**
- Produces:
  - `results.SourceError(Exception)` — user-facing, safe-to-show failure.
  - `results.jsonable(value) -> Any` — Decimal→float, date/datetime→ISO string, UUID→str, sets/tuples→list, recursive.
  - `results.has_dataset(claims: dict, source: str) -> bool` — true if scopes contain `source` or any `source.<table>`.
  - `results.forward_claims(claims: dict) -> dict` — the subset `{sub, roles, scopes, rows, metrics_only}` that may be re-minted for a downstream API.
  - `auth.PrismTokenVerifier(audience, secret)` with `async verify_token(token) -> AccessToken | None`; `auth.current_claims() -> dict` (raises `PermissionError` outside an authenticated request).
  - `client.mcp_client(url, token, *, mode="auto", asgi_app=None, timeout_s=30.0, read_timeout_s=300.0)` async context manager yielding an `mcp.Client`; `client.call_tool(url, token, name, arguments, *, asgi_app=None) -> CallToolResult`.
  - Test fixture helpers in `tests/mcp/conftest.py`: `mcp_token(settings, persona, source) -> str`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/mcp/conftest.py`:

```python
import pytest

from prism.config import Settings
from prism.security.personas import claims_for
from prism.security.tokens import mint


@pytest.fixture
def mcp_token():
    def make(settings: Settings, persona: str, source: str, ttl_s: int = 300) -> str:
        return mint(claims_for(persona), f"{source}-mcp", settings.jwt_secret, ttl_s=ttl_s)

    return make
```

`backend/tests/mcp/test_results.py`:

```python
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from prism.mcp.results import forward_claims, has_dataset, jsonable


def test_jsonable_converts_recursively():
    out = jsonable({"a": Decimal("1.5"), "b": [date(2026, 9, 30), datetime(2026, 9, 30, 1, 2, tzinfo=timezone.utc)],
                    "c": (1, {2}), "d": uuid.UUID(int=1)})
    assert out == {"a": 1.5, "b": ["2026-09-30", "2026-09-30T01:02:00+00:00"], "c": [1, [2]],
                   "d": "00000000-0000-0000-0000-000000000001"}


def test_has_dataset_accepts_db_or_table_scopes():
    assert has_dataset({"scopes": ["cashrecon"]}, "cashrecon")
    assert has_dataset({"scopes": ["refmaster.securities"]}, "refmaster")
    assert not has_dataset({"scopes": ["refmaster.securities"]}, "cashrecon")
    assert not has_dataset({}, "cashrecon")
    assert not has_dataset({"scopes": ["cashrecon_x"]}, "cashrecon")


def test_forward_claims_is_a_strict_subset():
    src = {"sub": "u", "roles": ["r"], "scopes": ["s"], "rows": {"region": ["*"]}, "metrics_only": True,
           "aud": "x-mcp", "exp": 1, "iat": 0, "name": "N", "evil": 1}
    assert forward_claims(src) == {"sub": "u", "roles": ["r"], "scopes": ["s"], "rows": {"region": ["*"]},
                                   "metrics_only": True}
    assert forward_claims({"sub": "u"}) == {"sub": "u"}
```

`backend/tests/mcp/test_auth.py`:

```python
import pytest

from prism.config import Settings
from prism.mcp.auth import PrismTokenVerifier, current_claims
from prism.security.personas import claims_for
from prism.security.tokens import mint

SECRET = Settings().jwt_secret


async def test_valid_token_yields_claims():
    v = PrismTokenVerifier("cashrecon-mcp", SECRET)
    tok = mint(claims_for("cash_ops_emea"), "cashrecon-mcp", SECRET)
    at = await v.verify_token(tok)
    assert at is not None and at.claims["sub"] == "cash_ops_emea" and at.claims["rows"]["region"] == ["EMEA"]
    assert at.scopes == []  # our 'scopes' claim are dataset entitlements, not OAuth scopes


@pytest.mark.parametrize("case", ["wrong_aud", "expired", "wrong_secret", "garbage"])
async def test_bad_tokens_return_none(case):
    v = PrismTokenVerifier("cashrecon-mcp", SECRET)
    tok = {
        "wrong_aud": mint(claims_for("steward"), "assetrecon-mcp", SECRET),
        "expired": mint(claims_for("steward"), "cashrecon-mcp", SECRET, ttl_s=-5),
        "wrong_secret": mint(claims_for("steward"), "cashrecon-mcp", "z" * 48),
        "garbage": "not.a.jwt",
    }[case]
    assert await v.verify_token(tok) is None


def test_current_claims_outside_a_request_raises():
    with pytest.raises(PermissionError):
        current_claims()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/mcp -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.mcp.results'`.

- [ ] **Step 3: Implement**

`backend/prism/mcp/results.py`:

```python
"""Small shared helpers for the source MCP servers."""
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

_FORWARDED = ("sub", "roles", "scopes", "rows", "metrics_only")


class SourceError(Exception):
    """A failure whose message is safe to show to the caller (surfaces as a tool error)."""


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v) for v in value]
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def has_dataset(claims: dict, source: str) -> bool:
    scopes = set(claims.get("scopes", ()))
    return source in scopes or any(s.startswith(f"{source}.") for s in scopes)


def forward_claims(claims: dict) -> dict:
    """The only claims that may be re-minted for a downstream API (never the raw incoming token)."""
    return {k: claims[k] for k in _FORWARDED if k in claims}
```

`backend/prism/mcp/auth.py`:

```python
"""Bearer-token verification for source MCP servers: audience-bound HS256 JWTs from prism.security.tokens."""
from typing import Any

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken

from prism.security.tokens import TokenError, verify


class PrismTokenVerifier:
    """Implements the SDK's TokenVerifier protocol. Must never raise: an escaping exception becomes a 500."""

    def __init__(self, audience: str, secret: str) -> None:
        self.audience = audience
        self.secret = secret

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = verify(token, self.audience, self.secret)
        except TokenError:
            return None
        return AccessToken(
            token=token,
            client_id=str(claims["sub"]),
            subject=str(claims["sub"]),
            scopes=[],  # our `scopes` claim holds dataset entitlements; it is not an OAuth scope list
            expires_at=int(claims["exp"]),
            claims=claims,
        )


def current_claims() -> dict[str, Any]:
    """Verified claims of the request being handled (set by the SDK's auth middleware)."""
    token = get_access_token()
    if token is None or token.claims is None:
        raise PermissionError("no authenticated principal in context")
    return token.claims
```

`backend/prism/mcp/client.py` (adapted from `docs/superpowers/spikes/plan-2/mcp/client.py`):

```python
"""MCP client helpers (the Plan 3 gateway reuses these). NOTE: the mcp 2.x client uses httpx2, not httpx."""
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx2
from mcp import Client
from mcp.client.client import ConnectMode
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult


@contextlib.asynccontextmanager
async def mcp_client(
    url: str,
    token: str,
    *,
    mode: ConnectMode = "auto",
    asgi_app: Any | None = None,
    timeout_s: float = 30.0,
    read_timeout_s: float = 300.0,
) -> AsyncIterator[Client]:
    """Connect to a streamable-HTTP MCP endpoint with a bearer token.

    asgi_app: talk to an ASGI app in-process (no socket); the caller must then run
    `mcp.session_manager.run()` itself because ASGITransport does not run the app lifespan.
    """
    http = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx2.Timeout(timeout_s, read=read_timeout_s),
        transport=httpx2.ASGITransport(app=asgi_app) if asgi_app is not None else None,
    )
    async with http:
        async with Client(streamable_http_client(url, http_client=http), mode=mode, cache=None) as client:
            yield client


async def call_tool(url: str, token: str, name: str, arguments: dict[str, Any] | None = None, *,
                    asgi_app: Any | None = None) -> CallToolResult:
    async with mcp_client(url, token, asgi_app=asgi_app) as client:
        return await client.call_tool(name, arguments or {})
```

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/mcp -q` → Expected: `8 passed`.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/mcp backend/tests/mcp
git commit -m "feat(mcp): shared helpers, audience-bound token verifier and client

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Generic source MCP app (the uniform contract)

**Files:**
- Create: `backend/prism/mcp/base.py`
- Test: `backend/tests/mcp/test_base.py`

**Interfaces:**
- Consumes: `PrismTokenVerifier`, `current_claims` (Task 2); `SourceError` (Task 2). Task 5 provides `prism.mcp.metrics.MetricError` and Task 4 provides `prism.mcp.sql_guard.SqlGuardError` — until those tasks land, `base.py` defines nothing about them: it catches `ValueError` subclasses **by importing them lazily inside `_run`** (see code) so Task 3 is testable alone.
- Produces:
  - Pydantic models `DescribeResult(source, kind, as_of, metrics: list[dict], objects: list[dict], notes: list[str])`, `MetricResult(source, metric_id, unit, dimensions: list[str], rows: list[dict], row_count, as_of, window: dict|None)`, `QueryResult(source, columns: list[str], rows: list[list], row_count, truncated)`.
  - `class SourceBackend(Protocol)`: `name: str`, `kind: str`, `async describe(claims) -> DescribeResult`, `async run_metric(claims, *, metric_id, dimensions, filters, time_range, order_by, limit) -> MetricResult`, `async query(claims, request: dict) -> QueryResult`, `async aclose() -> None`.
  - `build_source_app(backend, settings) -> tuple[MCPServer, Starlette]`. Tools: `describe()`, `run_metric(metric_id, dimensions=[], filters={}, time_range=None, order_by=None, limit=100)`, `query(request: dict)`. Audience is `f"{backend.name}-mcp"`; `GET /healthz` is public; MCP endpoint `/mcp`.

- [ ] **Step 1: Write the failing tests** — `backend/tests/mcp/test_base.py`

```python
import asyncio
import contextlib

import httpx2
import pytest

from prism.config import Settings
from prism.mcp.base import DescribeResult, MetricResult, QueryResult, build_source_app
from prism.mcp.client import mcp_client
from prism.mcp.results import SourceError
from prism.security.personas import claims_for
from prism.security.tokens import mint

URL = "http://127.0.0.1:8000/mcp"
SETTINGS = Settings()


class FakeBackend:
    name = "fake"
    kind = "sql"

    async def describe(self, claims):
        return DescribeResult(source="fake", kind="sql", as_of="2026-09-30", metrics=[], objects=[],
                              notes=[f"sub={claims['sub']}"])

    async def run_metric(self, claims, *, metric_id, dimensions, filters, time_range, order_by, limit):
        if metric_id == "boom":
            raise RuntimeError("password=hunter2 leaked in driver error")
        if metric_id == "denied":
            raise SourceError("not entitled to this dataset")
        await asyncio.sleep(0.02)  # let concurrent requests interleave
        return MetricResult(source="fake", metric_id=metric_id, unit=None, dimensions=dimensions,
                            rows=[{"who": claims["sub"], "value": limit}], row_count=1, as_of="2026-09-30",
                            window=None)

    async def query(self, claims, request):
        return QueryResult(source="fake", columns=["who"], rows=[[claims["sub"]]], row_count=1, truncated=False)

    async def aclose(self):
        pass


@contextlib.asynccontextmanager
async def running():
    mcp, app = build_source_app(FakeBackend(), SETTINGS)
    async with mcp.session_manager.run():  # ASGITransport does not run the lifespan
        yield app


def tok(persona="steward", aud="fake-mcp", ttl=300):
    return mint(claims_for(persona), aud, SETTINGS.jwt_secret, ttl_s=ttl)


async def test_uniform_tool_contract_and_schemas():
    async with running() as app:
        async with mcp_client(URL, tok(), asgi_app=app) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
    assert set(tools) == {"describe", "run_metric", "query"}
    rm = tools["run_metric"].input_schema
    assert rm["required"] == ["metric_id"]
    assert rm["properties"]["limit"]["minimum"] == 1 and rm["properties"]["limit"]["maximum"] == 1000
    assert tools["run_metric"].annotations.read_only_hint is True
    assert "rows" in tools["run_metric"].output_schema["properties"]


async def test_claims_reach_the_backend_and_text_is_a_short_summary():
    async with running() as app:
        async with mcp_client(URL, tok("cash_ops_emea"), asgi_app=app) as c:
            r = await c.call_tool("run_metric", {"metric_id": "m", "limit": 7})
    assert not r.is_error
    assert r.structured_content["rows"] == [{"who": "cash_ops_emea", "value": 7}]
    assert "1 row" in r.content[0].text and "cash_ops_emea" not in r.content[0].text


async def test_describe_and_query_are_wired():
    async with running() as app:
        async with mcp_client(URL, tok("head_data"), asgi_app=app) as c:
            d = await c.call_tool("describe", {})
            q = await c.call_tool("query", {"request": {"sql": "select 1"}})
    assert d.structured_content["notes"] == ["sub=head_data"]
    assert q.structured_content["rows"] == [["head_data"]]


async def test_metrics_only_principals_cannot_query_but_can_run_metrics():
    async with running() as app:
        async with mcp_client(URL, tok("bi_analyst"), asgi_app=app) as c:
            q = await c.call_tool("query", {"request": {"sql": "select 1"}})
            m = await c.call_tool("run_metric", {"metric_id": "m"})
    assert q.is_error and "metrics-only" in q.content[0].text
    assert not m.is_error


async def test_error_mapping_is_clean_and_does_not_leak():
    async with running() as app:
        async with mcp_client(URL, tok(), asgi_app=app) as c:
            denied = await c.call_tool("run_metric", {"metric_id": "denied"})
            boom = await c.call_tool("run_metric", {"metric_id": "boom"})
            bad = await c.call_tool("run_metric", {"metric_id": "m", "limit": 0})
    assert denied.is_error and denied.content[0].text.endswith("not entitled to this dataset")
    assert boom.is_error and "hunter2" not in boom.content[0].text
    assert bad.is_error


@pytest.mark.parametrize("case", ["wrong_audience", "expired", "missing"])
async def test_wrong_audience_and_expired_are_401(case):
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if case == "wrong_audience":
        headers["Authorization"] = f"Bearer {tok(aud='other-mcp')}"
    elif case == "expired":
        headers["Authorization"] = f"Bearer {tok(ttl=-5)}"
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
    async with running() as app:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as h:
            r = await h.post("/mcp", json=init, headers=headers)
            health = await h.get("/healthz")
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    assert health.status_code == 200 and health.json() == {"status": "ok", "source": "fake"}


async def test_concurrent_callers_never_see_each_others_identity():
    personas = ["steward", "cash_ops_emea", "invest_ops_growth", "bi_analyst", "head_data"] * 3
    async with running() as app:

        async def one(p):
            async with mcp_client(URL, tok(p), asgi_app=app) as c:
                r = await c.call_tool("run_metric", {"metric_id": "m"})
                return p, r.structured_content["rows"][0]["who"]

        results = await asyncio.wait_for(asyncio.gather(*(one(p) for p in personas)), 60)
    assert all(asked == got for asked, got in results)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/mcp/test_base.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.mcp.base'`.

- [ ] **Step 3: Implement `backend/prism/mcp/base.py`**

```python
"""One generic MCP server per data source. The three tools are identical for every source
(spec §4.3); a SourceBackend supplies the behaviour. Read docs/superpowers/spikes/plan-2/mcp/ for the
pattern this implements (SDK token verifier, stateless JSON responses, explicit host allow-list)."""
import json
import logging
import time
from typing import Annotated, Any, Protocol

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from prism.config import Settings
from prism.mcp.auth import PrismTokenVerifier, current_claims
from prism.mcp.results import SourceError

audit = logging.getLogger("prism.mcp.audit")


class DescribeResult(BaseModel):
    source: str
    kind: str
    as_of: str
    metrics: list[dict[str, Any]]
    objects: list[dict[str, Any]]
    notes: list[str] = []


class MetricResult(BaseModel):
    source: str
    metric_id: str
    unit: str | None
    dimensions: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    as_of: str
    window: dict[str, str] | None = None


class QueryResult(BaseModel):
    source: str
    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool


class SourceBackend(Protocol):
    name: str
    kind: str

    async def describe(self, claims: dict) -> DescribeResult: ...

    async def run_metric(self, claims: dict, *, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                         time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult: ...

    async def query(self, claims: dict, request: dict[str, Any]) -> QueryResult: ...

    async def aclose(self) -> None: ...


def _user_facing_errors() -> tuple[type[Exception], ...]:
    """Exceptions whose message is safe to show. Imported lazily so this module has no hard dependency
    on the SQL-only modules (they land in later tasks)."""
    errors: list[type[Exception]] = [SourceError]
    try:
        from prism.mcp.metrics import MetricError

        errors.append(MetricError)
    except ImportError:
        pass
    try:
        from prism.mcp.sql_guard import SqlGuardError

        errors.append(SqlGuardError)
    except ImportError:
        pass
    return tuple(errors)


def _reply(model: BaseModel, summary: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=summary)],
                          structured_content=model.model_dump(mode="json"))


def build_source_app(backend: SourceBackend, settings: Settings) -> tuple[MCPServer, Starlette]:
    source = backend.name
    visible = _user_facing_errors()
    mcp = MCPServer(
        f"prism-{source}",
        instructions=(f"Read-only access to the {source} data source. Use describe to see what is available, "
                      "run_metric for governed measures (preferred), query only for exploration."),
        token_verifier=PrismTokenVerifier(f"{source}-mcp", settings.jwt_secret),
        # issuer_url is metadata only; resource_server_url=None because the verifier checks `aud` itself.
        auth=AuthSettings(issuer_url="https://prism.invalid", resource_server_url=None),
    )

    @mcp.custom_route("/healthz", methods=["GET"])  # public: the start script polls it
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "source": source})

    async def run(tool: str, call, **meta) -> Any:
        claims = current_claims()
        started = time.perf_counter()
        outcome = "ok"
        try:
            return await call(claims)
        except visible as exc:
            outcome = "error"
            raise ToolError(str(exc)) from exc
        except Exception:
            outcome = "internal_error"
            raise
        finally:
            audit.info(json.dumps({"event": "tool_call", "source": source, "tool": tool, "sub": claims.get("sub"),
                                   "outcome": outcome, "ms": round((time.perf_counter() - started) * 1000, 1),
                                   **meta}))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def describe() -> Annotated[CallToolResult, DescribeResult]:
        """List the governed metrics and the tables/endpoints this caller may use on this source."""
        result = await run("describe", backend.describe)
        return _reply(result, f"{len(result.metrics)} metrics, {len(result.objects)} objects on {source}")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def run_metric(
        metric_id: Annotated[str, Field(description="Governed metric id from describe()", min_length=1)],
        dimensions: Annotated[list[str], Field(description="Dimension names to group by")] = [],
        filters: Annotated[dict[str, Any], Field(description="Filter name -> value | list | {gte,lte,between,ne}")] = {},
        time_range: Annotated[dict[str, Any] | None, Field(
            description="{'last_business_days': N} or {'from': ISO date, 'to': ISO date}")] = None,
        order_by: Annotated[str | None, Field(description="metric | metric_desc | <dimension> | -<dimension>")] = None,
        limit: Annotated[int, Field(description="Max rows", ge=1, le=1000)] = 100,
    ) -> Annotated[CallToolResult, MetricResult]:
        """Run a governed metric. Preferred over free-form queries: deterministic, cheap and always in policy."""
        result = await run(
            "run_metric",
            lambda claims: backend.run_metric(claims, metric_id=metric_id, dimensions=list(dimensions),
                                              filters=dict(filters), time_range=time_range, order_by=order_by,
                                              limit=limit),
            metric=metric_id,
        )
        return _reply(result, f"{result.row_count} row{'s' if result.row_count != 1 else ''} for {metric_id}")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    async def query(
        request: Annotated[dict[str, Any], Field(
            description="SQL sources: {'sql': '<single SELECT>'}. REST sources: {'endpoint_id': ..., 'params': {...}}")],
    ) -> Annotated[CallToolResult, QueryResult]:
        """Guarded free-form access for exploration when no governed metric fits. Not available to metrics-only principals."""

        async def call(claims: dict) -> QueryResult:
            if claims.get("metrics_only"):
                raise SourceError("free-form queries are not available to metrics-only principals; use run_metric")
            return await backend.query(claims, dict(request))

        result = await run("query", call)
        return _reply(result, f"{result.row_count} row{'s' if result.row_count != 1 else ''}"
                              f"{' (truncated)' if result.truncated else ''}")

    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=list(settings.mcp_allowed_hosts), allowed_origins=[])
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                  transport_security=security)
    return mcp, app
```

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/mcp/test_base.py -q` → Expected: `9 passed` (parametrized 401 cases count separately).

- [ ] **Step 5: Commit**

```bash
git add backend/prism/mcp/base.py backend/tests/mcp/test_base.py
git commit -m "feat(mcp): generic source MCP app with the uniform three-tool contract

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: SQL guard (from the spike)

**Files:**
- Create: `backend/prism/mcp/sql_guard.py`
- Test: `backend/tests/mcp/test_sql_guard.py`

**Interfaces:**
- Produces: `sql_guard.SqlGuardError(ValueError)`, `sql_guard.validate_select(sql, *, allowed_tables: frozenset[str], max_rows: int = 500) -> str` (returns SQL regenerated from the AST, wrapped as `SELECT * FROM (<q>) AS _q LIMIT n`).

- [ ] **Step 1: Copy the test file and adapt it (this is the failing test)**

Copy `docs/superpowers/spikes/plan-2/sqlguard/test_sql_guard.py` to `backend/tests/mcp/test_sql_guard.py`, then apply exactly these edits (nothing else — the corpora are the spec):
1. Change `from sql_guard import SqlGuardError, validate_select` to `from prism.mcp.sql_guard import SqlGuardError, validate_select`.
2. The live-DB part must use the seeded **test** databases and never the dev databases: delete `PG_DSN`, `_db_available` and the `conns` fixture, and replace them with this (add `from prism.config import Settings` is **not** needed; use the session `seeded` fixture from `backend/tests/conftest.py`):

```python
@pytest.fixture(scope="module")
def conns(seeded):
    cs = {db: psycopg.connect(seeded.dsn(db, admin=True)) for db in ("cashrecon", "assetrecon", "feedhub")}
    yield cs
    for c in cs.values():
        c.close()
```
3. In `test_seeded_tables_have_rows` keep the three tables but query `breaks`, `nav_checks`, `feed_deliveries` (unchanged) — they exist in the small profile.
4. Delete `test_psycopg_simple_protocol_runs_stacked_statements`'s dependency on a module-level DSN only if it references `PG_DSN` (it uses the `conns` fixture, so it stays as is).

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/mcp/test_sql_guard.py -q`
Expected: FAIL/ERROR with `ModuleNotFoundError: No module named 'prism.mcp.sql_guard'`.

- [ ] **Step 3: Copy the implementation**

Copy `docs/superpowers/spikes/plan-2/sqlguard/sql_guard.py` to `backend/prism/mcp/sql_guard.py` with exactly ONE edit (review of Task 3 decided user-facing errors share a base class instead of lazy imports): add `from prism.mcp.results import UserFacingError` and declare `class SqlGuardError(UserFacingError, ValueError):`. (Its module docstring already lists the required DB-side controls; Task 7 provides them: `bi_reader` role, read-only transaction, `statement_timeout`, pinned `search_path`, `prepare=True`.)

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/mcp/test_sql_guard.py -q`
Expected: about 200 passed (119 negative, 37 positive accepted, 37 positive executed on the seeded test DBs, plus the small tests). Output must be pristine.

- [ ] **Step 5: Add the Review-Focus test for this task** — append to `backend/tests/mcp/test_sql_guard.py`

```python
@pytest.mark.parametrize("sql", [
    "SELECT * FROM breaks; SELECT 1",          # stacked
    r"SELECT 'a\' AS x",                         # backslash
    "SELECT set_config('app.ctx', '', false)",  # context tampering
    "SELECT * FROM private.cash_accounts",      # base table behind the masking view
])
def test_review_focus_query_rejections(sql):
    with pytest.raises(SqlGuardError):
        validate_select(sql, allowed_tables=ALLOWED, max_rows=MAX_ROWS)
```

Also append:

```python
def test_guard_error_is_user_facing_and_a_value_error():
    from prism.mcp.results import UserFacingError

    assert issubclass(SqlGuardError, UserFacingError) and issubclass(SqlGuardError, ValueError)
```

Run: `cd backend && uv run pytest tests/mcp/test_sql_guard.py -q` → all pass.

- [ ] **Step 6: Commit**

```bash
git add backend/prism/mcp/sql_guard.py backend/tests/mcp/test_sql_guard.py
git commit -m "feat(mcp): AST-based read-only SQL guard with attack corpus

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Governed-metric compiler and the first six metrics (from the spike)

**Files:**
- Create: `backend/prism/mcp/metrics.py`, `backend/prism/mcp/metrics/{open_breaks,aged_open_breaks,late_feeds,position_exceptions,nav_break_bps_max,auto_match_rate}.yaml`
- Test: `backend/tests/mcp/test_metrics.py`

**Interfaces:**
- Produces (used by Tasks 6, 7): `metrics.Metric`, `metrics.MetricError(ValueError)`, `metrics.ALLOWED_TABLES: dict[str, frozenset[str]]`, `metrics.DEFAULT_METRICS_DIR: Path`, `metrics.load_metrics(dir) -> dict[str, Metric]`, `metrics.last_business_days(n, as_of) -> (date, date)`, **`metrics.resolve_time_range(tr, as_of) -> (date, date)` (public; was `_resolve_time_range` in the spike)**, `metrics.compile_metric(metric, *, dimensions, filters, time_range, as_of, order_by, limit, max_limit) -> (sql.Composed, list)`, `metrics.run_metric(conn, metric, *, db_prefix="", **kwargs) -> list[dict]`.

- [ ] **Step 1: Copy the test file and adapt it (the failing test)**

Copy `docs/superpowers/spikes/plan-2/metrics/test_metrics.py` to `backend/tests/mcp/test_metrics.py`, then apply exactly these edits:
1. Imports: replace `from metrics import Metric, MetricError, compile_metric, last_business_days, load_metrics, run_metric` with `from prism.mcp.metrics import DEFAULT_METRICS_DIR, Metric, MetricError, compile_metric, last_business_days, load_metrics, run_metric`; add `from prism.db.session import ctx_from_claims, prepare_statements`, `from prism.security.personas import claims_for`; remove the now-unneeded `base64`, `hashlib`, `hmac`, `json`, `time` imports if unused.
2. Delete the copied `sign_ctx`, `ctx_from_claims`, `prepare_statements`, `ALL_SOURCES`, `ALL_ROWS`, `PERSONAS`, `claims_for`, `_env`, `ENV`, `CTX_KEY`, `REPO` definitions — the repo's own functions replace them.
3. `METRICS = load_metrics(DEFAULT_METRICS_DIR)`; the metrics loaded here are the first six only (Task 6 adds more), so change `test_all_six_metrics_load` to assert the six ids are a **subset** of `METRICS`.
4. Real-data tests must use the seeded **test** databases through the session `seeded` fixture (small profile), not the dev databases. Replace the `scoped(...)` helper with:

```python
@contextmanager
def scoped(settings, persona: str, db: str):
    ctx = ctx_from_claims(claims_for(persona), settings.ctx_hmac_key)
    with psycopg.connect(settings.dsn(db)) as conn:
        with conn.transaction():
            for statement, args in prepare_statements(ctx, 5000):
                conn.execute(statement, args)
            yield conn


def run(settings, persona, metric_id, **kw):
    m = METRICS[metric_id]
    with scoped(settings, persona, m.source) as conn:
        return run_metric(conn, m, db_prefix=settings.db_prefix, as_of=AS_OF, **kw)
```

and give every real-data test a `seeded` parameter, calling `run(seeded, ...)`. `run_metric` must be passed `db_prefix=settings.db_prefix` (`"test_"`).
5. Story-number thresholds must be re-based to the **small profile** without weakening their intent: keep the Plan 1 thresholds — aged USD top-entity share ≥ 0.5 (was 0.7 on full data; Plan 1's test bound is 0.5), late-portfolio exceptions per day ≥ 3× the earlier baseline (compute both from the data instead of hard-coding 20/1.29), late feeds: `SRC001` (`stories.late_custodian_source_id`) is the top source in the last 6 business days and ≥ 2× the runner-up, NAV: the top rows are the story portfolios `PF003`/`PF009` with `value > 5` and every other row `< 3`, `auto_match_rate` between 0.85 and 1.0. If a bound is not met on the seeded small data, report BLOCKED with the measured numbers instead of loosening it.
6. `test_run_metric_refuses_wrong_db_and_unscoped_conn`: connect with `seeded.dsn(...)` (non-admin `prism_svc`); the wrong-database check passes `db_prefix="test_"`.

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/mcp/test_metrics.py -q`
Expected: FAIL/ERROR `ModuleNotFoundError: No module named 'prism.mcp.metrics'`.

- [ ] **Step 3: Copy the implementation and the six YAML files**

Copy `docs/superpowers/spikes/plan-2/metrics/metrics.py` to `backend/prism/mcp/metrics.py` and the six files from `docs/superpowers/spikes/plan-2/metrics/metrics/*.yaml` to `backend/prism/mcp/metrics/`. Apply exactly these edits to `metrics.py`:
1. Rename `_resolve_time_range` to public `resolve_time_range` (definition and its single use in `compile_metric`).
2. After the `ALLOWED_TABLES` definition add `DEFAULT_METRICS_DIR = Path(__file__).parent / "metrics"`.
3. `from prism.mcp.results import UserFacingError` and declare `class MetricError(UserFacingError, ValueError):` (user-facing errors share a base class; Task 3's review replaced lazy imports with `UserFacingError`). Add to `tests/mcp/test_metrics.py`: `def test_metric_error_is_user_facing(): assert issubclass(MetricError, UserFacingError) and issubclass(MetricError, ValueError)`.
4. Nothing else (the forbidden-token regex, table allow-lists and `run_metric` guards are the reviewed design).

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/mcp/test_metrics.py -q`
Expected: all pass (about 38). If a story-threshold test fails on the small data, follow edit 5's BLOCKED rule.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/mcp/metrics.py backend/prism/mcp/metrics backend/tests/mcp/test_metrics.py
git commit -m "feat(mcp): governed-metric compiler with the first six metrics

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Metric catalog (13 more SQL metrics)

**Files:**
- Create: 13 YAML files under `backend/prism/mcp/metrics/` (listed below)
- Test: `backend/tests/mcp/test_metric_catalog.py`

**Interfaces:**
- Consumes: `load_metrics`, `DEFAULT_METRICS_DIR`, `run_metric`, `compile_metric` (Task 5).
- Produces: 19 SQL metrics total (`open_breaks, aged_open_breaks, open_break_amount, avg_break_age, break_resolution_rate, manual_matches, auto_match_rate, position_exceptions, open_position_exceptions, exception_mv_abs, recon_unmatched_items, clean_recon_run_rate, nav_break_bps_max, nav_breaches_above_5bps, late_feeds, missing_or_failed_deliveries, feed_on_time_rate, avg_feed_latency_min, open_support_tickets`).

- [ ] **Step 1: Write the failing test** — `backend/tests/mcp/test_metric_catalog.py`

```python
from contextlib import contextmanager
from datetime import date

import psycopg
import pytest

from prism.db.session import ctx_from_claims, prepare_statements
from prism.mcp.metrics import DEFAULT_METRICS_DIR, ALLOWED_TABLES, compile_metric, load_metrics, run_metric
from prism.security.personas import claims_for

pytestmark = pytest.mark.db
METRICS = load_metrics(DEFAULT_METRICS_DIR)
AS_OF = date(2026, 9, 30)
EXPECTED = {
    "open_breaks", "aged_open_breaks", "open_break_amount", "avg_break_age", "break_resolution_rate",
    "manual_matches", "auto_match_rate", "position_exceptions", "open_position_exceptions", "exception_mv_abs",
    "recon_unmatched_items", "clean_recon_run_rate", "nav_break_bps_max", "nav_breaches_above_5bps",
    "late_feeds", "missing_or_failed_deliveries", "feed_on_time_rate", "avg_feed_latency_min",
    "open_support_tickets",
}


@contextmanager
def scoped(settings, persona, db):
    ctx = ctx_from_claims(claims_for(persona), settings.ctx_hmac_key)
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
    compile_metric(m, dimensions=[], as_of=AS_OF)
    for d in m.dimensions:
        compile_metric(m, dimensions=[d], as_of=AS_OF)
    compile_metric(m, dimensions=list(m.dimensions)[:2], as_of=AS_OF)


@pytest.mark.parametrize("metric_id", sorted(EXPECTED))
def test_every_metric_runs_and_returns_data_as_head_data(seeded, metric_id):
    m = METRICS[metric_id]
    with scoped(seeded, "head_data", m.source) as conn:
        rows = run_metric(conn, m, db_prefix=seeded.db_prefix, as_of=AS_OF, dimensions=[list(m.dimensions)[0]],
                          limit=1000)
    assert rows, f"{metric_id} returned no rows on the seeded data"
    assert all("value" in r for r in rows)
    assert any(r["value"] not in (None, 0) for r in rows), f"{metric_id} is all zero/null"


@pytest.mark.parametrize("metric_id", sorted(EXPECTED))
def test_every_metric_returns_nothing_without_the_dataset(seeded, metric_id):
    m = METRICS[metric_id]
    with scoped(seeded, "steward", m.source) as conn:  # steward has refmaster+marketmaster only
        rows = run_metric(conn, m, db_prefix=seeded.db_prefix, as_of=AS_OF, dimensions=[list(m.dimensions)[0]])
    assert rows == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd backend && uv run pytest tests/mcp/test_metric_catalog.py -q`
Expected: FAIL (`set(METRICS) == EXPECTED` mismatch: only 6 metrics exist).

- [ ] **Step 3: Create the 13 YAML files** in `backend/prism/mcp/metrics/`

`open_break_amount.yaml`:
```yaml
id: open_break_amount
source: cashrecon
description: Total amount of cash breaks that are not closed.
type: sum
expr: sum(amount)
from: breaks
base_filter: status <> 'closed'
time_column: opened_on
dimensions:
  region: region
  ccy: ccy
  legal_entity_id: legal_entity_id
  break_type: break_type
filters:
  region: {expr: region, type: str}
  ccy: {expr: ccy, type: str}
  legal_entity_id: {expr: legal_entity_id, type: str}
  break_type: {expr: break_type, type: str}
unit: amount (transaction currency)
default_order: metric_desc
```

`avg_break_age.yaml`:
```yaml
id: avg_break_age
source: cashrecon
description: Average age in days of cash breaks that are not closed.
type: avg
expr: avg(age_days)
from: breaks
base_filter: status <> 'closed'
time_column: opened_on
dimensions:
  region: region
  ccy: ccy
  legal_entity_id: legal_entity_id
filters:
  region: {expr: region, type: str}
  ccy: {expr: ccy, type: str}
  legal_entity_id: {expr: legal_entity_id, type: str}
unit: days
default_order: metric_desc
```

`break_resolution_rate.yaml`:
```yaml
id: break_resolution_rate
source: cashrecon
description: Share (0..1) of cash breaks that have been closed.
type: ratio
numerator: status = 'closed'
from: breaks
time_column: opened_on
dimensions:
  region: region
  ccy: ccy
  break_type: break_type
filters:
  region: {expr: region, type: str}
  ccy: {expr: ccy, type: str}
  break_type: {expr: break_type, type: str}
unit: fraction
default_order: metric_desc
```

`manual_matches.yaml`:
```yaml
id: manual_matches
source: cashrecon
description: Cash match groups that were matched manually (not by the engine).
type: count
expr: count(*)
from: match_groups
base_filter: status = 'manual'
dimensions:
  region: region
  matched_by: matched_by
  rule_id: rule_id
filters:
  region: {expr: region, type: str}
  matched_by: {expr: matched_by, type: str}
unit: match groups
default_order: metric_desc
```

`open_position_exceptions.yaml`:
```yaml
id: open_position_exceptions
source: assetrecon
description: Reconciliation exceptions that are still open.
type: count
expr: count(*)
from: recon_exceptions
base_filter: status = 'open'
time_column: business_date
dimensions:
  cause_code: cause_code
  fund_group: fund_group
  portfolio_id: portfolio_id
  business_date: business_date
filters:
  cause_code: {expr: cause_code, type: str}
  fund_group: {expr: fund_group, type: str}
  portfolio_id: {expr: portfolio_id, type: str}
unit: exceptions
default_order: metric_desc
```

`exception_mv_abs.yaml`:
```yaml
id: exception_mv_abs
source: assetrecon
description: Total absolute market-value difference across reconciliation exceptions.
type: sum
expr: sum(abs(diff_mv))
from: recon_exceptions
time_column: business_date
dimensions:
  cause_code: cause_code
  fund_group: fund_group
  portfolio_id: portfolio_id
filters:
  cause_code: {expr: cause_code, type: str}
  fund_group: {expr: fund_group, type: str}
  portfolio_id: {expr: portfolio_id, type: str}
unit: amount (base currency)
default_order: metric_desc
```

`recon_unmatched_items.yaml`:
```yaml
id: recon_unmatched_items
source: assetrecon
description: Total unmatched items across reconciliation runs.
type: sum
expr: sum(unmatched)
from: recon_runs
time_column: business_date
dimensions:
  recon_type: recon_type
  fund_group: fund_group
  portfolio_id: portfolio_id
  business_date: business_date
filters:
  recon_type: {expr: recon_type, type: str}
  fund_group: {expr: fund_group, type: str}
  portfolio_id: {expr: portfolio_id, type: str}
unit: items
default_order: metric_desc
```

`clean_recon_run_rate.yaml`:
```yaml
id: clean_recon_run_rate
source: assetrecon
description: Share (0..1) of reconciliation runs with no unmatched items.
type: ratio
numerator: unmatched = 0
from: recon_runs
time_column: business_date
dimensions:
  recon_type: recon_type
  fund_group: fund_group
  business_date: business_date
filters:
  recon_type: {expr: recon_type, type: str}
  fund_group: {expr: fund_group, type: str}
unit: fraction
default_order: metric_desc
```

`nav_breaches_above_5bps.yaml`:
```yaml
id: nav_breaches_above_5bps
source: assetrecon
description: NAV checks where the administrator and internal NAV differ by more than 5 basis points.
type: count
expr: count(*)
from: nav_checks
base_filter: abs(diff_bps) > 5
time_column: nav_date
dimensions:
  portfolio_id: portfolio_id
  fund_group: fund_group
  nav_date: nav_date
filters:
  portfolio_id: {expr: portfolio_id, type: str}
  fund_group: {expr: fund_group, type: str}
unit: checks
default_order: metric_desc
```

`missing_or_failed_deliveries.yaml`:
```yaml
id: missing_or_failed_deliveries
source: feedhub
description: Feed deliveries that never arrived usable (missing or failed).
type: count
expr: count(*)
from: feed_deliveries
base_filter: status IN ('missing', 'failed')
time_column: business_date
dimensions:
  source_id: source_id
  source_type: source_type
  status: status
  business_date: business_date
filters:
  source_id: {expr: source_id, type: str}
  source_type: {expr: source_type, type: str}
  status: {expr: status, type: str}
unit: deliveries
default_order: metric_desc
```

`feed_on_time_rate.yaml`:
```yaml
id: feed_on_time_rate
source: feedhub
description: Share (0..1) of feed deliveries that arrived on time.
type: ratio
numerator: status = 'on_time'
from: feed_deliveries
time_column: business_date
dimensions:
  source_id: source_id
  source_type: source_type
  business_date: business_date
filters:
  source_id: {expr: source_id, type: str}
  source_type: {expr: source_type, type: str}
unit: fraction
default_order: metric_desc
```

`avg_feed_latency_min.yaml`:
```yaml
id: avg_feed_latency_min
source: feedhub
description: Average lateness in minutes of late feed deliveries.
type: avg
expr: avg(latency_min)
from: feed_deliveries
base_filter: status = 'late'
time_column: business_date
dimensions:
  source_id: source_id
  source_type: source_type
  business_date: business_date
filters:
  source_id: {expr: source_id, type: str}
  source_type: {expr: source_type, type: str}
unit: minutes
default_order: metric_desc
```

`open_support_tickets.yaml`:
```yaml
id: open_support_tickets
source: feedhub
description: Support tickets on feeds that are still open.
type: count
expr: count(*)
from: support_tickets
base_filter: status = 'open'
dimensions:
  category: category
  source_id: source_id
  source_type: source_type
filters:
  category: {expr: category, type: str}
  source_id: {expr: source_id, type: str}
  source_type: {expr: source_type, type: str}
unit: tickets
default_order: metric_desc
```

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/mcp/test_metric_catalog.py tests/mcp/test_metrics.py -q` → all pass. If a metric returns no rows on the small seeded data, report the metric id and measured counts (BLOCKED) rather than editing the test.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/mcp/metrics backend/tests/mcp/test_metric_catalog.py
git commit -m "feat(mcp): governed metric catalog for CashRecon, AssetRecon and FeedHub

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: SqlBackend (CashRecon / AssetRecon / FeedHub)

**Files:**
- Create: `backend/prism/mcp/sql_backend.py`
- Test: `backend/tests/mcp/test_sql_backend.py`

**Interfaces:**
- Consumes: `SourceBackend`, models (Task 3); `SourceError`, `jsonable`, `has_dataset` (Task 2); `validate_select` (Task 4); `Metric`, `load_metrics`, `run_metric`, `resolve_time_range`, `ALLOWED_TABLES`, `DEFAULT_METRICS_DIR` (Task 5); `prism.db.session.ctx_from_claims`, `prepare_statements`; `prism.security.access.can`.
- **Amendment from the Task 4 red-team review (binding):** `query` must also bound result BYTES, not only rows: execute through a named server-side cursor (`conn.cursor(name=...)`, `itersize=25`) inside the scoped transaction, accumulate an estimate of the serialized size (`len(json.dumps(jsonable(row), default=str))`), stop fetching when it exceeds `MAX_RESULT_BYTES = 1_000_000` and return `truncated=True`; add `test_query_byte_budget` (`SELECT repeat('x', 400000) AS big FROM generate_series(1, 50)` returns <= 3 rows and `truncated is True`). (A named cursor rejects stacked statements on its own; keep `validate_select` first.)
- Produces: `SqlBackend(source: str, settings: Settings, metrics_dir: Path | None = None)` implementing `SourceBackend` (`kind = "sql"`, `name = source`). Also `SqlBackend.scoped(claims)` — a context manager yielding a pooled connection inside the scoped, read-only, `search_path = public` transaction (tests use it).

- [ ] **Step 1: Write the failing tests** — `backend/tests/mcp/test_sql_backend.py`

```python
import pytest

from prism.mcp.base import DescribeResult
from prism.mcp.metrics import MetricError
from prism.mcp.results import SourceError
from prism.mcp.sql_backend import SqlBackend
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
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/mcp/test_sql_backend.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.mcp.sql_backend'`.

- [ ] **Step 3: Implement `backend/prism/mcp/sql_backend.py`**

```python
"""SourceBackend for the Postgres-backed platforms. Every call runs in a READ ONLY transaction as bi_reader with
the caller's signed context (row-level security is the real boundary), with search_path pinned to public and a
statement timeout. Free-form SQL passes the AST guard and is executed with prepare=True (extended protocol), which
independently rejects stacked statements."""
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import psycopg
from psycopg_pool import ConnectionPool

from prism.config import Settings
from prism.db.session import ctx_from_claims, prepare_statements
from prism.mcp.base import DescribeResult, MetricResult, QueryResult
from prism.mcp.metrics import ALLOWED_TABLES, DEFAULT_METRICS_DIR, Metric, load_metrics, resolve_time_range, run_metric
from prism.mcp.results import SourceError, has_dataset, jsonable
from prism.mcp.sql_guard import validate_select
from prism.security.access import can


class SqlBackend:
    kind = "sql"

    def __init__(self, source: str, settings: Settings, metrics_dir: Path | None = None) -> None:
        if source not in ALLOWED_TABLES:
            raise ValueError(f"{source} is not a SQL source (expected one of {sorted(ALLOWED_TABLES)})")
        self.name = source
        self._settings = settings
        self._tables = ALLOWED_TABLES[source]
        self._metrics: dict[str, Metric] = {
            m.id: m for m in load_metrics(metrics_dir or DEFAULT_METRICS_DIR).values() if m.source == source
        }
        self._pool: ConnectionPool | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ infrastructure
    def _get_pool(self) -> ConnectionPool:
        with self._lock:
            if self._pool is None:
                pool = ConnectionPool(self._settings.dsn(self.name), min_size=1, max_size=8, open=False)
                pool.open(wait=True, timeout=10)
                self._pool = pool
            return self._pool

    @contextmanager
    def scoped(self, claims: dict):
        """Pooled connection inside the scoped transaction. SET LOCAL settings vanish at COMMIT/ROLLBACK, so a
        pooled connection can never carry one caller's context into the next call."""
        ctx = ctx_from_claims(claims, self._settings.ctx_hmac_key)
        with self._get_pool().connection() as conn:
            with conn.transaction():
                for statement, args in prepare_statements(ctx, self._settings.statement_timeout_ms):
                    conn.execute(statement, args)
                conn.execute("SET LOCAL search_path = public")  # the guard resolves bare names to public.<name>
                yield conn

    async def aclose(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await anyio.to_thread.run_sync(pool.close)

    @staticmethod
    def _translate(exc: psycopg.Error) -> SourceError:
        if isinstance(exc, psycopg.errors.QueryCanceled):
            return SourceError("query timed out")
        if isinstance(exc, psycopg.errors.InsufficientPrivilege):
            return SourceError("access denied by data policy")
        return SourceError(f"query failed: {exc.diag.message_primary or 'database error'}")

    def _entitled(self, claims: dict) -> None:
        if not has_dataset(claims, self.name):
            raise SourceError(f"not entitled to the {self.name} dataset")

    # ------------------------------------------------------------------ describe
    def _describe_sync(self, claims: dict) -> DescribeResult:
        base = dict(source=self.name, kind="sql", as_of=self._settings.as_of.isoformat())
        if not has_dataset(claims, self.name):
            return DescribeResult(**base, metrics=[], objects=[], notes=["no access to this dataset"])
        tables = sorted(t for t in self._tables if can(claims, self.name, t))
        metrics = [
            {"id": m.id, "description": m.description, "type": m.type, "unit": m.unit,
             "dimensions": sorted(m.dimensions), "filters": {k: f.type for k, f in sorted(m.filters.items())},
             "time_column": m.time_column}
            for m in sorted(self._metrics.values(), key=lambda m: m.id)
        ]
        columns: dict[str, list[dict[str, str]]] = {t: [] for t in tables}
        try:
            with self.scoped(claims) as conn:
                for name, col, typ in conn.execute(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = ANY(%s) ORDER BY table_name, ordinal_position",
                    (tables,),
                ):
                    columns[name].append({"name": col, "type": typ})
        except psycopg.Error as exc:
            raise self._translate(exc) from exc
        objects = [{"name": t, "kind": "table", "columns": columns[t]} for t in tables]
        return DescribeResult(**base, metrics=metrics, objects=objects,
                              notes=["Use run_metric for governed measures; query accepts a single SELECT."])

    async def describe(self, claims: dict) -> DescribeResult:
        return await anyio.to_thread.run_sync(self._describe_sync, claims)

    # ------------------------------------------------------------------ run_metric
    def _run_metric_sync(self, claims: dict, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                         time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult:
        metric = self._metrics.get(metric_id)
        if metric is None:
            raise SourceError(f"unknown metric {metric_id!r} on {self.name}; valid: {sorted(self._metrics)}")
        self._entitled(claims)
        as_of = self._settings.as_of
        window = None
        if time_range is not None and metric.time_column:
            lo, hi = resolve_time_range(time_range, as_of)
            window = {"from": lo.isoformat(), "to": hi.isoformat()}
        try:
            with self.scoped(claims) as conn:
                rows = run_metric(conn, metric, db_prefix=self._settings.db_prefix, dimensions=dimensions,
                                  filters=filters, time_range=time_range, as_of=as_of, order_by=order_by, limit=limit)
        except psycopg.Error as exc:
            raise self._translate(exc) from exc
        return MetricResult(source=self.name, metric_id=metric.id, unit=metric.unit, dimensions=dimensions,
                            rows=jsonable(rows), row_count=len(rows), as_of=as_of.isoformat(), window=window)

    async def run_metric(self, claims: dict, *, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                         time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult:
        return await anyio.to_thread.run_sync(self._run_metric_sync, claims, metric_id, dimensions, filters,
                                              time_range, order_by, limit)

    # ------------------------------------------------------------------ query
    def _query_sync(self, claims: dict, request: dict[str, Any]) -> QueryResult:
        sql = request.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise SourceError(f"query on {self.name} needs {{'sql': '<single SELECT>'}}")
        allowed = frozenset(f"public.{t}" for t in self._tables if can(claims, self.name, t))
        if not allowed:
            raise SourceError(f"not entitled to any table of {self.name}")
        max_rows = self._settings.mcp_query_max_rows
        safe_sql = validate_select(sql, allowed_tables=allowed, max_rows=max_rows)
        try:
            with self.scoped(claims) as conn, conn.cursor() as cur:
                cur.execute(safe_sql, prepare=True)  # extended protocol: stacked statements are a server error
                columns = [d.name for d in cur.description or []]
                rows = [list(r) for r in cur.fetchall()]
        except psycopg.Error as exc:
            raise self._translate(exc) from exc
        return QueryResult(source=self.name, columns=columns, rows=jsonable(rows), row_count=len(rows),
                           truncated=len(rows) >= max_rows)

    async def query(self, claims: dict, request: dict[str, Any]) -> QueryResult:
        return await anyio.to_thread.run_sync(self._query_sync, claims, request)
```

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/mcp/test_sql_backend.py -q` → Expected: all pass (about 22).

- [ ] **Step 5: Commit**

```bash
git add backend/prism/mcp/sql_backend.py backend/tests/mcp/test_sql_backend.py
git commit -m "feat(mcp): SQL source backend with RLS-scoped metrics and guarded queries

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: RestBackend (RefMaster / MarketMaster)

**Files:**
- Create: `backend/prism/mcp/rest/refmaster.yaml`, `backend/prism/mcp/rest/marketmaster.yaml`, `backend/prism/mcp/rest_backend.py`
- Test: `backend/tests/mcp/test_rest_backend.py`

**Interfaces:**
- Consumes: `SourceBackend`, models (Task 3); `SourceError`, `jsonable`, `has_dataset`, `forward_claims` (Task 2); `last_business_days`/`resolve_time_range` (Task 5); `prism.security.tokens.mint`, `prism.security.access.can`.
- Produces: `RestBackend(source: str, settings: Settings, *, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None)` implementing `SourceBackend` (`kind = "rest"`). YAML registry format (below) loaded by `load_rest_config(source) -> tuple[dict[str, Endpoint], dict[str, EndpointMetric]]`.

- [ ] **Step 1: Create the two registry files**

`backend/prism/mcp/rest/refmaster.yaml`:
```yaml
source: refmaster
endpoints:
  - id: securities
    path: /api/v1/securities
    table: securities
    result_key: items
    description: Security master (golden copy), filterable, paginated.
    params: {asset_class: {type: str}, ccy: {type: str}, country: {type: str}, status: {type: str}, q: {type: str}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: security
    path: /api/v1/securities/{security_id}
    table: securities
    result_key: null
    description: One security by id.
    params: {security_id: {type: str, location: path, required: true}}
  - id: entities
    path: /api/v1/entities
    table: legal_entities
    result_key: items
    description: Legal entities (LEI master).
    params: {country: {type: str}, sector: {type: str}, q: {type: str}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: accounts
    path: /api/v1/accounts
    table: accounts
    result_key: items
    description: Client accounts and their lifecycle state.
    params: {region: {type: str}, lifecycle_state: {type: str}, product_id: {type: str}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: corporate_actions
    path: /api/v1/corporate-actions
    table: corporate_actions
    result_key: items
    description: Corporate action events by ex-date.
    params: {security_id: {type: str}, event_type: {type: str}, status: {type: str}, from: {type: date}, to: {type: date}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: exceptions
    path: /api/v1/exceptions
    table: exceptions
    result_key: items
    description: Data-quality exceptions raised by validation rules.
    params: {domain: {type: str}, asset_class: {type: str}, status: {type: str}, from: {type: date}, to: {type: date}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: exceptions_summary
    path: /api/v1/exceptions/summary
    table: exceptions
    result_key: rows
    description: Exception counts grouped by domain, asset class, status or rule.
    params: {group_by: {type: str_list, enum: [domain, asset_class, status, rule_id]}, domain: {type: str}, asset_class: {type: str}, from: {type: date}, to: {type: date}}
  - id: data_dictionary
    path: /api/v1/data-dictionary
    table: data_dictionary
    result_key: items
    description: Business definitions, owners and lineage of governed attributes.
    params: {domain: {type: str}}
metrics:
  - id: open_dq_exceptions
    endpoint: exceptions_summary
    value_field: open_count
    description: Data-quality exceptions that are not closed.
    unit: exceptions
    dimensions: {domain: domain, asset_class: asset_class, status: status, rule_id: rule_id}
    filters: {domain: {param: domain, type: str}, asset_class: {param: asset_class, type: str}}
  - id: dq_exceptions_total
    endpoint: exceptions_summary
    value_field: total
    description: All data-quality exceptions in the window, open or closed.
    unit: exceptions
    dimensions: {domain: domain, asset_class: asset_class, status: status, rule_id: rule_id}
    filters: {domain: {param: domain, type: str}, asset_class: {param: asset_class, type: str}}
```

`backend/prism/mcp/rest/marketmaster.yaml`:
```yaml
source: marketmaster
endpoints:
  - id: vendors
    path: /api/v1/vendors
    table: vendors
    result_key: items
    description: Price vendors and their default source ranking.
    params: {}
  - id: instruments
    path: /api/v1/instruments
    table: instruments
    result_key: items
    description: Priced instruments.
    params: {asset_class: {type: str}, q: {type: str}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: timeseries
    path: /api/v1/instruments/{security_id}/timeseries
    table: golden_prices
    result_key: points
    description: Golden-price time series of one instrument.
    params: {security_id: {type: str, location: path, required: true}, from: {type: date}, to: {type: date}}
  - id: prices_conflicts
    path: /api/v1/prices/conflicts
    table: price_suspects
    result_key: items
    description: Vendor quotes that deviate from the golden price (conflicts).
    params: {vendor_id: {type: str}, asset_class: {type: str}, status: {type: str}, from: {type: date}, to: {type: date}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: prices_conflicts_summary
    path: /api/v1/prices/conflicts/summary
    table: price_suspects
    result_key: rows
    description: Conflict counts grouped by vendor, asset class, date or status.
    params: {group_by: {type: str_list, enum: [vendor_id, asset_class, price_date, status]}, from: {type: date}, to: {type: date}}
  - id: prices_suspects
    path: /api/v1/prices/suspects
    table: price_suspects
    result_key: items
    description: Price suspects (stale, spike, missing, conflict).
    params: {kind: {type: str}, vendor_id: {type: str}, asset_class: {type: str}, status: {type: str}, from: {type: date}, to: {type: date}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: golden_copy
    path: /api/v1/golden-copy/{security_id}
    table: golden_prices
    result_key: null
    description: Golden price of one instrument with the vendor quotes behind it.
    params: {security_id: {type: str, location: path, required: true}, date: {type: date}}
  - id: dq_metrics
    path: /api/v1/dq/metrics
    table: dq_stage_metrics
    result_key: items
    description: Data-quality funnel counts per pipeline stage.
    params: {domain: {type: str}, stage: {type: str}, from: {type: date}, to: {type: date}, limit: {type: int, min: 1, max: 500}, offset: {type: int, min: 0}}
  - id: esg
    path: /api/v1/esg/{entity_id}
    table: esg_scores
    result_key: items
    description: ESG scores of one entity.
    params: {entity_id: {type: str, location: path, required: true}}
metrics:
  - id: price_conflicts
    endpoint: prices_conflicts_summary
    value_field: conflicts
    description: Vendor price quotes deviating from the golden price (conflicts).
    unit: conflicts
    dimensions: {vendor_id: vendor_id, asset_class: asset_class, price_date: price_date, status: status}
    filters: {}
```

- [ ] **Step 2: Write the failing tests** — `backend/tests/mcp/test_rest_backend.py`

```python
import httpx
import pytest

from prism.mcp.rest_backend import RestBackend, load_rest_config
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
    assert [m["id"] for m in d.metrics] == ["price_conflicts"]
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

    claims = verify(captured["auth"].removeprefix("Bearer "), "refmaster-api", seeded.jwt_secret)
    assert claims["sub"] == "steward" and claims["aud"] == "refmaster-api" and claims["exp"] - claims["iat"] <= 60
```

- [ ] **Step 3: Run to verify failure**

Run: `cd backend && uv run pytest tests/mcp/test_rest_backend.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.mcp.rest_backend'`.

- [ ] **Step 4: Implement `backend/prism/mcp/rest_backend.py`**

```python
"""SourceBackend for the REST-fronted platforms. Each call re-mints a short-lived token for the downstream API
(audience `<source>-api`) from the caller's verified claims — the incoming token is never passed through — and lets
the API's own row-level security be the boundary. Endpoints and endpoint-backed metrics are declared in
rest/<source>.yaml; parameters are validated against that registry before any request is made."""
from datetime import date
from pathlib import Path
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict

from prism.config import Settings
from prism.mcp.base import DescribeResult, MetricResult, QueryResult
from prism.mcp.metrics import resolve_time_range
from prism.mcp.results import SourceError, forward_claims, has_dataset, jsonable
from prism.security.access import can
from prism.security.tokens import mint

REST_DIR = Path(__file__).parent / "rest"
TOKEN_TTL_S = 60
HTTP_TIMEOUT_S = 10.0


class Param(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["str", "int", "date", "str_list"] = "str"
    location: Literal["query", "path"] = "query"
    required: bool = False
    enum: list[str] | None = None
    min: int | None = None
    max: int | None = None


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    path: str
    table: str
    result_key: str | None
    description: str
    params: dict[str, Param]


class EndpointFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    param: str
    type: Literal["str", "int", "date"] = "str"


class EndpointMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    endpoint: str
    value_field: str
    description: str
    unit: str
    dimensions: dict[str, str]
    filters: dict[str, EndpointFilter]


def load_rest_config(source: str) -> tuple[dict[str, Endpoint], dict[str, EndpointMetric]]:
    raw = yaml.safe_load((REST_DIR / f"{source}.yaml").read_text())
    endpoints = {e["id"]: Endpoint.model_validate(e) for e in raw["endpoints"]}
    metrics = {m["id"]: EndpointMetric.model_validate(m) for m in raw["metrics"]}
    for m in metrics.values():
        if m.endpoint not in endpoints:
            raise ValueError(f"metric {m.id} references unknown endpoint {m.endpoint}")
    return endpoints, metrics


def _coerce(name: str, value: Any, p: Param) -> Any:
    if p.type == "str_list":
        if not isinstance(value, (list, tuple)) or not value:
            raise SourceError(f"parameter {name!r} needs a non-empty list")
        for v in value:
            if not isinstance(v, str) or (p.enum and v not in p.enum):
                raise SourceError(f"parameter {name!r} must be a list of {p.enum}; got {v!r}")
        return list(value)
    if p.type == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SourceError(f"parameter {name!r} must be an integer")
        if (p.min is not None and value < p.min) or (p.max is not None and value > p.max):
            raise SourceError(f"parameter {name!r} must be between {p.min} and {p.max}")
        return value
    if p.type == "date":
        try:
            return (value if isinstance(value, date) else date.fromisoformat(str(value))).isoformat()
        except ValueError:
            raise SourceError(f"parameter {name!r} must be an ISO date (YYYY-MM-DD)") from None
    if not isinstance(value, str) or len(value) > 200:
        raise SourceError(f"parameter {name!r} must be a string (<= 200 chars)")
    if p.enum and value not in p.enum:
        raise SourceError(f"parameter {name!r} must be one of {p.enum}")
    return value


class RestBackend:
    kind = "rest"

    def __init__(self, source: str, settings: Settings, *, base_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.name = source
        self._settings = settings
        self._endpoints, self._metrics = load_rest_config(source)
        url = base_url or getattr(settings, f"{source}_api_url")
        self._http = httpx.AsyncClient(base_url=url, timeout=HTTP_TIMEOUT_S, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ HTTP
    async def _get(self, claims: dict, ep: Endpoint, params: dict[str, Any]) -> Any:
        path = ep.path
        query: dict[str, Any] = {}
        for name, value in params.items():
            if ep.params[name].location == "path":
                path = path.replace("{" + name + "}", httpx.URL(f"/{value}").path.lstrip("/"))
            else:
                query[name] = value
        token = mint(forward_claims(claims), f"{self.name}-api", self._settings.jwt_secret, ttl_s=TOKEN_TTL_S)
        try:
            resp = await self._http.get(path, params=query, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise SourceError(f"{self.name} API unavailable ({type(exc).__name__})") from exc
        if resp.status_code == 200:
            return resp.json()
        detail = ""
        try:
            detail = str(resp.json().get("detail", ""))
        except ValueError:
            pass
        if resp.status_code in (401, 403):
            raise SourceError(f"{self.name} API denied the request (not entitled): {detail}".rstrip(": "))
        if resp.status_code == 404:
            raise SourceError(f"not found: {detail or ep.id}")
        if resp.status_code == 422:
            raise SourceError(f"invalid request: {detail}")
        raise SourceError(f"{self.name} API error ({resp.status_code})")

    def _validate(self, ep: Endpoint, params: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(set(params) - set(ep.params))
        if unknown:
            raise SourceError(f"unknown parameter(s) {unknown} for {ep.id}; valid: {sorted(ep.params)}")
        missing = [n for n, p in ep.params.items() if p.required and n not in params]
        if missing:
            raise SourceError(f"missing required parameter(s) {missing} for {ep.id}")
        return {n: _coerce(n, v, ep.params[n]) for n, v in params.items()}

    def _entitled(self, claims: dict, ep: Endpoint) -> None:
        if not can(claims, self.name, ep.table):
            raise SourceError(f"not entitled to {self.name}.{ep.table}")

    # ------------------------------------------------------------------ describe
    async def describe(self, claims: dict) -> DescribeResult:
        base = dict(source=self.name, kind="rest", as_of=self._settings.as_of.isoformat())
        if not has_dataset(claims, self.name):
            return DescribeResult(**base, metrics=[], objects=[], notes=["no access to this dataset"])
        allowed = {i: e for i, e in self._endpoints.items() if can(claims, self.name, e.table)}
        metrics = [
            {"id": m.id, "description": m.description, "type": "endpoint", "unit": m.unit,
             "dimensions": sorted(m.dimensions), "filters": {k: f.type for k, f in sorted(m.filters.items())},
             "time_column": "from/to"}
            for m in sorted(self._metrics.values(), key=lambda m: m.id) if m.endpoint in allowed
        ]
        objects = [{"name": e.id, "kind": "endpoint", "description": e.description,
                    "params": {n: {"type": p.type, "required": p.required, **({"enum": p.enum} if p.enum else {})}
                               for n, p in e.params.items()}}
                   for e in sorted(allowed.values(), key=lambda e: e.id)]
        return DescribeResult(**base, metrics=metrics, objects=objects,
                              notes=["Use run_metric for governed measures; query accepts {'endpoint_id', 'params'}."])

    # ------------------------------------------------------------------ run_metric
    async def run_metric(self, claims: dict, *, metric_id: str, dimensions: list[str], filters: dict[str, Any],
                         time_range: dict[str, Any] | None, order_by: str | None, limit: int) -> MetricResult:
        m = self._metrics.get(metric_id)
        if m is None:
            raise SourceError(f"unknown metric {metric_id!r} on {self.name}; valid: {sorted(self._metrics)}")
        ep = self._endpoints[m.endpoint]
        self._entitled(claims, ep)
        unknown = [d for d in dimensions if d not in m.dimensions]
        if unknown:
            raise SourceError(f"unknown dimension(s) {unknown} for {m.id}; valid: {sorted(m.dimensions)}")
        params: dict[str, Any] = {}
        if dimensions:
            params["group_by"] = [m.dimensions[d] for d in dimensions]
        for name, value in filters.items():
            f = m.filters.get(name)
            if f is None:
                raise SourceError(f"unknown filter {name!r} for {m.id}; valid: {sorted(m.filters)}")
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise SourceError(f"filter {name!r} on {self.name} metrics accepts a single value")
            params[f.param] = value
        window = None
        if time_range is not None:
            lo, hi = resolve_time_range(time_range, self._settings.as_of)
            params["from"], params["to"] = lo.isoformat(), hi.isoformat()
            window = {"from": params["from"], "to": params["to"]}
        data = await self._get(claims, ep, self._validate(ep, params))
        rows = data[ep.result_key or "rows"]
        if dimensions:
            out = [{**{d: r[m.dimensions[d]] for d in dimensions}, "value": r[m.value_field]} for r in rows]
        else:
            out = [{"value": sum(r[m.value_field] for r in rows)}]
        out = self._order(out, order_by, dimensions)[:limit]
        return MetricResult(source=self.name, metric_id=m.id, unit=m.unit, dimensions=dimensions,
                            rows=jsonable(out), row_count=len(out), as_of=self._settings.as_of.isoformat(),
                            window=window)

    @staticmethod
    def _order(rows: list[dict], order_by: str | None, dims: list[str]) -> list[dict]:
        key = order_by or "metric_desc"
        if key in ("metric", "metric_desc"):
            return sorted(rows, key=lambda r: (r["value"] is None, r["value"] if key == "metric" else -(r["value"] or 0),
                                               *[str(r.get(d)) for d in dims]))
        desc = key.startswith("-")
        name = key.lstrip("-")
        if name not in dims:
            raise SourceError(f"unknown order_by {order_by!r}; valid: metric, metric_desc, {dims}, -{dims}")
        return sorted(rows, key=lambda r: str(r[name]), reverse=desc)

    # ------------------------------------------------------------------ query
    async def query(self, claims: dict, request: dict[str, Any]) -> QueryResult:
        endpoint_id = request.get("endpoint_id")
        if not isinstance(endpoint_id, str):
            raise SourceError(f"query on {self.name} needs {{'endpoint_id': ..., 'params': {{...}}}}; "
                              f"valid endpoint ids: {sorted(self._endpoints)}")
        ep = self._endpoints.get(endpoint_id)
        if ep is None:
            raise SourceError(f"unknown endpoint {endpoint_id!r} on {self.name}; valid: {sorted(self._endpoints)}")
        self._entitled(claims, ep)
        params = request.get("params") or {}
        if not isinstance(params, dict):
            raise SourceError("'params' must be an object")
        data = await self._get(claims, ep, self._validate(ep, params))
        rows = data[ep.result_key] if ep.result_key else [data]
        columns = list(rows[0]) if rows else []
        table = [[r.get(c) for c in columns] for r in rows]
        return QueryResult(source=self.name, columns=columns, rows=jsonable(table), row_count=len(table),
                           truncated=bool(params.get("limit")) and len(table) >= params["limit"])
```

- [ ] **Step 5: Run tests**

Run: `cd backend && uv run pytest tests/mcp/test_rest_backend.py -q` → Expected: all pass (about 12).

- [ ] **Step 6: Commit**

```bash
git add backend/prism/mcp/rest backend/prism/mcp/rest_backend.py backend/tests/mcp/test_rest_backend.py
git commit -m "feat(mcp): REST source backend with endpoint registry and on-behalf-of tokens

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 9: The five servers and end-to-end tests

**Files:**
- Create: `backend/prism/mcp/servers.py`
- Test: `backend/tests/mcp/test_servers_e2e.py`

**Interfaces:**
- Consumes: `build_source_app` (Task 3), `SqlBackend` (Task 7), `RestBackend` (Task 8), `Settings`.
- Produces: `servers.SOURCES: tuple[str, ...]` (`refmaster, marketmaster, cashrecon, assetrecon, feedhub`), `servers.MCP_PORTS: dict[str, int]`, `servers.create_backend(source, settings, *, transport=None) -> SourceBackend`, `servers.create_app(source, settings=None, *, transport=None) -> tuple[MCPServer, Starlette]` (the Starlette lifespan also calls `backend.aclose()`), and no-argument uvicorn factories `refmaster_app()`, `marketmaster_app()`, `cashrecon_app()`, `assetrecon_app()`, `feedhub_app()` returning the ASGI app.

- [ ] **Step 1: Write the failing tests** — `backend/tests/mcp/test_servers_e2e.py`

```python
import asyncio
import contextlib

import httpx
import httpx2
import pytest

from prism.mcp.client import mcp_client
from prism.mcp.servers import MCP_PORTS, SOURCES, create_app, cashrecon_app
from prism.sources.marketmaster_api.app import create_app as create_marketmaster_api
from prism.sources.refmaster_api.app import create_app as create_refmaster_api

pytestmark = pytest.mark.db


def url(source):
    return f"http://127.0.0.1:{MCP_PORTS[source]}/mcp"


@contextlib.asynccontextmanager
async def serving(seeded, source):
    """In-process MCP server; REST sources talk to in-process copies of the mock APIs."""
    api = transport = None
    if source == "refmaster":
        api = create_refmaster_api(seeded)
    elif source == "marketmaster":
        api = create_marketmaster_api(seeded)
    if api is not None:
        transport = httpx.ASGITransport(app=api)
    mcp, app = create_app(source, seeded, transport=transport)
    try:
        async with mcp.session_manager.run():
            yield app
    finally:
        # ASGITransport skips the lifespan, so close the backend and API pools explicitly
        await mcp._prism_backend.aclose()
        if api is not None:
            await api.state.dbs.close()


async def call(seeded, source, persona, tool, args, mcp_token):
    async with serving(seeded, source) as app:
        async with mcp_client(url(source), mcp_token(seeded, persona, source), asgi_app=app) as c:
            return await c.call_tool(tool, args)


def test_ports_and_sources_are_fixed():
    assert SOURCES == ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
    assert MCP_PORTS == {"refmaster": 8201, "marketmaster": 8202, "cashrecon": 8203, "assetrecon": 8204,
                         "feedhub": 8205}


async def test_every_source_exposes_the_same_contract_and_a_public_healthz(seeded, mcp_token):
    for source in SOURCES:
        async with serving(seeded, source) as app:
            async with mcp_client(url(source), mcp_token(seeded, "head_data", source), asgi_app=app) as c:
                tools = {t.name for t in (await c.list_tools()).tools}
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                            base_url=f"http://127.0.0.1:{MCP_PORTS[source]}") as h:
                health = await h.get("/healthz")
        assert tools == {"describe", "run_metric", "query"}, source
        assert health.json() == {"status": "ok", "source": source}


async def test_describe_shows_metrics_per_source(seeded, mcp_token):
    for source, expect in {"cashrecon": "open_breaks", "assetrecon": "position_exceptions",
                           "feedhub": "late_feeds", "refmaster": "open_dq_exceptions",
                           "marketmaster": "price_conflicts"}.items():
        r = await call(seeded, source, "head_data", "describe", {}, mcp_token)
        assert not r.is_error, (source, r.content)
        assert expect in {m["id"] for m in r.structured_content["metrics"]}, source


async def test_cashrecon_rls_and_masking_over_mcp(seeded, mcp_token):
    head = await call(seeded, "cashrecon", "head_data", "run_metric",
                      {"metric_id": "open_breaks", "dimensions": ["region"]}, mcp_token)
    emea = await call(seeded, "cashrecon", "cash_ops_emea", "run_metric",
                      {"metric_id": "open_breaks", "dimensions": ["region"]}, mcp_token)
    assert len(head.structured_content["rows"]) >= 2
    assert {r["region"] for r in emea.structured_content["rows"]} == {"EMEA"}
    masked = await call(seeded, "cashrecon", "cash_ops_emea", "query",
                        {"request": {"sql": "SELECT nostro_no FROM cash_accounts LIMIT 3"}}, mcp_token)
    assert masked.structured_content["rows"] and all(r[0].startswith("****") for r in masked.structured_content["rows"])


async def test_query_rejections_are_clean_tool_errors(seeded, mcp_token):
    for sql in ("SELECT * FROM private.cash_accounts", "SELECT 1; SELECT 2", "SELECT set_config('a','b',false)"):
        r = await call(seeded, "cashrecon", "head_data", "query", {"request": {"sql": sql}}, mcp_token)
        assert r.is_error and r.content[0].text, sql
        assert "Traceback" not in r.content[0].text


async def test_metrics_only_persona_can_run_metrics_but_not_query(seeded, mcp_token):
    ok = await call(seeded, "cashrecon", "bi_analyst", "run_metric", {"metric_id": "open_breaks"}, mcp_token)
    no = await call(seeded, "cashrecon", "bi_analyst", "query", {"request": {"sql": "SELECT 1"}}, mcp_token)
    assert not ok.is_error and no.is_error and "metrics-only" in no.content[0].text


async def test_dataset_entitlement_is_an_explicit_error(seeded, mcp_token):
    r = await call(seeded, "assetrecon", "steward", "run_metric", {"metric_id": "position_exceptions"}, mcp_token)
    assert r.is_error and "not entitled" in r.content[0].text
    r = await call(seeded, "refmaster", "cash_ops_emea", "run_metric", {"metric_id": "open_dq_exceptions"}, mcp_token)
    assert r.is_error


async def test_the_planted_stories_are_reachable_over_mcp(seeded, mcp_token):
    heat = await call(seeded, "marketmaster", "steward", "run_metric",
                      {"metric_id": "price_conflicts", "dimensions": ["vendor_id", "asset_class"],
                       "time_range": {"last_business_days": 5}}, mcp_token)
    top = heat.structured_content["rows"][0]
    assert (top["vendor_id"], top["asset_class"]) == ("V_A", "Corp bond")
    late = await call(seeded, "feedhub", "head_data", "run_metric",
                      {"metric_id": "late_feeds", "dimensions": ["source_id"],
                       "time_range": {"last_business_days": 6}}, mcp_token)
    assert late.structured_content["rows"][0]["source_id"] == "SRC001"
    nav = await call(seeded, "assetrecon", "head_data", "run_metric",
                     {"metric_id": "nav_break_bps_max", "dimensions": ["portfolio_id"],
                      "time_range": {"last_business_days": 3}, "limit": 2}, mcp_token)
    assert {r["portfolio_id"] for r in nav.structured_content["rows"]} == {"PF003", "PF009"}


async def test_token_for_another_source_is_401(seeded, mcp_token):
    tok = mcp_token(seeded, "head_data", "cashrecon")
    async with serving(seeded, "assetrecon") as app:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                        base_url=f"http://127.0.0.1:{MCP_PORTS['assetrecon']}") as h:
            r = await h.post("/mcp", headers={"Authorization": f"Bearer {tok}",
                                              "Accept": "application/json, text/event-stream",
                                              "Content-Type": "application/json"},
                             json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                   "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "0"}}})
    assert r.status_code == 401


async def test_concurrent_personas_are_isolated(seeded, mcp_token):
    async with serving(seeded, "cashrecon") as app:

        async def one(persona):
            async with mcp_client(url("cashrecon"), mcp_token(seeded, persona, "cashrecon"), asgi_app=app) as c:
                r = await c.call_tool("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"]})
                return persona, {x["region"] for x in r.structured_content["rows"]}

        results = await asyncio.wait_for(asyncio.gather(*(one(p) for p in ["head_data", "cash_ops_emea"] * 8)), 120)
    for persona, regions in results:
        assert (regions == {"EMEA"}) if persona == "cash_ops_emea" else (len(regions) >= 2)


def test_uvicorn_factory_returns_an_asgi_app():
    assert callable(cashrecon_app())
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/mcp/test_servers_e2e.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.mcp.servers'`.

- [ ] **Step 3: Implement `backend/prism/mcp/servers.py`**

```python
"""The five source MCP servers (ports 8201-8205), each fronting one simulated platform."""
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette

from prism.config import Settings
from prism.mcp.base import SourceBackend, build_source_app
from prism.mcp.rest_backend import RestBackend
from prism.mcp.sql_backend import SqlBackend

SOURCES = ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
MCP_PORTS = {"refmaster": 8201, "marketmaster": 8202, "cashrecon": 8203, "assetrecon": 8204, "feedhub": 8205}
REST_SOURCES = frozenset({"refmaster", "marketmaster"})


def create_backend(source: str, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> SourceBackend:
    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}; valid: {list(SOURCES)}")
    if source in REST_SOURCES:
        return RestBackend(source, settings, transport=transport)
    return SqlBackend(source, settings)


def create_app(source: str, settings: Settings | None = None, *,
               transport: httpx.AsyncBaseTransport | None = None) -> tuple[MCPServer, Starlette]:
    settings = settings or Settings()
    backend = create_backend(source, settings, transport=transport)
    mcp, app = build_source_app(backend, settings)
    mcp._prism_backend = backend  # tests close it explicitly (ASGITransport does not run the lifespan)

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette):
        async with inner(a):
            yield
        await backend.aclose()

    app.router.lifespan_context = lifespan
    return mcp, app


def _factory(source: str):
    def make() -> Starlette:  # uvicorn --factory
        return create_app(source)[1]

    make.__name__ = f"{source}_app"
    return make


refmaster_app = _factory("refmaster")
marketmaster_app = _factory("marketmaster")
cashrecon_app = _factory("cashrecon")
assetrecon_app = _factory("assetrecon")
feedhub_app = _factory("feedhub")
```

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/mcp/test_servers_e2e.py -q` → Expected: all pass. Then the whole MCP suite and the full suite: `cd backend && uv run pytest tests/mcp -q && uv run pytest -q` → everything green, output pristine.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/mcp/servers.py backend/tests/mcp/test_servers_e2e.py
git commit -m "feat(mcp): five source MCP servers with end-to-end tests

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 10: CLI, process wiring, README and live verification

**Files:**
- Create: `backend/prism/mcp/cli.py`
- Modify: `backend/Procfile`, `scripts/start_backend.sh`, `README.md`, `Makefile`
- Test: `backend/tests/mcp/test_cli.py`

**Interfaces:**
- Consumes: `mcp_client` (Task 2), `MCP_PORTS`, `SOURCES` (Task 9), `claims_for`, `mint`.
- Produces: `python -m prism.mcp.cli list <source> [--as PERSONA]` and `python -m prism.mcp.cli call <source> <tool> [--as PERSONA] [--args JSON]` (exit code 0 on success, 1 on a tool error, 2 on connection/usage errors; prints the structured content as JSON on stdout).

- [ ] **Step 1: Write the failing test** — `backend/tests/mcp/test_cli.py`

```python
import json
import subprocess
import sys

import pytest


def run(*args):
    return subprocess.run([sys.executable, "-m", "prism.mcp.cli", *args], capture_output=True, text=True, timeout=60)


def test_usage_errors_exit_2():
    assert run().returncode == 2
    r = run("call", "nosuchsource", "describe")
    assert r.returncode == 2 and "unknown source" in r.stderr


def test_unreachable_server_exits_2_with_a_clear_message():
    r = run("call", "cashrecon", "describe", "--as", "head_data", "--url", "http://127.0.0.1:9/mcp")
    assert r.returncode == 2 and "could not reach" in r.stderr


def test_args_must_be_json_object():
    r = run("call", "cashrecon", "describe", "--args", "[1]")
    assert r.returncode == 2 and "JSON object" in r.stderr
```

- [ ] **Step 2: Run to verify failure** — `cd backend && uv run pytest tests/mcp/test_cli.py -q` → FAIL (`No module named prism.mcp.cli`, return code 1).

- [ ] **Step 3: Implement `backend/prism/mcp/cli.py`**

```python
"""Manual smoke tool for the source MCP servers.

  uv run python -m prism.mcp.cli list cashrecon --as head_data
  uv run python -m prism.mcp.cli call cashrecon run_metric --as cash_ops_emea \
      --args '{"metric_id": "open_breaks", "dimensions": ["region"]}'
"""
import argparse
import asyncio
import json
import sys

from prism.config import Settings
from prism.mcp.client import mcp_client
from prism.mcp.servers import MCP_PORTS, SOURCES
from prism.security.personas import PERSONAS, claims_for
from prism.security.tokens import mint


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    url = args.url or f"http://127.0.0.1:{MCP_PORTS[args.source]}/mcp"
    token = mint(claims_for(args.persona), f"{args.source}-mcp", settings.jwt_secret, ttl_s=300)
    try:
        async with mcp_client(url, token, timeout_s=10) as client:
            if args.command == "list":
                tools = (await client.list_tools()).tools
                print(json.dumps([{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                                  for t in tools], indent=2))
                return 0
            result = await client.call_tool(args.tool, json.loads(args.args))
    except BaseException as exc:  # the SDK raises nested ExceptionGroups for connection/auth failures
        if isinstance(exc, KeyboardInterrupt):
            raise
        print(f"could not reach {url} or the call failed at transport level: {type(exc).__name__}", file=sys.stderr)
        return 2
    if result.is_error:
        print(result.content[0].text if result.content else "tool error", file=sys.stderr)
        return 1
    print(json.dumps(result.structured_content, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prism.mcp.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("list", "call"):
        p = sub.add_parser(name)
        p.add_argument("source")
        if name == "call":
            p.add_argument("tool")
            p.add_argument("--args", default="{}")
        p.add_argument("--as", dest="persona", default="head_data", choices=sorted(PERSONAS))
        p.add_argument("--url", default=None, help="override the server URL (default: localhost:<port>/mcp)")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    if args.source not in SOURCES:
        print(f"unknown source {args.source!r}; valid: {list(SOURCES)}", file=sys.stderr)
        return 2
    if args.command == "call":
        try:
            parsed = json.loads(args.args)
        except json.JSONDecodeError:
            parsed = None
        if not isinstance(parsed, dict):
            print("--args must be a JSON object", file=sys.stderr)
            return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Update process wiring**

`backend/Procfile` — append:

```
mcp_refmaster: uvicorn prism.mcp.servers:refmaster_app --factory --host 127.0.0.1 --port 8201
mcp_marketmaster: uvicorn prism.mcp.servers:marketmaster_app --factory --host 127.0.0.1 --port 8202
mcp_cashrecon: uvicorn prism.mcp.servers:cashrecon_app --factory --host 127.0.0.1 --port 8203
mcp_assetrecon: uvicorn prism.mcp.servers:assetrecon_app --factory --host 127.0.0.1 --port 8204
mcp_feedhub: uvicorn prism.mcp.servers:feedhub_app --factory --host 127.0.0.1 --port 8205
```

`scripts/start_backend.sh` — change the port-busy loop to check every service port and extend the printed URLs. Replace `for port in 8101 8102; do` with `for port in 8101 8102 8201 8202 8203 8204 8205; do`, and replace the two `echo "  ... API ..."` lines above the final `exec` with:

```bash
echo "  RefMaster API      http://127.0.0.1:8101/docs"
echo "  MarketMaster API   http://127.0.0.1:8102/docs"
echo "  MCP servers        http://127.0.0.1:8201..8205/mcp  (refmaster, marketmaster, cashrecon, assetrecon, feedhub)"
echo "  smoke test:        (cd backend && uv run python -m prism.mcp.cli list cashrecon --as head_data)"
```

`Makefile` — add a target after `test-fast`:

```make
test-mcp: db
> cd backend && uv run pytest -q tests/mcp
```

(and add `test-mcp` to the `.PHONY` line).

`README.md` — add a section after "Try the APIs":

````markdown
## Source MCP servers
Every platform has an MCP server (ports 8201-8205) with the same three tools: `describe`, `run_metric` (governed
metrics, preferred) and `query` (guarded free-form access; not available to the metrics-only persona). Tokens are
audience-bound: a token for `cashrecon-mcp` is rejected by every other server.
```bash
cd backend
uv run python -m prism.mcp.cli list cashrecon --as head_data
uv run python -m prism.mcp.cli call cashrecon run_metric --as cash_ops_emea \
  --args '{"metric_id": "open_breaks", "dimensions": ["region"]}'
uv run python -m prism.mcp.cli call marketmaster run_metric --as steward \
  --args '{"metric_id": "price_conflicts", "dimensions": ["vendor_id", "asset_class"], "time_range": {"last_business_days": 5}}'
```
Personas: `steward`, `cash_ops_emea`, `invest_ops_growth`, `bi_analyst`, `head_data`.
````

- [ ] **Step 5: Run tests**

Run: `cd backend && uv run pytest tests/mcp/test_cli.py -q` → Expected: `3 passed`. Then `bash -n scripts/start_backend.sh` (must print nothing).

- [ ] **Step 6: Live verification (required; this is the acceptance test)**

1. Run `scripts/start_backend.sh` in the background with output to a log file; wait until all seven ports answer (`curl -s http://127.0.0.1:8203/healthz` → `{"status":"ok","source":"cashrecon"}` for each of 8201–8205).
2. `cd backend && uv run python -m prism.mcp.cli list cashrecon --as head_data` → prints the three tools.
3. `call cashrecon run_metric --as head_data --args '{"metric_id":"aged_open_breaks","dimensions":["legal_entity_id"],"filters":{"ccy":"USD"}}'` → first row `LE00016` (≈81% of the total) on the full dev data; `--as cash_ops_emea` with `open_breaks` by `region` → only `EMEA`.
4. `call cashrecon query --as head_data --args '{"request":{"sql":"SELECT * FROM private.cash_accounts"}}'` → exit code 1 and "table not allowed"; `--as bi_analyst` with any `query` → exit 1 and "metrics-only".
5. `call marketmaster run_metric --as steward` (the heat-map command above) → first row `V_A` / `Corp bond`; `call feedhub run_metric --as head_data --args '{"metric_id":"late_feeds","dimensions":["source_id"],"time_range":{"last_business_days":6}}'` → first row `SRC001`; `call assetrecon run_metric --as head_data --args '{"metric_id":"nav_break_bps_max","dimensions":["portfolio_id"],"time_range":{"last_business_days":3},"limit":2}'` → `PF003` and `PF009`.
6. `call refmaster run_metric --as cash_ops_emea --args '{"metric_id":"open_dq_exceptions"}'` → exit 1 with a "not entitled/denied" message.
7. A second `scripts/start_backend.sh` → `ERROR: port 8101 is already in use`, exit 1. Then stop the first run cleanly (kill the honcho/uvicorn processes you started) and confirm `lsof -iTCP:8101,8102,8201-8205 -sTCP:LISTEN` prints nothing.
8. Run the full suite `cd backend && uv run pytest -q`.

Paste the outputs (redact secrets) into the task report.

- [ ] **Step 7: Commit**

```bash
git add backend/prism/mcp/cli.py backend/tests/mcp/test_cli.py backend/Procfile scripts/start_backend.sh Makefile README.md
git commit -m "feat(mcp): smoke CLI, process wiring and docs for the source MCP servers

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review notes

- **Spec coverage:** §4.3 uniform contract → Tasks 3, 7, 8, 9; §4.3 `run_metric`/`query`/`describe` semantics → Tasks 5–8; §5 identity (audience-bound tokens, on-behalf-of, no identity in arguments, RLS at source, masking, metrics-only) → Tasks 2, 3, 7, 8, 9; §6 governed metrics before free SQL, no raw rows to the model (rows stay behind MCP; text is a summary) → Tasks 3, 5, 6; §2 exposure split (EDMs REST + MCP, others Postgres + MCP) → Tasks 7–9; Plan 1 carry-forward item 1 (`metrics_only`) → enforced at the source in Task 3 (defence in depth; the Plan 3 gateway still enforces it), items 3–4 (business-day definition, vocabulary) → Plan 3 glossary.
- **Deferred to Plan 3 (context graph + Semantic Gateway):** `describe()` output is consumed by the graph loader; the gateway mints `<source>-mcp` tokens on behalf of the user; result store, DuckDB combine, audit table, `app` DB idempotent migrations, Neo4j in compose.
- **Known limitations:** MCP servers open a fresh REST/SQL call per tool call (no result cache); SQL pools are sync psycopg pools driven through a thread pool; metric YAML fragments are trusted, human-reviewed code (the loader's lexical checks are defence in depth, not a parser); `query` on REST sources exposes only the registry's endpoints.
