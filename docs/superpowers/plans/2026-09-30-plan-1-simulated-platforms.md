# Plan 1 — Simulated Platforms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the five simulated enterprise data platforms with realistic, deterministic, story-bearing data. The platforms are:
- **RefMaster** and **MarketMaster**: EDMs, exposed as mock REST APIs.
- **CashRecon**, **AssetRecon** and **FeedHub**: Postgres databases.

Row-level security is enforced in Postgres for 5 demo personas, and one script starts it all.

**Architecture:**
- **Generator.** A seeded Python generator builds one shared "universe" (entities, securities, prices, custodians, portfolios, cash accounts, planted demo stories). It then projects the universe into five per-platform table sets.
- **Storage.** A migration creates one Postgres database per platform. Security is enforced there:
  - Every table has forced RLS.
  - Policies read a **signed, HMAC-verified security context** set per transaction.
  - A non-bypass role (`bi_reader`) is assumed via `SET LOCAL ROLE`.
- **EDM APIs.** The two EDM mock REST APIs (FastAPI) verify an audience-bound JWT and translate its claims into that same signed context. RLS is therefore the single enforcement point.

**Tech Stack:** Python 3.12, uv, Postgres 16 (Docker), psycopg 3 + psycopg-pool, FastAPI + uvicorn, PyJWT, pydantic-settings, pytest + pytest-asyncio + httpx, honcho.

**Spec:** `docs/superpowers/specs/2026-09-30-agentic-data-intelligence-design.md` (§2 platforms & simulation, §5 security, §9 layout). Read it before starting.

**Roadmap (later plans, written after this one lands):**
- Plan 2: source MCP servers + context graph (Neo4j) + Semantic Gateway MCP.
- Plan 3: agent service (supervisor, subagents, FastAPI `/chat` SSE).
- Plan 4: Next.js UI + `start_frontend.sh`.
- Plan 5: golden-question and red-team evals, hardening.

## Global Constraints

- **No client, vendor or real-product names anywhere:** code, data, UI strings, docs, commit messages. Price vendors are `Vendor A`…`Vendor F` (`V_A`…`V_F`), ESG providers are `ESG Provider 1/2`, and custodian/bank names are fictional compounds.
- Python ≥ 3.12, managed with **uv**. Postgres **16** in Docker on host port **5433** (container 5432).
- **Determinism:** seed `42`, as-of date **2026-09-30**. "This week" = the last 5 business days ending on the as-of date; "previous week" = the 5 business days before that.
- Services connect as `prism_svc` and query only after `SET LOCAL ROLE bi_reader`. Neither role is superuser, and both are `NOBYPASSRLS`. Every data table has `ENABLE` + `FORCE ROW LEVEL SECURITY`.
- Identity and entitlements come **only** from a verified JWT or signed context, never from request parameters.
- All data access is **read-only**: a `READ ONLY` transaction and SELECT-only policies. The statement timeout defaults to 5000 ms.
- Ports:
  - This plan: 8101 (RefMaster API), 8102 (MarketMaster API).
  - Reserved for later plans: 8000, 8200–8205, 3000.
- FIGI is intentionally omitted: its common prefix is a vendor brand.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Security keys regenerated after seeding.** If `.env` is recreated, the context key no longer matches the one stored in the databases. The expected behaviour is that `start_backend.sh` detects this and re-seeds, rather than every query failing with "invalid security context". Test: `test_is_seeded_false_when_ctx_key_changes` (Task 10).
2. **Search text containing `%`, `_` or `'`.** It must be matched literally and return 200, with no SQL error and no wildcard blow-up. Test: `test_search_text_with_sql_metacharacters_is_literal` (Task 12).
3. **Inverted date ranges** (`from` after `to`) → 422 with a clear message, not an empty 200. Tests: Task 12 and Task 13.
4. **Expired or wrong-audience tokens** → 401, never 500 or data. Tests: Task 12.
5. **A persona without a dataset entitlement** calling an endpoint → explicit 403, not an empty 200 that looks like "no data". Tests: Task 12 and Task 13.

---

## File Structure

```
.gitignore
.env.example
docker-compose.yml
Makefile
README.md
scripts/start_backend.sh
backend/pyproject.toml
backend/Procfile
backend/prism/__init__.py
backend/prism/config.py                 Settings (env PRISM_*), db naming, DSNs
backend/prism/sim/__init__.py
backend/prism/sim/ids.py                ISIN/CUSIP/SEDOL/LEI/BIC generators + check digits
backend/prism/sim/calendar.py           business days, UTC timestamp helper
backend/prism/sim/model.py              TableData, id_sequence
backend/prism/sim/universe.py           SimConfig, domain dataclasses, Stories, build_universe
backend/prism/sim/keys.py               cross-system feed/delivery id conventions
backend/prism/sim/project_refmaster.py
backend/prism/sim/project_marketmaster.py
backend/prism/sim/project_cashrecon.py
backend/prism/sim/project_assetrecon.py
backend/prism/sim/project_feedhub.py
backend/prism/sim/writer.py             COPY TableData into Postgres
backend/prism/sim/seed.py               PROJECTORS, seed_all, is_seeded
backend/prism/sim/cli.py                prism-seed
backend/prism/db/__init__.py
backend/prism/db/ddl/security.sql       prism_sec schema: signed-context verification functions
backend/prism/db/ddl/{refmaster,marketmaster,cashrecon,assetrecon,feedhub,app}.sql
backend/prism/db/policies.py            ROW_SCOPES, physical_name, policy_statements
backend/prism/db/migrate.py             roles, databases, DDL, policies, grants
backend/prism/db/session.py             sign_ctx, ctx_from_claims, prepare_statements, scoped_sync
backend/prism/security/__init__.py
backend/prism/security/personas.py      5 demo personas, claims_for
backend/prism/security/tokens.py        mint/verify audience-bound JWTs
backend/prism/security/access.py        can() — python mirror of the SQL dataset check
backend/prism/security/cli.py           prism-token
backend/prism/sources/__init__.py
backend/prism/sources/common.py         Databases pool registry, auth, where/select, fetch
backend/prism/sources/refmaster_api/__init__.py
backend/prism/sources/refmaster_api/app.py
backend/prism/sources/marketmaster_api/__init__.py
backend/prism/sources/marketmaster_api/app.py
backend/tests/conftest.py               `seeded` session fixture (test_ prefixed DBs, small profile)
backend/tests/test_config.py
backend/tests/sim/conftest.py           `universe` fixture
backend/tests/sim/test_ids.py
backend/tests/sim/test_universe.py
backend/tests/sim/test_refmaster_projection.py
backend/tests/sim/test_marketmaster_projection.py
backend/tests/sim/test_cashrecon_projection.py
backend/tests/sim/test_assetrecon_projection.py
backend/tests/sim/test_feedhub_projection.py
backend/tests/db/test_migrate.py
backend/tests/db/test_seed.py
backend/tests/db/test_rbac.py
backend/tests/security/test_tokens.py
backend/tests/api/conftest.py
backend/tests/api/test_refmaster_api.py
backend/tests/api/test_marketmaster_api.py
```

All commands below run from the repo root unless a `cd` is shown. Backend commands run in `backend/` via `uv run`.

---

### Task 1: Repository scaffold, configuration and Postgres container

**Files:**
- Create: `.gitignore`, `.env.example`, `docker-compose.yml`, `Makefile`, `backend/pyproject.toml`, `backend/prism/__init__.py`, `backend/prism/config.py`
- Test: `backend/tests/test_config.py`

**Interfaces:**
- Produces:
  - `prism.config.Settings`, with fields `pg_host`, `pg_port`, `pg_admin_user`, `pg_admin_password`, `pg_svc_user`, `pg_svc_password`, `db_prefix`, `ctx_hmac_key`, `jwt_secret`, `as_of: date`, `seed: int` and `statement_timeout_ms: int`.
  - Methods `Settings.dbname(logical: str) -> str` and `Settings.dsn(logical: str, admin: bool = False) -> str`.
  - Constants `LOGICAL_DBS = ("refmaster","marketmaster","cashrecon","assetrecon","feedhub")` and `APP_DB = "app"`.

- [ ] **Step 1: Initialise git and ignore files**

```bash
git init
cat > .gitignore <<'EOF'
.env
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
node_modules/
.next/
.logs/
EOF
```

- [ ] **Step 2: Create `.env.example`, `docker-compose.yml`, `Makefile`**

`.env.example`:
```dotenv
# Copied to .env by scripts/start_backend.sh on first run.
PRISM_PG_PORT=5433
PRISM_PG_ADMIN_PASSWORD=postgres
PRISM_PG_SVC_PASSWORD=prism_svc_dev
# PRISM_CTX_HMAC_KEY and PRISM_JWT_SECRET are generated into .env on first run.
# ANTHROPIC_API_KEY is required from Plan 3 onwards.
```

`docker-compose.yml`:
```yaml
name: prism
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: ${PRISM_PG_ADMIN_PASSWORD:-postgres}
    ports:
      - "${PRISM_PG_PORT:-5433}:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 2s
      timeout: 3s
      retries: 30
volumes:
  pgdata:
```

`Makefile` (uses `>` as recipe prefix so tabs are not needed):
```make
.RECIPEPREFIX = >
.PHONY: test test-fast seed reseed db

db:
> docker compose up -d --wait postgres

test: db
> cd backend && uv run pytest -q

test-fast:
> cd backend && uv run pytest -q tests/sim tests/test_config.py tests/security

seed: db
> cd backend && uv run prism-seed

reseed: db
> cd backend && uv run prism-seed --reset
```

- [ ] **Step 3: Create `backend/pyproject.toml`**

```toml
[project]
name = "prism"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.115",
  "uvicorn[standard]>=0.30",
  "psycopg[binary]>=3.2",
  "psycopg-pool>=3.2",
  "pydantic-settings>=2.4",
  "pyjwt>=2.9",
  "honcho>=2.0",
]

[project.scripts]
prism-seed = "prism.sim.cli:main"
prism-token = "prism.security.cli:main"

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "httpx>=0.27"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["prism"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["db: requires Postgres (make db)"]
```

Create empty `backend/prism/__init__.py`.

- [ ] **Step 4: Write the failing test** — `backend/tests/test_config.py`

```python
from datetime import date

from prism.config import LOGICAL_DBS, Settings


def test_dbname_applies_prefix_except_for_maintenance_db():
    s = Settings(db_prefix="test_")
    assert s.dbname("cashrecon") == "test_cashrecon"
    assert s.dbname("postgres") == "postgres"


def test_dsn_uses_service_role_unless_admin():
    s = Settings()
    assert "user=prism_svc" in s.dsn("feedhub")
    assert "user=postgres" in s.dsn("feedhub", admin=True)
    assert f"port={s.pg_port}" in s.dsn("feedhub")


def test_defaults_are_deterministic():
    s = Settings()
    assert s.as_of == date(2026, 9, 30)
    assert s.seed == 42
    assert LOGICAL_DBS == ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
```

- [ ] **Step 5: Run it to verify it fails**

Run: `cd backend && uv sync && uv run pytest tests/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.config'`

- [ ] **Step 6: Implement `backend/prism/config.py`**

```python
"""Runtime configuration, read from PRISM_* environment variables and the repo-root .env."""
from datetime import date

from pydantic_settings import BaseSettings, SettingsConfigDict

LOGICAL_DBS = ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
APP_DB = "app"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRISM_", env_file=("../.env", ".env"), extra="ignore")

    pg_host: str = "localhost"
    pg_port: int = 5433
    pg_admin_user: str = "postgres"
    pg_admin_password: str = "postgres"
    pg_svc_user: str = "prism_svc"
    pg_svc_password: str = "prism_svc_dev"
    db_prefix: str = ""
    ctx_hmac_key: str = "dev-only-ctx-key-change-me-0123456789abcdef"
    jwt_secret: str = "dev-only-jwt-secret-change-me-0123456789abcdef"
    as_of: date = date(2026, 9, 30)
    seed: int = 42
    statement_timeout_ms: int = 5000

    def dbname(self, logical: str) -> str:
        return logical if logical == "postgres" else f"{self.db_prefix}{logical}"

    def dsn(self, logical: str, admin: bool = False) -> str:
        user, password = (
            (self.pg_admin_user, self.pg_admin_password) if admin else (self.pg_svc_user, self.pg_svc_password)
        )
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.dbname(logical)} "
            f"user={user} password={password}"
        )
```

- [ ] **Step 7: Run tests and start Postgres**

Run: `cd backend && uv run pytest tests/test_config.py -q` → Expected: `3 passed`
Run: `docker compose up -d --wait postgres` → Expected: container `prism-postgres-1` reports healthy.

- [ ] **Step 8: Commit**

```bash
git add .gitignore .env.example docker-compose.yml Makefile backend docs
git commit -m "chore: scaffold backend, config and Postgres container

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Identifier generators with check digits

**Files:**
- Create: `backend/prism/sim/__init__.py` (empty), `backend/prism/sim/ids.py`
- Test: `backend/tests/sim/test_ids.py`

**Interfaces:**
- Produces:
  - Check-digit functions: `isin_check_digit(body11: str) -> str`, `cusip_check_digit(body8: str) -> str`, `sedol_check_digit(body6: str) -> str` and `lei_check_digits(base18: str) -> str`.
  - Validators: `is_valid_isin(isin: str) -> bool` and `is_valid_lei(lei: str) -> bool`.
  - Generators: `make_isin(rng, country) -> tuple[str, str | None, str | None]` returns `(isin, cusip, sedol)`. Also `make_lei(rng) -> str` and `make_bic(rng, country) -> str`.

- [ ] **Step 1: Write the failing test** — `backend/tests/sim/test_ids.py`

```python
import random

from prism.sim.ids import (
    cusip_check_digit,
    is_valid_isin,
    is_valid_lei,
    isin_check_digit,
    make_bic,
    make_isin,
    make_lei,
    sedol_check_digit,
)


def test_isin_check_digit_matches_published_examples():
    assert isin_check_digit("US037833100") == "5"  # US0378331005
    assert isin_check_digit("GB000263494") == "6"  # GB0002634946


def test_cusip_and_sedol_check_digits_match_published_examples():
    assert cusip_check_digit("03783310") == "0"
    assert sedol_check_digit("026349") == "4"


def test_generated_leis_validate_and_tampering_is_detected():
    rng = random.Random(1)
    for _ in range(200):
        lei = make_lei(rng)
        assert len(lei) == 20 and is_valid_lei(lei)
    bad = lei[:-1] + ("0" if lei[-1] != "0" else "1")
    assert not is_valid_lei(bad)


def test_make_isin_uses_national_identifier_for_us_and_gb():
    rng = random.Random(2)
    isin, cusip, sedol = make_isin(rng, "US")
    assert isin.startswith("US") and isin[2:11] == cusip and sedol is None and is_valid_isin(isin)
    isin, cusip, sedol = make_isin(rng, "GB")
    assert isin.startswith("GB00") and isin[4:11] == sedol and cusip is None and is_valid_isin(isin)
    isin, cusip, sedol = make_isin(rng, "JP")
    assert isin.startswith("JP") and cusip is None and sedol is None and is_valid_isin(isin)


def test_make_bic_shape():
    bic = make_bic(random.Random(3), "DE")
    assert len(bic) == 8 and bic[4:6] == "DE" and bic[:4].isalpha()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && uv run pytest tests/sim/test_ids.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.sim'`

- [ ] **Step 3: Implement `backend/prism/sim/ids.py`**

```python
"""Realistic financial identifiers with valid check digits (ISIN, CUSIP, SEDOL, LEI) and BICs."""
import random
import string

_ALNUM = string.digits + string.ascii_uppercase
_SEDOL_CHARS = "0123456789BCDFGHJKLMNPQRSTVWXYZ"
_SEDOL_WEIGHTS = (1, 3, 1, 7, 3, 9)


def _value(ch: str) -> int:
    return int(ch) if ch.isdigit() else ord(ch) - 55  # A=10 … Z=35


def isin_check_digit(body11: str) -> str:
    """Luhn over the digit expansion of the 11-character ISIN body."""
    digits = "".join(str(_value(c)) for c in body11)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def cusip_check_digit(body8: str) -> str:
    total = 0
    for i, ch in enumerate(body8):
        v = _value(ch)
        if i % 2 == 1:
            v *= 2
        total += v // 10 + v % 10
    return str((10 - total % 10) % 10)


def sedol_check_digit(body6: str) -> str:
    total = sum(_value(c) * w for c, w in zip(body6, _SEDOL_WEIGHTS))
    return str((10 - total % 10) % 10)


def lei_check_digits(base18: str) -> str:
    """ISO 17442 / ISO 7064 MOD 97-10."""
    n = int("".join(str(_value(c)) for c in base18 + "00"))
    return f"{98 - n % 97:02d}"


def is_valid_isin(isin: str) -> bool:
    return len(isin) == 12 and isin_check_digit(isin[:11]) == isin[11]


def is_valid_lei(lei: str) -> bool:
    return len(lei) == 20 and int("".join(str(_value(c)) for c in lei)) % 97 == 1


def make_cusip(rng: random.Random) -> str:
    body = "".join(rng.choice(string.digits) for _ in range(6)) + "".join(rng.choice(_ALNUM) for _ in range(2))
    return body + cusip_check_digit(body)


def make_sedol(rng: random.Random) -> str:
    body = "".join(rng.choice(_SEDOL_CHARS) for _ in range(6))
    return body + sedol_check_digit(body)


def make_isin(rng: random.Random, country: str) -> tuple[str, str | None, str | None]:
    """Return (isin, cusip, sedol); CUSIP only for US, SEDOL only for GB."""
    if country == "US":
        cusip = make_cusip(rng)
        body = "US" + cusip
        return body + isin_check_digit(body), cusip, None
    if country == "GB":
        sedol = make_sedol(rng)
        body = "GB00" + sedol
        return body + isin_check_digit(body), None, sedol
    body = country + "".join(rng.choice(_ALNUM) for _ in range(9))
    return body + isin_check_digit(body), None, None


def make_lei(rng: random.Random) -> str:
    base = "".join(rng.choice(string.digits) for _ in range(4)) + "00" + "".join(rng.choice(_ALNUM) for _ in range(12))
    return base + lei_check_digits(base)


def make_bic(rng: random.Random, country: str) -> str:
    return (
        "".join(rng.choice(string.ascii_uppercase) for _ in range(4))
        + country
        + "".join(rng.choice(_ALNUM) for _ in range(2))
    )
```

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest tests/sim/test_ids.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim backend/tests/sim
git commit -m "feat(sim): identifier generators with ISIN/CUSIP/SEDOL/LEI check digits

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Calendar, table container and the shared universe

**Files:**
- Create: `backend/prism/sim/calendar.py`, `backend/prism/sim/model.py`, `backend/prism/sim/universe.py`
- Test: `backend/tests/sim/conftest.py`, `backend/tests/sim/test_universe.py`

**Interfaces:**
- Consumes: `make_isin`, `make_lei`, `make_bic` (Task 2).
- Produces:
  - `calendar.business_days(as_of: date, n: int) -> list[date]` (ascending, ends at `as_of`) and `calendar.at(d: date, hour: int, minute: int = 0) -> datetime` (UTC).
  - `model.TableData(columns: tuple[str, ...])` with `.add(*values)`, `.rows: list[tuple]` and `.dicts() -> list[dict]`. Also `model.id_sequence() -> Callable[[str, int], str]`, where `nid("BRK", 6) -> "BRK000001"`.
  - In `universe`: `SimConfig` (fields below, plus `SimConfig.small(**overrides)`); the dataclasses `Entity`, `Security`, `Source`, `Portfolio`, `CashAccount` and `Stories`; and `Universe`.
  - `Universe` fields: `.days`, `.entities`, `.securities`, `.prices: dict[str, list[float]]`, `.sources`, `.portfolios`, `.holdings: dict[str, list[tuple[str, float]]]`, `.cash_accounts`, `.stories`.
  - `Universe` helpers: `.securities_by_class`, `.security_by_id` and `.golden(sid: str, i: int) -> float`.
  - `build_universe(cfg: SimConfig) -> Universe`.
  - Constants `ASSET_CLASSES`, `VENDORS`, `VENDOR_RANK`, `VENDOR_COVERAGE`, `FUND_GROUPS`, `REGIONS`, `LATE_CUSTODIAN_PORTFOLIOS`, `NAV_PORTFOLIOS`.

- [ ] **Step 1: Write `calendar.py` and `model.py`** (small helpers used by the test fixtures)

`backend/prism/sim/calendar.py`:
```python
"""Business-day calendar (weekends only; no holiday calendar in the MVP)."""
from datetime import UTC, date, datetime, timedelta


def business_days(as_of: date, n: int) -> list[date]:
    """The n most recent weekdays ending at as_of (inclusive when as_of is a weekday), ascending."""
    days: list[date] = []
    d = as_of
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def at(d: date, hour: int, minute: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=UTC)
```

`backend/prism/sim/model.py`:
```python
"""Container for one generated table, plus a sequential id helper."""
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TableData:
    columns: tuple[str, ...]
    rows: list[tuple] = field(default_factory=list)

    def add(self, *values) -> None:
        if len(values) != len(self.columns):
            raise ValueError(f"expected {len(self.columns)} values for {self.columns}, got {len(values)}")
        self.rows.append(values)

    def dicts(self) -> list[dict]:
        return [dict(zip(self.columns, row)) for row in self.rows]


def id_sequence() -> Callable[..., str]:
    counters: dict[str, int] = defaultdict(int)

    def nxt(prefix: str, width: int = 7) -> str:
        counters[prefix] += 1
        return f"{prefix}{counters[prefix]:0{width}d}"

    return nxt
```

- [ ] **Step 2: Write the failing tests**

`backend/tests/sim/conftest.py`:
```python
import pytest

from prism.sim.universe import SimConfig, build_universe


@pytest.fixture(scope="session")
def universe():
    return build_universe(SimConfig.small())
```

`backend/tests/sim/test_universe.py`:
```python
import pytest

from prism.sim.calendar import business_days
from prism.sim.ids import is_valid_isin, is_valid_lei
from prism.sim.model import TableData, id_sequence
from prism.sim.universe import (
    LATE_CUSTODIAN_PORTFOLIOS,
    NAV_PORTFOLIOS,
    SimConfig,
    build_universe,
)


def test_business_days_skip_weekends_and_end_at_as_of(universe):
    days = business_days(universe.cfg.as_of, 10)
    assert days[-1] == universe.cfg.as_of and len(days) == 10
    assert all(d.weekday() < 5 for d in days) and days == sorted(days)


def test_table_data_rejects_wrong_arity_and_ids_are_sequential():
    t = TableData(("a", "b"))
    with pytest.raises(ValueError):
        t.add(1)
    nid = id_sequence()
    assert [nid("X", 3), nid("X", 3), nid("Y", 2)] == ["X001", "X002", "Y01"]


def test_counts_follow_config(universe):
    cfg = universe.cfg
    assert len(universe.days) == cfg.n_days
    assert len(universe.securities) == cfg.n_securities
    assert len(universe.entities) == cfg.n_entities
    assert len(universe.portfolios) == cfg.n_portfolios
    assert len(universe.cash_accounts) == cfg.n_cash_accounts
    assert all(len(path) == cfg.n_days for path in universe.prices.values())


def test_identifiers_are_valid_and_unique(universe):
    isins = [s.isin for s in universe.securities]
    leis = [e.lei for e in universe.entities]
    assert len(set(isins)) == len(isins) and all(map(is_valid_isin, isins))
    assert len(set(leis)) == len(leis) and all(map(is_valid_lei, leis))


def test_build_is_deterministic():
    a, b = build_universe(SimConfig.small()), build_universe(SimConfig.small())
    assert [s.isin for s in a.securities] == [s.isin for s in b.securities]
    assert a.prices == b.prices and a.holdings == b.holdings


def test_story_wiring(universe):
    st = universe.stories
    by_id = {p.portfolio_id: p for p in universe.portfolios}
    assert st.late_portfolio_ids == LATE_CUSTODIAN_PORTFOLIOS
    assert all(by_id[pid].custodian_source_id == st.late_custodian_source_id for pid in st.late_portfolio_ids)
    assert by_id["PF001"].fund_group == "Growth"
    assert all(
        p.custodian_source_id != st.late_custodian_source_id
        for p in universe.portfolios
        if p.portfolio_id not in st.late_portfolio_ids
    )
    for pid in NAV_PORTFOLIOS:
        held = {sid for sid, _ in universe.holdings[pid]}
        assert set(st.stale_security_ids) <= held
    others = [pid for pid in universe.holdings if pid not in NAV_PORTFOLIOS]
    assert not any(sid in st.stale_security_ids for pid in others for sid, _ in universe.holdings[pid])
    accounts = {a.account_id: a for a in universe.cash_accounts}
    for aid in st.usd_break_account_ids:
        assert accounts[aid].ccy == "USD" and accounts[aid].region == "EMEA"
        assert accounts[aid].legal_entity_id == st.usd_break_entity_id


def test_golden_price_is_carried_forward_for_stale_securities(universe):
    st, n = universe.stories, len(universe.days)
    sid = st.stale_security_ids[0]
    frozen = universe.prices[sid][n - st.stale_days - 1]
    assert all(universe.golden(sid, i) == frozen for i in range(n - st.stale_days, n))
    assert universe.prices[sid][-1] != frozen


def test_config_guards_story_prerequisites():
    with pytest.raises(ValueError):
        SimConfig(n_portfolios=5)
    with pytest.raises(ValueError):
        SimConfig(n_days=10)
```

- [ ] **Step 3: Run to verify failure**

Run: `cd backend && uv run pytest tests/sim/test_universe.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.sim.universe'`

- [ ] **Step 4: Implement `backend/prism/sim/universe.py`**

```python
"""One fictional firm's shared universe. Every platform projection is derived from it,
so identifiers (security, entity, custodian, account) line up across systems."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cached_property

from prism.sim.calendar import business_days
from prism.sim.ids import make_bic, make_isin, make_lei

ASSET_CLASSES = ("Equity", "Corp bond", "Govt", "FX", "Deriv")
ASSET_CLASS_WEIGHTS = (0.35, 0.30, 0.12, 0.08, 0.15)
SUB_CLASSES = {
    "Equity": ("Common", "Preferred", "ADR"),
    "Corp bond": ("IG", "HY"),
    "Govt": ("Bill", "Note", "Bond"),
    "FX": ("Spot", "Forward"),
    "Deriv": ("Future", "Option", "Swap"),
}
DAILY_VOL = {"Equity": 0.018, "Corp bond": 0.004, "Govt": 0.002, "FX": 0.006, "Deriv": 0.03}
PRICE_RANGE = {"Equity": (10, 500), "Corp bond": (88, 112), "Govt": (94, 106), "FX": (0.5, 150), "Deriv": (1, 50)}
REGIONS = {
    "EMEA": ("GB", "DE", "FR", "NL", "CH", "IE", "LU"),
    "AMER": ("US", "CA", "BR"),
    "APAC": ("JP", "SG", "AU", "HK"),
}
REGION_OF = {c: r for r, cs in REGIONS.items() for c in cs}
COUNTRY_CCY = {
    "GB": "GBP", "DE": "EUR", "FR": "EUR", "NL": "EUR", "IE": "EUR", "LU": "EUR", "CH": "CHF",
    "US": "USD", "CA": "CAD", "BR": "BRL", "JP": "JPY", "SG": "SGD", "AU": "AUD", "HK": "HKD",
}
LEGAL_SUFFIX = {
    "GB": "PLC", "DE": "AG", "FR": "SA", "NL": "NV", "CH": "AG", "IE": "Ltd", "LU": "SA",
    "US": "Inc", "CA": "Corp", "BR": "SA", "JP": "KK", "SG": "Pte Ltd", "AU": "Ltd", "HK": "Ltd",
}
SECTORS = ("Financials", "Industrials", "Technology", "Energy", "Utilities",
           "Healthcare", "Consumer", "Materials", "Telecom", "Real Estate")
NAME_PARTS_A = ("Aldr", "Bram", "Cald", "Dorn", "Elst", "Fenw", "Garr", "Hask", "Ingl", "Jarr", "Kell", "Lorr",
                "Mald", "Norr", "Orwe", "Pell", "Quen", "Rask", "Stav", "Tarn", "Ulst", "Vard", "Wynd", "Yell")
NAME_PARTS_B = ("ane", "ex", "ford", "ic", "ion", "is", "ity", "on", "ora", "ova",
                "ridge", "sen", "ton", "trix", "um", "vale", "well", "wick", "worth", "yx")
SOURCE_WORDS = ("Aster", "Briar", "Cobalt", "Dunmore", "Ember", "Fairholt", "Granite", "Halcyon",
                "Ironwood", "Juniper", "Kestrel", "Larkspur", "Marlow", "Northgate", "Oakridge", "Pinecrest",
                "Quillon", "Redfern", "Silverbirch", "Thornbury", "Umberly", "Valemont", "Westbrook", "Yarrow")
SOURCE_LAYOUT = (("custodian", 22, "Custody"), ("bank", 14, "Bank"), ("prime_broker", 4, "Prime"))
VENDORS = (("V_A", "Vendor A"), ("V_B", "Vendor B"), ("V_C", "Vendor C"),
           ("V_D", "Vendor D"), ("V_E", "Vendor E"), ("V_F", "Vendor F"))
VENDOR_RANK = {"V_B": 1, "V_D": 2, "V_A": 3, "V_C": 4, "V_F": 5, "V_E": 6}
VENDOR_COVERAGE = {
    "V_A": ASSET_CLASSES,
    "V_B": ("Equity", "Corp bond", "Govt"),
    "V_C": ("Equity", "FX", "Deriv"),
    "V_D": ("Corp bond", "Govt", "FX"),
    "V_E": ("Equity", "Deriv"),
    "V_F": ASSET_CLASSES,
}
FUND_GROUPS = ("Growth", "Income", "Multi-Asset", "Liquidity")
FUND_GROUP_CLASSES = {
    "Growth": ("Equity",),
    "Income": ("Corp bond", "Govt"),
    "Multi-Asset": ("Equity", "Corp bond", "Govt"),
    "Liquidity": ("Govt", "FX"),
}
CASH_CCYS = ("USD", "EUR", "GBP", "JPY", "CHF", "SGD")
LATE_CUSTODIAN_PORTFOLIOS = ("PF001", "PF002", "PF005")
NAV_PORTFOLIOS = ("PF003", "PF009")
STALE_JUMP = 1.06


@dataclass(frozen=True)
class SimConfig:
    seed: int = 42
    as_of: date = date(2026, 9, 30)
    n_days: int = 90
    n_securities: int = 2000
    n_entities: int = 400
    n_portfolios: int = 30
    n_cash_accounts: int = 60
    holdings_range: tuple[int, int] = (40, 70)
    vendor_window_days: int = 20
    position_window_days: int = 20
    profile: str = "full"

    def __post_init__(self) -> None:
        if self.n_portfolios < 9:
            raise ValueError("n_portfolios must be >= 9 (stories use PF001-PF009)")
        if self.n_days < 25:
            raise ValueError("n_days must be >= 25 (stories need ~5 weeks of history)")
        if self.n_cash_accounts < 4:
            raise ValueError("n_cash_accounts must be >= 4")
        if self.n_entities < 40:
            raise ValueError("n_entities must be >= 40")

    @classmethod
    def small(cls, **overrides) -> SimConfig:
        base = dict(n_days=25, n_securities=240, n_entities=60, n_portfolios=12,
                    n_cash_accounts=18, holdings_range=(12, 20), profile="small")
        return cls(**{**base, **overrides})


@dataclass(frozen=True)
class Entity:
    entity_id: str
    lei: str
    name: str
    country: str
    region: str
    sector: str
    parent_entity_id: str | None
    status: str


@dataclass(frozen=True)
class Security:
    security_id: str
    isin: str
    cusip: str | None
    sedol: str | None
    ticker: str | None
    name: str
    asset_class: str
    sub_class: str
    ccy: str
    issuer_entity_id: str | None
    country: str
    status: str
    valid_from: date


@dataclass(frozen=True)
class Source:
    source_id: str
    name: str
    source_type: str
    bic: str
    country: str


@dataclass(frozen=True)
class Portfolio:
    portfolio_id: str
    name: str
    fund_group: str
    base_ccy: str
    custodian_source_id: str
    region: str


@dataclass(frozen=True)
class CashAccount:
    account_id: str
    legal_entity_id: str
    bank_source_id: str
    bank_bic: str
    nostro_no: str
    ccy: str
    region: str


@dataclass(frozen=True)
class Stories:
    """Planted, verifiable demo narratives (spec §2 'Planted stories')."""
    late_custodian_source_id: str
    late_start_idx: int
    usd_break_entity_id: str
    usd_break_extra: int
    stale_security_ids: tuple[str, ...]
    conflict_vendor_id: str = "V_A"
    conflict_asset_class: str = "Corp bond"
    conflict_daily_this_week: int = 12
    conflict_daily_prev_week: int = 9
    late_portfolio_ids: tuple[str, ...] = LATE_CUSTODIAN_PORTFOLIOS
    usd_break_account_ids: tuple[str, ...] = ("CA0001", "CA0002")
    nav_portfolio_ids: tuple[str, ...] = NAV_PORTFOLIOS
    stale_days: int = 3


@dataclass
class Universe:
    cfg: SimConfig
    days: list[date]
    entities: list[Entity]
    securities: list[Security]
    prices: dict[str, list[float]]
    sources: list[Source]
    portfolios: list[Portfolio]
    holdings: dict[str, list[tuple[str, float]]]
    cash_accounts: list[CashAccount]
    stories: Stories

    @cached_property
    def securities_by_class(self) -> dict[str, list[Security]]:
        out: dict[str, list[Security]] = {ac: [] for ac in ASSET_CLASSES}
        for s in self.securities:
            out[s.asset_class].append(s)
        return out

    @cached_property
    def security_by_id(self) -> dict[str, Security]:
        return {s.security_id: s for s in self.securities}

    def golden(self, sid: str, i: int) -> float:
        """EDM golden price: equals the market price, except the stale story carries it forward."""
        n, st = len(self.days), self.stories
        if sid in st.stale_security_ids and i >= n - st.stale_days:
            return self.prices[sid][n - st.stale_days - 1]
        return self.prices[sid][i]


def _make_entities(rng: random.Random, n: int) -> list[Entity]:
    entities: list[Entity] = []
    leis: set[str] = set()
    countries = [c for cs in REGIONS.values() for c in cs]

    def new_lei() -> str:
        while True:
            lei = make_lei(rng)
            if lei not in leis:
                leis.add(lei)
                return lei

    for i, country in enumerate(countries):  # one sovereign issuer per country
        entities.append(Entity(f"LE{i + 1:05d}", new_lei(), f"{country} Treasury", country,
                               REGION_OF[country], "Sovereign", None, "active"))
    names: set[str] = set()
    while len(entities) < n:
        country = rng.choice(countries)
        base = f"{rng.choice(NAME_PARTS_A)}{rng.choice(NAME_PARTS_B)}"
        if base in names:
            continue
        names.add(base)
        corporates = [e for e in entities if e.sector != "Sovereign"]
        parent = rng.choice(corporates).entity_id if corporates and rng.random() < 0.1 else None
        entities.append(Entity(
            f"LE{len(entities) + 1:05d}", new_lei(), f"{base} {LEGAL_SUFFIX[country]}", country,
            REGION_OF[country], rng.choice(SECTORS), parent, "active" if rng.random() < 0.97 else "inactive",
        ))
    return entities


def _make_securities(rng: random.Random, n: int, entities: list[Entity], start: date) -> list[Security]:
    sovereigns = {e.country: e for e in entities if e.sector == "Sovereign"}
    corporates = [e for e in entities if e.sector != "Sovereign" and e.status == "active"]
    fx_pairs = ("EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "USDSGD", "EURGBP")
    isins: set[str] = set()
    out: list[Security] = []
    while len(out) < n:
        ac = rng.choices(ASSET_CLASSES, weights=ASSET_CLASS_WEIGHTS)[0]
        sub = rng.choice(SUB_CLASSES[ac])
        if ac == "Govt":
            issuer = sovereigns[rng.choice(list(sovereigns))]
        elif ac in ("Equity", "Corp bond"):
            issuer = rng.choice(corporates)
        else:
            issuer = None
        country = issuer.country if issuer else rng.choice(("US", "GB", "DE", "JP"))
        isin, cusip, sedol = make_isin(rng, country)
        if isin in isins:
            continue
        isins.add(isin)
        short = issuer.name.split(" ")[0] if issuer else ""
        year = start.year + rng.randint(1, 12)
        coupon = rng.choice((1.25, 2.0, 2.75, 3.5, 4.125, 5.0, 6.25))
        name = {
            "Equity": f"{short} {sub}",
            "Corp bond": f"{short} {coupon:.3f}% {year}",
            "Govt": f"{country} Govt {sub} {coupon:.3f}% {year}",
            "FX": f"{rng.choice(fx_pairs)} {sub}",
            "Deriv": f"{rng.choice(('Index', 'Rates', 'Commodity', 'Credit'))} {sub} {year}",
        }[ac]
        out.append(Security(
            f"SEC{len(out) + 1:06d}", isin, cusip, sedol, short[:4].upper() if ac == "Equity" else None,
            name, ac, sub, COUNTRY_CCY[country], issuer.entity_id if issuer else None, country,
            "active" if rng.random() < 0.98 else "matured", start - timedelta(days=rng.randint(30, 3650)),
        ))
    return out


def _make_prices(rng: random.Random, securities: list[Security], n_days: int) -> dict[str, list[float]]:
    prices: dict[str, list[float]] = {}
    for s in securities:
        lo, hi = PRICE_RANGE[s.asset_class]
        p, vol, path = rng.uniform(lo, hi), DAILY_VOL[s.asset_class], []
        for _ in range(n_days):
            path.append(round(p, 6))
            p *= math.exp(rng.gauss(0, vol))
        prices[s.security_id] = path
    return prices


def _make_sources(rng: random.Random) -> list[Source]:
    countries = [c for cs in REGIONS.values() for c in cs]
    out: list[Source] = []
    bics: set[str] = set()
    idx = 0
    for source_type, count, suffix in SOURCE_LAYOUT:
        for _ in range(count):
            country = rng.choice(countries)
            bic = make_bic(rng, country)
            while bic in bics:
                bic = make_bic(rng, country)
            bics.add(bic)
            out.append(Source(f"SRC{idx + 1:03d}", f"{SOURCE_WORDS[idx % len(SOURCE_WORDS)]} {suffix}",
                              source_type, bic, country))
            idx += 1
    return out


def _make_portfolios(rng, cfg, sources, securities, late_src, excluded):
    custodians = [s for s in sources if s.source_type == "custodian" and s.source_id != late_src]
    by_class: dict[str, list[Security]] = {ac: [] for ac in ASSET_CLASSES}
    for s in securities:
        if s.status == "active" and s.security_id not in excluded:
            by_class[s.asset_class].append(s)
    portfolios, holdings = [], {}
    for i in range(cfg.n_portfolios):
        pid, group = f"PF{i + 1:03d}", FUND_GROUPS[i % len(FUND_GROUPS)]
        ccy = rng.choice(("USD", "EUR", "GBP"))
        custodian = late_src if pid in LATE_CUSTODIAN_PORTFOLIOS else rng.choice(custodians).source_id
        portfolios.append(Portfolio(pid, f"{group} Fund {i + 1:02d}", group, ccy, custodian,
                                    "AMER" if ccy == "USD" else "EMEA"))
        pool = [s for ac in FUND_GROUP_CLASSES[group] for s in by_class[ac]]
        chosen = rng.sample(pool, min(len(pool), rng.randint(*cfg.holdings_range)))
        holdings[pid] = [(s.security_id, float(rng.randint(10, 1000) * 100)) for s in chosen]
    return portfolios, holdings


def _pick_house_entities(entities: list[Entity]) -> dict[str, list[Entity]]:
    house = {
        region: [e for e in entities if e.region == region and e.sector != "Sovereign" and e.status == "active"][:3]
        for region in REGIONS
    }
    missing = [r for r, es in house.items() if not es]
    if missing:
        raise ValueError(f"no active corporate entity in region(s) {missing}; increase n_entities")
    return house


def _make_cash_accounts(rng, cfg, house, sources, story_entity) -> list[CashAccount]:
    banks = [s for s in sources if s.source_type == "bank"]
    all_house = [e for es in house.values() for e in es]
    out = []
    for i in range(cfg.n_cash_accounts):
        entity, ccy = (story_entity, "USD") if i < 2 else (rng.choice(all_house), rng.choice(CASH_CCYS))
        bank = rng.choice(banks)
        nostro = "".join(rng.choice("0123456789") for _ in range(12))
        out.append(CashAccount(f"CA{i + 1:04d}", entity.entity_id, bank.source_id, bank.bic, nostro, ccy, entity.region))
    return out


def build_universe(cfg: SimConfig) -> Universe:
    rng = random.Random(cfg.seed)
    days = business_days(cfg.as_of, cfg.n_days)
    entities = _make_entities(rng, cfg.n_entities)
    securities = _make_securities(rng, cfg.n_securities, entities, days[0])
    prices = _make_prices(rng, securities, len(days))
    sources = _make_sources(rng)
    late_src = next(s.source_id for s in sources if s.source_type == "custodian")
    equities = sorted(
        (s for s in securities if s.asset_class == "Equity" and s.status == "active"),
        key=lambda s: -prices[s.security_id][0],
    )
    stale = tuple(s.security_id for s in equities[:5])
    portfolios, holdings = _make_portfolios(rng, cfg, sources, securities, late_src, set(stale))
    for pid in NAV_PORTFOLIOS:
        holdings[pid].extend((sid, float(rng.randint(10, 1000) * 100)) for sid in stale)
    house = _pick_house_entities(entities)
    story_entity = house["EMEA"][0]
    cash_accounts = _make_cash_accounts(rng, cfg, house, sources, story_entity)
    stories = Stories(
        late_custodian_source_id=late_src,
        late_start_idx=len(days) - 6,
        usd_break_entity_id=story_entity.entity_id,
        usd_break_extra=max(12, cfg.n_cash_accounts),
        stale_security_ids=stale,
    )
    for sid in stale:  # the market moved; the EDM golden copy did not (see Universe.golden)
        path = prices[sid]
        for i in range(len(days) - stories.stale_days, len(days)):
            path[i] = round(path[i] * STALE_JUMP, 6)
    return Universe(cfg, days, entities, securities, prices, sources, portfolios, holdings, cash_accounts, stories)
```

- [ ] **Step 5: Run tests**

Run: `cd backend && uv run pytest tests/sim -q`
Expected: all pass (`12 passed` including Task 2).

- [ ] **Step 6: Commit**

```bash
git add backend/prism/sim backend/tests/sim
git commit -m "feat(sim): deterministic shared universe with planted demo stories

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: RefMaster projection

**Files:**
- Create: `backend/prism/sim/project_refmaster.py`
- Test: `backend/tests/sim/test_refmaster_projection.py`

**Interfaces:**
- Consumes: `Universe`, `TableData`, `id_sequence`, `at` (Task 3).
- Produces: `project_refmaster(u: Universe) -> dict[str, TableData]` with keys, in load order: `legal_entities`, `securities`, `products`, `accounts`, `corporate_actions`, `dq_rules`, `exceptions`, `change_requests`, `data_dictionary`. The column names equal the DDL in Task 9.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from prism.sim.project_refmaster import project_refmaster


@pytest.fixture(scope="module")
def rm(universe):
    return project_refmaster(universe)


def test_tables_and_counts(rm, universe):
    assert list(rm) == ["legal_entities", "securities", "products", "accounts", "corporate_actions",
                        "dq_rules", "exceptions", "change_requests", "data_dictionary"]
    assert len(rm["legal_entities"].rows) == len(universe.entities)
    assert len(rm["securities"].rows) == len(universe.securities)
    assert len(rm["exceptions"].rows) > 0 and len(rm["data_dictionary"].rows) == 20


def test_parent_entities_precede_children(rm):
    seen = set()
    for e in rm["legal_entities"].dicts():
        assert e["parent_entity_id"] is None or e["parent_entity_id"] in seen
        seen.add(e["entity_id"])


def test_exceptions_have_valid_state(rm, universe):
    for x in rm["exceptions"].dicts():
        assert x["status"] in {"open", "in_review", "closed"}
        assert (x["closed_at"] is not None) == (x["status"] == "closed")
        assert x["asset_class"] == "n/a" if x["domain"] == "entity" else x["asset_class"] != "n/a"
    open_count = sum(x["status"] != "closed" for x in rm["exceptions"].dicts())
    assert open_count > 0


def test_four_eyes_and_corporate_action_dates(rm):
    for c in rm["change_requests"].dicts():
        if c["status"] == "pending":
            assert c["checker"] is None
        else:
            assert c["checker"] not in (None, c["maker"])
    for ca in rm["corporate_actions"].dicts():
        assert ca["ex_date"] < ca["pay_date"]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/sim/test_refmaster_projection.py -q`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `backend/prism/sim/project_refmaster.py`**

```python
"""RefMaster EDM: security, entity, product/account master, corporate actions, DQ, 4-eyes, data dictionary."""
import random
from datetime import timedelta

from prism.sim.calendar import at
from prism.sim.model import TableData
from prism.sim.universe import Universe

PRODUCTS = (
    ("PR01", "Global Equity Fund", "fund"), ("PR02", "Euro Credit Fund", "fund"),
    ("PR03", "US Treasury Mandate", "mandate"), ("PR04", "Asia Growth Fund", "fund"),
    ("PR05", "Liquidity Reserve", "fund"), ("PR06", "Multi-Asset Income", "fund"),
    ("PR07", "Segregated Pension Mandate", "mandate"), ("PR08", "Securities Lending Programme", "service"),
)
DQ_RULES = (
    ("R001", "security", "ISIN check digit valid", "high"),
    ("R002", "security", "Mandatory issuer populated", "medium"),
    ("R003", "security", "Asset class consistent with sub-class", "medium"),
    ("R004", "price", "Price within tolerance of prior close", "high"),
    ("R005", "price", "Price received before cut-off", "medium"),
    ("R006", "price", "At least two vendor quotes", "low"),
    ("R007", "entity", "LEI checksum valid", "high"),
    ("R008", "entity", "Parent entity exists", "medium"),
    ("R009", "corporate_action", "Ex-date before pay-date", "high"),
    ("R010", "corporate_action", "Ratio populated for split", "medium"),
    ("R011", "security", "Duplicate identifier across records", "high"),
    ("R012", "entity", "Country code valid ISO 3166", "low"),
)
STEWARDS = ("steward01", "steward02", "steward03", "steward04", "steward05")
CA_EVENTS = (("dividend", 60), ("split", 10), ("merger", 5), ("rights_issue", 10), ("name_change", 15))
DATA_DICTIONARY = (
    ("security", "isin", "International Securities Identification Number (ISO 6166), 12 characters with check digit",
     "Reference Data Office", "Vendor feeds", "vendor file -> staging -> validation -> golden copy"),
    ("security", "asset_class", "Top-level classification: Equity, Corp bond, Govt, FX, Deriv",
     "Reference Data Office", "Derived", "vendor classification -> mapping rule -> golden copy"),
    ("security", "sub_class", "Second-level classification within the asset class",
     "Reference Data Office", "Derived", "mapping rule"),
    ("security", "ccy", "ISO 4217 trading currency", "Reference Data Office", "Vendor feeds", "vendor file -> golden copy"),
    ("security", "issuer_entity_id", "Legal entity that issued the security",
     "Entity Data Office", "Entity master", "entity match -> link"),
    ("security", "status", "Lifecycle status: active or matured", "Reference Data Office", "Derived", "maturity rule"),
    ("entity", "lei", "Legal Entity Identifier (ISO 17442), 20 characters",
     "Entity Data Office", "LEI registry", "registry file -> entity match -> golden copy"),
    ("entity", "parent_entity_id", "Direct parent legal entity",
     "Entity Data Office", "LEI registry", "relationship file -> golden copy"),
    ("entity", "sector", "Industry sector of the entity", "Entity Data Office", "Vendor feeds", "vendor file -> mapping rule"),
    ("price", "golden_price", "Chosen price after vendor ranking and validation",
     "Pricing Team", "Derived", "vendor quotes -> validation -> source ranking -> golden copy"),
    ("price", "price_conflict", "Vendor quote deviating more than 1% from the golden price",
     "Pricing Team", "Derived", "vendor quote vs golden price comparison"),
    ("price", "stale_price", "Golden price carried forward while vendor quotes moved",
     "Pricing Team", "Derived", "carry-forward rule"),
    ("corporate_action", "ex_date", "First date the security trades without the entitlement",
     "Corporate Actions Team", "Vendor feeds", "announcement -> scrubbing -> golden event"),
    ("corporate_action", "pay_date", "Date the entitlement is paid",
     "Corporate Actions Team", "Vendor feeds", "announcement -> golden event"),
    ("quality", "exception", "A record failing a data-quality rule, awaiting steward action",
     "Data Quality Office", "Derived", "rule engine -> exception queue"),
    ("quality", "four_eyes", "Maker-checker approval: a change needs a second approver",
     "Data Quality Office", "Workflow", "change request -> approval"),
    ("quality", "golden_copy_completeness", "Share of active instruments with a golden price for the business date",
     "Data Quality Office", "Derived", "golden prices / active instruments"),
    ("account", "lifecycle_state", "Account state: onboarding, active or closed",
     "Client Data Office", "Onboarding workflow", "onboarding -> activation"),
    ("account", "account_type", "Segregated, pooled or omnibus", "Client Data Office", "Onboarding workflow", "onboarding"),
    ("product", "product_type", "Fund, mandate or service", "Client Data Office", "Product master", "product setup"),
)


def project_refmaster(u: Universe) -> dict[str, TableData]:
    rng = random.Random(u.cfg.seed + 1)
    t = {
        "legal_entities": TableData(("entity_id", "lei", "name", "country", "region", "sector", "parent_entity_id", "status")),
        "securities": TableData(("security_id", "isin", "cusip", "sedol", "ticker", "name", "asset_class", "sub_class",
                                 "ccy", "issuer_entity_id", "country", "status", "valid_from", "valid_to")),
        "products": TableData(("product_id", "name", "product_type")),
        "accounts": TableData(("account_id", "product_id", "name", "account_type", "owner_entity_id", "region",
                               "lifecycle_state")),
        "corporate_actions": TableData(("ca_id", "security_id", "event_type", "ex_date", "pay_date", "ratio", "status")),
        "dq_rules": TableData(("rule_id", "domain", "name", "severity")),
        "exceptions": TableData(("exc_id", "rule_id", "domain", "record_ref", "asset_class", "status", "assignee",
                                 "opened_at", "closed_at")),
        "change_requests": TableData(("change_id", "domain", "record_ref", "maker", "checker", "status", "created_at")),
        "data_dictionary": TableData(("domain", "attribute", "definition", "owner", "source", "lineage")),
    }
    for e in u.entities:
        t["legal_entities"].add(e.entity_id, e.lei, e.name, e.country, e.region, e.sector, e.parent_entity_id, e.status)
    for s in u.securities:
        t["securities"].add(s.security_id, s.isin, s.cusip, s.sedol, s.ticker, s.name, s.asset_class, s.sub_class,
                            s.ccy, s.issuer_entity_id, s.country, s.status, s.valid_from, None)
    for row in PRODUCTS:
        t["products"].add(*row)
    corporates = [e for e in u.entities if e.sector != "Sovereign"]
    for i in range(max(40, len(u.securities) // 10)):
        product, owner = rng.choice(PRODUCTS), rng.choice(corporates)
        t["accounts"].add(f"AC{i + 1:05d}", product[0], f"{product[1]} - {owner.name}",
                          rng.choice(("segregated", "pooled", "omnibus")), owner.entity_id, owner.region,
                          rng.choices(("active", "onboarding", "closed"), weights=(85, 10, 5))[0])
    _corporate_actions(rng, u, t["corporate_actions"])
    for row in DQ_RULES:
        t["dq_rules"].add(*row)
    _exceptions(rng, u, t["exceptions"])
    _change_requests(rng, u, t["change_requests"])
    for row in DATA_DICTIONARY:
        t["data_dictionary"].add(*row)
    return t


def _corporate_actions(rng: random.Random, u: Universe, table: TableData) -> None:
    events, weights = zip(*CA_EVENTS)
    n = 0
    for s in u.securities_by_class["Equity"]:
        if rng.random() >= 0.15:
            continue
        event = rng.choices(events, weights=weights)[0]
        ex = rng.choice(u.days)
        pay = ex + timedelta(days=rng.randint(3, 20))
        ratio = {"dividend": round(rng.uniform(0.1, 3.0), 4), "split": float(rng.choice((2, 3, 4))),
                 "merger": 1.25, "rights_issue": 0.2, "name_change": None}[event]
        status = "processed" if pay <= u.cfg.as_of else rng.choice(("announced", "confirmed", "confirmed"))
        n += 1
        table.add(f"CA{n:06d}", s.security_id, event, ex, pay, ratio, status)


def _exceptions(rng: random.Random, u: Universe, table: TableData) -> None:
    rules_by_domain: dict[str, list[str]] = {}
    for rule_id, domain, _, _ in DQ_RULES:
        rules_by_domain.setdefault(domain, []).append(rule_id)
    cap = at(u.cfg.as_of, 18)
    n = 0
    for d in u.days:
        age = (u.cfg.as_of - d).days
        for _ in range(max(2, round(len(u.securities) * 0.012 * rng.uniform(0.7, 1.3)))):
            domain = rng.choices(("security", "price", "entity", "corporate_action"), weights=(40, 35, 15, 10))[0]
            if domain == "entity":
                ref, asset_class = rng.choice(u.entities).entity_id, "n/a"
            else:
                s = rng.choice(u.securities)
                ref, asset_class = s.security_id, s.asset_class
            if age <= 10:
                status = rng.choices(("open", "in_review", "closed"), weights=(70, 20, 10))[0]
            else:
                status = rng.choices(("closed", "open", "in_review"), weights=(85, 10, 5))[0]
            opened = at(d, 7) + timedelta(minutes=rng.randrange(600))
            closed = min(opened + timedelta(hours=rng.uniform(2, 120)), cap) if status == "closed" else None
            n += 1
            table.add(f"EX{n:07d}", rng.choice(rules_by_domain[domain]), domain, ref, asset_class, status,
                      rng.choice(STEWARDS), opened, closed)


def _change_requests(rng: random.Random, u: Universe, table: TableData) -> None:
    n = 0
    for d in u.days:
        age = (u.cfg.as_of - d).days
        for _ in range(rng.randint(3, 7)):
            domain = rng.choice(("security", "entity", "price", "corporate_action"))
            ref = rng.choice(u.entities).entity_id if domain == "entity" else rng.choice(u.securities).security_id
            maker, checker = rng.sample(STEWARDS, 2)
            if age <= 2 and rng.random() < 0.6:
                status, checker = "pending", None
            else:
                status = "approved" if rng.random() < 0.9 else "rejected"
            n += 1
            table.add(f"CR{n:06d}", domain, ref, maker, checker, status, at(d, 9) + timedelta(minutes=rng.randrange(480)))
```

- [ ] **Step 4: Run tests** — `cd backend && uv run pytest tests/sim/test_refmaster_projection.py -q` → `4 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/project_refmaster.py backend/tests/sim/test_refmaster_projection.py
git commit -m "feat(sim): RefMaster projection

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: MarketMaster projection (price-conflict and stale-price stories)

**Files:**
- Create: `backend/prism/sim/project_marketmaster.py`
- Test: `backend/tests/sim/test_marketmaster_projection.py`

**Interfaces:**
- Consumes: `Universe`, `VENDORS`, `VENDOR_RANK`, `VENDOR_COVERAGE`, `ASSET_CLASSES` (Task 3).
- Produces: `project_marketmaster(u) -> dict[str, TableData]` with keys `vendors`, `instruments`, `golden_prices`, `vendor_prices`, `price_suspects`, `dq_stage_metrics`, `esg_scores`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from prism.sim.project_marketmaster import project_marketmaster
from prism.sim.universe import VENDOR_COVERAGE


@pytest.fixture(scope="module")
def mm(universe):
    return project_marketmaster(universe)


def test_vendor_a_drives_corp_bond_conflicts_this_week(mm, universe):
    this_week, prev_week = set(universe.days[-5:]), set(universe.days[-10:-5])
    conflicts = [r for r in mm["price_suspects"].dicts() if r["kind"] == "conflict"]
    now = [r for r in conflicts if r["price_date"] in this_week]
    before = [r for r in conflicts if r["price_date"] in prev_week]
    story = [r for r in now if (r["vendor_id"], r["asset_class"]) == ("V_A", "Corp bond")]
    assert len(story) / len(now) >= 0.5
    assert 0.05 <= len(now) / len(before) - 1 <= 0.45


def test_stale_prices_are_carried_forward_and_flagged(mm, universe):
    st = universe.stories
    stale_days = set(universe.days[-st.stale_days:])
    golden = [r for r in mm["golden_prices"].dicts() if r["security_id"] in st.stale_security_ids]
    assert all(r["rule"] == "carry_forward" for r in golden if r["price_date"] in stale_days)
    flagged = {(r["security_id"], r["price_date"]) for r in mm["price_suspects"].dicts()
               if r["kind"] == "stale" and r["status"] != "resolved"}
    assert flagged == {(sid, d) for sid in st.stale_security_ids for d in stale_days}


def test_vendor_quotes_only_from_covering_vendors_within_window(mm, universe):
    window = set(universe.days[-universe.cfg.vendor_window_days:])
    for r in mm["vendor_prices"].dicts():
        assert r["asset_class"] in VENDOR_COVERAGE[r["vendor_id"]]
        assert r["price_date"] in window


def test_dq_stage_counts_are_consistent(mm):
    by_key: dict = {}
    for r in mm["dq_stage_metrics"].dicts():
        by_key.setdefault((r["business_date"], r["domain"]), {})[r["stage"]] = r["count"]
    for stages in by_key.values():
        assert stages["validated"] + stages["suspect"] == stages["acquired"]
        assert stages["approved"] >= stages["validated"]
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/sim/test_marketmaster_projection.py -q` → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `backend/prism/sim/project_marketmaster.py`**

```python
"""MarketMaster EDM: multi-vendor prices, golden copy, price suspects, DQ stage metrics, ESG."""
import itertools
import random
from collections import Counter
from datetime import timedelta

from prism.sim.calendar import at
from prism.sim.model import TableData
from prism.sim.universe import ASSET_CLASSES, VENDOR_COVERAGE, VENDOR_RANK, VENDORS, Universe

STAGES = ("acquired", "validated", "suspect", "approved", "distributed")
ESG_PROVIDERS = ("ESG Provider 1", "ESG Provider 2")


def project_marketmaster(u: Universe) -> dict[str, TableData]:
    rng = random.Random(u.cfg.seed + 2)
    n, st = len(u.days), u.stories
    t = {
        "vendors": TableData(("vendor_id", "name", "rank_default")),
        "instruments": TableData(("security_id", "isin", "name", "asset_class", "ccy")),
        "golden_prices": TableData(("security_id", "price_date", "value", "chosen_vendor_id", "rule", "asset_class")),
        "vendor_prices": TableData(("security_id", "vendor_id", "price_date", "price_type", "value", "ccy",
                                    "received_at", "asset_class")),
        "price_suspects": TableData(("suspect_id", "security_id", "vendor_id", "price_date", "kind", "deviation_pct",
                                     "status", "asset_class")),
        "dq_stage_metrics": TableData(("business_date", "domain", "stage", "count", "sla_met")),
        "esg_scores": TableData(("entity_id", "provider", "as_of", "score")),
    }
    for vid, name in VENDORS:
        t["vendors"].add(vid, name, VENDOR_RANK[vid])
    for s in u.securities:
        t["instruments"].add(s.security_id, s.isin, s.name, s.asset_class, s.ccy)

    covering = {ac: [v for v, _ in VENDORS if ac in VENDOR_COVERAGE[v]] for ac in ASSET_CLASSES}
    golden_vendor = {ac: min(covering[ac], key=VENDOR_RANK.__getitem__) for ac in ASSET_CLASSES}
    stale = set(st.stale_security_ids)
    for i, d in enumerate(u.days):
        for s in u.securities:
            is_stale = s.security_id in stale and i >= n - st.stale_days
            t["golden_prices"].add(s.security_id, d, u.golden(s.security_id, i), golden_vendor[s.asset_class],
                                   "carry_forward" if is_stale else "vendor_rank", s.asset_class)

    window = range(n - min(u.cfg.vendor_window_days, n), n)
    conflicts = _pick_conflicts(rng, u, window)
    seq = itertools.count(1)

    def add_suspect(sid, vid, i, kind, deviation, asset_class):
        if i >= n - 3:
            status = rng.choices(("open", "under_review"), weights=(80, 20))[0]
        else:
            status = "resolved" if rng.random() < 0.92 else "open"
        dev = round(deviation, 4) if deviation is not None else None
        t["price_suspects"].add(f"PS{next(seq):07d}", sid, vid, u.days[i], kind, dev, status, asset_class)

    for i in window:
        d = u.days[i]
        for s in u.securities:
            true_px = u.prices[s.security_id][i]
            for vid in covering[s.asset_class]:
                if rng.random() < 0.0005:
                    add_suspect(s.security_id, vid, i, "missing", None, s.asset_class)
                    continue
                dev = conflicts.get((s.security_id, vid, i))
                if dev is not None:
                    value = true_px * (1 + dev / 100)
                    add_suspect(s.security_id, vid, i, "conflict", dev, s.asset_class)
                elif rng.random() < 0.0003:
                    dev = rng.choice((-1, 1)) * rng.uniform(8, 20)
                    value = true_px * (1 + dev / 100)
                    add_suspect(s.security_id, vid, i, "spike", dev, s.asset_class)
                else:
                    value = true_px * (1 + rng.gauss(0, 0.0005))
                t["vendor_prices"].add(s.security_id, vid, d, "close", round(value, 6), s.ccy,
                                       at(d, 17, 30) + timedelta(minutes=rng.randrange(120)), s.asset_class)
    for sid in st.stale_security_ids:
        s = u.security_by_id[sid]
        for i in range(n - st.stale_days, n):
            golden, true_px = u.golden(sid, i), u.prices[sid][i]
            add_suspect(sid, golden_vendor[s.asset_class], i, "stale", (true_px - golden) / golden * 100, s.asset_class)
            if t["price_suspects"].rows[-1][6] == "resolved":  # never happens (i >= n-3), guard for clarity
                raise AssertionError("stale story suspects must be unresolved")

    _dq_metrics(rng, u, t, covering, window.start)
    _esg(rng, u, t["esg_scores"])
    return t


def _pick_conflicts(rng: random.Random, u: Universe, window: range) -> dict[tuple[str, str, int], float]:
    st, n = u.stories, len(u.days)
    this_week, prev_week = range(n - 5, n), range(n - 10, n - 5)
    story_cell = (st.conflict_vendor_id, st.conflict_asset_class)
    out: dict[tuple[str, str, int], float] = {}
    for i in window:
        for vid, _ in VENDORS:
            for ac in VENDOR_COVERAGE[vid]:
                if (vid, ac) == story_cell and i in this_week:
                    k = st.conflict_daily_this_week
                elif (vid, ac) == story_cell and i in prev_week:
                    k = st.conflict_daily_prev_week
                else:
                    r = rng.random()
                    k = 2 if r < 0.05 else 1 if r < 0.30 else 0
                pool = [s.security_id for s in u.securities_by_class[ac]]
                for sid in rng.sample(pool, min(k, len(pool))):
                    out[(sid, vid, i)] = rng.choice((-1, 1)) * rng.uniform(1.0, 4.0)
    return out


def _dq_metrics(rng, u, t, covering, window_start) -> None:
    date_col = t["price_suspects"].columns.index("price_date")
    suspects_per_day = Counter(row[date_col] for row in t["price_suspects"].rows)
    obs_per_day = sum(len(covering[s.asset_class]) for s in u.securities)
    n = len(u.days)
    for i, d in enumerate(u.days):
        for domain in ("security", "price", "entity"):
            if domain == "price":
                acquired = obs_per_day
            elif domain == "security":
                acquired = max(1, int(len(u.securities) * 0.05 * rng.uniform(0.8, 1.2)))
            else:
                acquired = max(1, int(len(u.entities) * 0.02 * rng.uniform(0.8, 1.2)))
            if domain == "price" and i >= window_start:
                suspect = suspects_per_day[d]
            else:
                suspect = int(acquired * rng.uniform(0.002, 0.008))
            validated = acquired - suspect
            approved = validated + (int(suspect * 0.8) if i < n - 1 else 0)
            sla_met = rng.random() > 0.05
            for stage, count in zip(STAGES, (acquired, validated, suspect, approved, approved)):
                t["dq_stage_metrics"].add(d, domain, stage, count, sla_met)


def _esg(rng: random.Random, u: Universe, table: TableData) -> None:
    n = len(u.days)
    dates = (u.days[max(0, n - 22)], u.days[-1])
    for e in u.entities:
        if e.sector == "Sovereign":
            continue
        for provider in ESG_PROVIDERS:
            base = rng.uniform(20, 90)
            for d in dates:
                table.add(e.entity_id, provider, d, round(min(100.0, max(0.0, base + rng.gauss(0, 3))), 2))
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/sim/test_marketmaster_projection.py -q` → `4 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/project_marketmaster.py backend/tests/sim/test_marketmaster_projection.py
git commit -m "feat(sim): MarketMaster projection with conflict and stale-price stories

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: CashRecon projection (aged USD breaks story)

**Files:**
- Create: `backend/prism/sim/project_cashrecon.py`
- Test: `backend/tests/sim/test_cashrecon_projection.py`

**Interfaces:**
- Produces: `project_cashrecon(u) -> dict[str, TableData]` with keys, in load order: `private.cash_accounts`, `statements`, `statement_entries`, `ledger_entries`, `match_rules`, `match_groups`, `match_items`, `breaks`, `break_actions`.
  - `match_rules` is placed before `match_groups` in dict order via explicit construction.
  - Every table except `match_rules` carries a denormalised `region`, which is the RLS dimension.

- [ ] **Step 1: Write the failing test**

```python
from collections import Counter, defaultdict

import pytest

from prism.sim.project_cashrecon import project_cashrecon


@pytest.fixture(scope="module")
def cr(universe):
    return project_cashrecon(universe)


def test_aged_usd_breaks_concentrate_on_story_entity(cr, universe):
    aged = [b for b in cr["breaks"].dicts() if b["ccy"] == "USD" and b["status"] != "closed" and b["age_days"] > 5]
    by_entity = Counter(b["legal_entity_id"] for b in aged)
    top_entity, top_count = by_entity.most_common(1)[0]
    assert top_entity == universe.stories.usd_break_entity_id
    assert top_count / len(aged) >= 0.5


def test_statement_balances_chain(cr):
    by_account = defaultdict(list)
    for s in cr["statements"].dicts():
        by_account[s["account_id"]].append(s)
    for stmts in by_account.values():
        stmts.sort(key=lambda s: s["value_date"])
        for prev, cur in zip(stmts, stmts[1:]):
            assert cur["opening_bal"] == prev["closing_bal"]


def test_every_match_has_both_sides(cr):
    sides = defaultdict(set)
    for m in cr["match_items"].dicts():
        sides[m["match_id"]].add(m["side"])
    assert sides and all(s == {"ledger", "statement"} for s in sides.values())


def test_break_state_is_consistent(cr):
    for b in cr["breaks"].dicts():
        assert (b["root_cause"] is None) == (b["status"] != "closed")
        assert b["amount"] > 0
```

- [ ] **Step 2: Run to verify failure** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `backend/prism/sim/project_cashrecon.py`**

```python
"""CashRecon: nostro statements (MT940/MT950/camt.053) vs ledger, matching, breaks and their workflow."""
import random
from datetime import date, timedelta

from prism.sim.calendar import at
from prism.sim.model import TableData, id_sequence
from prism.sim.universe import CashAccount, Universe

MATCH_RULES = (
    ("MR01", "Exact 1:1 amount and reference", "1:1", 0.00, 0),
    ("MR02", "Amount tolerance 1:1", "1:1", 1.00, 0),
    ("MR03", "Date tolerance 1:1", "1:1", 0.00, 2),
    ("MR04", "Aggregate 1:N by reference", "1:N", 0.00, 1),
    ("MR05", "Manual match", "N:M", None, None),
)
MSG_TYPES = ("MT940", "MT950", "CAMT053")
OUTCOMES = ("matched", "missing_statement", "missing_ledger", "amount_diff", "date_diff")
OUTCOME_WEIGHTS = (965, 12, 10, 8, 5)
ROOT_CAUSES = ("timing", "bank_fee", "fx_rounding", "missing_booking", "duplicate_entry", "bank_error")
NARRATIVES = ("Incoming payment", "Outgoing payment", "FX settlement", "Coupon received",
              "Custody fee", "Margin call", "Dividend received", "Interest")


def ops_users(region: str) -> tuple[str, ...]:
    return tuple(f"ops.{region.lower()}{k:02d}" for k in (1, 2, 3))


def project_cashrecon(u: Universe) -> dict[str, TableData]:
    rng, nid = random.Random(u.cfg.seed + 3), id_sequence()
    t = {
        "private.cash_accounts": TableData(("account_id", "legal_entity_id", "bank_source_id", "bank_bic",
                                            "nostro_no", "ccy", "region")),
        "statements": TableData(("stmt_id", "account_id", "msg_type", "stmt_no", "value_date", "opening_bal",
                                 "closing_bal", "region")),
        "statement_entries": TableData(("entry_id", "stmt_id", "account_id", "value_date", "amount", "dc",
                                        "reference", "narrative", "region")),
        "ledger_entries": TableData(("entry_id", "account_id", "gl_ref", "amount", "dc", "booking_date",
                                     "reference", "region")),
        "match_rules": TableData(("rule_id", "name", "cardinality", "tol_amount", "tol_days")),
        "match_groups": TableData(("match_id", "rule_id", "status", "matched_at", "matched_by", "region")),
        "match_items": TableData(("match_id", "side", "entry_id", "region")),
        "breaks": TableData(("break_id", "account_id", "legal_entity_id", "break_type", "amount", "ccy",
                             "opened_on", "age_days", "status", "owner", "root_cause", "region")),
        "break_actions": TableData(("action_id", "break_id", "action", "actor", "ts", "comment", "region")),
    }
    for rule in MATCH_RULES:
        t["match_rules"].add(*rule)
    for a in u.cash_accounts:
        t["private.cash_accounts"].add(a.account_id, a.legal_entity_id, a.bank_source_id, a.bank_bic,
                                       a.nostro_no, a.ccy, a.region)
        _account_activity(rng, u, a, t, nid)
    _usd_break_story(rng, u, t, nid)
    return t


def _account_activity(rng, u: Universe, a: CashAccount, t, nid) -> None:
    users = ops_users(a.region)
    msg_type = MSG_TYPES[int(a.bank_source_id[3:]) % len(MSG_TYPES)]
    balance = round(rng.uniform(1e6, 5e7), 2)
    for i, d in enumerate(u.days):
        stmt_id, lines = nid("ST"), []
        for _ in range(rng.randint(3, 10)):
            amount = round(rng.lognormvariate(9, 1.5), 2)
            dc, ref = rng.choice("CD"), nid("REF", 9)
            outcome = rng.choices(OUTCOMES, weights=OUTCOME_WEIGHTS)[0]
            ledger_id = stmt_entry_id = None
            stmt_amount = amount
            if outcome != "missing_ledger":
                ledger_id = nid("LGE")
                booking = u.days[i - 1] if outcome == "date_diff" and i > 0 else d
                t["ledger_entries"].add(ledger_id, a.account_id, f"GL-{a.ccy}-{rng.randint(1000, 1999)}",
                                        amount, dc, booking, ref, a.region)
            if outcome != "missing_statement":
                if outcome == "amount_diff":
                    stmt_amount = round(abs(amount + rng.choice((-1, 1)) * rng.uniform(0.5, 250)), 2)
                stmt_entry_id = nid("STE")
                lines.append((stmt_entry_id, stmt_amount, dc, ref))
            if outcome == "matched":
                _add_match(rng, t, nid, a, d, ledger_id, stmt_entry_id, users)
            else:
                diff = abs(amount - stmt_amount) if outcome == "amount_diff" else amount
                _add_break(rng, u, t, nid, a, d, outcome, max(diff, 0.01), users)
        opening = balance
        for entry_id, amt, dc, ref in lines:
            t["statement_entries"].add(entry_id, stmt_id, a.account_id, d, amt, dc, ref, rng.choice(NARRATIVES), a.region)
            balance += amt if dc == "C" else -amt
        balance = round(balance, 2)
        t["statements"].add(stmt_id, a.account_id, msg_type, i + 1, d, round(opening, 2), balance, a.region)


def _add_match(rng, t, nid, a: CashAccount, d: date, ledger_id: str, stmt_entry_id: str, users) -> None:
    rule = rng.choices(("MR01", "MR02", "MR04", "MR05"), weights=(88, 5, 2, 5))[0]
    manual = rule == "MR05"
    match_id = nid("M")
    matched_at = at(d, 19) + timedelta(days=1 if manual else 0, minutes=rng.randrange(120))
    t["match_groups"].add(match_id, rule, "manual" if manual else "auto", matched_at,
                          rng.choice(users) if manual else "system", a.region)
    t["match_items"].add(match_id, "ledger", ledger_id, a.region)
    t["match_items"].add(match_id, "statement", stmt_entry_id, a.region)


def _add_break(rng, u: Universe, t, nid, a: CashAccount, d: date, break_type: str, amount: float,
               users, force_open: bool = False) -> None:
    age = (u.cfg.as_of - d).days
    closed = False if force_open else rng.random() < (0.9 if age > 7 else 0.6 if age > 2 else 0.2)
    break_id, owner = nid("BRK", 6), rng.choice(users)
    if closed:
        status, age_days, cause = "closed", rng.randint(1, 6), rng.choice(ROOT_CAUSES)
    else:
        status, age_days, cause = rng.choice(("open", "open", "investigating")), age, None
    t["breaks"].add(break_id, a.account_id, a.legal_entity_id, break_type, round(amount, 2), a.ccy, d, age_days,
                    status, owner, cause, a.region)
    opened = at(d, 20)
    t["break_actions"].add(nid("BA"), break_id, "opened", "system", opened, None, a.region)
    t["break_actions"].add(nid("BA"), break_id, "assigned", "system", opened + timedelta(minutes=5),
                           f"Assigned to {owner}", a.region)
    if closed:
        t["break_actions"].add(nid("BA"), break_id, "commented", owner, opened + timedelta(hours=12),
                               f"Root cause: {cause}", a.region)
        t["break_actions"].add(nid("BA"), break_id, "closed", owner, opened + timedelta(days=age_days), None, a.region)


def _usd_break_story(rng, u: Universe, t, nid) -> None:
    st = u.stories
    accounts = {a.account_id: a for a in u.cash_accounts}
    eligible = [d for d in u.days if 6 <= (u.cfg.as_of - d).days <= 20]
    for k in range(st.usd_break_extra):
        a = accounts[st.usd_break_account_ids[k % len(st.usd_break_account_ids)]]
        _add_break(rng, u, t, nid, a, rng.choice(eligible), rng.choice(("amount_diff", "missing_statement")),
                   rng.uniform(5_000, 750_000), ops_users(a.region), force_open=True)
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/sim/test_cashrecon_projection.py -q` → `4 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/project_cashrecon.py backend/tests/sim/test_cashrecon_projection.py
git commit -m "feat(sim): CashRecon projection with aged USD breaks story

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: AssetRecon projection (late-custodian and NAV stories)

**Files:**
- Create: `backend/prism/sim/keys.py`, `backend/prism/sim/project_assetrecon.py`
- Test: `backend/tests/sim/test_assetrecon_projection.py`

**Interfaces:**
- Produces:
  - `keys.feed_id(source_id: str, data_type: str) -> str`, returning e.g. `"FD-SRC001-positions"`.
  - `keys.delivery_id(source_id: str, data_type: str, d: date) -> str`, returning e.g. `"DLV-FD-SRC001-positions-20260930"`.
  - `project_assetrecon(u) -> dict[str, TableData]` with keys `custodians`, `portfolios`, `internal_positions`, `custodian_positions`, `internal_transactions`, `custodian_transactions`, `recon_runs`, `recon_exceptions`, `nav_checks`. Row-scoped tables carry `fund_group`.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from prism.sim.project_assetrecon import project_assetrecon


@pytest.fixture(scope="module")
def ar(universe):
    return project_assetrecon(universe)


def test_late_custodian_causes_position_exceptions(ar, universe):
    st, n = universe.stories, len(universe.days)
    window_start = n - universe.cfg.position_window_days
    late_days = set(universe.days[st.late_start_idx:])
    before_days = set(universe.days[window_start:st.late_start_idx])
    exc = [r for r in ar["recon_exceptions"].dicts() if r["portfolio_id"] in st.late_portfolio_ids]
    late_rate = sum(r["business_date"] in late_days for r in exc) / len(late_days)
    before_rate = max(1, sum(r["business_date"] in before_days for r in exc)) / len(before_days)
    assert late_rate >= 3 * before_rate
    assert all(r["cause_code"] == "stale_custodian_data" for r in exc if r["business_date"] in late_days)


def test_stale_prices_create_nav_breaks_only_for_story_funds(ar, universe):
    st = universe.stories
    stale_days = set(universe.days[-st.stale_days:])
    for r in ar["nav_checks"].dicts():
        if r["portfolio_id"] in st.nav_portfolio_ids and r["nav_date"] in stale_days:
            assert abs(r["diff_bps"]) > 5
        else:
            assert abs(r["diff_bps"]) < 3


def test_ibor_and_abor_books_are_both_present(ar):
    books = [r["book"] for r in ar["internal_positions"].dicts()]
    assert books.count("IBOR") == books.count("ABOR") > 0


def test_late_custodian_positions_point_at_last_good_delivery(ar, universe):
    st = universe.stories
    last_good = universe.days[st.late_start_idx - 1].strftime("%Y%m%d")
    late_days = set(universe.days[st.late_start_idx:])
    for r in ar["custodian_positions"].dicts():
        if r["portfolio_id"] in st.late_portfolio_ids and r["as_of"] in late_days:
            assert r["delivery_id"].endswith(last_good)
```

- [ ] **Step 2: Run to verify failure** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `keys.py` and `project_assetrecon.py`**

`backend/prism/sim/keys.py`:
```python
"""Identifier conventions shared across platforms (FeedHub deliveries referenced by AssetRecon)."""
from datetime import date


def feed_id(source_id: str, data_type: str) -> str:
    return f"FD-{source_id}-{data_type}"


def delivery_id(source_id: str, data_type: str, d: date) -> str:
    return f"DLV-{feed_id(source_id, data_type)}-{d:%Y%m%d}"
```

`backend/prism/sim/project_assetrecon.py`:
```python
"""AssetRecon: internal (IBOR/ABOR) vs custodian positions & transactions, recon runs, exceptions, NAV checks."""
import random
from datetime import timedelta

from prism.sim.keys import delivery_id
from prism.sim.model import TableData, id_sequence
from prism.sim.universe import Portfolio, Universe

BASE_CAUSES = ("trade_date_vs_settle_date", "corporate_action_pending", "custodian_booking_error", "fx_rate", "unknown")
TXN_TYPES = ("buy", "sell", "dividend", "fee")
RECON_TYPES = ("position", "cash", "transaction", "nav")
INV_OPS = ("invops01", "invops02", "invops03", "invops04")


def project_assetrecon(u: Universe) -> dict[str, TableData]:
    rng, nid = random.Random(u.cfg.seed + 4), id_sequence()
    n = len(u.days)
    t = {
        "custodians": TableData(("custodian_id", "name", "feed_source_id")),
        "portfolios": TableData(("portfolio_id", "name", "fund_group", "base_ccy", "custodian_id", "region")),
        "internal_positions": TableData(("portfolio_id", "security_id", "book", "qty", "mv", "as_of", "fund_group")),
        "custodian_positions": TableData(("portfolio_id", "security_id", "qty", "mv", "as_of", "delivery_id",
                                          "fund_group")),
        "internal_transactions": TableData(("txn_id", "portfolio_id", "security_id", "trade_date", "settle_date",
                                            "txn_type", "qty", "amount", "fund_group")),
        "custodian_transactions": TableData(("txn_id", "portfolio_id", "security_id", "trade_date", "settle_date",
                                             "txn_type", "qty", "amount", "internal_ref", "fund_group")),
        "recon_runs": TableData(("run_id", "portfolio_id", "recon_type", "business_date", "matched", "unmatched",
                                 "status", "signed_off_by", "fund_group")),
        "recon_exceptions": TableData(("exc_id", "run_id", "portfolio_id", "security_id", "business_date", "diff_qty",
                                       "diff_mv", "cause_code", "assigned_to", "status", "sla_due", "fund_group")),
        "nav_checks": TableData(("portfolio_id", "nav_date", "admin_nav", "internal_nav", "diff_bps", "fund_group")),
    }
    custodian_ids: dict[str, str] = {}
    for k, s in enumerate(x for x in u.sources if x.source_type == "custodian"):
        custodian_ids[s.source_id] = f"CUS{k + 1:02d}"
        t["custodians"].add(custodian_ids[s.source_id], s.name, s.source_id)
    window = range(n - min(u.cfg.position_window_days, n), n)
    for p in u.portfolios:
        t["portfolios"].add(p.portfolio_id, p.name, p.fund_group, p.base_ccy, custodian_ids[p.custodian_source_id],
                            p.region)
        _transactions(rng, u, p, t, nid)
        for i in window:
            _positions_day(rng, u, p, i, t, nid)
    return t


def _is_late(u: Universe, p: Portfolio, i: int) -> bool:
    return p.custodian_source_id == u.stories.late_custodian_source_id and i >= u.stories.late_start_idx


def _positions_day(rng, u: Universe, p: Portfolio, i: int, t, nid) -> None:
    st, n, d, fg = u.stories, len(u.days), u.days[i], p.fund_group
    late = _is_late(u, p, i)
    delivered_on = u.days[st.late_start_idx - 1] if late else d
    dlv = delivery_id(p.custodian_source_id, "positions", delivered_on)
    p_exception = 0.12 if late else 0.008
    exceptions, internal_nav, admin_nav = [], 0.0, 0.0
    for sid, qty in u.holdings[p.portfolio_id]:
        golden, true_px = u.golden(sid, i), u.prices[sid][i]
        internal_nav += qty * golden
        admin_nav += qty * true_px
        mv = round(qty * golden, 2)
        for book in ("IBOR", "ABOR"):
            t["internal_positions"].add(p.portfolio_id, sid, book, qty, mv, d, fg)
        custodian_qty = qty
        if rng.random() < p_exception:
            delta = max(1.0, round(qty * rng.uniform(0.01, 0.05))) * rng.choice((-1, 1))
            custodian_qty = qty + delta
            cause = "stale_custodian_data" if late else rng.choice(BASE_CAUSES)
            exceptions.append((sid, delta, round(delta * true_px, 2), cause))
        t["custodian_positions"].add(p.portfolio_id, sid, custodian_qty, round(custodian_qty * true_px, 2), d, dlv, fg)

    for recon_type in RECON_TYPES:
        if recon_type == "position":
            unmatched = len(exceptions)
            matched = len(u.holdings[p.portfolio_id]) - unmatched
        elif recon_type == "nav":
            unmatched = int(p.portfolio_id in st.nav_portfolio_ids and i >= n - st.stale_days)
            matched = 1 - unmatched
        else:
            unmatched, matched = rng.choices((0, 1, 2), weights=(85, 12, 3))[0], rng.randint(20, 120)
        if i == n - 1:
            status, signer = "in_progress", None
        elif unmatched and i >= n - 3:
            status, signer = "exceptions_open", None
        else:
            status, signer = "signed_off", rng.choice(INV_OPS)
        run_id = nid("RR")
        t["recon_runs"].add(run_id, p.portfolio_id, recon_type, d, matched, unmatched, status, signer, fg)
        if recon_type == "position":
            for sid, diff_qty, diff_mv, cause in exceptions:
                is_open = i >= n - 3 or rng.random() < 0.15
                t["recon_exceptions"].add(nid("RX"), run_id, p.portfolio_id, sid, d, diff_qty, diff_mv, cause,
                                          rng.choice(INV_OPS), "open" if is_open else "closed",
                                          d + timedelta(days=2), fg)

    admin = admin_nav * (1 + rng.gauss(0, 0.00005))
    t["nav_checks"].add(p.portfolio_id, d, round(admin, 2), round(internal_nav, 2),
                        round((internal_nav - admin) / admin * 1e4, 3), fg)


def _transactions(rng, u: Universe, p: Portfolio, t, nid) -> None:
    holdings = u.holdings[p.portfolio_id]
    for i, d in enumerate(u.days):
        late = _is_late(u, p, i)
        for _ in range(rng.randint(0, 3)):
            sid, _ = rng.choice(holdings)
            txn_type = rng.choice(TXN_TYPES)
            qty = float(rng.randint(1, 50) * 100) if txn_type in ("buy", "sell") else 0.0
            amount = round(qty * u.prices[sid][i], 2) if qty else round(rng.uniform(100, 25_000), 2)
            internal_id, settle = nid("ITX"), d + timedelta(days=2)
            t["internal_transactions"].add(internal_id, p.portfolio_id, sid, d, settle, txn_type, qty, amount,
                                           p.fund_group)
            if not late and rng.random() < 0.98:
                t["custodian_transactions"].add(nid("CTX"), p.portfolio_id, sid, d, settle, txn_type, qty, amount,
                                                internal_id, p.fund_group)
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/sim/test_assetrecon_projection.py -q` → `4 passed`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/keys.py backend/prism/sim/project_assetrecon.py backend/tests/sim/test_assetrecon_projection.py
git commit -m "feat(sim): AssetRecon projection with late-custodian and NAV stories

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: FeedHub projection and cross-source integrity

**Files:**
- Create: `backend/prism/sim/project_feedhub.py`
- Test: `backend/tests/sim/test_feedhub_projection.py`

**Interfaces:**
- Consumes: `keys.feed_id`, `keys.delivery_id` (Task 7).
- Produces: `project_feedhub(u) -> dict[str, TableData]` with keys `sources`, `feeds`, `feed_deliveries`, `support_tickets`. All carry `source_type`, the RLS dimension.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from prism.sim.keys import feed_id
from prism.sim.project_assetrecon import project_assetrecon
from prism.sim.project_cashrecon import project_cashrecon
from prism.sim.project_feedhub import project_feedhub


@pytest.fixture(scope="module")
def fh(universe):
    return project_feedhub(universe)


def test_late_custodian_feeds_degrade_after_credential_change(fh, universe):
    st = universe.stories
    late_days = set(universe.days[st.late_start_idx:])
    story = [r for r in fh["feed_deliveries"].dicts() if r["source_id"] == st.late_custodian_source_id]
    during = [r for r in story if r["business_date"] in late_days]
    before = [r for r in story if r["business_date"] not in late_days]
    assert during and all(r["status"] != "on_time" for r in during)
    assert sum(r["status"] != "on_time" for r in before) / len(before) <= 0.25
    tickets = [r for r in fh["support_tickets"].dicts()
               if r["feed_id"] == feed_id(st.late_custodian_source_id, "positions")
               and r["category"] == "credential_change" and r["status"] == "open"]
    assert len(tickets) == 1


def test_delivery_fields_are_consistent(fh):
    for r in fh["feed_deliveries"].dicts():
        if r["status"] == "missing":
            assert r["received_at"] is None and r["record_count"] is None
        if r["status"] == "failed":
            assert r["error_code"] is not None and r["record_count"] is None
        if r["status"] == "late":
            assert r["latency_min"] > 0


def test_cross_source_keys_resolve(fh, universe):
    delivery_ids = {r["delivery_id"] for r in fh["feed_deliveries"].dicts()}
    ar = project_assetrecon(universe)
    assert {r["delivery_id"] for r in ar["custodian_positions"].dicts()} <= delivery_ids
    source_ids = {r["source_id"] for r in fh["sources"].dicts()}
    assert {r["feed_source_id"] for r in ar["custodians"].dicts()} <= source_ids
    cr = project_cashrecon(universe)
    assert {r["bank_source_id"] for r in cr["private.cash_accounts"].dicts()} <= source_ids
```

- [ ] **Step 2: Run to verify failure** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `backend/prism/sim/project_feedhub.py`**

```python
"""FeedHub: custodian/bank/prime-broker feeds, daily delivery monitoring and support tickets."""
import itertools
import random
from datetime import time, timedelta

from prism.sim.calendar import at
from prism.sim.keys import delivery_id, feed_id
from prism.sim.model import TableData
from prism.sim.universe import Universe

FEED_SPECS = {
    "custodian": (("positions", "MT535", time(6, 0)), ("transactions", "CSV", time(6, 0))),
    "bank": (("cash", "MT940", time(7, 0)),),
    "prime_broker": (("positions", "CSV", time(5, 30)), ("transactions", "CSV", time(5, 30))),
}
RECORD_RANGES = {"positions": (50, 5000), "transactions": (10, 800), "cash": (10, 400)}
ERROR_CODES = ("PARSE_ERROR", "SCHEMA_MISMATCH", "AUTH_FAILED")
TICKET_CATEGORIES = ("credential_change", "format_change", "late", "missing_file")


def project_feedhub(u: Universe) -> dict[str, TableData]:
    rng, st = random.Random(u.cfg.seed + 5), u.stories
    t = {
        "sources": TableData(("source_id", "name", "source_type", "bic", "country")),
        "feeds": TableData(("feed_id", "source_id", "data_type", "format", "frequency", "expected_by_utc", "source_type")),
        "feed_deliveries": TableData(("delivery_id", "feed_id", "source_id", "business_date", "status", "received_at",
                                      "latency_min", "record_count", "error_code", "source_type")),
        "support_tickets": TableData(("ticket_id", "feed_id", "source_id", "category", "status", "opened_at",
                                      "closed_at", "source_type")),
    }
    feeds = []
    for k, s in enumerate(u.sources):
        t["sources"].add(s.source_id, s.name, s.source_type, s.bic, s.country)
        for data_type, fmt, expected in FEED_SPECS[s.source_type]:
            if s.source_type == "bank" and k % 2:
                fmt = "CAMT053"
            fid = feed_id(s.source_id, data_type)
            feeds.append((fid, s, data_type, expected))
            t["feeds"].add(fid, s.source_id, data_type, fmt, "daily", expected, s.source_type)

    for fid, s, data_type, expected in feeds:
        story = s.source_id == st.late_custodian_source_id
        for i, d in enumerate(u.days):
            if story and i >= st.late_start_idx:
                status, late_range = rng.choices(("late", "missing", "failed"), weights=(70, 20, 10))[0], (90, 600)
            else:
                status = rng.choices(("on_time", "late", "missing", "failed"), weights=(94, 4, 1, 1))[0]
                late_range = (15, 240)
            due = at(d, expected.hour, expected.minute)
            received = latency = count = error = None
            if status == "on_time":
                received, latency = due - timedelta(minutes=rng.randint(5, 120)), 0
            elif status == "late":
                latency = rng.randint(*late_range)
                received = due + timedelta(minutes=latency)
            elif status == "failed":
                received = due + timedelta(minutes=rng.randint(0, 60))
                error = "AUTH_FAILED" if story else rng.choice(ERROR_CODES)
            if status in ("on_time", "late"):
                count = rng.randint(*RECORD_RANGES[data_type])
            t["feed_deliveries"].add(delivery_id(s.source_id, data_type, d), fid, s.source_id, d, status, received,
                                     latency, count, error, s.source_type)
    _tickets(rng, u, feeds, t["support_tickets"])
    return t


def _tickets(rng: random.Random, u: Universe, feeds, table: TableData) -> None:
    st, seq = u.stories, itertools.count(1)
    story_source = next(s for s in u.sources if s.source_id == st.late_custodian_source_id)
    table.add(f"TK{next(seq):05d}", feed_id(story_source.source_id, "positions"), story_source.source_id,
              "credential_change", "open", at(u.days[st.late_start_idx - 1], 9, 15), None, story_source.source_type)
    for d in u.days:
        if rng.random() >= 0.3:
            continue
        fid, s, _, _ = rng.choice(feeds)
        category = rng.choice(TICKET_CATEGORIES)
        if s.source_id == st.late_custodian_source_id and category == "credential_change":
            category = "format_change"  # keep the story ticket unique
        opened = at(d, 8) + timedelta(minutes=rng.randrange(540))
        closed = (u.cfg.as_of - d).days > 3 and rng.random() < 0.9
        table.add(f"TK{next(seq):05d}", fid, s.source_id, category, "closed" if closed else "open", opened,
                  opened + timedelta(hours=rng.uniform(1, 72)) if closed else None, s.source_type)
```

- [ ] **Step 4: Run all sim tests** — `uv run pytest tests/sim -q` → all pass

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/project_feedhub.py backend/tests/sim/test_feedhub_projection.py
git commit -m "feat(sim): FeedHub projection and cross-source key integrity

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Schemas, signed-context security functions, RLS policies and migration

**Files:**
- Create:
  - `backend/prism/db/__init__.py` (empty)
  - `backend/prism/db/ddl/security.sql`
  - `backend/prism/db/ddl/refmaster.sql`, `marketmaster.sql`, `cashrecon.sql`, `assetrecon.sql`, `feedhub.sql`, `app.sql`
  - `backend/prism/db/policies.py`, `backend/prism/db/migrate.py`
- Test: `backend/tests/db/test_migrate.py`

**Interfaces:**
- Consumes: `Settings`, `LOGICAL_DBS`, `APP_DB` (Task 1).
- Produces:
  - `policies.ROW_SCOPES: dict[str, dict[str, tuple[str, str] | None]]` maps logical table → `(dimension, column)`, or `None` for dataset-level only.
  - `policies.physical_name(db, table) -> str` and `policies.policy_statements(db) -> list[sql.Composed]`.
  - `migrate.migrate(settings) -> None` drops and recreates all prefixed databases.
- SQL: `prism_sec.claims() -> jsonb`, `prism_sec.can(db text, tbl text) -> bool`, `prism_sec.allowed(dim text) -> text[]` and `prism_sec.has_scope(s text) -> bool`. The signed context format is `base64(json) + "." + hex(hmac_sha256(key, base64(json)))`, set with `set_config('app.ctx', …, true)`.

- [ ] **Step 1: Write the failing test** — `backend/tests/db/test_migrate.py`

```python
import psycopg
import pytest

from prism.config import LOGICAL_DBS, Settings
from prism.db.migrate import migrate

pytestmark = pytest.mark.db


@pytest.fixture(scope="module")
def migrated():
    settings = Settings(db_prefix="testmig_")  # separate prefix: must not wipe the session-seeded test_ DBs
    try:
        psycopg.connect(settings.dsn("postgres", admin=True), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc}). Start it with: make db", pytrace=False)
    migrate(settings)
    return settings


def test_every_table_forces_rls_and_has_a_read_policy(migrated):
    for db in LOGICAL_DBS:
        with psycopg.connect(migrated.dsn(db, admin=True)) as conn:
            rows = conn.execute(
                """SELECT n.nspname, c.relname, c.relrowsecurity, c.relforcerowsecurity,
                          (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE c.relkind = 'r' AND n.nspname IN ('public', 'private')"""
            ).fetchall()
            assert rows, f"{db} has no tables"
            for ns, rel, rls, force, policies in rows:
                assert rls and force, f"{db}.{ns}.{rel} lacks forced RLS"
                assert policies == 1, f"{db}.{ns}.{rel} has {policies} policies"


def test_runtime_roles_cannot_bypass_rls(migrated):
    with psycopg.connect(migrated.dsn("postgres", admin=True)) as conn:
        rows = conn.execute(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = ANY(%s)",
            (["bi_reader", "prism_view_owner", migrated.pg_svc_user],),
        ).fetchall()
    assert len(rows) == 3
    assert all(not sup and not bypass for _, sup, bypass in rows)


def test_hmac_key_is_stored_and_hidden(migrated):
    with psycopg.connect(migrated.dsn("cashrecon", admin=True)) as conn:
        assert conn.execute("SELECT k FROM prism_sec.hmac_key").fetchone()[0] == migrated.ctx_hmac_key
        granted = conn.execute(
            "SELECT has_table_privilege('bi_reader', 'prism_sec.hmac_key', 'SELECT')"
        ).fetchone()[0]
    assert granted is False
```

- [ ] **Step 2: Run to verify failure**

Run: `make db && cd backend && uv run pytest tests/db/test_migrate.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.db'`

- [ ] **Step 3: Write `backend/prism/db/ddl/security.sql`**

```sql
-- Signed security context: RLS policies trust only claims whose HMAC verifies with a key
-- the query role cannot read. Context format: base64(json) || '.' || hex(hmac_sha256).
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS prism_sec;
REVOKE ALL ON SCHEMA prism_sec FROM PUBLIC;
CREATE TABLE prism_sec.hmac_key (k text NOT NULL);
REVOKE ALL ON prism_sec.hmac_key FROM PUBLIC;

CREATE OR REPLACE FUNCTION prism_sec.claims() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  raw text := current_setting('app.ctx', true);
  payload text;
  sig text;
  c jsonb;
BEGIN
  IF raw IS NULL OR raw = '' THEN
    RETURN NULL;
  END IF;
  payload := split_part(raw, '.', 1);
  sig := split_part(raw, '.', 2);
  IF encode(public.hmac(payload, (SELECT k FROM prism_sec.hmac_key LIMIT 1), 'sha256'), 'hex') <> sig THEN
    RAISE EXCEPTION 'invalid security context' USING ERRCODE = '42501';
  END IF;
  c := convert_from(decode(payload, 'base64'), 'UTF8')::jsonb;
  IF (c ->> 'exp') IS NULL OR (c ->> 'exp')::bigint < extract(epoch FROM clock_timestamp()) THEN
    RAISE EXCEPTION 'security context expired' USING ERRCODE = '42501';
  END IF;
  RETURN c;
END $$;

CREATE OR REPLACE FUNCTION prism_sec.can(db text, tbl text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT COALESCE((prism_sec.claims() -> 'scopes') ?| ARRAY[db, db || '.' || tbl], false)
$$;

CREATE OR REPLACE FUNCTION prism_sec.allowed(dim text) RETURNS text[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT ARRAY(SELECT jsonb_array_elements_text(prism_sec.claims() -> 'rows' -> dim))
$$;

CREATE OR REPLACE FUNCTION prism_sec.has_scope(s text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT COALESCE((prism_sec.claims() -> 'scopes') ? s, false)
$$;
```

- [ ] **Step 4: Write the per-platform DDL files**

`backend/prism/db/ddl/refmaster.sql`:
```sql
CREATE TABLE legal_entities (
  entity_id text PRIMARY KEY,
  lei char(20) NOT NULL UNIQUE,
  name text NOT NULL,
  country char(2) NOT NULL,
  region text NOT NULL,
  sector text NOT NULL,
  parent_entity_id text REFERENCES legal_entities (entity_id),
  status text NOT NULL
);
CREATE TABLE securities (
  security_id text PRIMARY KEY,
  isin char(12) NOT NULL UNIQUE,
  cusip char(9),
  sedol char(7),
  ticker text,
  name text NOT NULL,
  asset_class text NOT NULL,
  sub_class text NOT NULL,
  ccy char(3) NOT NULL,
  issuer_entity_id text REFERENCES legal_entities (entity_id),
  country char(2) NOT NULL,
  status text NOT NULL,
  valid_from date NOT NULL,
  valid_to date
);
CREATE INDEX ON securities (asset_class);
CREATE TABLE products (product_id text PRIMARY KEY, name text NOT NULL, product_type text NOT NULL);
CREATE TABLE accounts (
  account_id text PRIMARY KEY,
  product_id text NOT NULL REFERENCES products (product_id),
  name text NOT NULL,
  account_type text NOT NULL,
  owner_entity_id text NOT NULL REFERENCES legal_entities (entity_id),
  region text NOT NULL,
  lifecycle_state text NOT NULL
);
CREATE TABLE corporate_actions (
  ca_id text PRIMARY KEY,
  security_id text NOT NULL REFERENCES securities (security_id),
  event_type text NOT NULL,
  ex_date date NOT NULL,
  pay_date date NOT NULL,
  ratio numeric(12, 6),
  status text NOT NULL
);
CREATE TABLE dq_rules (rule_id text PRIMARY KEY, domain text NOT NULL, name text NOT NULL, severity text NOT NULL);
CREATE TABLE exceptions (
  exc_id text PRIMARY KEY,
  rule_id text NOT NULL REFERENCES dq_rules (rule_id),
  domain text NOT NULL,
  record_ref text NOT NULL,
  asset_class text NOT NULL,
  status text NOT NULL,
  assignee text,
  opened_at timestamptz NOT NULL,
  closed_at timestamptz
);
CREATE INDEX ON exceptions (status, domain);
CREATE TABLE change_requests (
  change_id text PRIMARY KEY,
  domain text NOT NULL,
  record_ref text NOT NULL,
  maker text NOT NULL,
  checker text,
  status text NOT NULL,
  created_at timestamptz NOT NULL
);
CREATE TABLE data_dictionary (
  domain text NOT NULL,
  attribute text NOT NULL,
  definition text NOT NULL,
  owner text NOT NULL,
  source text NOT NULL,
  lineage text NOT NULL,
  PRIMARY KEY (domain, attribute)
);
```

`backend/prism/db/ddl/marketmaster.sql`:
```sql
CREATE TABLE vendors (vendor_id text PRIMARY KEY, name text NOT NULL, rank_default int NOT NULL);
CREATE TABLE instruments (
  security_id text PRIMARY KEY,
  isin char(12) NOT NULL UNIQUE,
  name text NOT NULL,
  asset_class text NOT NULL,
  ccy char(3) NOT NULL
);
CREATE TABLE golden_prices (
  security_id text NOT NULL REFERENCES instruments,
  price_date date NOT NULL,
  value numeric(20, 6) NOT NULL,
  chosen_vendor_id text NOT NULL REFERENCES vendors,
  rule text NOT NULL,
  asset_class text NOT NULL,
  PRIMARY KEY (security_id, price_date)
);
CREATE TABLE vendor_prices (
  security_id text NOT NULL REFERENCES instruments,
  vendor_id text NOT NULL REFERENCES vendors,
  price_date date NOT NULL,
  price_type text NOT NULL,
  value numeric(20, 6) NOT NULL,
  ccy char(3) NOT NULL,
  received_at timestamptz NOT NULL,
  asset_class text NOT NULL,
  PRIMARY KEY (security_id, vendor_id, price_date, price_type)
);
CREATE TABLE price_suspects (
  suspect_id text PRIMARY KEY,
  security_id text NOT NULL REFERENCES instruments,
  vendor_id text NOT NULL REFERENCES vendors,
  price_date date NOT NULL,
  kind text NOT NULL,
  deviation_pct numeric(10, 4),
  status text NOT NULL,
  asset_class text NOT NULL
);
CREATE INDEX ON price_suspects (price_date, kind);
CREATE TABLE dq_stage_metrics (
  business_date date NOT NULL,
  domain text NOT NULL,
  stage text NOT NULL,
  count int NOT NULL,
  sla_met boolean NOT NULL,
  PRIMARY KEY (business_date, domain, stage)
);
CREATE TABLE esg_scores (
  entity_id text NOT NULL,
  provider text NOT NULL,
  as_of date NOT NULL,
  score numeric(5, 2) NOT NULL,
  PRIMARY KEY (entity_id, provider, as_of)
);
```

`backend/prism/db/ddl/cashrecon.sql`:
```sql
CREATE SCHEMA private;
CREATE TABLE private.cash_accounts (
  account_id text PRIMARY KEY,
  legal_entity_id text NOT NULL,
  bank_source_id text NOT NULL,
  bank_bic text NOT NULL,
  nostro_no text NOT NULL,
  ccy char(3) NOT NULL,
  region text NOT NULL
);
CREATE TABLE statements (
  stmt_id text PRIMARY KEY,
  account_id text NOT NULL REFERENCES private.cash_accounts,
  msg_type text NOT NULL,
  stmt_no int NOT NULL,
  value_date date NOT NULL,
  opening_bal numeric(20, 2) NOT NULL,
  closing_bal numeric(20, 2) NOT NULL,
  region text NOT NULL
);
CREATE TABLE statement_entries (
  entry_id text PRIMARY KEY,
  stmt_id text NOT NULL REFERENCES statements,
  account_id text NOT NULL,
  value_date date NOT NULL,
  amount numeric(20, 2) NOT NULL,
  dc char(1) NOT NULL,
  reference text NOT NULL,
  narrative text NOT NULL,
  region text NOT NULL
);
CREATE TABLE ledger_entries (
  entry_id text PRIMARY KEY,
  account_id text NOT NULL REFERENCES private.cash_accounts,
  gl_ref text NOT NULL,
  amount numeric(20, 2) NOT NULL,
  dc char(1) NOT NULL,
  booking_date date NOT NULL,
  reference text NOT NULL,
  region text NOT NULL
);
CREATE TABLE match_rules (
  rule_id text PRIMARY KEY,
  name text NOT NULL,
  cardinality text NOT NULL,
  tol_amount numeric(12, 2),
  tol_days int
);
CREATE TABLE match_groups (
  match_id text PRIMARY KEY,
  rule_id text NOT NULL REFERENCES match_rules,
  status text NOT NULL,
  matched_at timestamptz NOT NULL,
  matched_by text NOT NULL,
  region text NOT NULL
);
CREATE TABLE match_items (
  match_id text NOT NULL REFERENCES match_groups,
  side text NOT NULL,
  entry_id text NOT NULL,
  region text NOT NULL,
  PRIMARY KEY (match_id, side, entry_id)
);
CREATE TABLE breaks (
  break_id text PRIMARY KEY,
  account_id text NOT NULL REFERENCES private.cash_accounts,
  legal_entity_id text NOT NULL,
  break_type text NOT NULL,
  amount numeric(20, 2) NOT NULL,
  ccy char(3) NOT NULL,
  opened_on date NOT NULL,
  age_days int NOT NULL,
  status text NOT NULL,
  owner text,
  root_cause text,
  region text NOT NULL
);
CREATE INDEX ON breaks (status, ccy);
CREATE TABLE break_actions (
  action_id text PRIMARY KEY,
  break_id text NOT NULL REFERENCES breaks,
  action text NOT NULL,
  actor text NOT NULL,
  ts timestamptz NOT NULL,
  comment text,
  region text NOT NULL
);
-- Masking view: account numbers are visible only with the pii:read scope.
-- Owned by a non-superuser so the base table's RLS still applies through the view.
CREATE VIEW public.cash_accounts WITH (security_barrier = true) AS
  SELECT account_id, legal_entity_id, bank_source_id, bank_bic,
         CASE WHEN (SELECT prism_sec.has_scope('pii:read')) THEN nostro_no
              ELSE '****' || right(nostro_no, 4) END AS nostro_no,
         ccy, region
  FROM private.cash_accounts;
ALTER VIEW public.cash_accounts OWNER TO prism_view_owner;
GRANT USAGE ON SCHEMA private TO prism_view_owner;
GRANT SELECT ON private.cash_accounts TO prism_view_owner;
```

`backend/prism/db/ddl/assetrecon.sql`:
```sql
CREATE TABLE custodians (custodian_id text PRIMARY KEY, name text NOT NULL, feed_source_id text NOT NULL);
CREATE TABLE portfolios (
  portfolio_id text PRIMARY KEY,
  name text NOT NULL,
  fund_group text NOT NULL,
  base_ccy char(3) NOT NULL,
  custodian_id text NOT NULL REFERENCES custodians,
  region text NOT NULL
);
CREATE TABLE internal_positions (
  portfolio_id text NOT NULL REFERENCES portfolios,
  security_id text NOT NULL,
  book text NOT NULL,
  qty numeric(20, 4) NOT NULL,
  mv numeric(20, 2) NOT NULL,
  as_of date NOT NULL,
  fund_group text NOT NULL,
  PRIMARY KEY (portfolio_id, security_id, book, as_of)
);
CREATE TABLE custodian_positions (
  portfolio_id text NOT NULL REFERENCES portfolios,
  security_id text NOT NULL,
  qty numeric(20, 4) NOT NULL,
  mv numeric(20, 2) NOT NULL,
  as_of date NOT NULL,
  delivery_id text NOT NULL,
  fund_group text NOT NULL,
  PRIMARY KEY (portfolio_id, security_id, as_of)
);
CREATE TABLE internal_transactions (
  txn_id text PRIMARY KEY,
  portfolio_id text NOT NULL REFERENCES portfolios,
  security_id text NOT NULL,
  trade_date date NOT NULL,
  settle_date date NOT NULL,
  txn_type text NOT NULL,
  qty numeric(20, 4) NOT NULL,
  amount numeric(20, 2) NOT NULL,
  fund_group text NOT NULL
);
CREATE TABLE custodian_transactions (
  txn_id text PRIMARY KEY,
  portfolio_id text NOT NULL REFERENCES portfolios,
  security_id text NOT NULL,
  trade_date date NOT NULL,
  settle_date date NOT NULL,
  txn_type text NOT NULL,
  qty numeric(20, 4) NOT NULL,
  amount numeric(20, 2) NOT NULL,
  internal_ref text,
  fund_group text NOT NULL
);
CREATE TABLE recon_runs (
  run_id text PRIMARY KEY,
  portfolio_id text NOT NULL REFERENCES portfolios,
  recon_type text NOT NULL,
  business_date date NOT NULL,
  matched int NOT NULL,
  unmatched int NOT NULL,
  status text NOT NULL,
  signed_off_by text,
  fund_group text NOT NULL
);
CREATE TABLE recon_exceptions (
  exc_id text PRIMARY KEY,
  run_id text NOT NULL REFERENCES recon_runs,
  portfolio_id text NOT NULL,
  security_id text NOT NULL,
  business_date date NOT NULL,
  diff_qty numeric(20, 4) NOT NULL,
  diff_mv numeric(20, 2) NOT NULL,
  cause_code text NOT NULL,
  assigned_to text,
  status text NOT NULL,
  sla_due date NOT NULL,
  fund_group text NOT NULL
);
CREATE TABLE nav_checks (
  portfolio_id text NOT NULL REFERENCES portfolios,
  nav_date date NOT NULL,
  admin_nav numeric(22, 2) NOT NULL,
  internal_nav numeric(22, 2) NOT NULL,
  diff_bps numeric(10, 3) NOT NULL,
  fund_group text NOT NULL,
  PRIMARY KEY (portfolio_id, nav_date)
);
```

`backend/prism/db/ddl/feedhub.sql`:
```sql
CREATE TABLE sources (
  source_id text PRIMARY KEY,
  name text NOT NULL,
  source_type text NOT NULL,
  bic text NOT NULL,
  country char(2) NOT NULL
);
CREATE TABLE feeds (
  feed_id text PRIMARY KEY,
  source_id text NOT NULL REFERENCES sources,
  data_type text NOT NULL,
  format text NOT NULL,
  frequency text NOT NULL,
  expected_by_utc time NOT NULL,
  source_type text NOT NULL
);
CREATE TABLE feed_deliveries (
  delivery_id text PRIMARY KEY,
  feed_id text NOT NULL REFERENCES feeds,
  source_id text NOT NULL,
  business_date date NOT NULL,
  status text NOT NULL,
  received_at timestamptz,
  latency_min int,
  record_count int,
  error_code text,
  source_type text NOT NULL
);
CREATE INDEX ON feed_deliveries (business_date, status);
CREATE TABLE support_tickets (
  ticket_id text PRIMARY KEY,
  feed_id text NOT NULL REFERENCES feeds,
  source_id text NOT NULL,
  category text NOT NULL,
  status text NOT NULL,
  opened_at timestamptz NOT NULL,
  closed_at timestamptz,
  source_type text NOT NULL
);
```

`backend/prism/db/ddl/app.sql`:
```sql
CREATE TABLE seed_info (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  seed int NOT NULL,
  as_of date NOT NULL,
  profile text NOT NULL,
  seeded_at timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 5: Implement `backend/prism/db/policies.py`**

```python
"""Row-level security policy definitions: one SELECT policy per table, dataset scope + optional row dimension."""
from psycopg import sql

# logical table -> (claims dimension, column) or None for dataset-level access only.
ROW_SCOPES: dict[str, dict[str, tuple[str, str] | None]] = {
    "refmaster": {
        "legal_entities": None, "securities": ("asset_class", "asset_class"), "products": None, "accounts": None,
        "corporate_actions": None, "dq_rules": None, "exceptions": ("asset_class", "asset_class"),
        "change_requests": None, "data_dictionary": None,
    },
    "marketmaster": {
        "vendors": None, "instruments": ("asset_class", "asset_class"),
        "golden_prices": ("asset_class", "asset_class"), "vendor_prices": ("asset_class", "asset_class"),
        "price_suspects": ("asset_class", "asset_class"), "dq_stage_metrics": None, "esg_scores": None,
    },
    "cashrecon": {
        "cash_accounts": ("region", "region"), "statements": ("region", "region"),
        "statement_entries": ("region", "region"), "ledger_entries": ("region", "region"), "match_rules": None,
        "match_groups": ("region", "region"), "match_items": ("region", "region"), "breaks": ("region", "region"),
        "break_actions": ("region", "region"),
    },
    "assetrecon": {
        "custodians": None, "portfolios": ("fund_group", "fund_group"),
        "internal_positions": ("fund_group", "fund_group"), "custodian_positions": ("fund_group", "fund_group"),
        "internal_transactions": ("fund_group", "fund_group"), "custodian_transactions": ("fund_group", "fund_group"),
        "recon_runs": ("fund_group", "fund_group"), "recon_exceptions": ("fund_group", "fund_group"),
        "nav_checks": ("fund_group", "fund_group"),
    },
    "feedhub": {
        "sources": ("source_type", "source_type"), "feeds": ("source_type", "source_type"),
        "feed_deliveries": ("source_type", "source_type"), "support_tickets": ("source_type", "source_type"),
    },
}
_PHYSICAL = {("cashrecon", "cash_accounts"): "private.cash_accounts"}


def physical_name(db: str, table: str) -> str:
    return _PHYSICAL.get((db, table), table)


def policy_statements(db: str) -> list[sql.Composed]:
    statements: list[sql.Composed] = []
    for table, scope in ROW_SCOPES[db].items():
        ident = sql.Identifier(*physical_name(db, table).split("."))
        # Scalar sub-selects become init-plans: the signature is verified once per query, not per row.
        dataset = sql.SQL("(SELECT prism_sec.can({}, {}))").format(sql.Literal(db), sql.Literal(table))
        if scope is None:
            condition = dataset
        else:
            dim, column = scope
            allowed = sql.SQL("(SELECT prism_sec.allowed({}))").format(sql.Literal(dim))
            condition = sql.SQL("{ds} AND ({al} @> ARRAY['*'] OR {col} = ANY({al}))").format(
                ds=dataset, al=allowed, col=sql.Identifier(column)
            )
        statements += [
            sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(ident),
            sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(ident),
            sql.SQL("CREATE POLICY prism_read ON {} FOR SELECT USING ({})").format(ident, condition),
        ]
    return statements
```

- [ ] **Step 6: Implement `backend/prism/db/migrate.py`**

```python
"""(Re)create roles, databases, schemas, security functions, RLS policies and grants."""
from pathlib import Path

import psycopg
from psycopg import sql

from prism.config import APP_DB, LOGICAL_DBS, Settings
from prism.db.policies import policy_statements

DDL_DIR = Path(__file__).parent / "ddl"


def _admin(settings: Settings, logical: str, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(settings.dsn(logical, admin=True), autocommit=autocommit)


def ensure_roles(conn: psycopg.Connection, settings: Settings) -> None:
    for role, login in (("bi_reader", False), ("prism_view_owner", False), (settings.pg_svc_user, True)):
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
            conn.execute(sql.SQL("CREATE ROLE {} {} NOBYPASSRLS").format(
                sql.Identifier(role), sql.SQL("LOGIN" if login else "NOLOGIN")))
    conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
        sql.Identifier(settings.pg_svc_user), sql.Literal(settings.pg_svc_password)))
    conn.execute(sql.SQL("GRANT bi_reader TO {}").format(sql.Identifier(settings.pg_svc_user)))


def recreate_databases(conn: psycopg.Connection, settings: Settings) -> None:
    for logical in (*LOGICAL_DBS, APP_DB):
        name = sql.Identifier(settings.dbname(logical))
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(name))


def grant_statements(db_name: str, svc_user: str) -> list[sql.Composable]:
    return [
        sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db_name), sql.Identifier(svc_user)),
        sql.SQL("GRANT USAGE ON SCHEMA public TO bi_reader"),
        sql.SQL("GRANT USAGE ON SCHEMA prism_sec TO bi_reader, prism_view_owner"),
        sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO bi_reader"),
        sql.SQL("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA prism_sec FROM PUBLIC"),
        sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA prism_sec TO bi_reader, prism_view_owner"),
    ]


def migrate(settings: Settings) -> None:
    with _admin(settings, "postgres", autocommit=True) as conn:
        ensure_roles(conn, settings)
        recreate_databases(conn, settings)
    security_sql = (DDL_DIR / "security.sql").read_text()
    for logical in LOGICAL_DBS:
        with _admin(settings, logical) as conn:
            conn.execute(security_sql)
            conn.execute("INSERT INTO prism_sec.hmac_key (k) VALUES (%s)", (settings.ctx_hmac_key,))
            conn.execute((DDL_DIR / f"{logical}.sql").read_text())
            for statement in policy_statements(logical):
                conn.execute(statement)
            for statement in grant_statements(settings.dbname(logical), settings.pg_svc_user):
                conn.execute(statement)
    with _admin(settings, APP_DB) as conn:
        conn.execute((DDL_DIR / "app.sql").read_text())
```

- [ ] **Step 7: Run tests** — `cd backend && uv run pytest tests/db/test_migrate.py -q` → `3 passed`

- [ ] **Step 8: Commit**

```bash
git add backend/prism/db backend/tests/db/test_migrate.py
git commit -m "feat(db): per-platform schemas with signed-context RLS and masking view

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Seeding pipeline, scoped sessions and `prism-seed`

**Files:**
- Create: `backend/prism/db/session.py`, `backend/prism/sim/writer.py`, `backend/prism/sim/seed.py`, `backend/prism/sim/cli.py`
- Create test fixtures and tests: `backend/tests/conftest.py`, `backend/tests/db/test_seed.py`

**Interfaces:**
- Consumes: `migrate` (Task 9), all projectors (Tasks 4–8), `build_universe`, `SimConfig`.
- Produces:
  - `session.sign_ctx(claims: dict, key: str) -> str` and `session.ctx_from_claims(claims: dict, key: str) -> str`. The latter keeps `sub`, `scopes`, `rows` and `exp`.
  - `session.prepare_statements(ctx: str | None, timeout_ms: int) -> list[tuple[str, tuple | None]]`.
  - `session.scoped_sync(settings, logical_db, ctx) -> ContextManager[psycopg.Connection]` (rows returned as dicts).
  - `writer.write_tables(settings, logical_db, tables) -> dict[str, int]`.
  - `seed.PROJECTORS`, `seed.seed_all(settings, cfg) -> dict[str, dict[str, int]]` and `seed.is_seeded(settings) -> bool`.
  - CLI: `prism-seed [--reset] [--check] [--small]`.
  - Pytest fixture `seeded -> Settings` (session scope, `test_` prefix, small profile).

- [ ] **Step 1: Write the fixture and failing tests**

`backend/tests/conftest.py`:
```python
import psycopg
import pytest

from prism.config import Settings
from prism.sim.seed import seed_all
from prism.sim.universe import SimConfig


@pytest.fixture(scope="session")
def seeded() -> Settings:
    settings = Settings(db_prefix="test_")
    try:
        psycopg.connect(settings.dsn("postgres", admin=True), connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc}). Start it with: make db", pytrace=False)
    seed_all(settings, SimConfig.small())
    return settings
```

`backend/tests/db/test_seed.py`:
```python
import time

import psycopg
import pytest
from psycopg import sql

from prism.config import Settings
from prism.db.session import ctx_from_claims, scoped_sync
from prism.sim.seed import PROJECTORS, is_seeded
from prism.sim.universe import SimConfig, build_universe

pytestmark = pytest.mark.db


def test_seed_loads_every_projected_row(seeded):
    u = build_universe(SimConfig.small())
    for db, project in PROJECTORS.items():
        with psycopg.connect(seeded.dsn(db, admin=True)) as conn:
            for name, table in project(u).items():
                ident = sql.Identifier(*name.split("."))
                count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(ident)).fetchone()[0]
                assert count == len(table.rows), f"{db}.{name}"


def test_is_seeded(seeded):
    assert is_seeded(seeded)
    assert not is_seeded(Settings(db_prefix="nothere_"))


def test_is_seeded_false_when_ctx_key_changes(seeded):
    assert not is_seeded(seeded.model_copy(update={"ctx_hmac_key": "a-different-key"}))


def test_signed_context_grants_rows_and_missing_context_grants_none(seeded):
    claims = {"sub": "t", "scopes": ["cashrecon"], "rows": {"region": ["*"]}, "exp": int(time.time()) + 60}
    with scoped_sync(seeded, "cashrecon", ctx_from_claims(claims, seeded.ctx_hmac_key)) as conn:
        assert conn.execute("SELECT count(*) AS n FROM breaks").fetchone()["n"] > 0
    with scoped_sync(seeded, "cashrecon", None) as conn:
        assert conn.execute("SELECT count(*) AS n FROM breaks").fetchone()["n"] == 0
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/db/test_seed.py -q` → FAIL (`ModuleNotFoundError: prism.sim.seed`)

- [ ] **Step 3: Implement `backend/prism/db/session.py`**

```python
"""Per-transaction security context: sign claims, then run queries as bi_reader inside a read-only transaction."""
import base64
import hashlib
import hmac
import json
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from prism.config import Settings


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
    """Must run first inside a fresh transaction."""
    return [
        ("SET TRANSACTION READ ONLY", None),
        ("SET LOCAL ROLE bi_reader", None),
        ("SELECT set_config('app.ctx', %s, true)", (ctx or "",)),
        ("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),)),
    ]


@contextmanager
def scoped_sync(settings: Settings, logical_db: str, ctx: str | None) -> Iterator[psycopg.Connection]:
    with psycopg.connect(settings.dsn(logical_db), row_factory=dict_row) as conn:
        with conn.transaction():
            for statement, args in prepare_statements(ctx, settings.statement_timeout_ms):
                conn.execute(statement, args)
            yield conn
```

- [ ] **Step 4: Implement `writer.py`, `seed.py`, `cli.py`**

`backend/prism/sim/writer.py`:
```python
"""Bulk-load generated tables with COPY (as the admin role, which owns the tables)."""
import psycopg
from psycopg import sql

from prism.config import Settings
from prism.sim.model import TableData


def write_tables(settings: Settings, logical_db: str, tables: dict[str, TableData]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with psycopg.connect(settings.dsn(logical_db, admin=True)) as conn, conn.cursor() as cur:
        for name, table in tables.items():
            statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                sql.Identifier(*name.split(".")), sql.SQL(", ").join(map(sql.Identifier, table.columns))
            )
            with cur.copy(statement) as copy:
                for row in table.rows:
                    copy.write_row(row)
            counts[name] = len(table.rows)
    return counts
```

`backend/prism/sim/seed.py`:
```python
"""Create every platform database and load it from one shared universe."""
import psycopg

from prism.config import APP_DB, Settings
from prism.db.migrate import migrate
from prism.sim.project_assetrecon import project_assetrecon
from prism.sim.project_cashrecon import project_cashrecon
from prism.sim.project_feedhub import project_feedhub
from prism.sim.project_marketmaster import project_marketmaster
from prism.sim.project_refmaster import project_refmaster
from prism.sim.universe import SimConfig, build_universe
from prism.sim.writer import write_tables

PROJECTORS = {
    "refmaster": project_refmaster,
    "marketmaster": project_marketmaster,
    "cashrecon": project_cashrecon,
    "assetrecon": project_assetrecon,
    "feedhub": project_feedhub,
}


def seed_all(settings: Settings, cfg: SimConfig) -> dict[str, dict[str, int]]:
    migrate(settings)
    universe = build_universe(cfg)
    counts = {db: write_tables(settings, db, project(universe)) for db, project in PROJECTORS.items()}
    with psycopg.connect(settings.dsn(APP_DB, admin=True)) as conn:
        conn.execute("INSERT INTO seed_info (seed, as_of, profile) VALUES (%s, %s, %s)",
                     (cfg.seed, cfg.as_of, cfg.profile))
    return counts


def is_seeded(settings: Settings) -> bool:
    """True only if seeding completed AND the databases trust the currently configured context key."""
    try:
        with psycopg.connect(settings.dsn(APP_DB, admin=True), connect_timeout=3) as conn:
            if conn.execute("SELECT 1 FROM seed_info").fetchone() is None:
                return False
        with psycopg.connect(settings.dsn("refmaster", admin=True), connect_timeout=3) as conn:
            row = conn.execute("SELECT k FROM prism_sec.hmac_key").fetchone()
            return row is not None and row[0] == settings.ctx_hmac_key
    except psycopg.Error:
        return False
```

`backend/prism/sim/cli.py`:
```python
"""prism-seed: create and populate the simulated platform databases."""
import argparse
import sys
import time

from prism.config import Settings
from prism.sim.seed import is_seeded, seed_all
from prism.sim.universe import SimConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prism-seed", description="Create and seed the simulated platforms.")
    parser.add_argument("--reset", action="store_true", help="drop and re-seed even if already seeded")
    parser.add_argument("--check", action="store_true", help="exit 0 if seeded with the current key, else 1")
    parser.add_argument("--small", action="store_true", help="small dataset for quick demos")
    args = parser.parse_args(argv)
    settings = Settings()
    if args.check:
        return 0 if is_seeded(settings) else 1
    if is_seeded(settings) and not args.reset:
        print("Already seeded (use --reset to re-seed).")
        return 0
    base = dict(seed=settings.seed, as_of=settings.as_of)
    cfg = SimConfig.small(**base) if args.small else SimConfig(**base)
    started = time.monotonic()
    counts = seed_all(settings, cfg)
    for db, tables in counts.items():
        print(f"{db}: " + ", ".join(f"{name}={n}" for name, n in tables.items()))
    print(f"Seeded ({cfg.profile}) in {time.monotonic() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests** — `uv run pytest tests/db -q` → all pass

- [ ] **Step 6: Seed the full dataset once and note the timing**

Run: `cd backend && uv run prism-seed --reset`
Expected: per-database row counts are printed, ending `Seeded (full) in <N>s`. Expect N < 120 s on a laptop, and about 180k rows each for `golden_prices` and `internal_positions`. If it takes longer than 5 minutes, raise an issue: don't let the implementer optimise silently.

- [ ] **Step 7: Commit**

```bash
git add backend/prism/db/session.py backend/prism/sim/writer.py backend/prism/sim/seed.py backend/prism/sim/cli.py backend/tests/conftest.py backend/tests/db/test_seed.py
git commit -m "feat(sim): seeding pipeline, signed scoped sessions and prism-seed CLI

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Personas, tokens and the RBAC/RLS matrix

**Files:**
- Create:
  - `backend/prism/security/__init__.py` (empty)
  - `backend/prism/security/personas.py`, `backend/prism/security/tokens.py`
  - `backend/prism/security/access.py`, `backend/prism/security/cli.py`
- Test: `backend/tests/security/test_tokens.py`, `backend/tests/db/test_rbac.py`

**Interfaces:**
- Produces:
  - `personas.Persona` and `personas.PERSONAS: dict[str, Persona]`, with ids `steward`, `cash_ops_emea`, `invest_ops_growth`, `bi_analyst` and `head_data`.
  - `personas.claims_for(persona_id: str, ttl_s: int = 300, now: int | None = None) -> dict`, with keys `sub`, `name`, `roles`, `scopes`, `rows`, `metrics_only` and `exp`.
  - `tokens.TokenError`, `tokens.mint(claims, audience, secret, ttl_s=300, now=None) -> str` and `tokens.verify(token, audience, secret) -> dict`.
  - `access.can(claims, db, table) -> bool`.
  - CLI: `prism-token <persona> <audience> [--ttl N]`.

- [ ] **Step 1: Write the failing tests**

`backend/tests/security/test_tokens.py`:
```python
import pytest

from prism.security.access import can
from prism.security.personas import PERSONAS, claims_for
from prism.security.tokens import TokenError, mint, verify

SECRET = "test-secret-0123456789abcdef0123456789abcdef"


def test_round_trip_and_audience_binding():
    token = mint(claims_for("steward"), "refmaster-api", SECRET)
    claims = verify(token, "refmaster-api", SECRET)
    assert claims["sub"] == "steward" and "refmaster" in claims["scopes"]
    with pytest.raises(TokenError):
        verify(token, "marketmaster-api", SECRET)


def test_expired_and_tampered_tokens_are_rejected():
    with pytest.raises(TokenError):
        verify(mint(claims_for("steward"), "refmaster-api", SECRET, ttl_s=-10), "refmaster-api", SECRET)
    with pytest.raises(TokenError):
        verify(mint(claims_for("steward"), "refmaster-api", SECRET), "refmaster-api", SECRET + "x")


def test_dataset_access_mirror():
    assert can(claims_for("invest_ops_growth"), "refmaster", "securities")
    assert not can(claims_for("invest_ops_growth"), "refmaster", "exceptions")
    assert can(claims_for("head_data"), "cashrecon", "breaks")
    assert not can(claims_for("steward"), "cashrecon", "breaks")


def test_personas_are_well_formed():
    assert set(PERSONAS) == {"steward", "cash_ops_emea", "invest_ops_growth", "bi_analyst", "head_data"}
    assert claims_for("bi_analyst")["metrics_only"] is True
    with pytest.raises(KeyError):
        claims_for("nobody")
```

`backend/tests/db/test_rbac.py`:
```python
import time

import psycopg
import pytest
from psycopg import sql

from prism.config import LOGICAL_DBS
from prism.db.policies import ROW_SCOPES, physical_name
from prism.db.session import ctx_from_claims, sign_ctx, scoped_sync
from prism.security.personas import PERSONAS, claims_for

pytestmark = pytest.mark.db


def expected(persona: str, db: str, table: str) -> str:
    """The access specification, written independently of the policy code."""
    if persona in ("head_data", "bi_analyst"):
        return "all"
    if persona == "steward":
        return "all" if db in ("refmaster", "marketmaster") else "none"
    if persona == "cash_ops_emea":
        if db == "cashrecon":
            return "all" if table == "match_rules" else "some"
        return "some" if db == "feedhub" else "none"
    if persona == "invest_ops_growth":
        if db == "assetrecon":
            return "all" if table == "custodians" else "some"
        if db == "feedhub":
            return "some"
        return "all" if (db, table) in {("refmaster", "securities"), ("refmaster", "legal_entities")} else "none"
    raise AssertionError(persona)


@pytest.fixture(scope="module")
def totals(seeded):
    out = {}
    for db in LOGICAL_DBS:
        with psycopg.connect(seeded.dsn(db, admin=True)) as conn:
            for table in ROW_SCOPES[db]:
                ident = sql.Identifier(*physical_name(db, table).split("."))
                out[(db, table)] = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(ident)).fetchone()[0]
                assert out[(db, table)] > 0, f"{db}.{table} is empty; matrix would be ambiguous"
    return out


@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_visibility_matrix(seeded, totals, persona):
    ctx = ctx_from_claims(claims_for(persona), seeded.ctx_hmac_key)
    failures = []
    for db in LOGICAL_DBS:
        with scoped_sync(seeded, db, ctx) as conn:
            for table in ROW_SCOPES[db]:
                n = conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))).fetchone()["n"]
                total = totals[(db, table)]
                got = "none" if n == 0 else "all" if n == total else "some"
                want = expected(persona, db, table)
                if got != want:
                    failures.append(f"{db}.{table}: expected {want}, got {got} ({n}/{total})")
    assert not failures, "\n".join(failures)


def test_row_scopes_are_exact(seeded):
    ctx = ctx_from_claims(claims_for("cash_ops_emea"), seeded.ctx_hmac_key)
    with scoped_sync(seeded, "cashrecon", ctx) as conn:
        assert {r["region"] for r in conn.execute("SELECT DISTINCT region FROM breaks")} == {"EMEA"}
    with scoped_sync(seeded, "feedhub", ctx) as conn:
        assert {r["source_type"] for r in conn.execute("SELECT DISTINCT source_type FROM feeds")} == {"bank"}
    ctx = ctx_from_claims(claims_for("invest_ops_growth"), seeded.ctx_hmac_key)
    with scoped_sync(seeded, "assetrecon", ctx) as conn:
        assert {r["fund_group"] for r in conn.execute("SELECT DISTINCT fund_group FROM portfolios")} == {"Growth"}


def test_account_numbers_are_masked_without_pii_scope(seeded):
    for persona, masked in (("cash_ops_emea", True), ("head_data", False)):
        ctx = ctx_from_claims(claims_for(persona), seeded.ctx_hmac_key)
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            values = [r["nostro_no"] for r in conn.execute("SELECT nostro_no FROM cash_accounts")]
        assert values and all(v.startswith("****") == masked for v in values)


def test_base_table_behind_masking_view_is_not_directly_readable(seeded):
    ctx = ctx_from_claims(claims_for("head_data"), seeded.ctx_hmac_key)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            conn.execute("SELECT nostro_no FROM private.cash_accounts")


def test_forged_or_expired_context_is_rejected(seeded):
    forged = sign_ctx({"sub": "x", "scopes": ["cashrecon"], "rows": {"region": ["*"]},
                       "exp": int(time.time()) + 60}, "not-the-real-key")
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="invalid security context"):
        with scoped_sync(seeded, "cashrecon", forged) as conn:
            conn.execute("SELECT count(*) FROM breaks")
    expired = ctx_from_claims(claims_for("head_data", ttl_s=-5), seeded.ctx_hmac_key)
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match="expired"):
        with scoped_sync(seeded, "cashrecon", expired) as conn:
            conn.execute("SELECT count(*) FROM breaks")


def test_query_cannot_escalate_by_resetting_context(seeded):
    ctx = ctx_from_claims(claims_for("cash_ops_emea"), seeded.ctx_hmac_key)
    forged = sign_ctx({"sub": "x", "scopes": ["cashrecon"], "rows": {"region": ["*"]},
                       "exp": int(time.time()) + 60}, "guess")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            conn.execute("SELECT set_config('app.ctx', %s, true)", (forged,))
            conn.execute("SELECT count(*) FROM breaks")


def test_writes_and_key_reads_are_denied(seeded):
    ctx = ctx_from_claims(claims_for("head_data"), seeded.ctx_hmac_key)
    with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            conn.execute("DELETE FROM breaks")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with scoped_sync(seeded, "cashrecon", ctx) as conn:
            conn.execute("SELECT k FROM prism_sec.hmac_key")
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/security tests/db/test_rbac.py -q` → FAIL (`ModuleNotFoundError: prism.security`)

- [ ] **Step 3: Implement the security modules**

`backend/prism/security/personas.py`:
```python
"""Demo personas (spec §5). Scopes grant datasets ('db' or 'db.table'); rows grant values per RLS dimension."""
import time
from collections.abc import Mapping
from dataclasses import dataclass

ALL_SOURCES = ("refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub")
ALL_ROWS = {"asset_class": ("*",), "region": ("*",), "fund_group": ("*",), "source_type": ("*",)}


@dataclass(frozen=True)
class Persona:
    persona_id: str
    display_name: str
    scopes: tuple[str, ...]
    rows: Mapping[str, tuple[str, ...]]
    metrics_only: bool = False


PERSONAS: dict[str, Persona] = {p.persona_id: p for p in (
    Persona("steward", "Reference Data Steward", ("refmaster", "marketmaster"), {"asset_class": ("*",)}),
    Persona("cash_ops_emea", "Cash Ops Analyst - EMEA", ("cashrecon", "feedhub"),
            {"region": ("EMEA",), "source_type": ("bank",)}),
    Persona("invest_ops_growth", "Investment Ops - Growth Funds",
            ("assetrecon", "feedhub", "refmaster.securities", "refmaster.legal_entities"),
            {"fund_group": ("Growth",), "source_type": ("custodian",), "asset_class": ("*",)}),
    Persona("bi_analyst", "BI Analyst", ALL_SOURCES, ALL_ROWS, metrics_only=True),
    Persona("head_data", "Head of Data Operations", (*ALL_SOURCES, "pii:read"), ALL_ROWS),
)}


def claims_for(persona_id: str, ttl_s: int = 300, now: int | None = None) -> dict:
    p = PERSONAS[persona_id]
    issued = int(time.time() if now is None else now)
    return {
        "sub": p.persona_id,
        "name": p.display_name,
        "roles": [p.persona_id],
        "scopes": list(p.scopes),
        "rows": {dim: list(values) for dim, values in p.rows.items()},
        "metrics_only": p.metrics_only,
        "exp": issued + ttl_s,
    }
```

`backend/prism/security/tokens.py`:
```python
"""Short-lived, audience-bound HS256 JWTs (the MVP stand-in for OIDC + token exchange)."""
import time

import jwt


class TokenError(Exception):
    pass


def mint(claims: dict, audience: str, secret: str, ttl_s: int = 300, now: int | None = None) -> str:
    issued = int(time.time() if now is None else now)
    return jwt.encode({**claims, "aud": audience, "iat": issued, "exp": issued + ttl_s}, secret, algorithm="HS256")


def verify(token: str, audience: str, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"], audience=audience,
                          options={"require": ["exp", "aud", "sub"]})
    except jwt.PyJWTError as exc:
        raise TokenError(f"invalid token: {exc}") from exc
```

`backend/prism/security/access.py`:
```python
"""Python mirror of prism_sec.can() for early, explicit 403s (Postgres RLS remains the enforcement point)."""


def can(claims: dict, db: str, table: str) -> bool:
    scopes = set(claims.get("scopes", ()))
    return db in scopes or f"{db}.{table}" in scopes
```

`backend/prism/security/cli.py`:
```python
"""prism-token: print a bearer token for a demo persona (manual API testing)."""
import argparse
import sys

from prism.config import Settings
from prism.security.personas import PERSONAS, claims_for
from prism.security.tokens import mint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prism-token")
    parser.add_argument("persona", choices=sorted(PERSONAS))
    parser.add_argument("audience", help="e.g. refmaster-api or marketmaster-api")
    parser.add_argument("--ttl", type=int, default=3600)
    args = parser.parse_args(argv)
    print(mint(claims_for(args.persona), args.audience, Settings().jwt_secret, ttl_s=args.ttl))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/security tests/db -q` → all pass. If a matrix cell fails, the failure message lists `db.table: expected X, got Y (n/total)`. Fix the policy or data, never the `expected()` spec, unless the spec (§5) says otherwise.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/security backend/tests/security backend/tests/db/test_rbac.py
git commit -m "feat(security): personas, audience-bound tokens and RLS visibility matrix

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Shared API plumbing and the RefMaster mock REST API

**Files:**
- Create:
  - `backend/prism/sources/__init__.py` (empty) and `backend/prism/sources/common.py`
  - `backend/prism/sources/refmaster_api/__init__.py` (empty) and `backend/prism/sources/refmaster_api/app.py`
- Test: `backend/tests/api/conftest.py`, `backend/tests/api/test_refmaster_api.py`

**Interfaces:**
- Consumes: `verify`, `TokenError`, `can`, `ctx_from_claims`, `prepare_statements`, `Settings`.
- Produces:
  - `common.Databases(settings)`, with `await .pool(logical)` and `await .close()`.
  - `common.bearer_claims(request, audience) -> dict` (401 on failure).
  - `common.require_table(claims, db, table)` (403).
  - `common.check_range(start, end)` (422).
  - `common.where(filters: list[tuple[str, str, Any]]) -> tuple[sql.Composable, list]`, where op ∈ `eq|gte|lte|lt|ilike` and `None` values are skipped.
  - `common.select(table, columns, cond, order_by, limit, offset) -> sql.Composed` and `common.page(rows, limit, offset) -> dict`.
  - `async common.fetch(request, db, claims, query, params) -> list[dict]` (403 on a policy error, 504 on timeout).
  - `refmaster_api.app.create_app(settings: Settings | None = None) -> FastAPI` and `AUDIENCE = "refmaster-api"`.
  - Endpoints under `/api/v1`: `/securities`, `/securities/{security_id}`, `/entities`, `/accounts`, `/corporate-actions`, `/exceptions`, `/exceptions/summary` and `/data-dictionary`.

- [ ] **Step 1: Write the fixtures and failing tests**

`backend/tests/api/conftest.py`:
```python
import pytest

from prism.security.personas import claims_for
from prism.security.tokens import mint


@pytest.fixture
def headers_for(seeded):
    def make(persona: str, audience: str, ttl_s: int = 300) -> dict:
        return {"Authorization": f"Bearer {mint(claims_for(persona), audience, seeded.jwt_secret, ttl_s=ttl_s)}"}
    return make
```

`backend/tests/api/test_refmaster_api.py`:
```python
import pytest
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
    r = await client.get("/api/v1/securities", params={"q": "%"}, headers=h)
    assert r.status_code == 200 and r.json()["count"] == 0  # '%' matched literally, not as a wildcard


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
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/api/test_refmaster_api.py -q` → FAIL (`ModuleNotFoundError: prism.sources`)

- [ ] **Step 3: Implement `backend/prism/sources/common.py`**

```python
"""Shared plumbing for the mock platform REST APIs: auth, entitlement checks, safe SQL building, scoped fetch."""
import asyncio
from datetime import date
from typing import Any

import psycopg
from fastapi import HTTPException, Request
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from prism.config import Settings
from prism.db.session import ctx_from_claims, prepare_statements
from prism.security.access import can
from prism.security.tokens import TokenError, verify

_OPS = {"eq": "=", "gte": ">=", "lte": "<=", "lt": "<"}


class Databases:
    """Lazily opened connection pools (as prism_svc), one per logical database."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pools: dict[str, AsyncConnectionPool] = {}
        self._lock = asyncio.Lock()

    async def pool(self, logical: str) -> AsyncConnectionPool:
        async with self._lock:
            if logical not in self._pools:
                pool = AsyncConnectionPool(self._settings.dsn(logical), min_size=1, max_size=5, open=False)
                await pool.open(wait=True)
                self._pools[logical] = pool
            return self._pools[logical]

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()


def bearer_claims(request: Request, audience: str) -> dict:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    try:
        return verify(header.removeprefix("Bearer "), audience, request.app.state.settings.jwt_secret)
    except TokenError as exc:
        raise HTTPException(401, str(exc)) from exc


def require_table(claims: dict, db: str, table: str) -> None:
    if not can(claims, db, table):
        raise HTTPException(403, f"not entitled to {db}.{table}")


def check_range(start: date | None, end: date | None) -> None:
    if start and end and start > end:
        raise HTTPException(422, "'from' must be on or before 'to'")


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def where(filters: list[tuple[str, str, Any]]) -> tuple[sql.Composable, list]:
    parts: list[sql.Composable] = []
    params: list = []
    for column, op, value in filters:
        if value is None:
            continue
        if op == "ilike":
            parts.append(sql.SQL("{} ILIKE %s ESCAPE '\\'").format(sql.Identifier(column)))
            params.append(f"%{_escape_like(value)}%")
        else:
            parts.append(sql.SQL("{} {} %s").format(sql.Identifier(column), sql.SQL(_OPS[op])))
            params.append(value)
    if not parts:
        return sql.SQL(""), []
    return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts), params


def select(table: str, columns: tuple[str, ...], cond: sql.Composable, order_by: str,
           limit: int, offset: int) -> sql.Composed:
    return sql.SQL("SELECT {cols} FROM {t}{w} ORDER BY {o} LIMIT {l} OFFSET {off}").format(
        cols=sql.SQL(", ").join(map(sql.Identifier, columns)), t=sql.Identifier(table), w=cond,
        o=sql.Identifier(order_by), l=sql.Literal(limit), off=sql.Literal(offset),
    )


def page(rows: list[dict], limit: int, offset: int) -> dict:
    return {"items": rows, "count": len(rows), "limit": limit, "offset": offset}


async def fetch(request: Request, db: str, claims: dict, query: sql.Composable, params: list) -> list[dict]:
    settings: Settings = request.app.state.settings
    ctx = ctx_from_claims(claims, settings.ctx_hmac_key)
    pool = await request.app.state.dbs.pool(db)
    try:
        async with pool.connection() as conn, conn.transaction():
            for statement, args in prepare_statements(ctx, settings.statement_timeout_ms):
                await conn.execute(statement, args)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(query, params)
                return await cur.fetchall()
    except psycopg.errors.InsufficientPrivilege as exc:
        raise HTTPException(403, "access denied by data policy") from exc
    except psycopg.errors.QueryCanceled as exc:
        raise HTTPException(504, "query timed out") from exc
```

- [ ] **Step 4: Implement `backend/prism/sources/refmaster_api/app.py`**

```python
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
    rows = await fetch(request, DB, claims, select("securities", SECURITY_COLS, cond, "security_id", limit, offset), params)
    return page(rows, limit, offset)


@router.get("/securities/{security_id}")
async def get_security(request: Request, claims: Claims, security_id: str):
    require_table(claims, DB, "securities")
    cond, params = where([("security_id", "eq", security_id)])
    rows = await fetch(request, DB, claims, select("securities", SECURITY_COLS, cond, "security_id", 1, 0), params)
    if not rows:
        raise HTTPException(404, "security not found")
    return rows[0]


@router.get("/entities")
async def list_entities(request: Request, claims: Claims, country: str | None = None, sector: str | None = None,
                        q: str | None = None, limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "legal_entities")
    cond, params = where([("country", "eq", country), ("sector", "eq", sector), ("name", "ilike", q)])
    rows = await fetch(request, DB, claims, select("legal_entities", ENTITY_COLS, cond, "entity_id", limit, offset), params)
    return page(rows, limit, offset)


@router.get("/accounts")
async def list_accounts(request: Request, claims: Claims, region: str | None = None,
                        lifecycle_state: str | None = None, product_id: str | None = None,
                        limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "accounts")
    cond, params = where([("region", "eq", region), ("lifecycle_state", "eq", lifecycle_state),
                          ("product_id", "eq", product_id)])
    rows = await fetch(request, DB, claims, select("accounts", ACCOUNT_COLS, cond, "account_id", limit, offset), params)
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
    rows = await fetch(request, DB, claims, select("corporate_actions", CA_COLS, cond, "ex_date", limit, offset), params)
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
    rows = await fetch(request, DB, claims, select("exceptions", EXC_COLS, cond, "opened_at", limit, offset), params)
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
        "FROM exceptions{w} GROUP BY {g} ORDER BY total DESC"
    ).format(g=cols, w=cond)
    return {"group_by": groups, "rows": await fetch(request, DB, claims, query, params)}


@router.get("/data-dictionary")
async def data_dictionary(request: Request, claims: Claims, domain: str | None = None):
    require_table(claims, DB, "data_dictionary")
    cond, params = where([("domain", "eq", domain)])
    rows = await fetch(request, DB, claims, select("data_dictionary", DD_COLS, cond, "attribute", 500, 0), params)
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
```

- [ ] **Step 5: Run tests** — `uv run pytest tests/api/test_refmaster_api.py -q` → `8 passed`

- [ ] **Step 6: Commit**

```bash
git add backend/prism/sources backend/tests/api
git commit -m "feat(api): RefMaster mock REST API over RLS-scoped Postgres

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: MarketMaster mock REST API

**Files:**
- Create: `backend/prism/sources/marketmaster_api/__init__.py` (empty), `backend/prism/sources/marketmaster_api/app.py`
- Test: `backend/tests/api/test_marketmaster_api.py`

**Interfaces:**
- Consumes: everything in `prism.sources.common` (Task 12).
- Produces:
  - `marketmaster_api.app.create_app(settings=None) -> FastAPI` and `AUDIENCE = "marketmaster-api"`.
  - Endpoints under `/api/v1`:
    - `/vendors`, `/instruments`, `/instruments/{security_id}/timeseries`
    - `/prices/conflicts`, `/prices/conflicts/summary`, `/prices/suspects`
    - `/golden-copy/{security_id}`, `/dq/metrics`, `/esg/{entity_id}`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run to verify failure** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement `backend/prism/sources/marketmaster_api/app.py`**

```python
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
                       select("vendors", ("vendor_id", "name", "rank_default"), sql.SQL(""), "rank_default", 50, 0), [])
    return page(rows, 50, 0)


@router.get("/instruments")
async def list_instruments(request: Request, claims: Claims, asset_class: str | None = None, q: str | None = None,
                           limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "instruments")
    cond, params = where([("asset_class", "eq", asset_class), ("name", "ilike", q)])
    rows = await fetch(request, DB, claims, select("instruments", INSTRUMENT_COLS, cond, "security_id", limit, offset), params)
    return page(rows, limit, offset)


@router.get("/instruments/{security_id}/timeseries")
async def timeseries(request: Request, claims: Claims, security_id: str,
                     date_from: FromDate = None, date_to: ToDate = None):
    require_table(claims, DB, "golden_prices")
    check_range(date_from, date_to)
    cond, params = where([("security_id", "eq", security_id)])
    if not await fetch(request, DB, claims, select("instruments", ("security_id",), cond, "security_id", 1, 0), params):
        raise HTTPException(404, "instrument not found")
    cond, params = where([("security_id", "eq", security_id), ("price_date", "gte", date_from),
                          ("price_date", "lte", date_to)])
    points = await fetch(request, DB, claims,
                         select("golden_prices", ("price_date", "value", "chosen_vendor_id", "rule"), cond,
                                "price_date", 500, 0), params)
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
    rows = await fetch(request, DB, claims, select("price_suspects", SUSPECT_COLS, cond, "price_date", limit, offset), params)
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


@router.get("/prices/suspects")
async def list_suspects(request: Request, claims: Claims, kind: str | None = None, vendor_id: str | None = None,
                        asset_class: str | None = None, status: str | None = None,
                        date_from: FromDate = None, date_to: ToDate = None, limit: Limit = 100, offset: Offset = 0):
    require_table(claims, DB, "price_suspects")
    check_range(date_from, date_to)
    cond, params = where(_suspect_filters(kind, vendor_id, asset_class, status, date_from, date_to))
    rows = await fetch(request, DB, claims, select("price_suspects", SUSPECT_COLS, cond, "price_date", limit, offset), params)
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
                             select("vendor_prices", ("vendor_id", "value", "received_at"), cond, "vendor_id", 50, 0),
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
                              "business_date", limit, offset), params)
    return page(rows, limit, offset)


@router.get("/esg/{entity_id}")
async def esg(request: Request, claims: Claims, entity_id: str):
    require_table(claims, DB, "esg_scores")
    cond, params = where([("entity_id", "eq", entity_id)])
    rows = await fetch(request, DB, claims,
                       select("esg_scores", ("entity_id", "provider", "as_of", "score"), cond, "as_of", 50, 0), params)
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
```

- [ ] **Step 4: Run the whole suite** — `make test` → all pass

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sources/marketmaster_api backend/tests/api/test_marketmaster_api.py
git commit -m "feat(api): MarketMaster mock REST API

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 14: `start_backend.sh`, Procfile and README

**Files:**
- Create: `scripts/start_backend.sh`, `backend/Procfile`, `README.md`

**Interfaces:**
- Consumes: `prism-seed` (Task 10), `create_app` factories (Tasks 12–13), `prism-token` (Task 11).
- Produces: `scripts/start_backend.sh [--reseed]`. Plans 2–3 extend the Procfile with more services and port checks.

- [ ] **Step 1: Write `backend/Procfile`**

```
refmaster_api: uvicorn prism.sources.refmaster_api.app:create_app --factory --host 127.0.0.1 --port 8101
marketmaster_api: uvicorn prism.sources.marketmaster_api.app:create_app --factory --host 127.0.0.1 --port 8102
```

- [ ] **Step 2: Write `scripts/start_backend.sh`** (then `chmod +x scripts/start_backend.sh`)

```bash
#!/usr/bin/env bash
# Start the Prism backend: Postgres (Docker), seed on first run (or when keys changed), then all services.
# Usage: scripts/start_backend.sh [--reseed]
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RESEED=0
for arg in "$@"; do
  case "$arg" in
    --reseed) RESEED=1 ;;
    *) echo "Unknown option: $arg (supported: --reseed)" >&2; exit 2 ;;
  esac
done

die() { echo "ERROR: $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
docker info >/dev/null 2>&1 || die "the Docker daemon is not running - start Docker Desktop and retry"
command -v uv >/dev/null 2>&1 || die "uv is not installed - see https://docs.astral.sh/uv/getting-started/installation/"
command -v openssl >/dev/null 2>&1 || die "openssl is required to generate local secrets"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi
ensure_secret() {
  local name="$1"
  if ! grep -q "^${name}=." .env; then
    echo "${name}=$(openssl rand -hex 32)" >> .env
    echo "Generated ${name} in .env"
  fi
}
ensure_secret PRISM_CTX_HMAC_KEY
ensure_secret PRISM_JWT_SECRET

for port in 8101 8102; do
  if lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    die "port $port is already in use - is the backend already running?"
  fi
done

echo "Starting Postgres..."
docker compose up -d --wait postgres || die "Postgres did not become healthy (see: docker compose logs postgres)"

cd backend
uv sync --quiet
if [[ $RESEED -eq 1 ]]; then
  uv run prism-seed --reset
elif ! uv run prism-seed --check; then
  echo "Seeding simulated platforms (first run, or security key changed)..."
  uv run prism-seed --reset
fi

echo "Starting services (Ctrl-C to stop):"
echo "  RefMaster API    http://127.0.0.1:8101/docs"
echo "  MarketMaster API http://127.0.0.1:8102/docs"
exec uv run honcho start -f Procfile
```

- [ ] **Step 3: Write `README.md`**

````markdown
# Prism — agentic data intelligence MVP

Ask the data platform a question, get a dashboard back. Design: `docs/superpowers/specs/2026-09-30-agentic-data-intelligence-design.md`.

## Prerequisites
Docker Desktop, [uv](https://docs.astral.sh/uv/), openssl. (Node ≥ 20 from Plan 4.)

## Run the backend
```bash
scripts/start_backend.sh            # first run creates .env, starts Postgres, seeds data, starts services
scripts/start_backend.sh --reseed   # regenerate all simulated data
```

## Try the APIs
```bash
cd backend
TOKEN=$(uv run prism-token steward refmaster-api)
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8101/api/v1/exceptions/summary?group_by=domain"
TOKEN=$(uv run prism-token steward marketmaster-api)
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8102/api/v1/prices/conflicts/summary?from=2026-09-24&to=2026-09-30"
```
Personas: `steward`, `cash_ops_emea`, `invest_ops_growth`, `bi_analyst`, `head_data`.

## Tests
```bash
make test        # needs Docker (starts Postgres)
make test-fast   # pure-Python tests only
```
````

- [ ] **Step 4: Verify end-to-end manually**

1. Run: `scripts/start_backend.sh`. Expected: on the first run it seeds (row counts printed), then honcho shows both uvicorn processes listening.
2. In a second terminal, run the two curl commands from the README. Expected: JSON summaries. The conflicts summary's first row is `"vendor_id": "V_A", "asset_class": "Corp bond"`.
3. `TOKEN=$(cd backend && uv run prism-token cash_ops_emea refmaster-api)`, then curl `/api/v1/securities`. Expected: HTTP 403 `not entitled to refmaster.securities`.
4. Run `scripts/start_backend.sh` again in a third terminal while the first is running. Expected: `ERROR: port 8101 is already in use`.
5. Stop the backend (Ctrl-C). Delete the two generated secret lines from `.env` and restart. Expected: it generates new keys, prints "security key changed", re-seeds and starts cleanly.

- [ ] **Step 5: Commit**

```bash
git add scripts/start_backend.sh backend/Procfile README.md
git commit -m "feat: start_backend.sh with first-run seeding and key-drift detection

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Self-review notes (spec coverage for Plan 1)

- **§2 (five platforms, data model, shared keys, simulation, planted stories):** Tasks 3–8 and 10. Each story has a test: conflict (Task 5), stale/NAV (Tasks 5 and 7), late custodian (Tasks 7 and 8), aged USD breaks (Task 6).
- **§2 exposure split:** the EDMs are REST (Tasks 12–13). The other three are Postgres with RLS (Tasks 9–11). Their MCP servers are Plan 2.
- **§5 security:**
  - Signed context, FORCE RLS, NOBYPASSRLS roles, masking view, read-only role, and a key the query role cannot read: Tasks 9 and 11.
  - Audience-bound JWTs: Task 11.
  - 403 before the database is queried: Task 12.
  - Persona table: Task 11 `personas.py` matches §5 exactly.
- **§9 layout and scripts:** `start_backend.sh` (Task 14). `start_frontend.sh` is Plan 4. `ANTHROPIC_API_KEY` becomes required only in Plan 3.
- **Deferred by design:**
  - The `/metrics/{metric_id}` endpoints on the EDM APIs are Plan 2: they come with the governed-metric compiler, which is shared with the SQL sources.
  - Neo4j joins `docker-compose.yml` in Plan 2.
