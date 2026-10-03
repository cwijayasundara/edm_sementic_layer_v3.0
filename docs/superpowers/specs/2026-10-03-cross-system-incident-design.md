# M9 — Cross-system incident: questions that need all five sources

Date: 2026-10-03. Parent spec: `2026-09-30-agentic-data-intelligence-design.md`.
Follows: `2026-10-02-reasoning-trace-design.md`. Later step (separate spec): the full semantic-layer view (a
type-level "Model" map linked to instances, with the answer's path highlighted), which this spec makes worth
building.

## 1. Goal and scope
Today no question can span all five sources: every metric belongs to one source, no metric can group by
`security_id`, nothing links a fund to a legal entity or cash account, and every combine example and golden case
joins a source with itself. The `same_key` groups exist in the ontology but nothing exercises them.

This spec plants one realistic incident whose cause genuinely runs through all five systems, gives the semantic layer
the metrics, join-key dimensions and metric-to-metric join links needed to follow it with governed metrics only, and
teaches the agent a cross-system investigation pattern.

**In scope:**
- Simulator: a post-projection incident pass (`sim/incident.py`), new fund→entity and fund→cash-account links,
  background noise, a scale knob.
- Semantic layer: new and extended metrics (SQL and REST), new mock-API summary endpoints, wider `same_key` groups,
  `JOINABLE_ON` edges between metrics, `metric_links` in `search_context`, one-hop retrieval expansion.
- Agent: the cross-system pattern in the supervisor prompt, "Systems you can read" in the run context.
- Tests and golden evals for all of the above.

**Out of scope:**
- The full semantic-layer graph view and highlighting (next spec).
- Changing row volumes of the existing default profile.
- Moving or removing any existing planted story.
- Parallel per-source delegation for the chain (hops are sequential; `delegate` is unchanged).

**Success:**
- `make eval-check` is green, including three references that each `combine` 5–8 inputs covering all five sources.
- The existing test suite is green; existing tests are edited only as listed in §2.6.
- A live `make eval` run answers the PF003 question with a decision trace that touches all five sources.

## 2. The incident (simulator)

### 2.1 Chain
A missed corporate action, surfaced in every system:

| # | System | Event | Key to next hop |
|---|---|---|---|
| 1 | FeedHub | Custodian `S*` (PF003's custodian source) — its `corporate_actions` feed delivery for 23 Sep is `failed`; the 24 Sep delivery is `late`. | `source_id` |
| 2 | RefMaster | A 2-for-1 split on security `X`, effective 24 Sep, stays `pending`; an open DQ exception (`record_ref = X`) and an open change request sit on it. `X.issuer_entity_id = E`. | `security_id`, `issuer_entity_id` |
| 3 | MarketMaster | Vendor prices for `X` halve from 24 Sep; a `spike` suspect is raised on 24 Sep and accepted, so the golden price takes the post-split value. The price is right; the reference data missed the split. | `security_id` |
| 4 | AssetRecon | Custodian positions in `X` double from 24 Sep (split applied); internal positions keep the unsplit quantity, so internal market value of `X` halves while the administrator's NAV is unchanged. Every portfolio holding `X` gets exceptions with cause `corporate_action` on 24 and 25 Sep and breaches NAV tolerance (> 5 bps) on those days; PF003 is the anchor (largest weight in `X`). Internal ops correct the quantities by hand on 28 Sep (exceptions closed), but the RefMaster record stays `pending`. | `portfolio_id` → `fund_entity_id` |
| 5 | CashRecon | On 29 Sep a cash-in-lieu payment for fractional shares lands on PF003's fund cash account; no ledger entry matches; an open break of type `cash_in_lieu` is raised. | `legal_entity_id`, `bank_source_id` |

Entry questions (one golden case each, §5.2):
- "Why did PF003 breach its NAV tolerance on 24 September?" (enters at 4)
- "What did the late corporate-actions feed from `S*` on 24 September affect downstream?" (enters at 1)
- "What is going wrong for `E` across our systems?" (enters at 2)

### 2.2 Choosing the incident ids
Chosen deterministically by the incident pass (no RNG), then recorded in the `Stories` record and `app.seed_info`:
- `S*` = PF003's `custodian_source_id` (by construction not SRC001, which is reserved for PF001/002/005).
- `X` = the active equity, not among the five stale-story equities, held by PF003 and by the most other
  portfolios (at least two), ties broken by lowest `security_id`.
- `E` = `X.issuer_entity_id`.

The implementation plan pins the resulting literals in `golden.yaml` after the first generation.

### 2.3 New links
- **Fund entities:** one new legal entity per portfolio, appended to `refmaster.legal_entities` (`LE00401…` for the
  default profile, `entity_type` = fund), and a new column `assetrecon.portfolios.fund_entity_id`, assigned by index
  (no RNG).
- **Fund cash accounts:** one cash account per portfolio, appended to `cashrecon.private.cash_accounts`, owned by the
  fund entity, at a bank drawn from the incident stream.
- **Feed type:** feeds carry a `feed_type` (`positions`, `transactions`, `corporate_actions`, `statements`, …); added
  to the FeedHub DDL and projector if absent. `S*` gains a `corporate_actions` feed if it has none.

### 2.4 Background noise
So the agent must follow keys rather than take the top row:
- About 15 other corporate actions in the window, all `processed`, with matching custodian and internal positions.
- About 10 unrelated `spike` suspects on securities no incident portfolio holds.
- Other cash-in-lieu payments on other fund accounts that match their ledger entries.
- The existing late feeds (SRC001 and normal lateness) are unchanged and remain the larger late-feed signal.

### 2.5 Determinism and isolation
- The incident pass runs after all five projectors and before the writer, on the in-memory rows, with its own
  stream `Random(seed + 6)`.
- It only **appends** rows, or **edits rows it owns** (the incident security's positions and golden prices from 24 Sep,
  the NAV checks of every holder of `X` for 24 and 25 Sep, the `S*` deliveries for 23 and 24 Sep) without drawing from
  any other stream. In the sim, internal NAV is quantity × golden price and administrator NAV is quantity × true
  price (`project_assetrecon.py` `_positions_day`); the pass rewrites the owned NAV rows to the split arithmetic of
  §2.1 rather than relying on that formula.
- No existing projector gains or loses a draw; every existing story (SRC001 late custodian, LE00016 USD breaks,
  Vendor A corp-bond conflicts, stale equities on PF003/PF009 for 28–30 Sep, canaries) is unchanged.
- Derived aggregates that recount suspects or exceptions (e.g. `dq_stage_metrics`) are recomputed after the pass.
- `SimConfig` gains `scale: float = 1.0`, multiplying `n_securities`, `n_entities`, `n_portfolios` and
  `n_cash_accounts`; the default profile is unchanged. Story invariants that need fixed ids (PF001–PF009,
  `SOURCE_LAYOUT`) are guarded as today.

### 2.6 Existing tests that change
- `tests/sim/test_assetrecon_projection.py` (non-story NAV checks stay within 3 bps): the holders of `X` on 24 and
  25 Sep join the story exemptions.
- Tests that count projected rows against the universe (e.g. `tests/sim/test_refmaster_projection.py` legal entities
  == `universe.entities`, `tests/sim/test_universe.py` cash accounts == `n_cash_accounts`) count the incident's
  appended rows separately. The plan greps the suite for every such count before changing code.
- Every story assertion (SRC001 top late source, LE00016 = 64 aged USD breaks, Vendor A conflict delta, the single
  credential ticket, stale equities, canaries) is unchanged and must pass.

## 3. Semantic layer

### 3.1 Metrics and dimensions
Rule: every hop of the chain is answerable with a governed metric, so a metrics-only caller can follow it.

| Source | Metric | Change |
|---|---|---|
| FeedHub | `late_feeds`, `missing_or_failed_deliveries` | + dimension and filter `feed_type` |
| RefMaster (REST) | `pending_corporate_actions` (new) | count of corporate actions with status `pending`; dimensions `security_id`, `issuer_entity_id`, `action_type`, `effective_date`; filters on the same. New endpoint `GET /corporate-actions/summary` in the mock API. |
| RefMaster (REST) | `open_dq_exceptions`, `dq_exceptions_total` | + dimension and filter `record_ref` (a security id, or an entity id for the entity domain) |
| MarketMaster (REST) | `price_suspects` (new) | count of suspects of every kind; dimensions `security_id`, `kind`, `vendor_id`, `price_date`, `status`; filters on the same. New endpoint `GET /prices/suspects/summary`. |
| AssetRecon | `position_exceptions`, `open_position_exceptions` | + dimension and filter `security_id` |
| AssetRecon | `funds` (new) | count of portfolios; dimensions `portfolio_id`, `fund_group`, `fund_entity_id`, `custodian_source_id` — the fund→entity→custodian bridge |
| CashRecon | `open_breaks`, `open_break_amount` | + dimension and filter `bank_source_id`; `break_type` gains `cash_in_lieu` |

For REST metrics the yaml dimension enum, the metric dimensions and the API `Literal` all change together (the REST
backend rejects a dimension the endpoint does not return). REST metric dimensions already get `ON_COLUMN` edges to
the endpoint's backing table (`graph/model.py` `_metric_rows` → `ON_COLUMN`), so §3.2 covers the REST hops.

None of the new dimensions is sensitive. The identifiers (`security_id`, `fund_entity_id`, `record_ref`,
`bank_source_id`) are `grain: fine`, like the existing ids. The grain policy (`gateway/policy.py`) lets a
metrics-only caller pin at most one fine dimension per call (group-by and filter together), so some hops, e.g.
"exceptions in PF003 by security", are refused for `bi_analyst`. That is the governance working, and §5.2 expects a
partial chain for that persona.

### 3.2 Join knowledge
- `same_key` groups gain: `marketmaster.price_suspects.security_id`, `refmaster.corporate_actions.security_id`,
  `refmaster.exceptions.record_ref` (security group); `assetrecon.portfolios.fund_entity_id` (entity group);
  `assetrecon.portfolios.custodian_source_id` (source group).
- At graph build, a **`(:Metric)-[:JOINABLE_ON {key}]->(:Metric)`** edge is created for every pair of metrics in
  different sources whose dimensions map (`ON_COLUMN`) to columns in the same `same_key` group. `key` is the
  dimension name on the first metric. The edge has no scopes of its own: it is visible exactly when both endpoint
  metrics pass `gate()` (an intersection of two single-source scope sets would be empty and hide every edge).
- This edge is the semantic layer's statement of how the systems connect; the next spec draws it.

### 3.3 Retrieval
- `search_context` returns a new `metric_links: [{a, b, on}]` list: the `JOINABLE_ON` edges among the returned
  metrics, both endpoints passing `gate()`. Metric and dimension names only, so metrics-only callers get it
  (unlike `join_paths`, which stays full-caller-only and capped at 3).
- Retrieval expands one hop along `JOINABLE_ON` from the matched metrics (gated, within the existing per-kind item
  cap), so an AssetRecon-shaped question also retrieves the linked RefMaster, MarketMaster, CashRecon and FeedHub
  metrics.

## 4. Agent

### 4.1 Supervisor prompt
Step 3's cross-source guidance gains an investigation pattern:
1. **Anchor:** run the metric the question starts from, grouped by its key dimension(s).
2. **Follow links:** for each `metric_links` entry leading to a system not yet checked, run the linked metric
   filtered by the key values found so far. Stop when every readable system has been checked or a hop returns
   nothing.
3. **Join:** one `combine` over the handles (limit 8) into one incident table. The key changes along the chain
   (portfolio → security → source → entity), so the SQL joins hop by hop rather than on one key.
4. **Answer:** a causal chain in time order (feed → reference data → price → positions/NAV → cash), naming each
   system and citing numbers from results only.

### 4.2 Partial access
- The run context gains "Systems you can read: …" (source names only; already visible as header chips).
- The prompt requires naming the systems that could not be checked instead of implying the chain is complete.
  Hidden metrics never appear in `metric_links`, so nothing leaks.

### 4.3 To verify in planning
- The supervisor's step/tool-call budget fits a five-system chain (about 9–11 calls).
- The M8 decision trace records each hop as its own step touching that hop's metric.

## 5. Testing

### 5.1 Unit and integration
- `tests/sim/test_incident.py`: every link of §2.1 holds in the generated data; noise holds (§2.4); incident ids are
  chosen per §2.2.
- No-drift: for every table, a checksum over the **original columns of the original rows**, excluding rows the
  incident pass owns (§2.5) and the derived tables it recomputes (`marketmaster.dq_stage_metrics`), equals a
  baseline captured from `main` before any code changes.
- Graph: `JOINABLE_ON` exists for every hop of the chain; `metric_links` per persona: `steward` sees only
  RefMaster↔MarketMaster links, `bi_analyst` sees all and no sensitive dimension.
- REST: the new summary endpoints group and filter by `security_id`; an unknown dimension is still rejected.

### 5.2 Golden evals (`golden.yaml`, assertion-graded)
- Three `head_data` cases, one per entry question. Each reference is one `combine` over 5–8 `run_metric` inputs
  covering all five sources (the PF003 path alone needs three AssetRecon metrics: NAV breaches, position exceptions
  and the `funds` bridge). The plan checks that the reference format and `eval-check` support more than two named
  inputs, and extends them if not. Story assertions require the incident table to contain `X`, PF003, `S*` and
  PF003's fund entity.
- One `bi_analyst` case for the PF003 question (live only): the answer covers the hops the grain policy allows and
  names the refused hop rather than guessing.
- One `steward` case for the `E` question that must name the systems not checked. Graded by an answer-text
  assertion (each unreadable system's display name appears in the answer); `grade.py` gains this check if it has none.
  Live-suite only (`make eval`), since `eval-check` has no answer text.
- `make eval-check` replays references without a model; `make eval` runs the live agent.
