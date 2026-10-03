# Cross-System Incident (M9) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Plant one missed-split incident whose cause runs through all five source systems, give the semantic layer the metrics, join-key dimensions and metric-to-metric `JOINABLE_ON` links needed to follow it with governed metrics only, and teach the agent a cross-system investigation pattern.

**Architecture:** A new post-projection pass (`prism/sim/incident.py`) runs on the five projectors' in-memory tables with its own stream `Random(seed + 6)`. It appends rows, or edits rows from a fixed "owned" set. The projectors only gain draw-free link columns. The semantic layer gains three metrics (`funds` in SQL; `pending_corporate_actions` and `price_suspects` in REST) and new key dimensions. The graph builder derives `(:Metric)-[:JOINABLE_ON {key, other_key}]->(:Metric)` from the `same_key` groups. Retrieval returns `metric_links` and adds up to 4 linked metrics after the direct hits. The supervisor prompt gains the anchor → follow links → combine → answer pattern. Three new golden references each `combine` 6–7 inputs covering all five sources.

**Tech Stack:** Python 3.13, uv, pytest, psycopg 3, FastAPI mock APIs, pydantic, Neo4j 5.26 (Cypher), DuckDB combine, the Anthropic agent loop.

**Spec:** `docs/superpowers/specs/2026-10-03-cross-system-incident-design.md`

## Global Constraints

- Every command runs from `backend/` unless it says otherwise: `cd backend && uv run pytest ...`. Make targets run from the repo root.
- Commit as `cwijayasundara@gmail.com` only, with `git -c user.email=cwijayasundara@gmail.com commit ...`. Every message body ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Stage explicit paths only; never stage `frontend/e2e/zz_shots.spec.ts`.
- Incident dates are offsets from the end of `u.days`. For the default as-of (2026-09-30) they are:
  - `FAILED_FROM_END = 6` → 23 Sep
  - `SPLIT_FROM_END = 5` → 24 Sep
  - the second breach day is `n - 4` → 25 Sep
  - `FIX_FROM_END = 3` → 28 Sep
  - `CASH_FROM_END = 2` → 29 Sep
- `SPLIT_RATIO = 2`, `INCIDENT_ANCHOR = "PF003"`, `INCIDENT_MIN_HALF_WEIGHT_BPS = 10.0`.
- Noise volumes: `NOISE_CORPORATE_ACTIONS = 15`, `NOISE_SPIKES = 10`, `NOISE_CASH_IN_LIEU = 5`.
- The incident pass uses `random.Random(cfg.seed + 6)` and draws in this fixed order: refmaster, feedhub, marketmaster, assetrecon, cashrecon. It never draws from another projector's stream. Projector changes are draw-free: no projector gains or loses a draw.
- Full-profile literals, pinned in Task 4 and checked against the reseed in Task 16:
  - X = `SEC001982`
  - E = `LE00364`
  - S* = `SRC005`
  - holders = `PF003, PF019, PF021, PF025`
  - PF003's fund entity = `LE00403`
  - PF003's fund cash account = `CA0063`

  The small profile has X = `SEC000059` (sole holder PF003), E = `LE00052` and S* = `SRC020`. Small-profile tests compute ids; they never hard-code them.
- New categorical values: corporate-action status `pending`, price-suspect status `accepted`, recon cause `corporate_action`, break type `cash_in_lieu`, feed type `corporate_actions`, entity types `sovereign`, `corporate` and `fund`.
- Retrieval: `MAX_LINKED_METRICS = 4`. Linked metrics come after the direct hits, at most one per source not already in the pack. The direct-hit cap `KIND_LIMITS["Metric"] = 5` is unchanged.
- Recipes: `MAX_NODES = 8` is unchanged, so every golden `combine` reference has at most 7 inputs.
- Agent run limits are unchanged: 8 turns, 24 tool calls, 90 s (`tests/agent/test_settings_migration.py` pins them).
- Never name a Cypher variable `s` in a statement that uses `gate()`.
- Source display names, spelled exactly as in `frontend/lib/session.ts`: RefMaster, MarketMaster, CashRecon, AssetRecon, FeedHub.

### Named spec deviations (decided while planning; listed for the user at handoff)

1. **§2.2 choice of X.** The literal rule picks `SEC000789` on the full profile. One of its holders is PF001, a SRC001 late-custodian portfolio. Its positions are frozen, so it cannot show the split, and the SRC001 story test would break. The rule therefore excludes securities held by a late-custodian portfolio, and also requires every holder's half-value to move its NAV by at least 10 bps, so every holder breaches. "PF003 has the largest weight" is dropped (PF019's weight is larger). "At least two other holders" holds on the full profile, which has 3. The small profile has no such equity, so it falls back to the best candidate, which has 0 other holders.
2. **§3.2 join keys.** `ON_COLUMN` only exists for bare columns of the metric's own table, so every hop needs a column. The projectors gain these draw-free columns:
   - `assetrecon.portfolios.fund_entity_id`, `assetrecon.portfolios.custodian_source_id`
   - `refmaster.corporate_actions.issuer_entity_id`
   - `cashrecon.breaks.bank_source_id`
   - `feedhub.feed_deliveries.feed_type`
   - `refmaster.legal_entities.entity_type`

   `same_key` changes:
   - The security group gains the three columns the spec lists.
   - The entity group gains `portfolios.fund_entity_id` and `corporate_actions.issuer_entity_id`.
   - The existing source group is left unchanged, because `test_same_key_declarations` pins it.
   - Two new groups are added:
     - custodian: `feed_deliveries.source_id`, `custodians.feed_source_id`, `portfolios.custodian_source_id`
     - bank: `feed_deliveries.source_id`, `cash_accounts.bank_source_id`, `breaks.bank_source_id`

   This stops `funds.custodian_source_id` from linking to `open_breaks.bank_source_id`, a link that would always be empty.
3. **§2.5 owned rows.** The owned set also includes:
   - X's `vendor_prices` from 24 Sep
   - the holders' position and nav `recon_runs` on 24 and 25 Sep
   - every `recon_exceptions` row the projector drew for (holder, X, ≥ 24 Sep)
   - the price-domain `dq_stage_metrics` rows inside the vendor window, recomputed with `_dq_metrics`'s own formula and no draws
4. **§5.1 no-drift.** The check has two parts:
   - (a) A digest of the projectors' output on the original columns, committed from `main` before any change (Task 1).
   - (b) An in-memory diff showing the pass only appends rows or edits owned rows (Task 8).
5. **§3.3 cap (user decision, 2026-10-03).** Linked metrics get up to 4 extra slots after the 5 direct hits, rather than fitting within the cap.
6. **§5.2 input count.** References have 6–7 inputs, not up to 8, because `MAX_NODES` counts the combine node itself.
7. **§2.6 test edits.**
   - The projector tests need no change, because the pass is separate from `PROJECTORS`.
   - These tests are edited instead:
     - `tests/db/test_seed.py` (counts come from `project_all`)
     - `tests/mcp/test_metric_catalog.py` (`EXPECTED` gains `funds`)
     - `tests/graph/test_loader.py` (22 → 25 metrics)
     - `tests/agent/test_recipes.py` (unchanged)
     - `tests/agent/test_tools.py` (unchanged: the "Systems you can read" clause appears only when the user's sources are known)
8. **`metric_links[].on`** is `"<a's dimension> = <b's dimension>"`, for example `"security_id = record_ref"`, so the agent sees both key names.
9. **`SimConfig.scale`** is applied to the counts in `__post_init__` and then reset to 1.0, so `dataclasses.replace` never scales twice. The profile name records the factor, e.g. `fullx2`.
10. **`pending_corporate_actions`** uses a dedicated endpoint, `GET /api/v1/corporate-actions/pending/summary`, so non-pending groups never show up as zero rows.

## Review Focus

1. **Re-seeding twice** must give byte-identical data. The pass draws only from its own stream, in a fixed order. Pinned in Task 8 (`test_incident_pass_is_deterministic`).
2. **Small or scaled profiles where X has fewer than two other holders** must still produce a complete incident on every system, not crash or silently skip a hop. Pinned in Task 8 (`test_scaled_profile_still_gets_a_complete_incident`).
3. **A metrics-only caller and link dimensions.** `metric_links` must never name a dimension the caller cannot see. Pinned in Task 13 (`test_metric_links_follow_the_callers_scopes`).
4. **Budget trimming that drops a linked metric** must not leave a `metric_links` entry pointing at a metric the pack no longer holds. Pinned in Task 13 (`test_trimmed_pack_has_no_dangling_links`).
5. **The agent passing a list of key values to a REST metric filter**, e.g. every holder at once, must get a clear `single value` error rather than a silent partial result. Pinned in Task 11 (`test_rest_filters_take_one_value`).

---

### Task 1: No-drift baseline digest (captured from `main` before any other change)

**Files:**
- Create: `backend/prism/sim/digest.py`
- Create: `backend/tests/sim/projection_digest.json` (generated)
- Create: `backend/tests/sim/test_no_drift.py`

**Interfaces:**
- Produces: `table_digest(table: TableData, columns: Sequence[str]) -> str` and `projection_digest(cfg: SimConfig) -> dict[str, dict]`, which maps `"<db>.<table>"` to `{"columns": [...], "rows": int, "sha256": str}`. Also a CLI, `python -m prism.sim.digest`, that prints `{"small": ..., "full": ...}` as JSON.

- [ ] **Step 1: Branch from `main` and commit this plan first**

```bash
git switch -c m9-cross-system-incident main
git add docs/superpowers/plans/2026-10-03-plan-9-cross-system-incident.md
git -c user.email=cwijayasundara@gmail.com commit -m "docs: M9 cross-system incident implementation plan

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git status --short
```
Expected: the branch is created, the plan is committed, and `status` prints nothing.

- [ ] **Step 2: Write the digest module**

```python
"""Order-sensitive digests of projected tables, for no-drift checks (M9 spec §5.1). A digest covers only the columns
it is given, so a column a projector adds later never changes the digest of the columns that were there before."""
import hashlib
import json
import sys
from collections.abc import Sequence

from prism.sim.model import TableData
from prism.sim.seed import PROJECTORS
from prism.sim.universe import SimConfig, build_universe


def table_digest(table: TableData, columns: Sequence[str]) -> str:
    idx = [table.columns.index(c) for c in columns]
    h = hashlib.sha256()
    for row in table.rows:
        h.update(repr(tuple(row[i] for i in idx)).encode())
        h.update(b"\n")
    return h.hexdigest()


def projection_digest(cfg: SimConfig) -> dict[str, dict]:
    u = build_universe(cfg)
    out = {}
    for db, project in PROJECTORS.items():
        for name, t in project(u).items():
            out[f"{db}.{name}"] = {"columns": list(t.columns), "rows": len(t.rows),
                                   "sha256": table_digest(t, t.columns)}
    return out


def main() -> int:
    json.dump({"small": projection_digest(SimConfig.small()), "full": projection_digest(SimConfig())},
              sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Capture the baseline from unchanged projectors**

Run: `cd backend && uv run python -m prism.sim.digest > tests/sim/projection_digest.json && python -c "import json;d=json.load(open('tests/sim/projection_digest.json'));print(len(d['small']), len(d['full']))"`
Expected: `38 38` (refmaster 9 + marketmaster 7 + cashrecon 9 + assetrecon 9 + feedhub 4 tables per profile).

- [ ] **Step 4: Write the no-drift test (part a)**

```python
"""No drift (M9 spec §5.1, part a): on the columns they had on `main`, the projectors' output is unchanged.
projection_digest.json was generated from `main` before any M9 change (`uv run python -m prism.sim.digest`).
Part b (the incident pass only appends or edits rows it owns) lives in test_incident.py."""
import json
from pathlib import Path

import pytest

from prism.sim.digest import table_digest
from prism.sim.seed import PROJECTORS
from prism.sim.universe import SimConfig, build_universe

BASELINE = json.loads(Path(__file__).with_name("projection_digest.json").read_text())


@pytest.mark.parametrize("profile", ["small", pytest.param("full", marks=pytest.mark.slow)])
def test_projectors_match_the_main_baseline(profile):
    cfg = SimConfig.small() if profile == "small" else SimConfig()
    u = build_universe(cfg)
    seen = set()
    for db, project in PROJECTORS.items():
        for name, t in project(u).items():
            key = f"{db}.{name}"
            want = BASELINE[profile][key]
            seen.add(key)
            assert len(t.rows) == want["rows"], key
            assert table_digest(t, want["columns"]) == want["sha256"], key
    assert seen == set(BASELINE[profile])
```

- [ ] **Step 5: Run it**

Run: `cd backend && uv run pytest -q tests/sim/test_no_drift.py`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/prism/sim/digest.py backend/tests/sim/projection_digest.json backend/tests/sim/test_no_drift.py
git -c user.email=cwijayasundara@gmail.com commit -m "test(sim): no-drift digest of the projectors captured from main

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Draw-free link columns in the projectors and DDL

**Files:**
- Modify: `backend/prism/sim/keys.py` (add `fund_entity_id`, `fund_cash_account_id`)
- Modify: `backend/prism/sim/project_refmaster.py` (`legal_entities.entity_type`, `corporate_actions.issuer_entity_id`)
- Modify: `backend/prism/sim/project_assetrecon.py` (`portfolios.fund_entity_id`, `portfolios.custodian_source_id`)
- Modify: `backend/prism/sim/project_cashrecon.py` (`breaks.bank_source_id`)
- Modify: `backend/prism/sim/project_feedhub.py` (`feed_deliveries.feed_type`)
- Modify: `backend/prism/db/ddl/refmaster.sql`, `assetrecon.sql`, `cashrecon.sql`, `feedhub.sql`
- Test: `backend/tests/sim/test_link_columns.py`

**Interfaces:**
- Produces: `keys.fund_entity_id(n_entities: int, index: int) -> str` (`LE{n_entities + index + 1:05d}`) and `keys.fund_cash_account_id(n_cash_accounts: int, index: int) -> str` (`CA{n_cash_accounts + index + 1:04d}`). `index` is the 0-based position in `u.portfolios`, so PF003 is index 2.
- Each new column is appended at the end of its `TableData.columns` and its DDL table:
  - `legal_entities`: `entity_type`
  - `corporate_actions`: `issuer_entity_id`
  - `portfolios`: `fund_entity_id`, `custodian_source_id`
  - `breaks`: `bank_source_id`
  - `feed_deliveries`: `feed_type`

- [ ] **Step 1: Write the failing test**

```python
"""Draw-free link columns (M9 spec §2.3): every hop of the incident chain has a column to join on."""
import pytest

from prism.sim.keys import fund_cash_account_id, fund_entity_id
from prism.sim.project_assetrecon import project_assetrecon
from prism.sim.project_cashrecon import project_cashrecon
from prism.sim.project_feedhub import project_feedhub
from prism.sim.project_refmaster import project_refmaster


@pytest.fixture(scope="module")
def rm(universe):
    return project_refmaster(universe)


def test_fund_ids_follow_the_universe_counts():
    assert fund_entity_id(400, 0) == "LE00401" and fund_entity_id(400, 2) == "LE00403"
    assert fund_cash_account_id(60, 2) == "CA0063"


def test_entity_type_marks_sovereigns(rm, universe):
    types = {r["entity_id"]: r["entity_type"] for r in rm["legal_entities"].dicts()}
    for e in universe.entities:
        assert types[e.entity_id] == ("sovereign" if e.sector == "Sovereign" else "corporate")


def test_corporate_actions_carry_the_issuer(rm, universe):
    for r in rm["corporate_actions"].dicts():
        assert r["issuer_entity_id"] == universe.security_by_id[r["security_id"]].issuer_entity_id


def test_portfolios_carry_fund_entity_and_custodian_source(universe):
    rows = project_assetrecon(universe)["portfolios"].dicts()
    for k, (r, p) in enumerate(zip(rows, universe.portfolios, strict=True)):
        assert r["fund_entity_id"] == fund_entity_id(universe.cfg.n_entities, k)
        assert r["custodian_source_id"] == p.custodian_source_id


def test_breaks_carry_the_bank_source(universe):
    bank = {a.account_id: a.bank_source_id for a in universe.cash_accounts}
    for r in project_cashrecon(universe)["breaks"].dicts():
        assert r["bank_source_id"] == bank[r["account_id"]]


def test_deliveries_carry_their_feed_type(universe):
    fh = project_feedhub(universe)
    data_type = {r["feed_id"]: r["data_type"] for r in fh["feeds"].dicts()}
    for r in fh["feed_deliveries"].dicts():
        assert r["feed_type"] == data_type[r["feed_id"]]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && uv run pytest -q tests/sim/test_link_columns.py`
Expected: FAIL with `ImportError: cannot import name 'fund_cash_account_id'`.

- [ ] **Step 3: Implement**

`backend/prism/sim/keys.py`, appended:

```python
def fund_entity_id(n_entities: int, index: int) -> str:
    """Legal entity of the portfolio at `index` (0-based) in the universe: appended after the universe's entities by
    the incident pass (LE00401... by default); AssetRecon portfolios cite it."""
    return f"LE{n_entities + index + 1:05d}"


def fund_cash_account_id(n_cash_accounts: int, index: int) -> str:
    """Cash account of the portfolio at `index` (0-based), owned by its fund entity; appended by the incident pass."""
    return f"CA{n_cash_accounts + index + 1:04d}"
```

`project_refmaster.py`:
- Add `"entity_type"` to the end of the `legal_entities` columns. Write each row as `t["legal_entities"].add(e.entity_id, e.lei, e.name, e.country, e.region, e.sector, e.parent_entity_id, e.status, "sovereign" if e.sector == "Sovereign" else "corporate")`.
- Add `"issuer_entity_id"` to the end of the `corporate_actions` columns. In `_corporate_actions`, write `table.add(f"CA{n:06d}", s.security_id, event, ex, pay, ratio, status, s.issuer_entity_id)`.

`project_assetrecon.py`:
- Add `"fund_entity_id", "custodian_source_id"` to the end of the `portfolios` columns.
- Import `fund_entity_id` from `prism.sim.keys`.
- Change the portfolio loop to `for k, p in enumerate(u.portfolios):`, and write the row as `t["portfolios"].add(p.portfolio_id, p.name, p.fund_group, p.base_ccy, custodian_ids[p.custodian_source_id], p.region, fund_entity_id(u.cfg.n_entities, k), p.custodian_source_id)`.

`project_cashrecon.py`:
- Add `"bank_source_id"` to the end of the `breaks` columns.
- In `_add_break`, end the row with `..., status, owner, cause, a.region, a.bank_source_id)`.

`project_feedhub.py`:
- Add `"feed_type"` to the end of the `feed_deliveries` columns.
- End the row with `..., latency, count, error, s.source_type, data_type)`.

DDL, each column appended at the end of its table:
- `refmaster.sql`:
  - `legal_entities`: `,\n  entity_type text NOT NULL`
  - `corporate_actions`: `,\n  issuer_entity_id text REFERENCES legal_entities (entity_id)`
- `assetrecon.sql`, `portfolios`: `,\n  fund_entity_id text NOT NULL,\n  custodian_source_id text NOT NULL`
- `cashrecon.sql`, `breaks`: `,\n  bank_source_id text NOT NULL`
- `feedhub.sql`, `feed_deliveries`: `,\n  feed_type text NOT NULL`

- [ ] **Step 4: Run the new test and the no-drift test**

Run: `cd backend && uv run pytest -q tests/sim/test_link_columns.py tests/sim/test_no_drift.py`
Expected: all pass. The digest still matches because the added columns sit outside the baseline columns and draw nothing.

- [ ] **Step 5: Run the sim, DB and API suites**

Run: `make test-fast && cd backend && uv run pytest -q tests/db tests/api`
Expected: all pass. `make db` must be up; the `seeded` fixture re-creates the test databases from the new DDL.

- [ ] **Step 6: Commit**

```bash
git add backend/prism/sim/keys.py backend/prism/sim/project_refmaster.py backend/prism/sim/project_assetrecon.py \
  backend/prism/sim/project_cashrecon.py backend/prism/sim/project_feedhub.py backend/prism/db/ddl/refmaster.sql \
  backend/prism/db/ddl/assetrecon.sql backend/prism/db/ddl/cashrecon.sql backend/prism/db/ddl/feedhub.sql \
  backend/tests/sim/test_link_columns.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(sim): draw-free link columns for the cross-system chain

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `SimConfig.scale` and `prism-seed --scale`

**Files:**
- Modify: `backend/prism/sim/universe.py` (`SimConfig`)
- Modify: `backend/prism/sim/cli.py`
- Test: `backend/tests/sim/test_universe.py` (append)

**Interfaces:**
- Produces: `SimConfig(scale: float = 1.0)`. After `__post_init__`, `n_securities`, `n_entities`, `n_portfolios` and `n_cash_accounts` have been multiplied by `round(n * scale)`, `scale == 1.0`, and `profile == f"{profile}x{factor:g}"` whenever the factor was not 1.

- [ ] **Step 1: Write the failing tests** (append to `tests/sim/test_universe.py`)

```python
def test_scale_multiplies_the_universe_counts_once():
    from dataclasses import replace
    cfg = SimConfig(scale=2)
    assert (cfg.n_securities, cfg.n_entities, cfg.n_portfolios, cfg.n_cash_accounts) == (4000, 800, 60, 120)
    assert cfg.scale == 1.0 and cfg.profile == "fullx2"
    assert replace(cfg) == cfg                                    # never scaled twice
    assert SimConfig() == SimConfig(scale=1.0)                    # the default profile is unchanged
    small = SimConfig.small(scale=1.5)
    assert small.n_portfolios == 18 and small.profile == "smallx1.5"


def test_scale_still_guards_story_prerequisites():
    with pytest.raises(ValueError, match="n_portfolios"):
        SimConfig(scale=0.2)                                      # 30 -> 6 portfolios
    with pytest.raises(ValueError, match="scale"):
        SimConfig(scale=0)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/sim/test_universe.py -k scale`
Expected: FAIL with `TypeError: SimConfig.__init__() got an unexpected keyword argument 'scale'`.

- [ ] **Step 3: Implement**

In `SimConfig`, add the field after `profile`: `scale: float = 1.0`. Then add the constant and extend `__post_init__`; its first lines run before the existing guards:

```python
SCALED_FIELDS = ("n_securities", "n_entities", "n_portfolios", "n_cash_accounts")
```

```python
    def __post_init__(self) -> None:
        if not self.scale > 0:
            raise ValueError("scale must be > 0")
        if self.scale != 1.0:   # consumed here: replace() never scales twice; the profile records the factor
            for name in SCALED_FIELDS:
                object.__setattr__(self, name, round(getattr(self, name) * self.scale))
            object.__setattr__(self, "profile", f"{self.profile}x{self.scale:g}")
            object.__setattr__(self, "scale", 1.0)
        if self.n_portfolios < 9:
            ...  # existing guards unchanged
```

In `cli.py`:
- Add `parser.add_argument("--scale", type=float, default=1.0, help="multiply securities, entities, portfolios and cash accounts (default 1)")`.
- Build the config as `cfg = SimConfig.small(**base, scale=args.scale) if args.small else SimConfig(**base, scale=args.scale)`.

- [ ] **Step 4: Run the sim suite**

Run: `make test-fast`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/universe.py backend/prism/sim/cli.py backend/tests/sim/test_universe.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(sim): scale knob for the universe size

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Choose the incident ids (`Incident`, `choose_incident`, `Stories.incident`)

**Files:**
- Modify: `backend/prism/sim/universe.py`
- Create: `backend/tests/sim/test_incident.py`

**Interfaces:**
- Produces, in `prism.sim.universe`:
  - `INCIDENT_ANCHOR = "PF003"`, `INCIDENT_SPLIT_FROM_END = 5`, `INCIDENT_MIN_HALF_WEIGHT_BPS = 10.0`
  - a frozen dataclass `Incident(security_id: str, issuer_entity_id: str, source_id: str, anchor_portfolio_id: str, holder_ids: tuple[str, ...])`, with `holder_ids` sorted
  - `choose_incident(u: Universe) -> Incident`
  - `Stories.incident: Incident | None = None`, set by `build_universe`

- [ ] **Step 1: Write the failing tests** (`tests/sim/test_incident.py`)

```python
"""M9 cross-system incident (spec 2026-10-03): ids, every link of the chain, noise, determinism, isolation."""
import pytest

from prism.sim.universe import (INCIDENT_ANCHOR, INCIDENT_MIN_HALF_WEIGHT_BPS, INCIDENT_SPLIT_FROM_END, SimConfig,
                                build_universe)


def _holders(u) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for pid in sorted(u.holdings):
        for sid, qty in u.holdings[pid]:
            out.setdefault(sid, {})[pid] = qty
    return out


def _eligible(u, sid: str, holders: dict[str, dict[str, float]]) -> bool:
    s, i = u.security_by_id[sid], len(u.days) - INCIDENT_SPLIT_FROM_END
    hs = holders.get(sid, {})
    nav = {p: sum(q * u.golden(x, i) for x, q in u.holdings[p]) for p in hs}
    return (s.asset_class == "Equity" and s.status == "active" and sid not in u.stories.stale_security_ids
            and INCIDENT_ANCHOR in hs and not set(u.stories.late_portfolio_ids) & set(hs)
            and all(q * u.prices[sid][i] / 2 / nav[p] * 1e4 >= INCIDENT_MIN_HALF_WEIGHT_BPS for p, q in hs.items()))


def test_incident_ids_follow_the_rule(universe):
    inc, holders = universe.stories.incident, _holders(universe)
    assert _eligible(universe, inc.security_id, holders)
    assert inc.holder_ids == tuple(sorted(holders[inc.security_id]))
    best = max(len(holders[s.security_id]) for s in universe.securities if _eligible(universe, s.security_id, holders))
    assert len(inc.holder_ids) == best                     # most holders; ties go to the lowest id
    assert inc.issuer_entity_id == universe.security_by_id[inc.security_id].issuer_entity_id
    anchor = next(p for p in universe.portfolios if p.portfolio_id == INCIDENT_ANCHOR)
    assert inc.anchor_portfolio_id == INCIDENT_ANCHOR and inc.source_id == anchor.custodian_source_id
    assert inc.source_id != universe.stories.late_custodian_source_id


@pytest.mark.slow
def test_full_profile_incident_literals():
    inc = build_universe(SimConfig()).stories.incident
    assert (inc.security_id, inc.issuer_entity_id, inc.source_id) == ("SEC001982", "LE00364", "SRC005")
    assert inc.holder_ids == ("PF003", "PF019", "PF021", "PF025")      # the anchor and at least two others
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/sim/test_incident.py`
Expected: FAIL with `ImportError: cannot import name 'INCIDENT_ANCHOR'`.

- [ ] **Step 3: Implement** (in `universe.py`)

Add the constants after `STALE_JUMP`:

```python
INCIDENT_ANCHOR = "PF003"
INCIDENT_SPLIT_FROM_END = 5            # the missed split is effective 24 Sep for the default as-of
INCIDENT_MIN_HALF_WEIGHT_BPS = 10.0    # halving the security moves every holder's NAV by at least this much
```

Add `Incident` above `Stories`, and the field on `Stories`, placed last since it has a default:

```python
@dataclass(frozen=True)
class Incident:
    """M9 cross-system incident ids (spec 2026-10-03 §2.2), chosen by choose_incident (no RNG)."""
    security_id: str
    issuer_entity_id: str
    source_id: str                 # S*: the anchor portfolio's custodian feed source
    anchor_portfolio_id: str
    holder_ids: tuple[str, ...]    # every portfolio holding the security, sorted
```

```python
    stale_days: int = 3
    incident: Incident | None = None
```

Add `choose_incident` after `_make_cash_accounts`:

```python
def choose_incident(u: Universe) -> Incident:
    """The missed-split security (spec §2.2, refined): an active, non-stale equity held by the anchor portfolio and by
    no late-custodian portfolio (their positions are frozen on the last good delivery, so they could not show the
    split), whose halving moves every holder's NAV by at least INCIDENT_MIN_HALF_WEIGHT_BPS (so every holder breaches
    5 bps); most holders first, then the lowest id. No RNG draw."""
    i = len(u.days) - INCIDENT_SPLIT_FROM_END
    holders: dict[str, dict[str, float]] = {}
    for pid in sorted(u.holdings):
        for sid, qty in u.holdings[pid]:
            holders.setdefault(sid, {})[pid] = qty
    nav = {pid: sum(q * u.golden(sid, i) for sid, q in h) for pid, h in u.holdings.items()}
    stale, late = set(u.stories.stale_security_ids), set(u.stories.late_portfolio_ids)

    def eligible(s: Security) -> bool:
        hs = holders.get(s.security_id, {})
        return (s.asset_class == "Equity" and s.status == "active" and s.security_id not in stale
                and INCIDENT_ANCHOR in hs and not late & set(hs)
                and all(q * u.prices[s.security_id][i] / 2 / nav[p] * 1e4 >= INCIDENT_MIN_HALF_WEIGHT_BPS
                        for p, q in hs.items()))

    candidates = [s for s in u.securities if eligible(s)]
    if not candidates:
        raise ValueError(f"no incident security: {INCIDENT_ANCHOR} holds no eligible equity; change the seed")
    x = min(candidates, key=lambda s: (-len(holders[s.security_id]), s.security_id))
    anchor = next(p for p in u.portfolios if p.portfolio_id == INCIDENT_ANCHOR)
    return Incident(x.security_id, x.issuer_entity_id, anchor.custodian_source_id, INCIDENT_ANCHOR,
                    tuple(sorted(holders[x.security_id])))
```

At the end of `build_universe`, replace `return Universe(...)` with:

```python
    u = Universe(cfg, days, entities, securities, prices, sources, portfolios, holdings, cash_accounts, stories)
    u.stories = replace(stories, incident=choose_incident(u))   # draw-free, after the stale jump
    return u
```

Then change the import line to `from dataclasses import dataclass, replace`.

- [ ] **Step 4: Run the sim suite with the slow marker**

Run: `cd backend && uv run pytest -q tests/sim`
Expected: all pass, including `test_full_profile_incident_literals` and the no-drift digests.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/universe.py backend/tests/sim/test_incident.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(sim): choose the cross-system incident ids deterministically

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Incident pass, part 1: helpers, owned rows, RefMaster and FeedHub

**Files:**
- Create: `backend/prism/sim/incident.py`
- Test: `backend/tests/sim/test_incident.py` (append)

**Interfaces:**
- Consumes: `Incident`, `INCIDENT_SPLIT_FROM_END` (Task 4); `fund_entity_id`, `fund_cash_account_id` (Task 2).
- Produces:
  - `apply_incident(u: Universe, tables: dict[str, dict[str, TableData]]) -> None`, which mutates `tables`, keyed by db then table name exactly as the projectors return them
  - `owned(u: Universe, db: str, table: str, row: dict) -> bool`
  - `incident_record(u: Universe) -> dict`
  - helpers `_set(t, i, **values)` and `_next_id(t, column, prefix, width) -> Callable[[], str]`
  - constants `FAILED_FROM_END`, `SPLIT_FROM_END`, `FIX_FROM_END`, `CASH_FROM_END`, `SPLIT_RATIO`, `NOISE_CORPORATE_ACTIONS`, `NOISE_SPIKES`, `NOISE_CASH_IN_LIEU`, `CA_FEED_TYPE = "corporate_actions"`

- [ ] **Step 1: Write the failing tests** (append to `tests/sim/test_incident.py`)

```python
import copy
from datetime import timedelta

from prism.sim.incident import (CA_FEED_TYPE, FAILED_FROM_END, NOISE_CORPORATE_ACTIONS, SPLIT_FROM_END, _next_id,
                                apply_incident)
from prism.sim.keys import fund_entity_id
from prism.sim.model import TableData
from prism.sim.seed import PROJECTORS


@pytest.fixture(scope="module")
def before(universe):
    return {db: project(universe) for db, project in PROJECTORS.items()}


@pytest.fixture(scope="module")
def after(universe, before):
    tables = copy.deepcopy(before)
    apply_incident(universe, tables)
    return tables


def test_next_id_continues_past_the_largest_id():
    t = TableData(("id",), [("CA000002",), ("CA000010",), ("X9",)])
    nxt = _next_id(t, "id", "CA", 6)
    assert [nxt(), nxt()] == ["CA000011", "CA000012"]


def test_fund_entities_are_appended_one_per_portfolio(after, before, universe):
    rows = after["refmaster"]["legal_entities"].dicts()[len(before["refmaster"]["legal_entities"].rows):]
    assert [r["entity_id"] for r in rows] == [fund_entity_id(universe.cfg.n_entities, k)
                                              for k in range(len(universe.portfolios))]
    assert {r["entity_type"] for r in rows} == {"fund"} and all(r["status"] == "active" for r in rows)
    leis = [r["lei"] for r in after["refmaster"]["legal_entities"].dicts()]
    assert len(leis) == len(set(leis))


def test_refmaster_misses_the_split(after, universe):
    inc, n = universe.stories.incident, len(universe.days)
    pending = [r for r in after["refmaster"]["corporate_actions"].dicts() if r["status"] == "pending"]
    assert len(pending) == 1
    ca = pending[0]
    assert (ca["security_id"], ca["event_type"], ca["ratio"], ca["issuer_entity_id"]) == \
        (inc.security_id, "split", 2.0, inc.issuer_entity_id)
    assert ca["ex_date"] == universe.days[n - SPLIT_FROM_END]
    exc = [r for r in after["refmaster"]["exceptions"].dicts() if r["record_ref"] == inc.security_id
           and r["rule_id"] == "R010" and r["status"] == "open"]
    assert len(exc) == 1 and exc[0]["domain"] == "corporate_action"
    crs = [r for r in after["refmaster"]["change_requests"].dicts() if r["record_ref"] == inc.security_id
           and r["status"] == "pending" and r["domain"] == "corporate_action"]
    assert len(crs) == 1 and crs[0]["checker"] is None


def test_noise_corporate_actions_are_processed_on_held_equities(after, before, universe):
    inc, n = universe.stories.incident, len(universe.days)
    new = after["refmaster"]["corporate_actions"].dicts()[len(before["refmaster"]["corporate_actions"].rows):]
    noise = [r for r in new if r["security_id"] != inc.security_id]
    held = {sid for h in universe.holdings.values() for sid, _ in h}
    assert len(noise) == NOISE_CORPORATE_ACTIONS
    window = set(universe.days[n - universe.cfg.position_window_days:])
    for r in noise:
        assert r["status"] == "processed" and r["event_type"] in ("dividend", "name_change")
        assert r["security_id"] in held and r["ex_date"] in window and r["ex_date"] <= r["pay_date"] <= universe.cfg.as_of


def test_custodian_corporate_actions_feed_fails_then_is_late(after, universe):
    inc, n = universe.stories.incident, len(universe.days)
    feeds = [r for r in after["feedhub"]["feeds"].dicts() if r["source_id"] == inc.source_id
             and r["data_type"] == CA_FEED_TYPE]
    assert len(feeds) == 1
    dl = {r["business_date"]: r for r in after["feedhub"]["feed_deliveries"].dicts()
          if r["feed_id"] == feeds[0]["feed_id"]}
    assert len(dl) == n and {r["feed_type"] for r in dl.values()} == {CA_FEED_TYPE}
    assert dl[universe.days[n - FAILED_FROM_END]]["status"] == "failed"
    assert dl[universe.days[n - SPLIT_FROM_END]]["status"] == "late"
    others = {d: r["status"] for d, r in dl.items()
              if d not in (universe.days[n - FAILED_FROM_END], universe.days[n - SPLIT_FROM_END])}
    assert set(others.values()) == {"on_time"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/sim/test_incident.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.sim.incident'`.

- [ ] **Step 3: Implement `backend/prism/sim/incident.py`**

```python
"""M9 cross-system incident (spec 2026-10-03 §2): a 2-for-1 split on one equity that the reference data missed,
surfaced in every system.

FeedHub fails, then delivers late, the custodian's corporate-actions feed. RefMaster leaves the split pending.
MarketMaster accepts the halved price. AssetRecon's internal book keeps the unsplit quantity, so every holder breaks
NAV on two days until ops fix the quantities by hand. CashRecon receives a cash-in-lieu payment with no ledger
entry. Background noise of the same kinds (processed corporate actions, unrelated spikes, matched cash in lieu)
makes an agent follow the keys rather than take the top row.

Runs after the five projectors, on their in-memory tables, with its own stream Random(seed + 6), drawing in a fixed
order (refmaster, feedhub, marketmaster, assetrecon, cashrecon). It appends rows, or edits rows `owned` names, and
never draws from another projector's stream, so every planted story is unchanged."""
import itertools
import random
from collections.abc import Callable
from datetime import time, timedelta

from prism.sim.calendar import at
from prism.sim.ids import make_lei
from prism.sim.keys import delivery_id, feed_id, fund_cash_account_id, fund_entity_id
from prism.sim.model import TableData
from prism.sim.project_refmaster import STEWARDS
from prism.sim.universe import INCIDENT_SPLIT_FROM_END, LEGAL_SUFFIX, REGION_OF, Incident, Universe

FAILED_FROM_END = 6                    # 23 Sep for the default as-of: the corporate-actions delivery fails
SPLIT_FROM_END = INCIDENT_SPLIT_FROM_END   # 24 Sep: the split is effective; that day's delivery is late
FIX_FROM_END = 3                       # 28 Sep: internal ops correct the quantities by hand
CASH_FROM_END = 2                      # 29 Sep: the cash in lieu lands
SPLIT_RATIO = 2
NOISE_CORPORATE_ACTIONS, NOISE_SPIKES, NOISE_CASH_IN_LIEU = 15, 10, 5
CA_FEED_TYPE = "corporate_actions"
CA_FEED = (CA_FEED_TYPE, "MT564", time(6, 30))
FUND_COUNTRY = {"USD": "US", "EUR": "LU", "GBP": "GB"}


# ----------------------------------------------------------------------------------------------- helpers
def _set(t: TableData, i: int, **values) -> None:
    row = list(t.rows[i])
    for name, value in values.items():
        row[t.columns.index(name)] = value
    t.rows[i] = tuple(row)


def _next_id(t: TableData, column: str, prefix: str, width: int) -> Callable[[], str]:
    """Sequential ids continuing past the table's largest `prefix<digits>` id, so appends never collide."""
    c = t.columns.index(column)
    top = max((int(r[c][len(prefix):]) for r in t.rows
               if r[c].startswith(prefix) and r[c][len(prefix):].isdigit()), default=0)
    counter = itertools.count(top + 1)
    return lambda: f"{prefix}{next(counter):0{width}d}"


def _split_days(u: Universe) -> list:
    n = len(u.days)
    return [u.days[n - SPLIT_FROM_END], u.days[n - SPLIT_FROM_END + 1]]


def owned(u: Universe, db: str, table: str, row: dict) -> bool:
    """Projector rows the incident pass may edit (spec §2.5, completed): everything else it only appends to."""
    inc, n = u.stories.incident, len(u.days)
    split_day, split_days = u.days[n - SPLIT_FROM_END], set(_split_days(u))
    x, holders = inc.security_id, set(inc.holder_ids)
    key = f"{db}.{table}"
    if key in ("marketmaster.golden_prices", "marketmaster.vendor_prices"):
        return row["security_id"] == x and row["price_date"] >= split_day
    if key == "marketmaster.dq_stage_metrics":
        return row["domain"] == "price" and row["business_date"] >= u.days[n - min(u.cfg.vendor_window_days, n)]
    if key in ("assetrecon.internal_positions", "assetrecon.custodian_positions"):
        return row["security_id"] == x and row["portfolio_id"] in holders and row["as_of"] >= split_day
    if key == "assetrecon.recon_exceptions":
        return row["security_id"] == x and row["portfolio_id"] in holders and row["business_date"] >= split_day
    if key == "assetrecon.recon_runs":
        return (row["portfolio_id"] in holders and row["recon_type"] in ("position", "nav")
                and row["business_date"] in split_days)
    if key == "assetrecon.nav_checks":
        return row["portfolio_id"] in holders and row["nav_date"] in split_days
    return False


def incident_record(u: Universe) -> dict:
    """The incident ids as recorded in app.seed_info.incident."""
    inc = u.stories.incident
    anchor = next(k for k, p in enumerate(u.portfolios) if p.portfolio_id == inc.anchor_portfolio_id)
    return {"security_id": inc.security_id, "issuer_entity_id": inc.issuer_entity_id, "source_id": inc.source_id,
            "anchor_portfolio_id": inc.anchor_portfolio_id, "holder_ids": list(inc.holder_ids),
            "fund_entity_id": fund_entity_id(u.cfg.n_entities, anchor),
            "fund_cash_account_id": fund_cash_account_id(u.cfg.n_cash_accounts, anchor)}


# ----------------------------------------------------------------------------------------------- refmaster
def _refmaster(u: Universe, inc: Incident, rng: random.Random, t: dict[str, TableData]) -> None:
    n = len(u.days)
    ents = t["legal_entities"]
    leis = {r[ents.columns.index("lei")] for r in ents.rows}
    for k, p in enumerate(u.portfolios):
        country = FUND_COUNTRY[p.base_ccy]
        lei = make_lei(rng)
        while lei in leis:
            lei = make_lei(rng)
        leis.add(lei)
        ents.add(fund_entity_id(u.cfg.n_entities, k), lei, f"{p.name} {LEGAL_SUFFIX[country]}", country,
                 REGION_OF[country], "Financials", None, "active", "fund")
    ca = t["corporate_actions"]
    next_ca = _next_id(ca, "ca_id", "CA", 6)
    split_day = u.days[n - SPLIT_FROM_END]
    ca.add(next_ca(), inc.security_id, "split", split_day, u.days[n - CASH_FROM_END], float(SPLIT_RATIO), "pending",
           inc.issuer_entity_id)
    held = {sid for h in u.holdings.values() for sid, _ in h}
    pool = sorted(s.security_id for s in u.securities_by_class["Equity"]
                  if s.security_id in held and s.security_id != inc.security_id and s.status == "active")
    window = u.days[n - min(u.cfg.position_window_days, n):n - 1]
    for sid in rng.sample(pool, min(NOISE_CORPORATE_ACTIONS, len(pool))):
        event = rng.choice(("dividend", "name_change"))      # no quantity effect: positions keep matching
        ex = rng.choice(window)
        pay = min(ex + timedelta(days=rng.randint(1, 5)), u.cfg.as_of)
        ratio = round(rng.uniform(0.1, 3.0), 4) if event == "dividend" else None
        ca.add(next_ca(), sid, event, ex, pay, ratio, "processed", u.security_by_id[sid].issuer_entity_id)
    exc = t["exceptions"]
    exc.add(_next_id(exc, "exc_id", "EX", 7)(), "R010", "corporate_action", inc.security_id, "Equity", "open",
            STEWARDS[0], at(split_day, 9, 30), None)
    cr = t["change_requests"]
    cr.add(_next_id(cr, "change_id", "CR", 6)(), "corporate_action", inc.security_id, STEWARDS[1], None, "pending",
           at(split_day, 10))


# ----------------------------------------------------------------------------------------------- feedhub
def _feedhub(u: Universe, inc: Incident, rng: random.Random, t: dict[str, TableData]) -> None:
    n = len(u.days)
    src = next(s for s in u.sources if s.source_id == inc.source_id)
    data_type, fmt, expected = CA_FEED
    fid = feed_id(src.source_id, data_type)
    t["feeds"].add(fid, src.source_id, data_type, fmt, "daily", expected, src.source_type)
    for i, d in enumerate(u.days):
        due = at(d, expected.hour, expected.minute)
        if i == n - FAILED_FROM_END:
            status, received, latency, count, error = \
                "failed", due + timedelta(minutes=rng.randint(0, 60)), None, None, "SCHEMA_MISMATCH"
        elif i == n - SPLIT_FROM_END:
            latency = rng.randint(300, 600)
            status, received, count, error = "late", due + timedelta(minutes=latency), rng.randint(1, 40), None
        else:
            status, latency, count, error = "on_time", 0, rng.randint(1, 40), None
            received = due - timedelta(minutes=rng.randint(5, 120))
        t["feed_deliveries"].add(delivery_id(src.source_id, data_type, d), fid, src.source_id, d, status, received,
                                 latency, count, error, src.source_type, data_type)


# ----------------------------------------------------------------------------------------------- entry point
def apply_incident(u: Universe, tables: dict[str, dict[str, TableData]]) -> None:
    inc = u.stories.incident
    rng = random.Random(u.cfg.seed + 6)
    _refmaster(u, inc, rng, tables["refmaster"])
    _feedhub(u, inc, rng, tables["feedhub"])
```

Later tasks add their own imports: Task 6 adds `Counter`, `STAGES`, `VENDOR_COVERAGE`, `VENDOR_RANK` and `VENDORS`; Task 7 adds `INV_OPS`; Task 8 adds `ops_users`, `statement_format` and `CashAccount`.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run pytest -q tests/sim/test_incident.py tests/sim/test_no_drift.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/incident.py backend/tests/sim/test_incident.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(sim): incident pass - fund entities, pending split, corporate-actions feed

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Incident pass, part 2: MarketMaster (halved price, accepted spike, noise, DQ recount)

**Files:**
- Modify: `backend/prism/sim/incident.py`
- Test: `backend/tests/sim/test_incident.py` (append)

**Interfaces:**
- Produces: `_marketmaster(u, inc, rng, t)` and `recount_price_stages(u: Universe, t: dict[str, TableData]) -> None`. `apply_incident` calls `_marketmaster` third, after `_feedhub`.

- [ ] **Step 1: Write the failing tests** (append)

```python
from prism.sim.incident import NOISE_SPIKES


def test_prices_halve_from_the_split_day(after, before, universe):
    inc, n = universe.stories.incident, len(universe.days)
    split_day = universe.days[n - SPLIT_FROM_END]
    for name in ("golden_prices", "vendor_prices"):
        old, new = before["marketmaster"][name].dicts(), after["marketmaster"][name].dicts()
        hits = 0
        for o, r in zip(old, new):
            if r["security_id"] == inc.security_id and r["price_date"] >= split_day:
                assert r["value"] == round(o["value"] / 2, 6)
                hits += 1
        assert hits, name


def test_the_spike_is_accepted_and_noise_avoids_the_incident(after, before, universe):
    inc, n = universe.stories.incident, len(universe.days)
    new = after["marketmaster"]["price_suspects"].dicts()[len(before["marketmaster"]["price_suspects"].rows):]
    accepted = [r for r in new if r["status"] == "accepted"]
    assert len(accepted) == 1
    s = accepted[0]
    assert (s["security_id"], s["kind"], s["deviation_pct"], s["price_date"]) == \
        (inc.security_id, "spike", -50.0, universe.days[n - SPLIT_FROM_END])
    noise = [r for r in new if r is not s]
    held = {sid for pid in inc.holder_ids for sid, _ in universe.holdings[pid]}
    assert len(noise) == NOISE_SPIKES
    assert all(r["kind"] == "spike" and r["status"] == "resolved" and r["security_id"] not in held for r in noise)


def test_price_funnel_counts_every_suspect(after, universe):
    from collections import Counter
    n = len(universe.days)
    per_day = Counter(r["price_date"] for r in after["marketmaster"]["price_suspects"].dicts())
    window = set(universe.days[n - universe.cfg.vendor_window_days:])
    rows = [r for r in after["marketmaster"]["dq_stage_metrics"].dicts()
            if r["domain"] == "price" and r["stage"] == "suspect" and r["business_date"] in window]
    assert rows and all(r["count"] == per_day[r["business_date"]] for r in rows)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/sim/test_incident.py -k "halve or spike or funnel"`
Expected: FAIL with `ImportError: cannot import name 'NOISE_SPIKES'`, or with assertion failures if it already imports.

- [ ] **Step 3: Implement** (in `incident.py`, above the entry point)

Add these imports: `from collections import Counter`, `from prism.sim.project_marketmaster import STAGES`, and `VENDOR_COVERAGE, VENDOR_RANK, VENDORS` to the `prism.sim.universe` import.

```python
# ----------------------------------------------------------------------------------------------- marketmaster
def _golden_vendor(asset_class: str) -> str:
    return min((v for v, _ in VENDORS if asset_class in VENDOR_COVERAGE[v]), key=VENDOR_RANK.__getitem__)


def _marketmaster(u: Universe, inc: Incident, rng: random.Random, t: dict[str, TableData]) -> None:
    n = len(u.days)
    split_day = u.days[n - SPLIT_FROM_END]
    for name in ("golden_prices", "vendor_prices"):   # the market halved; the accepted spike carries it to golden
        tb = t[name]
        sid, day, value = (tb.columns.index(c) for c in ("security_id", "price_date", "value"))
        for k, r in enumerate(tb.rows):
            if r[sid] == inc.security_id and r[day] >= split_day:
                _set(tb, k, value=round(r[value] / SPLIT_RATIO, 6))
    ps = t["price_suspects"]
    next_ps = _next_id(ps, "suspect_id", "PS", 7)
    ps.add(next_ps(), inc.security_id, _golden_vendor("Equity"), split_day, "spike", -50.0, "accepted", "Equity")
    near = {sid for pid in inc.holder_ids for sid, _ in u.holdings[pid]}
    pool = sorted(s.security_id for s in u.securities if s.security_id not in near and s.status == "active")
    days = range(n - min(u.cfg.vendor_window_days, n), n - 3)          # older than 3 days: already resolved
    for sid in rng.sample(pool, min(NOISE_SPIKES, len(pool))):
        s = u.security_by_id[sid]
        vid = rng.choice([v for v, _ in VENDORS if s.asset_class in VENDOR_COVERAGE[v]])
        dev = round(rng.choice((-1, 1)) * rng.uniform(8, 20), 4)
        ps.add(next_ps(), sid, vid, u.days[rng.choice(days)], "spike", dev, "resolved", s.asset_class)
    recount_price_stages(u, t)


def recount_price_stages(u: Universe, t: dict[str, TableData]) -> None:
    """Inside the vendor window the price-domain funnel counts that day's suspects (project_marketmaster._dq_metrics):
    recompute it from the current suspects with the same formula, without drawing."""
    n = len(u.days)
    ps = t["price_suspects"]
    per_day = Counter(r[ps.columns.index("price_date")] for r in ps.rows)
    dq = t["dq_stage_metrics"]
    at_key = {(r["business_date"], r["domain"], r["stage"]): k for k, r in enumerate(dq.dicts())}
    for i in range(n - min(u.cfg.vendor_window_days, n), n):
        d = u.days[i]
        acquired = dq.rows[at_key[(d, "price", "acquired")]][dq.columns.index("count")]
        suspect = per_day[d]
        validated = acquired - suspect
        approved = validated + (int(suspect * 0.8) if i < n - 1 else 0)
        for stage, count in zip(STAGES, (acquired, validated, suspect, approved, approved), strict=True):
            _set(dq, at_key[(d, "price", stage)], count=count)
```

Extend `apply_incident` with `_marketmaster(u, inc, rng, tables["marketmaster"])` after `_feedhub`.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run pytest -q tests/sim/test_incident.py tests/sim/test_marketmaster_projection.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/incident.py backend/tests/sim/test_incident.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(sim): incident pass - halved price, accepted spike, recounted price funnel

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Incident pass, part 3: AssetRecon (split positions, corporate-action exceptions, NAV breaches)

**Files:**
- Modify: `backend/prism/sim/incident.py`
- Test: `backend/tests/sim/test_incident.py` (append)

**Interfaces:**
- Produces: `_assetrecon(u, inc, t)` (no draws), called fourth by `apply_incident`.

- [ ] **Step 1: Write the failing tests** (append)

```python
from prism.sim.incident import FIX_FROM_END


def _rows(tables, db, name, **match):
    return [r for r in tables[db][name].dicts() if all(r[k] == v for k, v in match.items())]


def test_every_holder_breaches_nav_on_the_two_split_days(after, universe):
    inc, n = universe.stories.incident, len(universe.days)
    for pid in inc.holder_ids:
        for i in (n - SPLIT_FROM_END, n - SPLIT_FROM_END + 1):
            (nav,) = _rows(after, "assetrecon", "nav_checks", portfolio_id=pid, nav_date=universe.days[i])
            assert nav["diff_bps"] < -5, (pid, nav)                      # internal NAV fell: half the value missing


def test_positions_show_the_split_and_the_manual_fix(after, before, universe):
    inc, n = universe.stories.incident, len(universe.days)
    for pid in inc.holder_ids:
        q0 = dict(universe.holdings[pid])[inc.security_id]
        for i in range(n - SPLIT_FROM_END, n):
            d = universe.days[i]
            internal = {r["qty"] for r in _rows(after, "assetrecon", "internal_positions", portfolio_id=pid,
                                                security_id=inc.security_id, as_of=d)}
            assert internal == {q0 * (2 if i >= n - FIX_FROM_END else 1)}
            (old,) = _rows(before, "assetrecon", "custodian_positions", portfolio_id=pid, security_id=inc.security_id,
                           as_of=d)
            (new,) = _rows(after, "assetrecon", "custodian_positions", portfolio_id=pid, security_id=inc.security_id,
                           as_of=d)
            assert new["qty"] == old["qty"] + q0


def test_split_days_raise_one_closed_corporate_action_exception_per_holder(after, universe):
    inc, n = universe.stories.incident, len(universe.days)
    for pid in inc.holder_ids:
        for i in (n - SPLIT_FROM_END, n - SPLIT_FROM_END + 1):
            d = universe.days[i]
            (x,) = _rows(after, "assetrecon", "recon_exceptions", portfolio_id=pid, security_id=inc.security_id,
                         business_date=d)
            assert (x["cause_code"], x["status"]) == ("corporate_action", "closed")
            (cust,) = _rows(after, "assetrecon", "custodian_positions", portfolio_id=pid, security_id=inc.security_id,
                            as_of=d)
            internal = _rows(after, "assetrecon", "internal_positions", portfolio_id=pid,
                             security_id=inc.security_id, as_of=d)[0]
            assert x["diff_qty"] == cust["qty"] - internal["qty"]
            (run,) = _rows(after, "assetrecon", "recon_runs", run_id=x["run_id"])
            assert run["unmatched"] == len(_rows(after, "assetrecon", "recon_exceptions", run_id=x["run_id"]))
            (nav_run,) = _rows(after, "assetrecon", "recon_runs", portfolio_id=pid, recon_type="nav", business_date=d)
            assert (nav_run["matched"], nav_run["unmatched"]) == (0, 1)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/sim/test_incident.py -k "holder or positions"`
Expected: FAIL on the NAV and position assertions, since nothing has edited AssetRecon yet.

- [ ] **Step 3: Implement** (in `incident.py`)

Add the import `from prism.sim.project_assetrecon import INV_OPS`.

```python
# ----------------------------------------------------------------------------------------------- assetrecon
def _assetrecon(u: Universe, inc: Incident, t: dict[str, TableData]) -> None:
    n = len(u.days)
    split, fix = n - SPLIT_FROM_END, n - FIX_FROM_END
    split_days = _split_days(u)
    x, holders = inc.security_id, set(inc.holder_ids)
    day = {d: i for i, d in enumerate(u.days)}
    unsplit = {pid: dict(u.holdings[pid])[x] for pid in holders}
    extra = {pid: q * (SPLIT_RATIO - 1) for pid, q in unsplit.items()}   # shares the split adds

    def price(i: int) -> float:            # post-split price; golden == market for a non-stale security
        return u.prices[x][i] / SPLIT_RATIO

    def mine(r: dict, date_col: str) -> bool:
        return r["security_id"] == x and r["portfolio_id"] in holders and day[r[date_col]] >= split

    ip = t["internal_positions"]
    for k, r in enumerate(ip.dicts()):
        if mine(r, "as_of"):
            i = day[r["as_of"]]
            qty = unsplit[r["portfolio_id"]] * (SPLIT_RATIO if i >= fix else 1)   # fixed by hand from FIX
            _set(ip, k, qty=qty, mv=round(qty * price(i), 2))
    cp = t["custodian_positions"]
    for k, r in enumerate(cp.dicts()):
        if mine(r, "as_of"):                                  # the custodian applied the split
            qty = r["qty"] + extra[r["portfolio_id"]]
            _set(cp, k, qty=qty, mv=round(qty * price(day[r["as_of"]]), 2))
    rx = t["recon_exceptions"]
    covered = set()
    for k, r in enumerate(rx.dicts()):
        if mine(r, "business_date"):
            i = day[r["business_date"]]
            if r["business_date"] in split_days:              # a booking difference already drawn that day
                diff = r["diff_qty"] + extra[r["portfolio_id"]]
                _set(rx, k, diff_qty=diff, diff_mv=round(diff * price(i), 2), cause_code="corporate_action",
                     status="closed")
                covered.add((r["portfolio_id"], r["business_date"]))
            else:
                _set(rx, k, diff_mv=round(r["diff_qty"] * price(i), 2))
    runs = t["recon_runs"]
    run_at = {(r["portfolio_id"], r["recon_type"], r["business_date"]): k for k, r in enumerate(runs.dicts())
              if r["portfolio_id"] in holders and r["business_date"] in split_days}
    next_rx = _next_id(rx, "exc_id", "RX", 7)
    fund_group = {p.portfolio_id: p.fund_group for p in u.portfolios}
    for pid in sorted(holders):
        for d in split_days:
            _set(runs, run_at[(pid, "nav", d)], matched=0, unmatched=1)
            if (pid, d) in covered:
                continue
            k = run_at[(pid, "position", d)]
            run = dict(zip(runs.columns, runs.rows[k]))
            _set(runs, k, matched=run["matched"] - 1, unmatched=run["unmatched"] + 1)
            rx.add(next_rx(), run["run_id"], pid, x, d, extra[pid], round(extra[pid] * price(day[d]), 2),
                   "corporate_action", INV_OPS[0], "closed", d + timedelta(days=2), fund_group[pid])
    nav = t["nav_checks"]
    for k, r in enumerate(nav.dicts()):
        if r["portfolio_id"] in holders and r["nav_date"] in split_days:
            # the internal book kept the unsplit quantity at the halved golden price; the administrator did not
            internal = round(r["internal_nav"] - extra[r["portfolio_id"]] * price(day[r["nav_date"]]), 2)
            _set(nav, k, internal_nav=internal,
                 diff_bps=round((internal - r["admin_nav"]) / r["admin_nav"] * 1e4, 3))
```

Extend `apply_incident` with `_assetrecon(u, inc, tables["assetrecon"])` after `_marketmaster`.

- [ ] **Step 4: Run the tests**

Run: `cd backend && uv run pytest -q tests/sim`
Expected: all pass. The AssetRecon projector tests are untouched because they run the projector without the pass.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/incident.py backend/tests/sim/test_incident.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(sim): incident pass - split positions, corporate-action exceptions, NAV breaches

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Incident pass, part 4: CashRecon, plus isolation and determinism checks

**Files:**
- Modify: `backend/prism/sim/incident.py`
- Test: `backend/tests/sim/test_incident.py` (append)

**Interfaces:**
- Produces: `_cashrecon(u, inc, rng, t)`, called last by `apply_incident`. After this task `apply_incident` is complete.

- [ ] **Step 1: Write the failing tests** (append)

```python
from datetime import UTC, date, datetime
from datetime import time as dtime

from prism.sim.incident import CASH_FROM_END, NOISE_CASH_IN_LIEU, owned
from prism.sim.keys import fund_cash_account_id


def test_cash_in_lieu_lands_on_the_anchor_fund_account_unmatched(after, universe):
    inc, n = universe.stories.incident, len(universe.days)
    anchor = next(k for k, p in enumerate(universe.portfolios) if p.portfolio_id == inc.anchor_portfolio_id)
    account = fund_cash_account_id(universe.cfg.n_cash_accounts, anchor)
    (acc,) = _rows(after, "cashrecon", "private.cash_accounts", account_id=account)
    assert acc["legal_entity_id"] == fund_entity_id(universe.cfg.n_entities, anchor)
    (brk,) = _rows(after, "cashrecon", "breaks", break_type="cash_in_lieu", status="open")
    day = universe.days[n - CASH_FROM_END]
    assert (brk["account_id"], brk["legal_entity_id"], brk["opened_on"], brk["bank_source_id"]) == \
        (account, acc["legal_entity_id"], day, acc["bank_source_id"])
    (entry,) = [r for r in _rows(after, "cashrecon", "statement_entries", account_id=account)
                if r["reference"].startswith("CIL-")]
    assert entry["amount"] == brk["amount"] and entry["value_date"] == day
    assert not _rows(after, "cashrecon", "ledger_entries", reference=entry["reference"])


def test_other_cash_in_lieu_payments_match_their_ledger(after, universe):
    inc = universe.stories.incident
    noise = [r for r in after["cashrecon"]["statement_entries"].dicts()
             if r["reference"].startswith("CIL-") and r["reference"].split("-")[2] != _anchor_account(universe)]
    assert len(noise) == NOISE_CASH_IN_LIEU
    items = after["cashrecon"]["match_items"].dicts()
    for e in noise:
        (ledger,) = _rows(after, "cashrecon", "ledger_entries", reference=e["reference"])
        assert ledger["amount"] == e["amount"]
        stmt_match = {r["match_id"] for r in items if r["entry_id"] == e["entry_id"]}
        ledger_match = {r["match_id"] for r in items if r["entry_id"] == ledger["entry_id"]}
        assert stmt_match and stmt_match == ledger_match
    assert len(_rows(after, "cashrecon", "breaks", break_type="cash_in_lieu")) == 1
    assert not {e["account_id"] for e in noise} & {fund_cash_account_id(universe.cfg.n_cash_accounts, k)
                                                   for k, p in enumerate(universe.portfolios)
                                                   if p.portfolio_id in inc.holder_ids}


def _anchor_account(universe):
    inc = universe.stories.incident
    anchor = next(k for k, p in enumerate(universe.portfolios) if p.portfolio_id == inc.anchor_portfolio_id)
    return fund_cash_account_id(universe.cfg.n_cash_accounts, anchor)


def test_the_pass_only_appends_or_edits_owned_rows(before, after, universe):
    edited = Counter()
    for db, tables in before.items():
        for name, t in tables.items():
            a = after[db][name]
            assert a.columns == t.columns and len(a.rows) >= len(t.rows), f"{db}.{name}"
            for old, new in zip(t.rows, a.rows):
                if old != new:
                    assert owned(universe, db, name, dict(zip(t.columns, old))), (db, name, old)
                    edited[f"{db}.{name}"] += 1
    assert {"assetrecon.nav_checks", "assetrecon.internal_positions", "marketmaster.golden_prices"} <= set(edited)


def test_incident_pass_is_deterministic(universe, after):
    again = {db: project(universe) for db, project in PROJECTORS.items()}
    apply_incident(universe, again)
    assert all(again[db][name].rows == after[db][name].rows for db in after for name in after[db])


def test_incident_rows_never_happen_after_as_of(after, before, universe):
    future_ok = {"settle_date", "pay_date", "ex_date", "sla_due"}
    end = datetime.combine(universe.cfg.as_of, dtime(23, 59, 59), tzinfo=UTC)
    for db, tables in after.items():
        for name, t in tables.items():
            for row in t.rows[len(before[db][name].rows):]:
                for col, v in zip(t.columns, row):
                    if col in future_ok:
                        continue
                    if isinstance(v, datetime):
                        assert v <= end, (db, name, col, v)
                    elif isinstance(v, date):
                        assert v <= universe.cfg.as_of, (db, name, col, v)


def test_scaled_profile_still_gets_a_complete_incident():
    u = build_universe(SimConfig.small(scale=1.5))
    tables = {db: project(u) for db, project in PROJECTORS.items()}
    apply_incident(u, tables)
    inc = u.stories.incident
    assert _rows(tables, "refmaster", "corporate_actions", status="pending", security_id=inc.security_id)
    assert _rows(tables, "cashrecon", "breaks", break_type="cash_in_lieu")
    assert _rows(tables, "assetrecon", "recon_exceptions", cause_code="corporate_action")
    assert _rows(tables, "marketmaster", "price_suspects", status="accepted")
    assert _rows(tables, "feedhub", "feed_deliveries", feed_type=CA_FEED_TYPE, status="late")


@pytest.mark.slow
def test_full_profile_keeps_every_planted_story():
    u = build_universe(SimConfig())
    tables = {db: project(u) for db, project in PROJECTORS.items()}
    apply_incident(u, tables)
    n, st = len(u.days), u.stories
    since = u.days[n - 6]
    late = Counter(r["source_id"] for r in tables["feedhub"]["feed_deliveries"].dicts()
                   if r["status"] != "on_time" and r["business_date"] >= since)
    assert late.most_common(1)[0][0] == st.late_custodian_source_id
    aged = Counter(r["legal_entity_id"] for r in tables["cashrecon"]["breaks"].dicts()
                   if r["status"] != "closed" and r["age_days"] > 5 and r["ccy"] == "USD")
    assert aged.most_common(1)[0][0] == st.usd_break_entity_id
    for pid in u.stories.incident.holder_ids:                     # every holder breaches on both split days
        for i in (n - SPLIT_FROM_END, n - SPLIT_FROM_END + 1):
            (nav,) = _rows(tables, "assetrecon", "nav_checks", portfolio_id=pid, nav_date=u.days[i])
            assert abs(nav["diff_bps"]) > 5
```

Add `from collections import Counter` to the test file's imports.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/sim/test_incident.py -k "cash or owned or deterministic or scaled"`
Expected: FAIL in the cash tests with `ValueError: not enough values to unpack`, because there is no fund account yet.

- [ ] **Step 3: Implement** (in `incident.py`)

Add these imports: `from prism.sim.project_cashrecon import ops_users`, `statement_format` to the `prism.sim.keys` import, and `CashAccount` to the `prism.sim.universe` import.

```python
# ----------------------------------------------------------------------------------------------- cashrecon
_CASH_IDS = {"statement": ("statements", "stmt_id", "ST", 7), "entry": ("statement_entries", "entry_id", "STE", 7),
             "ledger": ("ledger_entries", "entry_id", "LGE", 7), "match": ("match_groups", "match_id", "M", 7),
             "break": ("breaks", "break_id", "BRK", 6), "action": ("break_actions", "action_id", "BA", 7)}


def _statement(rng, t, nid, a: CashAccount, d, amount: float, security_id: str) -> tuple[str, str]:
    """One statement for the day with a single credit: cash paid for fractional shares after a corporate action."""
    stmt, entry = nid["statement"](), nid["entry"]()
    ref = f"CIL-{security_id}-{a.account_id}-{d:%Y%m%d}"
    opening = round(rng.uniform(1e6, 5e7), 2)
    t["statements"].add(stmt, a.account_id, statement_format(a.bank_source_id), 1, d, opening,
                        round(opening + amount, 2), a.region)
    t["statement_entries"].add(entry, stmt, a.account_id, d, amount, "C", ref, f"Cash in lieu {security_id}", a.region)
    return ref, entry


def _cashrecon(u: Universe, inc: Incident, rng: random.Random, t: dict[str, TableData]) -> None:
    n = len(u.days)
    banks = [s for s in u.sources if s.source_type == "bank"]
    accounts: dict[str, CashAccount] = {}
    for k, p in enumerate(u.portfolios):            # one fund cash account per portfolio, owned by its fund entity
        bank = rng.choice(banks)
        nostro = "".join(rng.choice("0123456789") for _ in range(12))
        a = CashAccount(fund_cash_account_id(u.cfg.n_cash_accounts, k), fund_entity_id(u.cfg.n_entities, k),
                        bank.source_id, bank.bic, nostro, p.base_ccy, p.region)
        t["private.cash_accounts"].add(a.account_id, a.legal_entity_id, a.bank_source_id, a.bank_bic, a.nostro_no,
                                       a.ccy, a.region)
        accounts[p.portfolio_id] = a
    nid = {name: _next_id(t[table], col, prefix, width) for name, (table, col, prefix, width) in _CASH_IDS.items()}
    a, d = accounts[inc.anchor_portfolio_id], u.days[n - CASH_FROM_END]
    amount = round(rng.uniform(150, 2_500), 2)
    _statement(rng, t, nid, a, d, amount, inc.security_id)          # no ledger entry: an open break
    owner, brk = ops_users(a.region)[0], nid["break"]()
    t["breaks"].add(brk, a.account_id, a.legal_entity_id, "cash_in_lieu", amount, a.ccy, d, (u.cfg.as_of - d).days,
                    "open", owner, None, a.region, a.bank_source_id)
    opened = at(d, 20)
    t["break_actions"].add(nid["action"](), brk, "opened", "system", opened, None, a.region)
    t["break_actions"].add(nid["action"](), brk, "assigned", "system", opened + timedelta(minutes=5),
                           f"Assigned to {owner}", a.region)
    equities = sorted(s.security_id for s in u.securities_by_class["Equity"]
                      if s.status == "active" and s.security_id != inc.security_id)
    others = [pid for pid in sorted(accounts) if pid not in inc.holder_ids]
    for pid in rng.sample(others, min(NOISE_CASH_IN_LIEU, len(others))):   # matched, so no break
        a, d = accounts[pid], u.days[rng.randrange(n - 10, n - 1)]
        amount = round(rng.uniform(150, 2_500), 2)
        ref, entry = _statement(rng, t, nid, a, d, amount, rng.choice(equities))
        ledger, match = nid["ledger"](), nid["match"]()
        t["ledger_entries"].add(ledger, a.account_id, f"GL-{a.ccy}-1500", amount, "C", d, ref, a.region)
        t["match_groups"].add(match, "MR01", "auto", at(d, 19, 30), "system", a.region)
        t["match_items"].add(match, "ledger", ledger, a.region)
        t["match_items"].add(match, "statement", entry, a.region)
```

Extend `apply_incident` with `_cashrecon(u, inc, rng, tables["cashrecon"])` last. The final entry point reads:

```python
def apply_incident(u: Universe, tables: dict[str, dict[str, TableData]]) -> None:
    """Plant the incident into the projected tables in place (spec §2.5): appends, plus edits of `owned` rows only."""
    inc = u.stories.incident
    rng = random.Random(u.cfg.seed + 6)
    _refmaster(u, inc, rng, tables["refmaster"])
    _feedhub(u, inc, rng, tables["feedhub"])
    _marketmaster(u, inc, rng, tables["marketmaster"])
    _assetrecon(u, inc, tables["assetrecon"])
    _cashrecon(u, inc, rng, tables["cashrecon"])
```

- [ ] **Step 4: Run the whole sim suite, slow tests included**

Run: `cd backend && uv run pytest -q tests/sim`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/incident.py backend/tests/sim/test_incident.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(sim): incident pass - unmatched cash in lieu, isolation and determinism checks

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Wire the pass into seeding (`project_all`, `seed_info.incident`, CLI)

**Files:**
- Modify: `backend/prism/sim/seed.py`
- Modify: `backend/prism/db/app_migrate.py` (migration 6)
- Modify: `backend/prism/sim/cli.py` (print the incident)
- Modify: `backend/tests/db/test_seed.py` (count rows from `project_all`; spec §2.6)
- Test: `backend/tests/db/test_seed.py` (append)

**Interfaces:**
- Produces: `seed.project_all(universe) -> dict[str, dict[str, TableData]]`, and the `app.seed_info.incident jsonb` column (the `incident_record` dict).

- [ ] **Step 1: Write the failing test and edit the count test** (`tests/db/test_seed.py`)

Change `test_seed_loads_every_projected_row` to count the incident's appended rows as well:

```python
def test_seed_loads_every_projected_row(seeded):
    u = build_universe(SimConfig.small())
    for db, tables in project_all(u).items():   # projectors + the incident pass's appended rows
        with psycopg.connect(seeded.dsn(db, admin=True)) as conn:
            for name, table in tables.items():
                ident = sql.Identifier(*name.split("."))
                count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(ident)).fetchone()[0]
                assert count == len(table.rows), f"{db}.{name}"


def test_seed_info_records_the_incident(seeded):
    from prism.config import APP_DB
    from prism.sim.incident import incident_record
    want = incident_record(build_universe(SimConfig.small()))
    with psycopg.connect(seeded.dsn(APP_DB, admin=True)) as conn:
        got = conn.execute("SELECT incident FROM seed_info").fetchone()[0]
    assert got == want
```

Change the import to `from prism.sim.seed import is_seeded, project_all`.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && uv run pytest -q tests/db/test_seed.py`
Expected: FAIL with `ImportError: cannot import name 'project_all'`.

- [ ] **Step 3: Implement**

`app_migrate.py`, appended to `MIGRATIONS`:

```python
    (6, """
        ALTER TABLE public.seed_info ADD COLUMN IF NOT EXISTS incident jsonb;
    """),
```

`seed.py`:

```python
from psycopg.types.json import Jsonb

from prism.sim.incident import apply_incident, incident_record
from prism.sim.model import TableData


def project_all(universe) -> dict[str, dict[str, TableData]]:
    """Every platform's tables, then the cross-system incident pass over all of them (M9 spec §2.5)."""
    tables = {db: project(universe) for db, project in PROJECTORS.items()}
    apply_incident(universe, tables)
    return tables
```

In `seed_all`, replace the counts line and the `seed_info` insert:

```python
    counts = {db: write_tables(settings, db, tables) for db, tables in project_all(universe).items()}
    with psycopg.connect(settings.dsn(APP_DB, admin=True)) as conn:
        conn.execute("""INSERT INTO seed_info (seed, as_of, profile, incident) VALUES (%s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET seed = EXCLUDED.seed, as_of = EXCLUDED.as_of,
                          profile = EXCLUDED.profile, incident = EXCLUDED.incident, seeded_at = now()""",
                     (cfg.seed, cfg.as_of, cfg.profile, Jsonb(incident_record(universe))))
```

`seed_all` runs `migrate(settings)` first, and `migrate()` already calls `migrate_app` (`prism/db/migrate.py:57`), so migration 6 is in place before the insert.

`cli.py`: after the per-db counts loop, print the incident:

```python
    inc = incident_record(build_universe(cfg))
    print(f"Incident: {inc['security_id']} (issuer {inc['issuer_entity_id']}) via {inc['source_id']}; "
          f"anchor {inc['anchor_portfolio_id']} -> {inc['fund_entity_id']}; holders {', '.join(inc['holder_ids'])}")
```

Import `incident_record` and `build_universe` there.

- [ ] **Step 4: Run the DB suite**

Run: `make db && cd backend && uv run pytest -q tests/db`
Expected: all pass, including `test_app_migrate`, whose version list follows `MIGRATIONS`.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/sim/seed.py backend/prism/db/app_migrate.py backend/prism/sim/cli.py backend/tests/db/test_seed.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(sim): seed the incident and record its ids in seed_info

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: SQL metrics: `feed_type`, `security_id`, `bank_source_id`, and the new `funds` bridge

**Files:**
- Modify: `backend/prism/mcp/metrics/late_feeds.yaml`, `missing_or_failed_deliveries.yaml`, `position_exceptions.yaml`, `open_position_exceptions.yaml`, `open_breaks.yaml`, `open_break_amount.yaml`
- Create: `backend/prism/mcp/metrics/funds.yaml`
- Modify: `backend/tests/mcp/test_metric_catalog.py` (`EXPECTED` gains `funds`; new hop test)

**Interfaces:**
- Produces these metric contracts. Every new dimension also exists as a same-named `str` filter. "Fine" means `fine_grain_dimensions`.
  - `late_feeds` and `missing_or_failed_deliveries`: new dimension `feed_type` (not fine).
  - `position_exceptions` and `open_position_exceptions`: new dimension `security_id`. Fine: `portfolio_id`, `business_date`, `security_id`.
  - `open_breaks` and `open_break_amount`: new dimension `bank_source_id`. Fine: `bank_source_id`.
  - `funds` (new): dimensions `portfolio_id`, `fund_group`, `fund_entity_id`, `custodian_source_id`. Fine: `portfolio_id`, `fund_entity_id`, `custodian_source_id`.

- [ ] **Step 1: Write the failing test** (append to `tests/mcp/test_metric_catalog.py`; add `"funds"` to `EXPECTED`)

```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && uv run pytest -q tests/mcp/test_metric_catalog.py -k "incident or complete"`
Expected: FAIL. `funds` is not in `METRICS`, and the `KeyError` comes from the hop test.

- [ ] **Step 3: Implement the YAML**

`funds.yaml`:

```yaml
id: funds
source: assetrecon
description: 'Funds (portfolios) with their fund legal entity and the custodian feed source that reports their positions: the bridge from a portfolio''s NAV breaches and position exceptions to RefMaster and CashRecon entities and FeedHub sources.'
type: count
expr: count(*)
from: portfolios
dimensions:
  portfolio_id: portfolio_id
  fund_group: fund_group
  fund_entity_id: fund_entity_id
  custodian_source_id: custodian_source_id
filters:
  portfolio_id: {expr: portfolio_id, type: str}
  fund_group: {expr: fund_group, type: str}
  fund_entity_id: {expr: fund_entity_id, type: str}
  custodian_source_id: {expr: custodian_source_id, type: str}
unit: funds
default_order: metric_desc
fine_grain_dimensions: [portfolio_id, fund_entity_id, custodian_source_id]  # identifiers: at most one per metrics-only call
```

Edit the existing YAML files as follows:
- `late_feeds.yaml`: under `dimensions:` add `feed_type: feed_type`, and under `filters:` add `feed_type: {expr: feed_type, type: str}`. Change the description to `'Feed deliveries that were not on time: late, failed or missing (superset of missing_or_failed_deliveries); feed_type is the data delivered (positions, transactions, cash, corporate_actions).'`
- `missing_or_failed_deliveries.yaml`: the same two lines.
- `position_exceptions.yaml` and `open_position_exceptions.yaml`:
  - dimension `security_id: security_id`
  - filter `security_id: {expr: security_id, type: str}`
  - `fine_grain_dimensions: [portfolio_id, business_date, security_id]`, keeping the comment
  - append to the description: ` cause_code corporate_action marks a corporate action applied by the custodian but not internally.`
- `open_breaks.yaml` and `open_break_amount.yaml`:
  - dimension `bank_source_id: bank_source_id`
  - filter `bank_source_id: {expr: bank_source_id, type: str}`
  - `fine_grain_dimensions: [bank_source_id]  # identifier: at most one per metrics-only call`

- [ ] **Step 4: Run the MCP suite**

Run: `cd backend && uv run pytest -q tests/mcp`
Expected: all pass. `test_every_metric_runs_and_returns_data_as_head_data[funds]` runs too.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/mcp/metrics/ backend/tests/mcp/test_metric_catalog.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(metrics): key dimensions for the incident chain and the funds bridge metric

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: REST metrics: `pending_corporate_actions`, `price_suspects`, and `record_ref`

**Files:**
- Modify: `backend/prism/sources/refmaster_api/app.py` (new pending summary; `record_ref` on the exceptions endpoints)
- Modify: `backend/prism/sources/marketmaster_api/app.py` (new suspects summary)
- Modify: `backend/prism/mcp/rest/refmaster.yaml`, `backend/prism/mcp/rest/marketmaster.yaml`
- Test: `backend/tests/api/test_refmaster_api.py`, `backend/tests/api/test_marketmaster_api.py`, `backend/tests/mcp/test_rest_backend.py` (append)

**Interfaces:**
- New endpoints:
  - `GET /api/v1/corporate-actions/pending/summary` (endpoint id `pending_corporate_actions_summary`, rows key `rows`, value field `pending`).
    - `group_by` ∈ {security_id, issuer_entity_id, event_type, ex_date}.
    - Params: `security_id`, `issuer_entity_id`, `event_type`, `ex_date` (date), `from`, `to`.
  - `GET /api/v1/prices/suspects/summary` (endpoint id `prices_suspects_summary`, value field `suspects`).
    - `group_by` ∈ {security_id, kind, vendor_id, price_date, status, asset_class}.
    - Params: `kind`, `vendor_id`, `status`, `security_id`, `price_date` (date), `from`, `to`.
- `exceptions_summary` gains `record_ref` in its `group_by` enum and as a parameter.
- Metrics:
  - `pending_corporate_actions`
    - dimensions: `security_id`, `issuer_entity_id`, `action_type → event_type`, `effective_date → ex_date`
    - filters: the same names
    - fine: `security_id`, `issuer_entity_id`, `effective_date`
  - `price_suspects`
    - dimensions and filters: `security_id`, `kind`, `vendor_id`, `price_date`, `status`
    - fine: `security_id`, `vendor_id`, `price_date`
  - `open_dq_exceptions` and `dq_exceptions_total`: + `record_ref` (fine)

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_refmaster_api.py`:

```python
async def test_pending_corporate_actions_summary(client, headers_for):
    from prism.sim.universe import SimConfig, build_universe
    inc = build_universe(SimConfig.small()).stories.incident
    h = headers_for("steward", AUDIENCE)
    r = await client.get("/api/v1/corporate-actions/pending/summary",
                         params=[("group_by", "security_id"), ("group_by", "issuer_entity_id")], headers=h)
    assert r.status_code == 200
    assert r.json()["rows"] == [{"security_id": inc.security_id, "issuer_entity_id": inc.issuer_entity_id,
                                 "pending": 1}]
    by_issuer = await client.get("/api/v1/corporate-actions/pending/summary",
                                 params={"group_by": "event_type", "issuer_entity_id": inc.issuer_entity_id}, headers=h)
    assert by_issuer.json()["rows"] == [{"event_type": "split", "pending": 1}]
    bad = await client.get("/api/v1/corporate-actions/pending/summary", params={"group_by": "ratio"}, headers=h)
    assert bad.status_code == 422


async def test_exceptions_summary_groups_and_filters_by_record_ref(client, headers_for):
    from prism.sim.universe import SimConfig, build_universe
    inc = build_universe(SimConfig.small()).stories.incident
    r = await client.get("/api/v1/exceptions/summary",
                         params={"group_by": "record_ref", "record_ref": inc.security_id, "domain": "corporate_action"},
                         headers=headers_for("steward", AUDIENCE))
    rows = r.json()["rows"]
    assert len(rows) == 1 and rows[0]["record_ref"] == inc.security_id and rows[0]["open_count"] >= 1
```

Append to `tests/api/test_marketmaster_api.py`. Check the file's client fixture and the `AUDIENCE` import name first; they mirror the refmaster file.

```python
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
```

Append to `tests/mcp/test_rest_backend.py`:

```python
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
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/api tests/mcp/test_rest_backend.py -k "pending or suspects or record_ref or incident or one_value"`
Expected: FAIL. The endpoints return 404, and the metric is unknown.

- [ ] **Step 3: Implement the RefMaster API**

```python
ExceptionGroup = Literal["domain", "asset_class", "status", "rule_id", "record_ref"]
PendingGroup = Literal["security_id", "issuer_entity_id", "event_type", "ex_date"]
```

Change `_exception_filters` to accept `record_ref`, and pass it in both callers. `list_exceptions` passes `None`. `exceptions_summary` gains the param `record_ref: str | None = None`.

```python
def _exception_filters(domain, asset_class, status, date_from, date_to, record_ref=None):
    return [("domain", "eq", domain), ("asset_class", "eq", asset_class), ("status", "eq", status),
            ("record_ref", "eq", record_ref), ("opened_at", "gte", date_from),
            ("opened_at", "lt", date_to + timedelta(days=1) if date_to else None)]
```

Then, in `exceptions_summary`: `cond, params = where(_exception_filters(domain, asset_class, None, date_from, date_to, record_ref))`.

Add this endpoint above `/exceptions`:

```python
@router.get("/corporate-actions/pending/summary")
async def pending_corporate_actions_summary(request: Request, claims: Claims,
                                            group_by: Annotated[list[PendingGroup], Query()] = ["event_type"],
                                            security_id: str | None = None, issuer_entity_id: str | None = None,
                                            event_type: str | None = None, ex_date: date | None = None,
                                            date_from: FromDate = None, date_to: ToDate = None):
    """Corporate actions still pending in the security master (effective but not processed), counted per group."""
    require_table(claims, DB, "corporate_actions")
    check_range(date_from, date_to)
    groups = list(dict.fromkeys(group_by))
    cond, params = where([("status", "eq", "pending"), ("security_id", "eq", security_id),
                          ("issuer_entity_id", "eq", issuer_entity_id), ("event_type", "eq", event_type),
                          ("ex_date", "eq", ex_date), ("ex_date", "gte", date_from), ("ex_date", "lte", date_to)])
    cols = sql.SQL(", ").join(map(sql.Identifier, groups))
    query = sql.SQL("SELECT {g}, count(*) AS pending FROM corporate_actions{w} GROUP BY {g} ORDER BY pending DESC, {g}"
                    ).format(g=cols, w=cond)
    return {"group_by": groups, "rows": await fetch(request, DB, claims, query, params)}
```

Also add `"issuer_entity_id"` to `CA_COLS`, so `/corporate-actions` returns it.

- [ ] **Step 4: Implement the MarketMaster API**

```python
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
```

Place it above `@router.get("/prices/suspects")`.

- [ ] **Step 5: Implement the REST registries**

`refmaster.yaml`:
- In the `exceptions_summary` endpoint, set `params: {group_by: {type: str_list, enum: [domain, asset_class, status, rule_id, record_ref]}, domain: {type: str}, asset_class: {type: str}, record_ref: {type: str}, from: {type: date}, to: {type: date}}`.
- Add the endpoint:

```yaml
  - id: pending_corporate_actions_summary
    path: /api/v1/corporate-actions/pending/summary
    table: corporate_actions
    result_key: rows
    description: Pending corporate actions (effective but not processed) counted by security, issuer, event type or ex-date.
    params: {group_by: {type: str_list, enum: [security_id, issuer_entity_id, event_type, ex_date]}, security_id: {type: str}, issuer_entity_id: {type: str}, event_type: {type: str}, ex_date: {type: date}, from: {type: date}, to: {type: date}}
```

Metrics:
- Give both `open_dq_exceptions` and `dq_exceptions_total` `record_ref: record_ref` in `dimensions`, `record_ref: {param: record_ref, type: str}` in `filters`, and `fine_grain_dimensions: [record_ref]  # a security or entity id: at most one per metrics-only call`.
- Append to both descriptions: ` record_ref is the security id (or the entity id for the entity domain) the exception is about.`
- Add the metric:

```yaml
  - id: pending_corporate_actions
    endpoint: pending_corporate_actions_summary
    value_field: pending
    description: Corporate actions still pending in the security master although effective (not processed), e.g. a stock split the reference data missed; by security, issuer, action type or effective date.
    unit: corporate actions
    dimensions: {security_id: security_id, issuer_entity_id: issuer_entity_id, action_type: event_type, effective_date: ex_date}
    filters: {security_id: {param: security_id, type: str}, issuer_entity_id: {param: issuer_entity_id, type: str}, action_type: {param: event_type, type: str}, effective_date: {param: ex_date, type: date}}
    fine_grain_dimensions: [security_id, issuer_entity_id, effective_date]  # identifiers / date: at most one per metrics-only call
```

`marketmaster.yaml` gets the endpoint and the metric:

```yaml
  - id: prices_suspects_summary
    path: /api/v1/prices/suspects/summary
    table: price_suspects
    result_key: rows
    description: Price suspect counts (stale, spike, missing, conflict) grouped by security, kind, vendor, date or status.
    params: {group_by: {type: str_list, enum: [security_id, kind, vendor_id, price_date, status, asset_class]}, kind: {type: str}, vendor_id: {type: str}, status: {type: str}, security_id: {type: str}, price_date: {type: date}, from: {type: date}, to: {type: date}}
```

```yaml
  - id: price_suspects
    endpoint: prices_suspects_summary
    value_field: suspects
    description: Price suspects of every kind (stale, spike, missing, conflict) by security, kind, vendor, date or status; an accepted spike is a large price move taken into the golden copy (e.g. after a split).
    unit: suspects
    dimensions: {security_id: security_id, kind: kind, vendor_id: vendor_id, price_date: price_date, status: status}
    filters: {security_id: {param: security_id, type: str}, kind: {param: kind, type: str}, vendor_id: {param: vendor_id, type: str}, status: {param: status, type: str}, price_date: {param: price_date, type: date}}
    fine_grain_dimensions: [security_id, vendor_id, price_date]  # identifiers / date: at most one per metrics-only call
```

- [ ] **Step 6: Run the API and MCP suites**

Run: `cd backend && uv run pytest -q tests/api tests/mcp`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/prism/sources/refmaster_api/app.py backend/prism/sources/marketmaster_api/app.py \
  backend/prism/mcp/rest/refmaster.yaml backend/prism/mcp/rest/marketmaster.yaml backend/tests/api/test_refmaster_api.py \
  backend/tests/api/test_marketmaster_api.py backend/tests/mcp/test_rest_backend.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(rest): pending corporate actions, price suspects and record_ref metrics

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Join knowledge: `same_key` groups, glossary terms, `JOINABLE_ON` edges

**Files:**
- Modify: `backend/prism/graph/knowledge/ontology.yaml`, `backend/prism/graph/knowledge/glossary.yaml`
- Modify: `backend/prism/graph/model.py` (`_add_joinable`)
- Modify: `backend/tests/graph/test_loader.py` (22 → 25 metrics)
- Create: `backend/tests/graph/test_joinable.py`

**Interfaces:**
- Produces graph edges `metric:<a> -[JOINABLE_ON {key, other_key}]-> metric:<b>`, with `a < b` by id, different sources, and dimensions whose `ON_COLUMN` targets share a `same_key` group. `key` is a's dimension name, `other_key` is b's.

- [ ] **Step 1: Write the failing tests** (`tests/graph/test_joinable.py`)

```python
"""JOINABLE_ON (M9 spec §3.2): the semantic layer's statement of how the five systems connect."""
import pytest

from prism.graph.model import build_graph


@pytest.fixture(scope="module")
def links(graph_embedder) -> set[frozenset]:
    g = build_graph(graph_embedder)
    out = set()
    for a, typ, b, props in g.edges:
        if typ == "JOINABLE_ON":
            ma, mb = a.removeprefix("metric:"), b.removeprefix("metric:")
            assert ma < mb and g.nodes[a]["props"]["source"] != g.nodes[b]["props"]["source"]
            assert set(props) == {"key", "other_key"}
            out.add(frozenset({(ma, props["key"]), (mb, props["other_key"])}))
    return out


@pytest.mark.parametrize("hop", [
    (("late_feeds", "source_id"), ("funds", "custodian_source_id")),                         # FeedHub -> AssetRecon
    (("pending_corporate_actions", "security_id"), ("position_exceptions", "security_id")),  # RefMaster -> AssetRecon
    (("open_dq_exceptions", "record_ref"), ("position_exceptions", "security_id")),
    (("price_suspects", "security_id"), ("position_exceptions", "security_id")),             # MarketMaster -> AssetRecon
    (("price_suspects", "security_id"), ("pending_corporate_actions", "security_id")),       # MarketMaster -> RefMaster
    (("funds", "fund_entity_id"), ("open_breaks", "legal_entity_id")),                       # AssetRecon -> CashRecon
    (("funds", "fund_entity_id"), ("pending_corporate_actions", "issuer_entity_id")),
    (("open_breaks", "bank_source_id"), ("late_feeds", "source_id")),                        # CashRecon -> FeedHub
])
def test_every_hop_of_the_chain_is_joinable(links, hop):
    assert frozenset(hop) in links


def test_a_custodian_is_never_joined_to_a_bank(links):
    assert frozenset({("funds", "custodian_source_id"), ("open_breaks", "bank_source_id")}) not in links


def test_same_source_metrics_are_never_linked(links):
    assert frozenset({("funds", "portfolio_id"), ("position_exceptions", "portfolio_id")}) not in links
```

In `tests/graph/test_loader.py::test_metric_nodes_carry_the_gateway_contract`, change `len(metrics) == 22` to `len(metrics) == 25`.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/graph/test_joinable.py`
Expected: FAIL. The links set is empty, so the hop assertions fail.

- [ ] **Step 3: Update the ontology and glossary**

`ontology.yaml` `same_key`, the full new list:

```yaml
same_key:
  - [column:refmaster.securities.security_id, column:marketmaster.instruments.security_id, column:assetrecon.internal_positions.security_id, column:assetrecon.custodian_positions.security_id, column:assetrecon.recon_exceptions.security_id, column:marketmaster.price_suspects.security_id, column:refmaster.corporate_actions.security_id, column:refmaster.exceptions.record_ref]
  - [column:refmaster.securities.isin, column:marketmaster.instruments.isin]
  - [column:refmaster.legal_entities.entity_id, column:cashrecon.cash_accounts.legal_entity_id, column:cashrecon.breaks.legal_entity_id, column:marketmaster.esg_scores.entity_id, column:refmaster.securities.issuer_entity_id, column:assetrecon.portfolios.fund_entity_id, column:refmaster.corporate_actions.issuer_entity_id]
  - [column:feedhub.sources.source_id, column:assetrecon.custodians.feed_source_id, column:cashrecon.cash_accounts.bank_source_id]
  # custodian and bank sources share the source_id space but never each other's rows: separate groups keep a
  # custodian from being joined to a bank (funds.custodian_source_id vs open_breaks.bank_source_id)
  - [column:feedhub.feed_deliveries.source_id, column:assetrecon.custodians.feed_source_id, column:assetrecon.portfolios.custodian_source_id]
  - [column:feedhub.feed_deliveries.source_id, column:cashrecon.cash_accounts.bank_source_id, column:cashrecon.breaks.bank_source_id]
  - [column:feedhub.feed_deliveries.delivery_id, column:assetrecon.custodian_positions.delivery_id]
```

`glossary.yaml` new terms, each added in its section:

```yaml
  # cash
  - {name: cash in lieu, glossary: cash, tags: [column:cashrecon.breaks.break_type], synonyms: [cash-in-lieu, fractional share payment], definition: "Cash paid for fractional shares after a corporate action such as a split; one with no ledger entry is a cash_in_lieu break."}
  # feeds
  - {name: feed type, glossary: feeds, tags: [column:feedhub.feed_deliveries.feed_type], synonyms: [feed data type, kind of feed], definition: "What a feed delivers: positions, transactions, cash statements or corporate actions."}
  # assets
  - {name: fund entity, glossary: assets, defines: [metric:funds], tags: [column:assetrecon.portfolios.fund_entity_id], synonyms: [fund legal entity, fund bridge, fund custodian mapping], definition: "The legal entity a fund is booked under and the custodian feed source reporting its positions; links a portfolio to RefMaster, CashRecon and FeedHub."}
  # reference
  - {name: pending corporate action, glossary: reference, broader: corporate action, defines: [metric:pending_corporate_actions], synonyms: [missed corporate action, unprocessed corporate action, missed split], definition: "A corporate action that is effective but not yet processed in the security master, such as a split the reference data missed."}
  # market
  - {name: price suspect, glossary: market, defines: [metric:price_suspects], synonyms: [suspect price, price spike, flagged price], definition: "A vendor price flagged by validation (stale, spike, missing or conflict); an accepted spike is a large move taken into the golden copy."}
```

- [ ] **Step 4: Implement `_add_joinable` in `model.py`**

```python
def _add_joinable(g: Graph, k: Knowledge) -> None:
    """(:Metric)-[:JOINABLE_ON {key, other_key}]->(:Metric) for metrics of different sources whose dimensions map
    (ON_COLUMN) to columns of one same_key group: the semantic layer's statement of how the systems connect. One edge
    per dimension pair, from the lower metric id. The edge has no scopes of its own: retrieval shows it only when both
    metrics (and both dimensions) pass gate()."""
    groups: dict[str, set[int]] = {}
    for n, group in enumerate(k.same_key):
        for col in group:
            groups.setdefault(col, set()).add(n)
    keyed = []
    for a, typ, b, _ in g.edges:
        if typ == "ON_COLUMN" and b in groups:
            d = g.nodes[a]["props"]
            keyed.append((d["metric"], g.nodes[f"metric:{d['metric']}"]["props"]["source"], d["name"], groups[b]))
    for ma, sa, da, ga in keyed:
        for mb, sb, db, gb in keyed:
            if sa != sb and ma < mb and ga & gb:
                g.edge(f"metric:{ma}", "JOINABLE_ON", f"metric:{mb}", key=da, other_key=db)
```

In `build_graph`, call `_add_joinable(g, k)` right after `_add_knowledge(g, k, exclude)`.

- [ ] **Step 5: Run the graph suites**

Run: `cd backend && uv run pytest -q tests/graph/test_joinable.py tests/graph/test_knowledge.py tests/graph/test_loader.py && uv run pytest -q tests/graph/test_retrieval.py -k "recall or budget or join_path"`
Expected: all pass. `make db` must be up for the neo4j-marked tests.

The second command matters. Tasks 2 and 10–12 change what the graph embeds: new columns, metrics, descriptions and glossary terms. Recall has about two misses of headroom at R@3, so a drop must surface here, where it is caused, not in Task 13. If recall falls:
- Read the printed misses.
- Remove the synonym or description phrase that pulled the wrong metric up. New synonyms are the usual culprit.
- Do not lower the thresholds.

- [ ] **Step 6: Commit**

```bash
git add backend/prism/graph/knowledge/ontology.yaml backend/prism/graph/knowledge/glossary.yaml \
  backend/prism/graph/model.py backend/tests/graph/test_joinable.py backend/tests/graph/test_loader.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(graph): JOINABLE_ON links between metrics of different systems

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Retrieval: `metric_links` plus one-hop linked metrics after the direct hits

**Files:**
- Modify: `backend/prism/graph/retrieval.py`
- Test: `backend/tests/graph/test_retrieval.py` (append)

**Interfaces:**
- Produces:
  - `PACK_KEYS` gains `"metric_links"`. Each link is `{"a": str, "b": str, "on": "<a dim> = <b dim>"}`.
  - `MAX_LINKED_METRICS = 4`
  - `LINKED_CYPHER`
  - `_metric_map(x: str) -> str`
  - `_link_finish(direct: list[dict], rec: dict, max_linked: int = MAX_LINKED_METRICS) -> tuple[list[dict], list[dict]]`
  - `prune_links(pack: dict) -> dict`

  `context_pack` and `acontext_pack` return packs whose `metric_links` only name metrics in `metrics`.

- [ ] **Step 1: Write the failing tests** (append to `tests/graph/test_retrieval.py`)

```python
from prism.graph.retrieval import MAX_LINKED_METRICS, _link_finish, prune_links
from prism.security.personas import ALL_SOURCES

PF003_Q = "Why did PF003 breach its NAV tolerance on 24 September?"


def test_link_finish_picks_one_linked_metric_per_new_source():
    direct = [{"uid": "u:pe", "id": "position_exceptions", "source": "assetrecon"}]
    rec = {"linked": [{"uid": "u:ca", "id": "pending_corporate_actions", "source": "refmaster"},
                      {"uid": "u:dq", "id": "open_dq_exceptions", "source": "refmaster"},
                      {"uid": "u:ps", "id": "price_suspects", "source": "marketmaster"},
                      {"uid": "u:x", "id": "orphan", "source": "feedhub"}],
           "links": [{"a": "u:ca", "b": "u:pe", "key": "security_id", "other_key": "security_id"},
                     {"a": "u:dq", "b": "u:pe", "key": "record_ref", "other_key": "security_id"},
                     {"a": "u:pe", "b": "u:ps", "key": "security_id", "other_key": "security_id"},
                     {"a": "u:ca", "b": "u:ps", "key": "security_id", "other_key": "security_id"}]}
    metrics, links = _link_finish(direct, rec)
    assert [m["id"] for m in metrics] == ["position_exceptions", "pending_corporate_actions", "price_suspects"]
    assert links == [{"a": "pending_corporate_actions", "b": "position_exceptions", "on": "security_id = security_id"},
                     {"a": "pending_corporate_actions", "b": "price_suspects", "on": "security_id = security_id"},
                     {"a": "position_exceptions", "b": "price_suspects", "on": "security_id = security_id"}]
    assert len(_link_finish(direct, rec, max_linked=1)[0]) == 2


def test_trimmed_pack_has_no_dangling_links():
    pack = {"metrics": [{"id": "funds"}], "metric_links": [{"a": "funds", "b": "open_breaks", "on": "x = y"}]}
    assert prune_links(pack)["metric_links"] == []


@pytest.mark.neo4j
def test_cross_system_question_reaches_all_five_sources(pack):
    p = pack(PF003_Q, "head_data")
    ids = [m["id"] for m in p["metrics"]]
    assert {m["source"] for m in p["metrics"]} == set(ALL_SOURCES), ids
    assert len(ids) <= KIND_LIMITS["Metric"] + MAX_LINKED_METRICS
    assert p["metric_links"] and all(x["a"] in ids and x["b"] in ids for x in p["metric_links"])


@pytest.mark.neo4j
def test_metric_links_follow_the_callers_scopes(pack):
    st = pack("Which pending corporate actions have price spikes?", "steward")
    source = {m["id"]: m["source"] for m in st["metrics"]}
    assert st["metric_links"]
    assert all({source[x["a"]], source[x["b"]]} <= {"refmaster", "marketmaster"} for x in st["metric_links"])
    bi = pack(PF003_Q, "bi_analyst")
    dims = {m["id"]: set(m["dimensions"]) for m in bi["metrics"]}
    assert bi["metric_links"]
    for x in bi["metric_links"]:
        da, db = x["on"].split(" = ")
        assert da in dims[x["a"]] and db in dims[x["b"]]          # never a dimension the caller cannot see
    assert "matched_by" not in _body(bi)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/graph/test_retrieval.py -k "link or five_sources"`
Expected: FAIL with `ImportError: cannot import name 'MAX_LINKED_METRICS'`.

- [ ] **Step 3: Implement** (`retrieval.py`)

Constants:

```python
PACK_KEYS = ("metrics", "terms", "concepts", "columns", "examples", "join_paths", "metric_links")
DROP_ORDER = ("examples", "columns", "join_paths", "metric_links", "concepts", "terms", "metrics")
MAX_LINKED_METRICS = 4   # one-hop JOINABLE_ON expansion: extra metric slots after the direct hits, one per new source
```

Put the shared metric projection after `gate()`, and use it in `EXPAND_CYPHER`. There, replace the inline `collect(m {{.uid, ... }}) AS metrics` with `collect({_metric_map('m')}) AS metrics`.

```python
def _metric_map(x: str) -> str:
    """Cypher map projection of Metric `x` with its visible dimension names (shared by expand and linking)."""
    return (f"{x} {{.uid, .local_uid, .id, .source, .kind, .mcp_tool, .endpoint_id, .unit, .definition, "
            f".required_dimensions, .sensitive_dimensions, .filters, .time_column, .tables, "
            f"dims: COLLECT {{ MATCH ({x})-[:HAS_DIMENSION]->(d:Dimension) WHERE {gate('d')} "
            f"RETURN d.name ORDER BY d.name }}}}")
```

`LINKED_CYPHER`, after `PATH_CYPHER`:

```python
LINKED_CYPHER = f"""
CALL () {{
  UNWIND range(0, size($direct) - 1) AS i
  MATCH (a:Metric {{uid: $direct[i], ns: $ns}})-[:JOINABLE_ON]-(m:Metric)
  WHERE {gate('a')} AND {gate('m')} AND NOT m.uid IN $direct AND NOT m.source IN $sources
  WITH m, min(i) AS r ORDER BY r, m.id
  RETURN collect({_metric_map('m')}) AS linked
}}
CALL (linked) {{
  WITH $direct + [x IN linked | x.uid] AS uids
  MATCH (a:Metric)-[r:JOINABLE_ON]->(b:Metric)
  WHERE a.uid IN uids AND b.uid IN uids AND {gate('a')} AND {gate('b')}
    AND EXISTS {{ MATCH (a)-[:HAS_DIMENSION]->(da:Dimension {{name: r.key}}) WHERE {gate('da')} }}
    AND EXISTS {{ MATCH (b)-[:HAS_DIMENSION]->(dm:Dimension {{name: r.other_key}}) WHERE {gate('dm')} }}
  RETURN collect({{a: a.uid, b: b.uid, key: r.key, other_key: r.other_key}}) AS links
}}
RETURN linked, links
"""
```

Python helpers, above `expand`:

```python
def _linked_params(direct: list[dict], scopes, metrics_only, ns) -> dict:
    return {"direct": [m["uid"] for m in direct], "sources": sorted({m["source"] for m in direct}),
            **gate_params(scopes, metrics_only, ns)}


def _link_finish(direct: list[dict], rec: dict, max_linked: int = MAX_LINKED_METRICS) -> tuple[list[dict], list[dict]]:
    """Linked metrics in rank order, one per source not yet in the pack, each joined to a direct metric by a visible
    JOINABLE_ON edge; then the links whose both ends are in the pack, as {a, b, on: "<a dim> = <b dim>"}."""
    direct_uids = {m["uid"] for m in direct}
    attached = {e[x] for e in rec["links"] for x, y in (("a", "b"), ("b", "a")) if e[y] in direct_uids}
    sources, picked = {m["source"] for m in direct}, []
    for m in rec["linked"]:
        if len(picked) < max_linked and m["uid"] in attached and m["source"] not in sources:
            picked.append(m)
            sources.add(m["source"])
    metrics = [*direct, *picked]
    ids = {m["uid"]: m["id"] for m in metrics}
    links = [{"a": ids[e["a"]], "b": ids[e["b"]], "on": f"{e['key']} = {e['other_key']}"}
             for e in rec["links"] if e["a"] in ids and e["b"] in ids]
    return metrics, sorted(links, key=lambda x: (x["a"], x["b"], x["on"]))


def prune_links(pack: dict) -> dict:
    """After budget trimming: drop links whose metrics were trimmed away."""
    ids = {m["id"] for m in pack["metrics"]}
    pack["metric_links"] = [x for x in pack["metric_links"] if x["a"] in ids and x["b"] in ids]
    return pack
```

In `expand`, the sync version, add the following after `pack, metrics = _expand_finish(rec, metrics_only)`. `metrics` stays the direct hits, so `join_paths` is unchanged.

```python
    lrec = run_read(driver, LINKED_CYPHER, _linked_params(metrics, scopes, metrics_only, ns), timeout_s)[0] \
        if metrics else {"linked": [], "links": []}
    linked, links = _link_finish(metrics, lrec)
    pack["metrics"] += [_metric_entry(m, metrics_only) for m in linked[len(metrics):]]
    pack["metric_links"] = links
```

`aexpand` gets the same with `(await arun_read(adriver, LINKED_CYPHER, ...))[0]`.

In `context_pack` and `acontext_pack`, return `prune_links(trim_pack(pack, max_tokens))`. `trim_pack` already returns every `PACK_KEYS` key.

- [ ] **Step 4: Run the retrieval suite, recall and latency included**

Run: `cd backend && uv run pytest -q tests/graph/test_retrieval.py tests/gateway`
Expected: all pass, including `test_typed_metric_recall` (direct hits keep their order), `test_pack_latency` (p95 < 100 ms) and `test_issuer_country_join_path`.

If `test_cross_system_question_reaches_all_five_sources` fails, first tell trimming apart from ranking. Compare `expand(...)["metrics"]` with `context_pack(...)["metrics"]` for `PF003_Q` as head_data. A pack can now hold 9 full metric entries plus links on top of everything else, and `trim_pack` removes from the longest list first.
- If `expand` reaches all five sources but `context_pack` does not, the 3000-token budget is the cause. That is a budget decision for the user, like the cap. Stop and report the token counts; do not tweak descriptions.
- Otherwise it is ranking. Print `[m["id"] for m in p["metrics"]]`:
- If `funds` or `position_exceptions` is not among the direct hits, the linked hop cannot reach FeedHub/CashRecon or MarketMaster. Strengthen the retrieval text of the missing metric. Append ` Explains which security, custodian and legal entity sit behind a portfolio's NAV breach.` to its YAML description.
- Re-run this test together with `test_typed_metric_recall`.
- Do not change `KIND_LIMITS` or `MAX_LINKED_METRICS`; that decision belongs to the user.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/graph/retrieval.py backend/tests/graph/test_retrieval.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(retrieval): metric_links and one-hop linked metrics in the context pack

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Also stage the metric YAML if Step 4 changed it.

---

### Task 14: Agent: "Systems you can read" and the cross-system investigation pattern

**Files:**
- Modify: `backend/prism/security/personas.py` (`SOURCE_DISPLAY`)
- Modify: `backend/prism/agent/auth.py` (`UserContext.sources`)
- Modify: `backend/prism/agent/prompts.py` (`dynamic_context`, `SUPERVISOR_SYSTEM`)
- Test: `backend/tests/agent/test_auth.py`, `backend/tests/agent/test_tools.py` (append only)

**Interfaces:**
- Produces:
  - `SOURCE_DISPLAY: dict[str, str]`
  - `UserContext.sources: tuple[str, ...] = ()`, the source ids readable through the caller's scopes, in `ALL_SOURCES` order
  - `dynamic_context(...)`, which appends ` Systems you can read: <display names>.` only when `user.sources` is non-empty

- [ ] **Step 1: Write the failing tests**

Append to `tests/agent/test_tools.py`:

```python
def test_dynamic_context_names_the_readable_systems():
    user = UserContext(sub="u1", roles=("steward",), metrics_only=False, token="t",
                       sources=("refmaster", "marketmaster"))
    assert dynamic_context(user, date(2026, 10, 1), date(2026, 9, 30)).endswith(
        "Metrics-only: False. Systems you can read: RefMaster, MarketMaster.")


def test_supervisor_prompt_teaches_the_cross_system_pattern():
    for phrase in ("metric_links", "Anchor", "Follow links", "single value", "same turn",
                   "Name every system you could not check"):
        assert phrase in SUPERVISOR_SYSTEM, phrase


def test_backend_display_names_match_the_ui():
    import re
    from pathlib import Path

    from prism.security.personas import ALL_SOURCES, SOURCE_DISPLAY
    ts = (Path(__file__).resolve().parents[3] / "frontend/lib/session.ts").read_text()
    ui = dict(re.findall(r"(\w+): \"(\w+)\"", ts.split("SOURCE_NAMES")[1].split("};")[0]))
    assert SOURCE_DISPLAY == {s: ui[s] for s in ALL_SOURCES}
```

Append to `tests/agent/test_auth.py`, which already has `tok(persona)` and `S = Settings()`:

```python
def test_verify_user_lists_readable_sources():
    # invest_ops_growth: assetrecon, feedhub, refmaster.securities, refmaster.legal_entities
    assert verify_user(tok("invest_ops_growth"), S).sources == ("refmaster", "assetrecon", "feedhub")
    assert verify_user(tok("steward"), S).sources == ("refmaster", "marketmaster")
    assert verify_user(tok("head_data"), S).sources == ("refmaster", "marketmaster", "cashrecon", "assetrecon",
                                                        "feedhub")                     # pii:read is not a source
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/agent/test_tools.py tests/agent/test_auth.py`
Expected: FAIL with `TypeError: UserContext.__init__() got an unexpected keyword argument 'sources'`.

- [ ] **Step 3: Implement**

`personas.py`:

```python
# Display names, spelled as the UI's header chips (frontend/lib/session.ts SOURCE_NAMES).
SOURCE_DISPLAY = {"refmaster": "RefMaster", "marketmaster": "MarketMaster", "cashrecon": "CashRecon",
                  "assetrecon": "AssetRecon", "feedhub": "FeedHub"}
```

`auth.py`: add `sources: tuple[str, ...] = ()` after `scope_digest`, and in `verify_user`:

```python
    scopes = claims.get("scopes") if isinstance(claims.get("scopes"), list) else []
    readable = {s.split(".", 1)[0] for s in scopes if isinstance(s, str)}
    ...
    return UserContext(..., scope_digest=digest[:16], sources=tuple(s for s in ALL_SOURCES if s in readable))
```

Import `ALL_SOURCES` there.

`prompts.py`:

```python
def dynamic_context(user: UserContext, today: date, as_of: date) -> str:
    text = (f"Today: {today}. Data as of: {as_of}. Caller roles: {', '.join(user.roles)}. "
            f"Metrics-only: {user.metrics_only}.")
    if user.sources:
        text += f" Systems you can read: {', '.join(SOURCE_DISPLAY[s] for s in user.sources)}."
    return text
```

In `SUPERVISOR_SYSTEM`, insert this block right after workflow step 3 and before step 4, keeping the numbering of the steps after it:

```
   Cross-system investigation (the question asks why something happened, or what else it affected):
   a. Anchor: run the metric the question starts from, grouped by its key dimension(s).
   b. Follow links: the context pack's metric_links say which metrics of other systems join on which key ("on" is "<a dimension> = <b dimension>"). For each link leading to a system not yet checked, run the linked metric filtered by the key values found so far. Issue the calls whose key values you already have in the same turn. Filters on RefMaster and MarketMaster metrics take a single value each. Stop when every readable system has been checked or a hop returns nothing.
   c. Join: one combine over the handles (at most 8) into one incident table. The key changes along the chain (portfolio -> security -> source -> entity), so join hop by hop rather than on one key.
   d. Answer: the causal chain in time order (feed -> reference data -> price -> positions/NAV -> cash), naming each system and citing numbers from results only. Name every system you could not check (not in "Systems you can read", refused, or not linked) instead of implying the chain is complete.
```

- [ ] **Step 4: Run the agent suite**

Run: `cd backend && uv run pytest -q tests/agent`
Expected: all pass. The existing `test_dynamic_context_and_tool_lists` is unchanged, because its user has no sources.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/security/personas.py backend/prism/agent/auth.py backend/prism/agent/prompts.py \
  backend/tests/agent/test_tools.py backend/tests/agent/test_auth.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(agent): cross-system investigation pattern and readable-systems context

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 15: Golden evals: answer-text check and the five incident cases

**Files:**
- Modify: `backend/prism/evals/cases.py` (`AnswerCheck`, `Expect.answer`)
- Modify: `backend/prism/evals/grade.py` (`answer_check`)
- Modify: `backend/prism/evals/golden.yaml` (five cases)
- Test: `backend/tests/evals/test_grade.py`, `backend/tests/evals/test_cases.py` (append)

**Interfaces:**
- Produces: `AnswerCheck(all: list[str] = [], any: list[str] = [])`, which needs at least one entry. `Expect.answer: AnswerCheck | None = None`. `answer_check(spec, text) -> Check` is case-insensitive.

- [ ] **Step 1: Write the failing tests**

Append to `tests/evals/test_grade.py`:

```python
from prism.evals.cases import AnswerCheck
from prism.evals.grade import answer_check


def test_answer_check_all_and_any_case_insensitive():
    spec = AnswerCheck.model_validate({"all": ["AssetRecon", "FeedHub"], "any": ["grain", "refused"]})
    assert answer_check(spec, "Could not check assetrecon or FEEDHUB: the grain was refused.").ok
    miss = answer_check(spec, "Could not check AssetRecon: refused.")
    assert not miss.ok and "FeedHub" in miss.detail
    assert not answer_check(AnswerCheck.model_validate({"any": ["grain"]}), None).ok


def test_grade_golden_adds_the_answer_check():
    c = case(answer={"all": ["PF003"]})
    chat = ChatResult(widgets=[widget()], summary="PF003 breached on two days.")
    names = [x.name for x in grade_golden(c, chat, {"r_aaaaaaaaaaaa": REF}, REF)]
    assert "answer" in names
```

Append to `tests/evals/test_cases.py`:

```python
def test_answer_check_needs_a_phrase():
    import pytest
    from prism.evals.cases import AnswerCheck
    with pytest.raises(ValueError):
        AnswerCheck.model_validate({})


def test_incident_references_cover_all_five_sources():
    from prism.evals.cases import load_golden
    from prism.mcp.metrics import DEFAULT_METRICS_DIR, load_metrics
    from prism.mcp.rest_backend import load_rest_config
    source = {m.id: m.source for m in load_metrics(DEFAULT_METRICS_DIR).values()}
    for rest in ("refmaster", "marketmaster"):
        source |= {metric_id: rest for metric_id in load_rest_config(rest)[1]}
    cases = {c.id: c for c in load_golden()}
    for cid in ("incident_pf003_nav_breach", "incident_late_ca_feed_downstream", "incident_issuer_across_systems"):
        ref = cases[cid].reference
        inputs = ref["args"]["inputs"]
        assert ref["tool"] == "combine" and 5 <= len(inputs) <= 7
        assert {source[i["args"]["metric_id"]] for i in inputs.values()} == {
            "refmaster", "marketmaster", "cashrecon", "assetrecon", "feedhub"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd backend && uv run pytest -q tests/evals/test_grade.py tests/evals/test_cases.py`
Expected: FAIL with `ImportError: cannot import name 'AnswerCheck'`.

- [ ] **Step 3: Implement**

`cases.py`:

```python
class AnswerCheck(_M):
    """Phrases the written answer must contain (case-insensitive): every `all` phrase and at least one `any` phrase."""
    all: list[str] = []
    any: list[str] = []

    @model_validator(mode="after")
    def _some(self) -> "AnswerCheck":
        if not (self.all or self.any):
            raise ValueError("answer needs at least one `all` or `any` phrase")
        return self
```

Add `answer: AnswerCheck | None = None` to `Expect`.

`grade.py`:

```python
def answer_check(spec: AnswerCheck, text: str | None) -> Check:
    low = (text or "").casefold()
    missing = [w for w in spec.all if w.casefold() not in low]
    some = not spec.any or any(w.casefold() in low for w in spec.any)
    detail = f"missing {missing}" if missing else ("" if some else f"none of {spec.any}")
    return Check("answer", not missing and some, detail)
```

At the end of `grade_golden`, before `return checks`, add `if e.answer is not None: checks.append(answer_check(e.answer, chat.summary))`. Import `AnswerCheck`.

`golden.yaml`: append these five cases, using the literals from Global Constraints.

```yaml
# M9 cross-system incident (spec 2026-10-03): a missed 2-for-1 split on SEC001982 (issuer LE00364), seen in all five
# systems. Literals are the full profile's (seed 42, as of 2026-09-30); `make reseed` prints them.
- id: incident_pf003_nav_breach
  persona: head_data
  question: Why did PF003 breach its NAV tolerance on 24 September?
  reference:
    tool: combine
    args:
      sql: >-
        SELECT n.portfolio_id, e.security_id, c.issuer_entity_id, f.fund_entity_id, f.custodian_source_id,
        n.value AS nav_breaches, e.value AS position_exceptions, c.value AS pending_corporate_actions,
        p.value AS price_spikes, l.value AS late_corporate_action_feeds, b.value AS cash_in_lieu_breaks
        FROM n JOIN f ON f.portfolio_id = n.portfolio_id JOIN e ON e.portfolio_id = n.portfolio_id
        JOIN c ON c.security_id = e.security_id LEFT JOIN p ON p.security_id = e.security_id
        LEFT JOIN l ON l.source_id = f.custodian_source_id LEFT JOIN b ON b.legal_entity_id = f.fund_entity_id
      inputs:
        n: {tool: run_metric, args: {metric_id: nav_breaches_above_5bps, dimensions: [portfolio_id], filters: {portfolio_id: PF003}}}
        e: {tool: run_metric, args: {metric_id: position_exceptions, dimensions: [portfolio_id, security_id], filters: {portfolio_id: PF003, cause_code: corporate_action}}}
        f: {tool: run_metric, args: {metric_id: funds, dimensions: [portfolio_id, fund_entity_id, custodian_source_id], filters: {portfolio_id: PF003}}}
        c: {tool: run_metric, args: {metric_id: pending_corporate_actions, dimensions: [security_id, issuer_entity_id]}}
        p: {tool: run_metric, args: {metric_id: price_suspects, dimensions: [security_id], filters: {kind: spike}}}
        l: {tool: run_metric, args: {metric_id: late_feeds, dimensions: [source_id], filters: {feed_type: corporate_actions}}}
        b: {tool: run_metric, args: {metric_id: open_breaks, dimensions: [legal_entity_id], filters: {break_type: cash_in_lieu}}}
  expect:
    metric_id: nav_breaches_above_5bps
    story: [{contains: {portfolio_id: PF003, security_id: SEC001982, custodian_source_id: SRC005, fund_entity_id: LE00403}}]
- id: incident_late_ca_feed_downstream
  persona: head_data
  question: What did the late corporate-actions feed from SRC005 on 24 September affect downstream?
  reference:
    tool: combine
    args:
      sql: >-
        SELECT f.custodian_source_id, f.portfolio_id, e.security_id, c.issuer_entity_id, f.fund_entity_id,
        l.value AS late_deliveries, e.value AS position_exceptions, c.value AS pending_corporate_actions,
        p.value AS price_spikes, b.value AS cash_in_lieu_breaks
        FROM l JOIN f ON f.custodian_source_id = l.source_id JOIN e ON e.portfolio_id = f.portfolio_id
        JOIN c ON c.security_id = e.security_id LEFT JOIN p ON p.security_id = e.security_id
        LEFT JOIN b ON b.legal_entity_id = f.fund_entity_id
      inputs:
        l: {tool: run_metric, args: {metric_id: late_feeds, dimensions: [source_id], filters: {source_id: SRC005, feed_type: corporate_actions, business_date: "2026-09-24"}}}
        f: {tool: run_metric, args: {metric_id: funds, dimensions: [portfolio_id, fund_entity_id, custodian_source_id], filters: {custodian_source_id: SRC005}}}
        e: {tool: run_metric, args: {metric_id: position_exceptions, dimensions: [portfolio_id, security_id], filters: {cause_code: corporate_action}}}
        c: {tool: run_metric, args: {metric_id: pending_corporate_actions, dimensions: [security_id, issuer_entity_id]}}
        p: {tool: run_metric, args: {metric_id: price_suspects, dimensions: [security_id], filters: {kind: spike}}}
        b: {tool: run_metric, args: {metric_id: open_breaks, dimensions: [legal_entity_id], filters: {break_type: cash_in_lieu}}}
  expect:
    metric_id: late_feeds
    story: [{contains: {custodian_source_id: SRC005, portfolio_id: PF003, security_id: SEC001982, fund_entity_id: LE00403}}]
- id: incident_issuer_across_systems
  persona: head_data
  question: What is going wrong for LE00364 across our systems?
  reference:
    tool: combine
    args:
      sql: >-
        SELECT c.issuer_entity_id, c.security_id, e.portfolio_id, f.fund_entity_id, f.custodian_source_id,
        c.value AS pending_corporate_actions, d.value AS open_dq_exceptions, p.value AS price_spikes,
        e.value AS position_exceptions, l.value AS late_corporate_action_feeds, b.value AS cash_in_lieu_breaks
        FROM c LEFT JOIN d ON d.record_ref = c.security_id LEFT JOIN p ON p.security_id = c.security_id
        JOIN e ON e.security_id = c.security_id JOIN f ON f.portfolio_id = e.portfolio_id
        LEFT JOIN l ON l.source_id = f.custodian_source_id LEFT JOIN b ON b.legal_entity_id = f.fund_entity_id
      inputs:
        c: {tool: run_metric, args: {metric_id: pending_corporate_actions, dimensions: [security_id, issuer_entity_id], filters: {issuer_entity_id: LE00364}}}
        d: {tool: run_metric, args: {metric_id: open_dq_exceptions, dimensions: [record_ref], filters: {domain: corporate_action}}}
        p: {tool: run_metric, args: {metric_id: price_suspects, dimensions: [security_id], filters: {kind: spike}}}
        e: {tool: run_metric, args: {metric_id: position_exceptions, dimensions: [portfolio_id, security_id], filters: {cause_code: corporate_action}}}
        f: {tool: run_metric, args: {metric_id: funds, dimensions: [portfolio_id, fund_entity_id, custodian_source_id]}}
        l: {tool: run_metric, args: {metric_id: late_feeds, dimensions: [source_id], filters: {feed_type: corporate_actions}}}
        b: {tool: run_metric, args: {metric_id: open_breaks, dimensions: [legal_entity_id], filters: {break_type: cash_in_lieu}}}
  expect:
    metric_id: pending_corporate_actions
    story: [{contains: {issuer_entity_id: LE00364, security_id: SEC001982, portfolio_id: PF003, custodian_source_id: SRC005, fund_entity_id: LE00403}}]
# Partial access: the reference is what the persona can run; the answer must cover the allowed hops and name the rest.
- id: incident_pf003_nav_breach_bi
  persona: bi_analyst
  question: Why did PF003 breach its NAV tolerance on 24 September?
  reference: {tool: run_metric, args: {metric_id: nav_breaches_above_5bps, dimensions: [portfolio_id]}}
  expect:
    metric_id: nav_breaches_above_5bps
    story: [{contains: {portfolio_id: PF003}}]
    answer: {all: [PF003], any: [grain, too fine, not available, refused, could not]}
- id: incident_issuer_steward_partial
  persona: steward
  question: What is going wrong for LE00364 across our systems?
  reference: {tool: run_metric, args: {metric_id: pending_corporate_actions, dimensions: [security_id], filters: {issuer_entity_id: LE00364}}}
  expect:
    metric_id: pending_corporate_actions
    story: [{contains: {security_id: SEC001982}}]
    answer: {all: [AssetRecon, CashRecon, FeedHub]}
```

- [ ] **Step 4: Run the eval suite**

Run: `cd backend && uv run pytest -q tests/evals tests/agent/test_recipes.py`
Expected: all pass. Each reference has at most 8 recipe nodes.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/evals/cases.py backend/prism/evals/grade.py backend/prism/evals/golden.yaml \
  backend/tests/evals/test_grade.py backend/tests/evals/test_cases.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(evals): five-source incident cases and an answer-text check

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 16: End to end: reseed, graph, `eval-check`, live PF003 run, README

**Files:**
- Modify: `README.md` (planted stories: the incident)
- Modify: `backend/prism/evals/golden.yaml` (only if the reseed prints different literals)

- [ ] **Step 1: Run the full suite**

Run: `make test`
Expected: all pass, with no skips other than the existing live and optional ones.

- [ ] **Step 2: Reseed the full profile and check the printed literals**

Run: `make reseed`
Expected: the last lines include `Incident: SEC001982 (issuer LE00364) via SRC005; anchor PF003 -> LE00403; holders PF003, PF019, PF021, PF025`. If any id differs, the plan's literals are stale. Update every literal in `golden.yaml` and in `test_full_profile_incident_literals`, re-run `uv run pytest -q tests/sim tests/evals`, and amend nothing. Make a new commit instead.

- [ ] **Step 3: Reload the graph and replay every reference**

Run: `make graph && make eval-check`
Expected: `0 problem(s) in N golden and M red-team case(s)`. The new references replay through the running stack, so start it first with `scripts/start_backend.sh`. Every pre-existing story assertion still holds.

- [ ] **Step 4: Live run of the incident cases (costs API money: confirm with the user first)**

Run: `cd backend && HF_HUB_OFFLINE=1 uv run python -m prism.evals.cli --suite golden --case incident_pf003_nav_breach --case incident_pf003_nav_breach_bi --case incident_issuer_steward_partial --max-cost-usd 2`
Expected: the report shows `incident_pf003_nav_breach` passing. In its decision trace (`GET /runs/{run_id}/trace`, or the UI's Reasoning tab), the `run_metric` steps touch metrics of all five sources.

If the run ends with `run_limit` (8 turns), report that to the user with the step count. Do not raise the limits without a decision, because `test_settings_migration` pins them.

- [ ] **Step 5: README**

Under the planted-stories description, near "drives the planted stories", add one paragraph:

```markdown
**Cross-system incident (M9).** A 2-for-1 split on `SEC001982` (issuer `LE00364`) runs through all five systems:
SRC005's corporate-actions feed fails on 23 Sep and is late on 24 Sep (FeedHub); the split stays `pending` with an
open DQ exception (RefMaster); the halved price is accepted as a `spike` (MarketMaster); PF003, PF019, PF021 and
PF025 breach NAV tolerance on 24-25 Sep with `corporate_action` exceptions (AssetRecon); and a cash-in-lieu payment
lands unmatched on PF003's fund account `CA0063` on 29 Sep (CashRecon). The semantic layer links the systems with
`JOINABLE_ON` metric edges (`metric_links` in `search_context`); the three `incident_*` golden cases replay it.
```

- [ ] **Step 6: Commit**

```bash
git add README.md
git -c user.email=cwijayasundara@gmail.com commit -m "docs: the cross-system incident in the planted stories

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Also stage `backend/prism/evals/golden.yaml` and `backend/tests/sim/test_incident.py` if Step 2 changed them.
