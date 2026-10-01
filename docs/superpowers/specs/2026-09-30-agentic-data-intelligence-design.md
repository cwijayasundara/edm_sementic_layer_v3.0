# Agentic Data Intelligence MVP — Design Proposal

**Date:** 2026-09-30 · **Status:** Approved (decisions in §12 resolved) · **Codename:** *Prism* (generic, rename freely)

> "Ask the data platform a question, get a dashboard back."
> A natural-language BI layer over five simulated enterprise data platforms, with a
> graph semantic layer, role-based + row-level security, a supervisor agent behind a
> FastAPI service, and a Next.js dashboard UI.

---

## 1. Understanding (what we're building and why)

**Stated by you**
- Five enterprise data platforms, all simulated with realistic data: two EDMs (reference-data
  mastering and market-data mastering), a cash reconciliation platform, an investment-management
  reconciliation platform (positions / custodian data) and a market/bank data-feed aggregator.
- The EDMs expose data as **REST APIs**, wrapped by **MCP servers**. How the other three are
  exposed is for this proposal to decide.
- An agentic layer (supervisor + subagents) queries all five **uniformly**, grounded by a
  **context graph / semantic layer** (Neo4j, neocarta-style).
- **RBAC + RLS**: the logged-in user only sees the datasets and rows they are entitled to.
- The supervisor is exposed via **FastAPI**. The **Next.js** UI has static KPIs, a
  collapsible AI assistant, and every question produces a visual plus a short summary.
- Postgres and Neo4j run in Docker. Two scripts: `start_backend.sh` and `start_frontend.sh`.
- **No client or vendor names anywhere** in code, data, UI or docs.

**Derived from the reference slides (assumptions — please correct)**
- MVP use case = *conversational BI*. The landing KPIs are deterministic, and the visual
  type (table, heat map, bar, …) is chosen per question.
- "Efficient AI": no LLM tokens spent on deterministic work (KPIs, metric maths, joins).
- MCP-first: internal now, customer-facing MCP later.
- Out of scope: point-in-time/as-reported history, warehouse, regulatory reports, write actions.

**Success criteria**
1. A demo user signs in as one of ~5 personas, sees role-appropriate KPIs, asks ~30 golden
   questions (single- and cross-source), and gets a correct chart + 2–3 sentence summary with
   a "view query / source rows" provenance link. Target is < 15 s p50.
2. The same question asked by two personas returns different, correctly-scoped results.
   A forbidden request returns an explicit "not entitled", never leaked data.
3. Token use is measured per question. Median context stays under ~10k input tokens per question,
   thanks to graph retrieval, caching and the no-raw-rows rule.

---

## 2. The five simulated platforms

The research findings, genericised. Everything below is realistic but fictional.

| # | Simulated system | Real-world archetype | Mastered data | Exposure in MVP |
|---|---|---|---|---|
| 1 | **RefMaster EDM** | Buy-side EDM with SQL heritage. Staging → golden copy, data dictionary, 4-eyes approval, exception management, entity matching | Security master, legal entities (LEI), product & account master, corporate actions, DQ rules/exceptions, data dictionary | **REST API** (mock service) → **MCP server** |
| 2 | **MarketMaster EDM** | Sell-side-heritage EDM. Domain-model REST API, time-series store, multi-vendor source ranking, data-quality intelligence metrics per pipeline stage | Prices & time series from multiple vendors, price validation *suspects* (stale/spike/missing), vendor source ranking & golden price, DQ metrics, ESG scores | **REST API** (mock service) → **MCP server** |
| 3 | **CashRecon** | Cash & transaction reconciliation. Ledger vs bank statement (SWIFT MT940/950, ISO 20022 camt.053), n-way matching, tolerance rules, break workflow | Cash accounts (nostros), statements & entries, ledger entries, match rules, match groups, breaks + actions | **Postgres** → **MCP server** (SQL + metrics) |
| 4 | **AssetRecon** | Investment-management reconciliation. Internal (IBOR/ABOR) vs custodian positions & transactions, NAV checks, sign-offs | Portfolios, custodian accounts, internal & custodian positions/transactions, recon runs, exceptions, NAV checks | **Postgres** → **MCP server** (SQL + metrics) |
| 5 | **FeedHub** | Feed connectivity aggregator. Thousands of custodian/bank/prime-broker feeds, delivery monitoring, late/missing files, support tickets | Sources, feeds, deliveries (status, latency, record counts), support tickets | **Postgres** → **MCP server** (SQL + metrics) |

### Why this exposure split (recommendation)
- **EDMs → REST + MCP.** Real EDMs expose domain-model REST APIs, and you asked for this. It also
  proves the harder case: an agent working over APIs, not only SQL.
- **The two reconciliation platforms and the feed aggregator → Postgres.** They are transactional,
  high-volume, relational stores (matches, breaks, deliveries), and aggregation questions like
  "break ageing by currency" are natural SQL. The real products do have REST APIs, but they are
  workflow APIs (assign, sign-off), not analytics APIs. For BI the realistic path is a
  **read replica** with RLS, which matches the "reads from real-time database, read-only MCP,
  RLS enforced at source" direction.
- **Uniformity comes from the MCP layer, not the storage.** All five sources get an MCP server
  with the *same tool contract* (§5). The agent never knows or cares whether a source is SQL or REST.
- Behind the mock REST services, the two EDMs are also backed by Postgres (separate databases).
  The agent can reach them *only* via the REST API.

### Core data model (abridged)

**RefMaster** (`db: refmaster`, served at `/api/v1/...`)
- `securities(security_id, isin, cusip, sedol, ticker, name, asset_class, sub_class, ccy, issuer_entity_id, country, status, valid_from, valid_to)`
- `legal_entities(entity_id, lei, name, country, sector, parent_entity_id, status)`
- `accounts(account_id, product_id, name, type, owner_entity_id, region, lifecycle_state)`, `products(...)`
- `corporate_actions(ca_id, security_id, event_type, ex_date, pay_date, ratio, status)`
- `dq_rules(rule_id, domain, name, severity)`, `exceptions(exc_id, rule_id, domain, record_ref, status, assignee, opened_at, closed_at)`
- `change_requests(change_id, domain, record_ref, maker, checker, status)` — 4-eyes
- `data_dictionary(attribute, domain, definition, owner, source, lineage)`
- Endpoints: `GET /securities`, `/securities/{id}`, `/entities`, `/accounts`, `/corporate-actions`,
  `/exceptions`, `/exceptions/summary`, `/data-dictionary`, `/metrics/{metric_id}`

**MarketMaster** (`db: marketmaster`)
- `vendors(vendor_id, name, rank_default)` — names like "Vendor A…F" (no real vendor names)
- `vendor_prices(security_id, vendor_id, price_date, price_type, value, ccy, received_at)`
- `golden_prices(security_id, price_date, value, chosen_vendor_id, rule)`
- `price_suspects(suspect_id, security_id, vendor_id, price_date, kind[stale|spike|missing|conflict], deviation_pct, status)`
- `dq_stage_metrics(business_date, stage[acquired|validated|suspect|approved|distributed], domain, count, sla_met)`
- `esg_scores(entity_id, provider, score, as_of)`
- Endpoints: `/instruments/{id}/timeseries`, `/prices/conflicts`, `/prices/suspects`, `/golden-copy/{id}`,
  `/dq/metrics`, `/vendors`, `/metrics/{metric_id}`

**CashRecon** (`db: cashrecon`)
- `cash_accounts(account_id, legal_entity_id, bank_bic, nostro_no, ccy, region)`
- `statements(stmt_id, account_id, msg_type[MT940|MT950|CAMT053], stmt_no, value_date, opening_bal, closing_bal)`
- `statement_entries(...)`, `ledger_entries(...)`
- `match_rules(rule_id, name, cardinality[1:1|1:N|N:M], tol_amount, tol_days)`
- `match_groups(match_id, rule_id, status[auto|manual|proposed], matched_at, matched_by)`
- `match_items(...)`
- `breaks(break_id, account_id, break_type, amount, ccy, opened_on, age_days, status, owner, root_cause)`
- `break_actions(...)`

**AssetRecon** (`db: assetrecon`)
- `portfolios(portfolio_id, name, fund_group, base_ccy, custodian_id, region)`, `custodians(...)`
- `internal_positions(portfolio_id, security_id, book[IBOR|ABOR], qty, mv, as_of)`, `custodian_positions(...)`
- `internal_transactions(...)`, `custodian_transactions(...)`
- `recon_runs(run_id, recon_type[position|cash|txn|price|nav], business_date, matched, unmatched, status, signed_off_by)`
- `recon_exceptions(exc_id, run_id, portfolio_id, security_id, diff_qty, diff_mv, cause_code, assigned_to, status, sla_due)`
- `nav_checks(portfolio_id, nav_date, admin_nav, internal_nav, diff_bps)`

**FeedHub** (`db: feedhub`)
- `sources(source_id, name, type[custodian|bank|prime_broker|fund_admin], bic, country)`
- `feeds(feed_id, source_id, data_type[positions|transactions|cash|security], format[MT535|MT940|CSV|CAMT053], frequency, expected_by_utc)`
- `feed_deliveries(delivery_id, feed_id, business_date, received_at, status[on_time|late|missing|failed], latency_min, record_count, error_code)`
- `support_tickets(ticket_id, feed_id, category[credential_change|format_change|late], status, opened_at)`

**Shared keys across systems** make cross-source questions possible, and the context graph records them:
- `security_id` / `isin` join RefMaster ↔ MarketMaster ↔ AssetRecon.
- `legal_entity_id` joins RefMaster ↔ CashRecon.
- `custodian/source` joins AssetRecon ↔ FeedHub.
- `account` joins CashRecon ↔ FeedHub (bank statements arrive via FeedHub feeds).

### Data simulation approach
- One Python generator (`sim/`) with a **fixed seed**, building a single *shared universe* first
  and then projecting it into each system:
  - ~2,000 securities across Equity / Corp bond / Govt / FX / Derivatives.
  - ~400 legal entities with **valid LEI checksums (ISO 17442)** and **valid ISIN check digits**.
  - 6 price vendors, 40 custodians/banks, 30 portfolios in 4 fund groups, 60 cash accounts in 3 regions.
  - 90 business days of history.
- Realistic distributions: ~97% auto-match rate, break ageing that follows a long tail, feed latency
  with custodian-specific profiles, and vendor price noise proportional to asset-class volatility.
- **Planted stories** give the demo answers worth finding, and the evals check them:
  - *Vendor A* drives most corporate-bond price conflicts this week (+18% WoW). This is the reference slide's heat map.
  - One custodian's feeds turned late after a "credential change" ticket, which then caused
    AssetRecon position breaks for the portfolios it services. **Cross-source story.**
  - USD nostro breaks ageing > 5 days concentrated on one legal entity (EMEA).
  - NAV check drift > 5 bps on two funds, traced to stale prices flagged in MarketMaster.
- The generator emits Postgres DDL + COPY files. A single command resets everything
  (`make reseed` or `start_backend.sh --reseed`).

---

## 3. Approaches considered

**A. Graph semantic layer + governed metrics + uniform source MCPs (recommended)**
- A Neo4j context graph holds schema, API endpoints, glossary, metrics, join keys, golden
  questions and role entitlements.
- The agent retrieves only the relevant slice, then calls a small, uniform tool set.
- KPIs and common measures are *governed metrics*, compiled to SQL or API calls by code. Free
  text-to-SQL is a guarded fallback.
- Pros: directly realises your context-graph idea and the MCP-first north star; most token-efficient;
  RBAC can prune the context itself; explicit errors instead of plausible wrong answers.
- Cons: the most moving parts; the graph must be authored and loaded.

**B. Federated SQL engine (Trino/DuckDB over everything) + single text-to-SQL agent**
- Project all five sources, including the REST APIs, as tables, and let one agent write SQL.
- Pros: simplest agent, a single dialect.
- Cons: hides the REST/MCP story you want to prove; RLS becomes hard across the federation;
  text-to-SQL accuracy drops without a semantic layer.
- We **borrow one idea**: in-process DuckDB for *server-side* cross-source joins.

**C. Code-execution-with-MCP (agent writes Python in a sandbox that calls the MCP tools)**
- Very token-efficient for heavy data wrangling.
- Cons: needs a sandbox to run and secure, and the latency and security review are harder for a first MVP.
- A candidate for phase 2.

**Recommendation: A**, with DuckDB from B for combining results.

---

## 4. Architecture

```
┌─────────────────────────── Next.js UI (:3000) ────────────────────────────┐
│ Persona login · KPI strip (deterministic) · Canvas of generated widgets    │
│ Collapsible assistant (streams plan → widgets → summary) · Provenance pane │
└───────────────▲──────────────────────── SSE / REST ────────────────────────┘
                │ JWT (persona)
┌───────────────┴──────────── Agent API — FastAPI (:8000) ───────────────────┐
│ Auth → UserContext{sub, roles, entitlements}                               │
│ /kpis (no LLM) · /chat (SSE) · /dashboards · /results/{id}                 │
│                                                                            │
│  Supervisor agent (Claude Sonnet 5.5)                                      │
│   ├─ Source-query subagent ×N in parallel (Haiku 4.5 / Sonnet 5.5 low)     │
│   └─ Visualization subagent (Haiku 4.5, structured DashboardSpec output)   │
│  (the agents hold only the user's session token, forwarded as-is;          │
│   the model never sees it)                                                 │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │ MCP (streamable HTTP) — the ONLY MCP the agents see
┌──────────────────────────────────▼─────────────────────────────────────────┐
│ Semantic Gateway MCP (:8200) — the single front door to all data           │
│  (1) resolve: search / metric lookup in the context graph (cached)  ◄──────┼── Neo4j
│  (2) authorise: role grants from the graph + policy check + audit row      │   context graph
│  (3) compile: metric → SQL or endpoint call; validate free-form requests   │   (metadata only)
│  (4) route: mint audience-bound on-behalf-of token → call source MCP       │
│  (5) hold results: result store (rows stay here) · DuckDB combine          │
└──────┬───────────────┬──────────────┬──────────────┬──────────────┬────────┘
       │ MCP (streamable HTTP, audience-bound JWT per server)       │
┌──────▼─────┐ ┌───────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐ ┌──────▼─────┐
│RefMaster   │ │MarketMaster │ │CashRecon   │ │AssetRecon  │ │FeedHub     │
│MCP  :8201  │ │MCP   :8202  │ │MCP  :8203  │ │MCP  :8204  │ │MCP  :8205  │
└──────┬─────┘ └───────┬─────┘ └──────┬─────┘ └──────┬─────┘ └──────┬─────┘
  REST │          REST │          SQL │ (RLS)   SQL │ (RLS)   SQL │ (RLS)
┌──────▼─────┐ ┌───────▼─────┐        │              │              │
│RefMaster   │ │MarketMaster │        │              │              │
│API   :8101 │ │API   :8102  │        │              │              │
└──────┬─────┘ └───────┬─────┘        │              │              │
       └───────────────┴──── Postgres (Docker :5432): refmaster · marketmaster ·
                              cashrecon · assetrecon · feedhub · app (users, audit, results, cache)
```

**Key principle:** agents never call the source MCP servers directly. They speak *business
language* (metrics, terms, sub-questions) to the Semantic Gateway, which resolves, authorises,
compiles and routes. Neo4j is in the *metadata* path only. Data flows source MCP → gateway result
store and never through the graph. The same gateway can later be exposed as the customer-facing
MCP (other MCP hosts, client agents) unchanged.

### 4.1 Agent runtime choice
**Recommendation: the Anthropic Python SDK Messages API with our own small agent loop (or the SDK's
tool runner), hosted inside FastAPI.** Subagents are nested `messages.create` loops with their own
prompt, tools and model.

| Option | Verdict |
|---|---|
| Messages API loop (recommended) | Stateless, lowest latency, streams naturally to SSE. Per-request identity is a plain Python context object; the model never sees it. Full control of caching and parallel tool execution. |
| Claude Agent SDK | Good for autonomous, long-running agents. Per its hosting docs each session runs a CLI subprocess with a sizeable memory footprint and ships built-in tools we'd have to strip. That's heavy for second-scale BI turns. Keep it as a swap-in option: our loop sits behind a `Runner` interface. |
| Claude Managed Agents | Built for long-running sandboxed work. At the time of research it was beta and stateful, with data-retention terms to check against financial-data requirements. A fit for the later "co-worker & actions" use cases, not this one. |

Model access is **Anthropic API** for the MVP. The model client is configurable, so a switch to
Amazon Bedrock is a config change later.

Model tiers:
- **Supervisor:** `claude-sonnet-5-5`, escalating to `claude-opus-5-5` on multi-source or low-confidence plans.
- **Subagents:** `claude-haiku-4-5`.

### 4.2 Supervisor & subagents
- **Supervisor**
  - Calls `search_context` once.
  - Classifies the question as `metric` (fast path), `single-source` or `cross-source`.
  - Plans and fans out, then combines and writes the answer.
- **Fast path:** if the context pack contains a governed metric that answers the question, the
  supervisor calls `run_metric` directly. No subagents; typically 2 LLM turns.
- **Source-query subagent** (one per source involved, run in parallel)
  - Gets a sub-question plus the context slice for its single source.
  - Produces a metric call, endpoint call or guarded SELECT, and self-repairs on validation errors (max 2 retries).
  - Returns a *result handle* and a summary, never rows.
- **Visualization subagent**
  - Gets the result summaries (schema, row count, stats, top-N) and the user question.
  - Emits a `DashboardSpec` via structured output, plus a 2–3 sentence narrative.
  - Chart choice is its job, not tied to any grid or chart library.

### 4.3 Uniform tool contract

Every **source MCP server** exposes exactly these tools:

| Tool | Purpose |
|---|---|
| `describe()` | Returns the server's metadata: tables/endpoints, fields, metrics. Used by the graph loader, not the agent. |
| `run_metric(metric_id, dimensions[], filters{}, time_range)` | Governed measure. Compiled by code to SQL, or to API call + aggregation. |
| `query(request)` | SQL sources: a single `SELECT` (validated with sqlglot, allow-listed tables, row cap, timeout). REST sources: `{endpoint_id, params}` validated against the endpoint's parameter schema. |

These source tools are called **only by the Semantic Gateway**. Source MCP servers accept
tokens whose audience is the gateway-minted `aud=<source>-mcp`, so nothing else can reach them.

The **Semantic Gateway MCP** exposes the agent-facing tools:

| Gateway tool | Behaviour |
|---|---|
| `search_context(question)` | Role-pruned *context pack* from the graph |
| `run_metric(metric_id, dims, filters, time_range)` | Looks up the metric in the graph (it knows its source), checks entitlement, compiles and routes to the source's `run_metric`. Returns a result handle and summary. **No `source` argument: the graph decides.** |
| `query_source(source, request)` | Free-form SELECT / endpoint call. Validated against the graph (tables, endpoints and columns the role may read), then routed to the source's `query`. Disabled for metrics-only roles. |
| `combine(result_ids, duckdb_sql)` | Server-side DuckDB over result handles. No database access, only in-memory results. |

The **supervisor** sees six tools in total: the four gateway tools plus two agent-side tools. Because
the list is fixed, the prompt prefix stays byte-stable and cacheable, and no tool search is needed.

| Agent-side tool | Behaviour |
|---|---|
| `delegate(source, sub_question)` | Spawns a source-query subagent. It sees only `search_context` (scoped to that source) and `query_source`/`run_metric`, all via the gateway. |
| `visualize(result_ids, intent)` | Calls the visualization subagent → `DashboardSpec` |

The Agent API passes the user's session JWT to the gateway on the MCP connection (an HTTP header,
set per request, never in model-visible arguments). The gateway derives the `UserContext` from it.

### 4.4 Semantic layer: the context graph

This is the core of "how does the semantic layer integrate with the MCP servers":
**the graph is the map, the source MCP servers are the territory, and the Semantic Gateway is the
guide that reads the map and makes the trip on the user's behalf.**
- Every executable node in the graph carries its **address** (`source`, `mcp_server`, `mcp_tool`,
  `endpoint_id`/`table`). A retrieval result is therefore directly actionable.
- Each MCP server's `describe()` output is **synced into the graph at startup**, so the graph never
  drifts from what is actually callable.

**Graph model**, extending neocarta's conventions:

```
(:Source {name, kind: 'rest'|'sql', mcp_server})
  -[:HAS_TABLE]->(:Table)-[:HAS_COLUMN]->(:Column {type, sample_values, pii})
  -[:HAS_ENDPOINT]->(:Endpoint {method, path, endpoint_id})-[:HAS_PARAM]->(:Parameter)
                                                             -[:RETURNS]->(:Field {json_path, type})
(:Column|:Field)-[:REFERENCES]->(:Column|:Field)            // FK within a source
(:Column|:Field)-[:SAME_KEY_AS]->(:Column|:Field)           // cross-source join keys
(:Column|:Field)-[:TAGGED_WITH]->(:BusinessTerm)-[:IN_GLOSSARY]->(:Glossary)
(:BusinessTerm)-[:RELATED_TO {predicate}]->(:BusinessTerm)  // entity-map style concepts
(:Metric {id, definition, grain, unit})-[:COMPUTED_FROM]->(:Column|:Field)
(:Metric)-[:HAS_DIMENSION]->(:Dimension)
(:GoldenQuestion {text, embedding})-[:ANSWERED_BY]->(:Metric|:QueryTemplate)
(:Role)-[:CAN_READ {row_scope}]->(:Source|:Table|:Endpoint|:Metric|:Column)
```

#### The three knowledge layers: ontology, business glossary, query history

The graph holds more than schema. Three layers of knowledge make the agents "understand" the business.

**1. Domain ontology: what things *are* and how they relate.** The ontology is the conceptual model,
independent of any one system. It gives the planner a map of the business in which every
node is tied to where it physically lives.
```
(:Concept {name:'Security'})-[:ISSUED_BY]->(:Concept {name:'LegalEntity'})
(:Concept {name:'Price'})-[:PRICE_OF]->(:Concept {name:'Security'})
(:Concept {name:'Price'})-[:QUOTED_BY]->(:Concept {name:'Vendor'})
(:Concept {name:'Position'})-[:HOLDS]->(:Concept {name:'Security'}); (:Position)-[:HELD_IN]->(:Portfolio)
(:Concept {name:'Break'})-[:RAISED_ON]->(:Concept {name:'CashAccount'})
(:Concept {name:'FeedDelivery'})-[:FEEDS]->(:Concept {name:'Position'})      // FeedHub → AssetRecon
(:Concept)-[:IMPLEMENTED_BY]->(:Table|:Endpoint)   // e.g. Security → refmaster.securities, MarketMaster /instruments
(:Concept)-[:IDENTIFIED_BY]->(:Column|:Field)      // e.g. Security → isin, security_id (the cross-source keys)
(:Concept)-[:SUBCLASS_OF]->(:Concept)              // e.g. CorporateBond ⊂ Bond ⊂ Security
```
- **Authored in** `semantic/model/ontology.yaml`, covering about 25 concepts and 40 relations.
- **Seeded from** the generic entity map: its concepts and typed relations, with evidence chunks kept as provenance.
- **How agents use it:** a question about "vendors behind price conflicts on bonds" resolves to Vendor → Price → Security(Bond). Following `IMPLEMENTED_BY` gives the exact tables and endpoints, and following `IDENTIFIED_BY` gives the join keys.
- **Performance:** the ontology is what makes cross-source join paths cheap and correct. The `SHORTEST` path between concepts is found at the concept level first, then expanded to physical columns.

**2. Business glossary: what words *mean* here.** The business's own vocabulary, with synonyms and owners.
```
(:BusinessTerm {name, definition, synonyms[], owner, status:'approved'|'draft'})
  -[:IN_GLOSSARY]->(:Glossary {domain})
(:BusinessTerm)-[:DEFINES]->(:Concept|:Metric)      // "Golden copy" DEFINES Concept GoldenPrice
(:BusinessTerm)-[:TAGS]->(:Column|:Field)           // "Nostro" TAGS cashrecon.cash_accounts
(:BusinessTerm)-[:BROADER]->(:BusinessTerm)         // "Aged break" BROADER "Break"
(:BusinessTerm)-[:HAS_RULE]->(:Rule {expression})   // "Aged break" = status<>'closed' AND age_days>5
```
- **Authored in** `semantic/model/glossary.yaml`, with about 60 terms.
- **Grounding the questions:** the glossary is how "aged breaks", "late feeds", "golden copy completeness" or "price-source conflicts" map to exact filters and metrics.
- **Synonyms:** terms such as "nostro" / "cash account" / "bank account", or "suspect" / "exception", are indexed for full-text and vector search.
- **Governed:** terms carry an owner and a status. Only approved terms are used for automatic filter expansion; draft terms are offered as hints only.

**3. Query history: what has *worked* before.** Past questions and the executions that answered them. neocarta mines warehouse query logs for this; here it grows from our own traffic.
```
(:Question {text, embedding, asked_at, persona_role})
  -[:ANSWERED_BY]->(:Execution {kind:'metric'|'sql'|'endpoint', text, source, latency_ms, rows, status, feedback})
(:Execution)-[:USED]->(:Metric|:Table|:Column|:Endpoint|:Field)
(:Execution)-[:JOINED_ON]->(:Column|:Field)          // mined join paths, weighted by usage
(:Question)-[:SIMILAR_TO {score}]->(:Question)
```
- **Sources:**
  - Every execution the Semantic Gateway performs is logged in Postgres `app.query_log` (question, persona role, compiled query, source, outcome, latency, tokens).
  - An hourly job, plus one at gateway start, distils the log into the graph.
  - **Seed history:** about 150 historical question→execution pairs covering the planted stories and common KPIs, validated by the eval harness, so the system is "experienced" on day one.
  - **User feedback:** thumbs up or down in the UI marks an execution as `validated` or `rejected`.
- **How agents use it:**
  - `search_context` returns up to three similar validated past executions as few-shot examples, or as a **direct replay** when similarity is above 0.92 and the entitlements match. A replay skips planning entirely.
  - Usage weights on `JOINED_ON` edges rank join paths.
  - Frequently used columns and endpoints rank higher in retrieval.
- **RBAC:**
  - History is filtered like everything else. A past execution is returned only if the current role can read every object it `USED`.
  - Question text from other users is shown only when the question contains no literal values; literals are stripped before storage.
  - **Replays re-execute under the current user's entitlements. Results are never reused across users.**

**Where each layer sits in retrieval** (`search_context`):
1. Glossary match (term + synonyms) → Concepts and Metrics.
2. Ontology expansion → implementing Tables/Endpoints and identifying keys; concept-level join path.
3. History → similar validated executions for few-shot or replay; usage-weighted joins.
4. Role pruning at every step.
5. Compact pack of at most about 3k tokens.

**Populated by a loader** (`semantic/loader.py`) from these inputs:
1. **Introspection**
   - Postgres: `information_schema` plus sample values.
   - REST: the mock services' OpenAPI specs.
   - Both flow in via each MCP server's `describe()`.
2. **Authored YAML** (`semantic/model/*.yaml`, the source of truth, reviewed like code):
   `ontology.yaml`, `glossary.yaml`, metrics, dimensions, cross-source keys and role grants.
   - The format is **OSI-compatible** where possible, so neocarta's OSI connector or other tools can
     consume it later.
3. **A generic entity map** (`semantic/model/entitymap.json`) in the public `entitymap.org v1.0` shape
   (entities, typed relations, evidence chunks), written by us with no vendor content. It seeds
   business concepts like *Golden copy*, *Break*, *Price suspect* and *Four-eyes*.
4. **Query history**:
   - `semantic/model/seed_history.yaml` holds about 150 validated question→execution pairs.
   - The live `app.query_log` is distilled into the graph hourly and at gateway start.

**Retrieval** (`search_context`) follows the neocarta blog pipeline:
1. Hybrid search (vector + full-text) over BusinessTerm (incl. synonyms) / Concept / Metric / Question / Column / Field.
   Embeddings come from a local `fastembed` model, so no extra API key is needed.
2. **Role filter applied inside the Cypher.**
3. Shortest join path between the selected objects, including `SAME_KEY_AS` edges across sources.
4. The result is a compact JSON pack (target ≤ 3k tokens) containing: candidate metrics with dims,
   tables/endpoints with only the relevant columns, join keys, 1–3 similar golden questions with
   their validated plans, and glossary definitions.

**Why not use neocarta as-is**
- neocarta has no Postgres connector and no API-endpoint model.
- It lists access control as future work.

So we adopt its **schema conventions and retrieval pattern**, implement our own loader and the
retrieval/routing inside the Semantic Gateway, and keep the YAML OSI-compatible so the library can
be adopted later.

**Graph lookups on the hot path** (metric → source/address, role grants) are loaded into an
in-memory cache at gateway startup and refreshed on graph reload. Only `search_context` hits Neo4j per request.

---

## 5. Security: RBAC + RLS, defence in depth

Enforcement happens in **three layers**, and only the last one is the security boundary:

| Layer | Mechanism | Purpose |
|---|---|---|
| 1. Context pruning | `(:Role)-[:CAN_READ]->` filter in every retrieval Cypher | The model never *learns about* forbidden datasets or columns. This saves tokens and avoids hints. |
| 2. Semantic Gateway policy | Checks `(source, table/endpoint/metric/column)` against the role grants in the graph before routing; writes an audit row per call | Early explicit "not entitled" errors; single choke point |
| 3. **Enforcement at source** | Postgres **RLS** + column grants/masking views. The mock REST APIs enforce JWT scopes *and* reuse the same Postgres RLS. | The real boundary |

**Identity flow (on-behalf-of)**
- The user signs in; the MVP uses a persona picker that issues a locally signed JWT, and OIDC comes later.
- The Agent API forwards the session JWT to the Semantic Gateway (`aud=semantic-gateway`), which
  builds the `UserContext`.
- For each source call, the gateway **mints a short-lived, audience-bound token** (`aud=cashrecon-mcp`)
  carrying `sub`, `roles` and `entitlements`. This is an RFC 8693-style token exchange, simplified for the MVP.
- MCP servers verify the token. The REST-wrapping servers call the EDM API with a further
  audience-bound token and never pass through the token they received.
- **The LLM never sees tokens.** No tool accepts `user_id` or roles as arguments; identity always
  comes from server-side request context.

**Postgres RLS pattern**, one transaction per tool call:
```sql
BEGIN READ ONLY;
SET LOCAL ROLE bi_reader;                         -- non-owner, NOBYPASSRLS
SELECT set_config('app.ctx', '<signed-claims>', true);
-- policy: USING (region = ANY (app_entitled('region')))
--   where app_entitled() is SECURITY DEFINER and verifies the HMAC on app.ctx
<validated SELECT>;
COMMIT;
```
- `FORCE ROW LEVEL SECURITY` is set on every table, together with `statement_timeout` and a row cap.
- The claims are **signed** and verified inside the database. Even if LLM-written SQL tried to call
  `set_config`, it could not forge entitlements. The sqlglot validator also rejects `set_config`,
  `SET`, `COPY`, DDL, `pg_*` admin functions and multi-statement input.
- Sensitive columns (account numbers, IBAN-like `nostro_no`) are exposed through masking views unless the role holds `pii:read`.

**Demo personas**

| Persona | Datasets | Row scope | Notes |
|---|---|---|---|
| Reference Data Steward | RefMaster (all), MarketMaster (prices, suspects) | all asset classes | Matches the reference slide ("Security master ✓ Price master ✓") |
| Cash Ops Analyst – EMEA | CashRecon, FeedHub (bank feeds) | region = EMEA | Account numbers masked |
| Investment Ops – Growth Funds | AssetRecon, FeedHub (custodian feeds), RefMaster securities | fund_group = Growth | |
| BI Analyst | All sources, **metrics only** | all | metrics-only: no free-form query; sensitive dimensions blocked at the source; grain / minimum-group-size policy enforced at the gateway (Plan 3) |
| Head of Data Operations | All | all | `pii:read` |

**Prompt-injection hygiene**
- All tool output (narratives, DQ comments, ticket text) is treated as untrusted data and delimited as such.
- The toolset is read-only, with no generic fetch and no write tools.
- The system prompt states that entitlements cannot be changed by the conversation.
- RBAC tests include injection attempts (§8).

---

## 6. Performance & token efficiency

1. **Deterministic first.** The KPI strip, metric maths, joins and chart data never touch the LLM.
   KPIs come from `/kpis`, cached per role with a 60 s TTL.
2. **Just-in-time context.** The graph retrieval pack is about 1–3k tokens, instead of a schema dump of 20k+.
3. **Governed metrics before text-to-SQL.** This is more accurate, fails explicitly instead of plausibly,
   and uses fewer output tokens.
4. **Rows never go to the model.** Results live in the server-side result store; the model gets a
   handle, the schema, the row count, stats and the top 5 rows. The UI fetches full data by handle.
5. **Fixed six-tool surface + frozen system prompt**, both behind `cache_control`. Per-user
   role and date context goes *after* the cache breakpoint. `cache_read_input_tokens` is logged per turn.
6. **Parallel fan-out.** Independent source calls run concurrently with `asyncio.gather`.
7. **Model tiering + effort tuning.** Haiku for spec writing and simple source queries; Sonnet for
   the supervisor; Opus only on escalation.
8. **Semantic answer cache.** Validated `question → plan` pairs are stored as `GoldenQuestion` nodes;
   a near-duplicate question replays the plan and skips planning tokens.
9. **Structured outputs** for `DashboardSpec`, so there are no JSON-repair retries.
10. **Streaming.** Plan, then each widget, then the summary are streamed over SSE, so perceived latency beats total latency.
11. **Telemetry.** Per question we record tokens in/out, cache hits, LLM turns, tool latency and cost,
    shown in a dev panel and stored in `app.agent_runs`.

---

## 7. Frontend (Next.js)

- **Stack:** Next.js (App Router, TypeScript), Tailwind + shadcn/ui, **Apache ECharts**
  (`echarts-for-react`; it has native heat maps, which Recharts lacks), Vercel AI SDK `useChat`
  consuming FastAPI SSE with custom `data-*` parts (`data-plan`, `data-widget`, `data-summary`).
- **Screens**
  - **Login:** persona picker (MVP).
  - **Workspace**
    - Header chips showing the signed-in persona and access (e.g. "Security master ✓ Price master ✓ · RBAC + RLS").
    - A static KPI strip of four role-dependent tiles (e.g. open exceptions, price-source conflicts,
      feeds late today, golden-copy completeness).
    - A canvas grid of generated widgets, each with pin, remove and expand.
    - A collapsible right-hand assistant.
    - Each answer carries **"view query · source rows"**, which opens a provenance drawer with the
      executed metric/SQL/endpoint call, source system, row count and a paginated row table.
  - **Saved dashboards:** pinned widgets are persisted per user and re-executed with the *current*
    user's entitlements.
- **Renderer:** a deterministic `DashboardSpec → ECharts option` mapper.
  - Widget types: `kpi`, `bar`, `stacked_bar`, `line`, `heatmap`, `table`, `pie`, `scatter`.
  - The spec is Zod-validated on the client, mirroring the Pydantic model on the server.
- **Visual design:** a restrained enterprise look (navy/crimson accent in the spirit of the
  reference) with no client branding.

---

## 8. Testing & evaluation

- **Unit:** sim invariants (ISIN/LEI checksums, balances reconcile, planted stories present), SQL
  validator (allow/deny corpus), metric compiler, token minting/verification, RLS policies
  (pytest against Dockerised Postgres).
- **RBAC matrix tests:** every persona × every source/table/metric gives the expected
  allow/deny/row-count. These are deterministic, with no LLM involved.
- **Golden-question evals:** 30–50 questions with expected answers computed from the seed.
  - Scored on answer correctness (numeric tolerance), correct source/metric chosen, chart-type
    sanity, tokens and latency.
  - Run as `make eval`, with results in `evals/reports/`.
- **Red-team set:** prompt injection embedded in ticket/comment text, "ignore your role" prompts,
  and SQL smuggling (`set_config`, `;`, comments). Every case must yield no leakage.
- **E2E:** a Playwright smoke test (login → KPI strip → ask a question → widget renders).

---

## 9. Repository layout & runtime

```
/docker-compose.yml          postgres:16, neo4j:5 (APOC + vector index)
/scripts/start_backend.sh    docker up → wait healthy → migrate/seed (if empty or --reseed)
                             → load graph → start all Python services (honcho Procfile) → tail logs
/scripts/start_frontend.sh   check Node ≥ 20 → npm install (if needed) → next dev on :3000
/backend/  (Python 3.12, uv)
  sim/                        universe generator + per-system projections + planted stories
  sources/refmaster_api/      mock REST (FastAPI) :8101
  sources/marketmaster_api/   mock REST (FastAPI) :8102
  mcp/ common/                shared tool contract, token verify, SQL guard, metric compiler
  mcp/ refmaster|marketmaster|cashrecon|assetrecon|feedhub/   source FastMCP servers :8201-8205
  gateway/                    Semantic Gateway MCP :8200 (resolve · authorise · compile · route ·
                              result store · DuckDB combine · audit)
  semantic/ model/*.yaml, entitymap.json, loader.py, retrieval.cypher
  agent/ supervisor.py, subagents/, spec.py (DashboardSpec)
  api/   FastAPI app :8000 (auth, /kpis, /chat SSE, /dashboards, /results)
  evals/ golden_questions.yaml, redteam.yaml, runner.py
  tests/
/frontend/  Next.js app
/.env.example  ANTHROPIC_API_KEY, JWT dev keys, DB/Neo4j creds
```

**Configuration:** only `ANTHROPIC_API_KEY` must be supplied. Everything else has local defaults.
Dev signing keys are generated on first run.

---

## 10. Delivery milestones

| # | Milestone | Outcome |
|---|---|---|
| M0 | Foundations | Repo, docker-compose, scripts skeleton, `.env`, CI-less `make test` |
| M1 | Simulated platforms | Generator + 5 databases seeded, 2 mock REST APIs, RLS + masking, RBAC matrix tests green |
| M2 | Source MCP servers | 5 MCP servers with the uniform contract, SQL guard, metric compiler, ~20 governed metrics |
| M3 | Context graph + Semantic Gateway | Ontology, business glossary, seeded query history, YAML metrics + entity map + loader; `app.query_log` capture and distillation. Semantic Gateway MCP with role-filtered hybrid retrieval, metric routing, token minting, audit, result store, DuckDB combine |
| M4 | Agent service | Supervisor + subagents (via the gateway only), DashboardSpec, `/chat` SSE, telemetry |
| M5 | UI | Login, KPI strip, canvas, assistant, provenance drawer, saved dashboards |
| M6 | Hardening | Golden + red-team evals, caching/latency tuning, demo script around the planted stories |

---

## 11. Out of scope (MVP)
- Point-in-time/as-reported history.
- A warehouse.
- Regulatory reporting.
- Write-back or workflow actions (assign a break, approve a price).
- Real SSO/OIDC.
- Cloud deployment (AWS).
- Customer-facing MCP exposure.
- Code-execution sandbox (Approach C).

## 12. Decisions (resolved 2026-09-30)
1. **Agent runtime:** Anthropic Messages API with our own loop, behind a `Runner` interface so the
   Agent SDK can be swapped in later.
2. **Model access:** Anthropic API key. The model client is configurable for Bedrock later.
3. **Exposure split:** EDMs as REST + MCP; CashRecon, AssetRecon and FeedHub as Postgres + MCP.
4. **Guarded free-form queries:** enabled for steward/ops/head personas, disabled for the BI Analyst (metrics only).
5. **Names:** *Prism*, RefMaster, MarketMaster, CashRecon, AssetRecon, FeedHub.
6. **Data access path:** agents talk only to the Semantic Gateway MCP, never to source MCP servers directly.
