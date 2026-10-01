# Plan 3: Context Graph and Semantic Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Neo4j context graph (ontology, business glossary, query history, schema, roles) with local embeddings, and a single Semantic Gateway MCP server (:8200) that is the only door agents use to reach the five source MCP servers.

**Architecture:** A graph loader builds the graph idempotently from the repo's own registries (metric YAML, DDL, REST YAML) plus hand-written knowledge YAML. Retrieval is role-filtered hybrid search (vector + BM25, RRF fused) inside Cypher, returning a compact context pack. The gateway keeps an in-memory catalog (metric to source/tool/dimensions) refreshed from the graph, enforces role and metrics-only policy, forwards calls to the source MCP servers with re-minted tokens, stores results by handle, combines handles with DuckDB, and audits every call to Postgres `app`.

**Tech Stack:** Python 3.13, Neo4j 5.26 Community (Docker), `neo4j` driver 6.3, `fastembed` 0.8 (`BAAI/bge-small-en-v1.5`, 384-d, local, offline), DuckDB, MCP SDK v2 (`mcp>=2.2,<3`), psycopg3, pytest.

**Spec:** `docs/superpowers/specs/2026-09-30-agentic-data-intelligence-design.md` (§4 semantic layer, §4.4 ontology / glossary / query history, §5 security, §6 token efficiency). Carry-forward: `docs/superpowers/plans/plan-1-carry-forward.md`, `plan-2-carry-forward.md`. Proven spikes (read them, port them): `docs/superpowers/spikes/plan-3/neo4j/` (schema.py, loader.py, retrieval.py, catalog.py, model.yaml, test_spike.py, gotchas_output.txt, bench_output.txt) and `docs/superpowers/spikes/plan-3/embed/` (embedder.py, retrieval.py, glossary.yaml, eval_questions.yaml, eval_harness.py, test_embedder.py).

## Global Constraints

- Generic names only (Prism, RefMaster, MarketMaster, CashRecon, AssetRecon, FeedHub). Never the original vendor/client names anywhere (code, YAML, docs, commits).
- Agents reach data ONLY through the gateway (:8200). The gateway is the only caller of the source MCP servers (:8201-8205).
- Every gateway tool call carries the caller's JWT (`aud == "gateway-mcp"`); source calls use `mint_source_token(claims, source, settings, ttl_s=60)`; never pass the incoming token through.
- Neo4j and every new service bind to `127.0.0.1` only. Neo4j host ports 7475 (http) and 7688 (bolt) (7474/7687 are taken on the dev machine; 17475/17688 were spike-only).
- Embeddings are local only: `BAAI/bge-small-en-v1.5`, 384-d, query prefix `Represent this sentence for searching relevant passages: `, vectors cast to float32 and rounded to 6 decimals. The app never touches the network for models at runtime (`HF_HUB_OFFLINE=1`, explicit `FASTEMBED_CACHE_PATH`).
- Python 3.13 venv; run with `cd backend && uv run ...`; tests `uv run pytest -q -W error`; no `tests/__init__.py`; no sys.path hacks. The suite must stay green (1026 tests today).
- Neo4j tests: one container per session (compose service), real graph loaded once into ns `prism`; mutating tests use their own namespace (`uid` prefix plus `ns` property) and delete it in teardown. Tests marked `neo4j` (new marker) and skipped with a clear message if Neo4j is unreachable, exactly like the `db` marker.
- Every Neo4j call is wrapped in `asyncio.wait_for` (default 5 s) because a hung server blocks the pooled driver forever.
- Fail closed: a caller with no matching scope gets an empty pack; unknown metric ids are errors, never guesses.
- Dev-default secrets (carry-forward #6) are refused outside tests for `jwt_secret` / `ctx_hmac_key` in the gateway process.

## Review Focus

- A question about a source the persona cannot read (e.g. `cash_ops_emea` asking about security prices): pack must be empty of that source, and the gateway must say "not available to your role" without leaking object names.
- `bi_analyst` (metrics-only): no `query_source`, no tables/columns in the pack, sensitive dimensions (`matched_by`) invisible and rejected, grain/minimum-group-size enforced at the gateway.
- Question text with Lucene syntax (`AND OR ( ) : " ^ ~ * ?`), empty string, 50k characters, emoji, SQL-injection text: never crashes or injects.
- Neo4j down or hung, embedding model missing, Postgres `app` down: a clear user-facing error and an audit line; the gateway must not hang or fall open.
- Two sources reply with different currencies / grains and the agent asks to `combine` them: no silent cross-currency sums; truncated results flagged partial.
- Parallel sub-agent fan-out (6 concurrent `run_metric` calls for the same user): no "too many concurrent requests" leaking out (gateway semaphore + retry).
- Reload of the graph while the gateway runs: catalog swaps atomically; a metric removed from the graph disappears from the gateway without restart.

## File Structure

```
docker-compose.yml                      + neo4j service
backend/pyproject.toml                  + neo4j, fastembed, duckdb; marker neo4j
backend/prism/config.py                 + neo4j_*, gateway_*, embed_* settings
backend/prism/graph/__init__.py
backend/prism/graph/embedder.py         Embedder (ported from spike), warmup, LRU cache
backend/prism/graph/schema.py           constraints + indexes (idempotent)
backend/prism/graph/knowledge/*.yaml    ontology.yaml, glossary.yaml, history.yaml (hand-written)
backend/prism/graph/model.py            build_graph(): registries + knowledge -> node/rel batches
backend/prism/graph/loader.py           load(), counts(), delete_ns(), versioned stale cleanup
backend/prism/graph/retrieval.py        search_context / expand / join_paths / context_pack
backend/prism/graph/catalog.py          Catalog dataclass + load_catalog (one query) + scope helper
backend/prism/graph/cli.py              python -m prism.graph.cli load|search|warmup|check
backend/prism/gateway/__init__.py
backend/prism/gateway/policy.py         role/metrics-only/grain checks on the catalog
backend/prism/gateway/results.py        ResultStore (handles, TTL, caps)
backend/prism/gateway/combine.py        DuckDB combine over handles
backend/prism/gateway/downstream.py     Downstream: per-(sub,source) semaphore, retry, error mapping
backend/prism/gateway/audit.py          Postgres app.audit writer, query history capture
backend/prism/gateway/server.py         MCP tools + create_app/factory (:8200)
backend/prism/gateway/cli.py            smoke tool: python -m prism.gateway.cli list|call
backend/prism/db/app_migrate.py         idempotent app DB migrations (audit, query_log)
backend/tests/graph/*, backend/tests/gateway/*
backend/Procfile, scripts/start_backend.sh, Makefile, README.md
```

---

### Task 1: Dependencies, Neo4j service, settings, test marker

**Files:**
- Modify: `backend/pyproject.toml`, `docker-compose.yml`, `backend/prism/config.py`, `Makefile`, `.env.example`, `backend/tests/conftest.py` (or the existing fixtures file; follow how the `db` marker is skipped)
- Create: `backend/tests/graph/test_neo4j_service.py`

**Interfaces:**
- Produces: `Settings.neo4j_uri` (`bolt://127.0.0.1:7688`), `neo4j_user`, `neo4j_password`, `neo4j_timeout_s` (5.0), `gateway_port` (8200), `gateway_url`, `embed_model` (`BAAI/bge-small-en-v1.5`), `embed_cache_dir`, `graph_ns` (`prism`); pytest marker `neo4j`; fixture `neo4j_driver` (session scope, sync `neo4j.GraphDatabase.driver(..., notifications_min_severity="OFF")`).

- [ ] **Step 1:** Add dependencies: `uv add "neo4j>=6.3,<7" "fastembed>=0.8,<0.9" "duckdb>=1.3"`. Run `uv lock` and confirm only additions in `git diff uv.lock` for existing pins (mcp, sqlglot, pydantic unchanged; spike proved this for fastembed).
- [ ] **Step 2:** Add compose service (keep the existing postgres service untouched):

```yaml
  neo4j:
    image: neo4j:5.26-community
    ports: ["127.0.0.1:${PRISM_NEO4J_HTTP_PORT:-7475}:7474", "127.0.0.1:${PRISM_NEO4J_BOLT_PORT:-7688}:7687"]
    environment:
      NEO4J_AUTH: neo4j/${PRISM_NEO4J_PASSWORD:-prism-dev-neo4j}
      NEO4J_server_memory_heap_initial__size: 512m
      NEO4J_server_memory_heap_max__size: 1g
    volumes: ["neo4j_data:/data"]
    healthcheck:
      test: ["CMD", "cypher-shell", "-u", "neo4j", "-p", "${PRISM_NEO4J_PASSWORD:-prism-dev-neo4j}", "RETURN 1"]
      interval: 3s
      timeout: 5s
      retries: 30
```
  plus `volumes: neo4j_data:`. Add `PRISM_NEO4J_*` to `.env.example` (dev password only; loopback). Spike finding: bolt is ready ~7.5 s after a restart; poll `RETURN 1`.
- [ ] **Step 3:** Settings fields per Interfaces; register marker `neo4j: requires Neo4j (make db)`; `Makefile`: `db` also starts neo4j, add `test-graph` (`pytest -m neo4j`). Fixture skips with message "Neo4j not reachable: run `make db`" when `ServiceUnavailable`/`AuthError`.
- [ ] **Step 4:** Test `test_neo4j_service.py` (marker neo4j): driver connects, `RETURN 1`, `SHOW PROCEDURES` contains `db.index.vector.queryNodes` and `db.index.fulltext.queryNodes`, server version starts with `5.26`.
- [ ] **Step 5:** `make db`, run the test, run the full suite, commit `feat: neo4j service, settings and dependencies`.

### Task 2: Local embedder

**Files:**
- Create: `backend/prism/graph/__init__.py`, `backend/prism/graph/embedder.py`, `backend/tests/graph/test_embedder.py`
- Source: port `docs/superpowers/spikes/plan-3/embed/embedder.py` and `test_embedder.py`.

**Interfaces:**
- Produces: `class EmbedderError(Exception)`; `class Embedder(model=..., cache_dir=..., threads=None, cache_size=2048)` with `load()`, `warmup()` (embeds one string, validates dim == 384, finite, unit length, raises `EmbedderError`), `embed_query(text) -> list[float]`, `embed_documents(texts) -> list[list[float]]`, `async aembed_query(text)` (`asyncio.to_thread` behind `asyncio.Semaphore(4)`). CLI `python -m prism.graph.embedder download` (build-time model fetch into `cache_dir`).

- [ ] **Step 1:** Port the spike class unchanged in behaviour. Non-negotiable, all proven in the spike: refuse to load unless `cache_dir` or `FASTEMBED_CACHE_PATH` is set (default cache is the OS temp dir, which macOS purges); add the bge query prefix in `embed_query` (fastembed does not); cast to float32, round to 6 decimals, return Python floats; normalise text for the LRU key (strip, collapse whitespace, lower); never raise on odd input (empty/None -> treat as empty string, truncate at 2,000 characters); all load failures become `EmbedderError` in well under a second.
- [ ] **Step 2:** Tests (no network after first download; mark `embed` is not needed, the model is cached by `make models`): dim 384, unit norm, determinism across two calls and across a subprocess, cache hit returns the identical list, junk inputs (empty, None, 50,000 chars, emoji, `AND OR ( ) : "`) return 384 floats, missing cache dir raises `EmbedderError`, truncated `.onnx` in a temp cache raises `EmbedderError`, `asyncio.gather` of 8 calls equals sequential results. Add `make models` (runs the download CLI into `backend/.models`, git-ignored; add `.models/` to `.gitignore`) and `Settings.embed_cache_dir` default `backend/.models`.
- [ ] **Step 3:** Commit `feat: local embedder (bge-small, offline, cached)`.

### Task 3: Knowledge YAML (ontology, glossary, query history)

**Files:**
- Create: `backend/prism/graph/knowledge/ontology.yaml`, `glossary.yaml`, `history.yaml`, `backend/prism/graph/knowledge/__init__.py` (loader + pydantic models `Concept`, `Term`, `SeedQuestion`), `backend/tests/graph/test_knowledge.py`
- Source: `docs/superpowers/spikes/plan-3/neo4j/model.yaml` (concepts, relations, terms, history) and `docs/superpowers/spikes/plan-3/embed/glossary.yaml` (40 terms) and `eval_questions.yaml`.

**Interfaces:**
- Produces: `load_knowledge() -> Knowledge` with `concepts`, `concept_relations`, `terms` (name, glossary, definition, synonyms, rule, broader, defines: list of `metric:<id>` / `concept:<Name>`, tags: list of `column:<src>.<table>.<col>`), `history` (question text, tool plan, used metric ids, status). Every `defines` and `history` reference must resolve.

- [ ] **Step 1:** Merge the two spike glossaries into one `glossary.yaml` (~40 terms, all five platforms; include "WoW" / "this week = trailing 5 business days ending at the as-of date", "aged break = status <> 'closed' AND age_days > 5", "golden copy", "price conflict", "stale price", "late feed", "NAV break", "four-eyes approval", "auto-match rate", "LEI", "ISIN", recon_type `transaction`). Put the term-to-metric edges (`defines`) on every term that has a governed metric: the embedding spike showed these edges fix the top-3 misses.
- [ ] **Step 2:** `ontology.yaml`: 10 concepts with `implemented_by` / `identified_by` refs and concept relations (spike model.yaml), plus `same_key` join-key declarations: `[column:refmaster.securities.security_id, column:marketmaster.instruments.security_id, column:assetrecon.internal_positions.security_id]`, the legal-entity keys between RefMaster and CashRecon, and source ids between FeedHub and AssetRecon custodians / CashRecon bank accounts (verify each against `backend/prism/db/ddl/*.sql` column names; the test in step 4 enforces it).
- [ ] **Step 3:** `history.yaml`: 12 seed question -> plan pairs (the four demo stories first: Vendor A x Corp bond conflicts WoW, late SRC001 feeds, PF001/002/005 position exceptions, LE00016 aged USD breaks). Each has `question`, `metrics: [ids]`, `plan` (one-line text of which metric/dimensions), `status: verified`.
- [ ] **Step 4:** Tests: YAML parses into the pydantic models; every `metric:` ref exists in `load_metrics()` + REST registry; every `column:` / `table:` ref exists in the parsed DDL (`docs/.../loader.py::parse_ddl` port goes in Task 5; for this task use a minimal check against metric `tables`, and add the full DDL check in Task 5); term names unique; synonyms unique across terms (a duplicate synonym is ambiguous); no banned vendor/client names (grep test over the knowledge dir).
- [ ] **Step 5:** Commit `feat: ontology, glossary and seed query history`.

### Task 4: Graph schema and idempotent loader

**Files:**
- Create: `backend/prism/graph/schema.py`, `backend/prism/graph/model.py`, `backend/prism/graph/loader.py`, `backend/tests/graph/test_loader.py`
- Source: spike `schema.py`, `loader.py` (`parse_ddl`, `build_graph`, `_write`, `load`, `counts`, `delete_ns`).

**Interfaces:**
- Consumes: `load_knowledge()` (Task 3), `Embedder` (Task 2), metric registries (`prism.mcp.metrics.load_metrics`, `prism.mcp.rest_backend` endpoint registry or its YAML via `prism.mcp.rest/*.yaml` loaders; if importing the backend pulls the `mcp` package that is fine here, the spike only avoided it for isolation), DDL files.
- Produces: `create_schema(driver)`, `build_graph(embedder) -> Graph`, `load(driver, embedder, ns="prism") -> LoadReport(version, nodes, rels, deleted)`, `counts(driver, ns)`, `delete_ns(driver, ns)`.

- [ ] **Step 1 (ruling recorded in plan-3 ledger):** the loader reads the repo registries directly, not `describe()` through a service principal. Reason: the YAML/DDL are the single source of truth the source servers themselves load; it avoids a fake principal with broad scopes; a consistency test (step 5) proves graph metrics == what the servers describe for `head_data`. Plan 2's `svc:graph-loader` item is therefore closed as "not needed" (record in carry-forward).
- [ ] **Step 2:** Schema exactly as the spike (`schema.py`), with one change: fulltext analyzer `english` (the spike showed `standard-no-stop-words` does not stem "breaks" to "break"); keep `vector.quantization.enabled: false`. Every node carries `:Ctx {uid, ns, loaded_version}`; searchable nodes (BusinessTerm, Metric, Column, Question, Concept) also `:Searchable` with `name`, `description`, `synonyms`, `embedding`, and `allowed_scopes` (list of scopes that may read the object, `['*']` for glossary/ontology that are not source-bound). Natural-key uniqueness constraints include `ns` (composite). `create_schema` ends with `CALL db.awaitIndexes($t)`.
- [ ] **Step 3:** Model per the spike with these node/edge kinds: Source, Table, Column, Endpoint, Field, Metric, Dimension, BusinessTerm, Concept, Question, Execution, Role; edges `HAS_TABLE, HAS_COLUMN, HAS_ENDPOINT, BACKED_BY, COMPUTED_FROM, HAS_DIMENSION, REFERENCES, SAME_KEY_AS, DEFINES, TAGGED_WITH, BROADER, IMPLEMENTED_BY, IDENTIFIED_BY, ANSWERED_BY, USED, CAN_READ{scope,row_scope}`. `allowed_scopes` for a source object is computed from the existing access rules (`prism.security.access`): source scope `cashrecon`, table scope `refmaster.securities`, etc. Sensitive dimensions carry `sensitive: true`. Searchable text for embeddings = `name + synonyms + description/definition`; embed all documents in ONE `embed_documents` batch (500 docs ~2 s).
- [ ] **Step 4:** `load()`: one write transaction, `UNWIND` batches of 500 with `MERGE (n:Ctx {uid:$uid})`, then `DELETE` stale nodes `WHERE n.ns = $ns AND n.loaded_version < $version` (`DETACH DELETE`), then `db.awaitIndexes`. Version = epoch seconds; `counts()` returns per-label counts.
- [ ] **Step 5:** Tests: (a) load twice -> identical `counts()` (spike: 681 nodes / 1024 rels; expect similar); (b) remove a metric from the model in a scratch namespace then reload -> its nodes and edges are gone, other nodes untouched; (c) parse_ddl covers every table referenced by knowledge and by metric `tables` (closes the Task 3 step 4 gap); (d) every `Metric` node's `id` set equals the union of SQL and REST metric ids (22); (e) consistency: for `head_data` the `describe` output of each source server lists exactly the graph's metrics for that source (skip if MCP servers are not running, marker `live`); (f) a scoped namespace load never touches ns `prism` (count before/after); (g) schema is idempotent (run twice).
- [ ] **Step 6:** Commit `feat: context graph schema and idempotent loader`.

### Task 5: Role-filtered retrieval, context pack, catalog

**Files:**
- Create: `backend/prism/graph/retrieval.py`, `backend/prism/graph/catalog.py`, `backend/tests/graph/test_retrieval.py`, `backend/tests/graph/test_catalog.py`
- Source: spike `retrieval.py` and `catalog.py` (Cypher is proven: `SEARCH_CYPHER`, `EXPAND_CYPHER`, `PATH_CYPHER`, `CATALOG_CYPHER`, `lucene_query`).

**Interfaces:**
- Produces: `search_context(driver, qvec, text, scopes, k, *, metrics_only, ns, timeout_s) -> list[Hit]` (async twin `asearch_context` using `neo4j.AsyncDriver` and `asyncio.wait_for`); `expand(...)`; `context_pack(question, persona_claims, ...) -> dict` with keys `metrics`, `terms`, `concepts`, `columns`, `examples`, `join_paths`; `load_catalog(driver, ns) -> Catalog` where `Catalog.metrics[id]` has `source, kind, tool, endpoint, dimensions, required, sensitive, tables, allowed_scopes` and `Catalog.roles`.

- [ ] **Step 1:** Port the spike retrieval with these changes: real embeddings instead of `fake_embed`; typed lists (embedding spike: one shared list lets glossary terms push metrics down): run the search Cypher separately per kind and return metrics top-5 and terms top-3 (plus columns top-5 for non-metrics-only callers, past questions top-3); over-fetch `fetch = min(max(k*25, 100), 1000)` because vector top-k is global and the role gate runs after; `lucene_query` tokenises free text to lower-cased `\w+` tokens (len > 1) so user text never reaches the Lucene parser raw; scopes always get `'*'` appended.
- [ ] **Step 2:** Role gate in every statement (spike `gate()`): `ns` match, `any(s IN allowed_scopes WHERE s IN $scopes)`, metrics-only callers never see Table/Column/Field/Endpoint and never see `sensitive` dimensions, a Question is visible only if every object its Execution USED is visible. Keep the gate in ONE Python function used by all Cypher builders.
- [ ] **Step 3:** Pack budget: cap at 3,000 estimated tokens (chars/4); drop lowest-ranked items first; spike pack sizes were <= 965 tokens.
- [ ] **Step 4:** Tests with real embeddings on the real graph (ns `prism`, loaded once by a session fixture): `cash_ops_emea` question "security price conflicts by issuer country" returns no refmaster/marketmaster/assetrecon object; `steward` gets the MarketMaster metric, a join path price_suspects -> instruments -> securities -> legal_entities with join keys, and `head_data` likewise; table-level scope (`refmaster.securities`) works; persona with no matching scope -> empty pack; `bi_analyst` pack has no tables/columns and never contains `matched_by`; question with Lucene specials, empty string, 50k chars, emoji -> no exception; a Question node whose Execution used an unreadable object is invisible to that persona; retrieval quality: port `eval_questions.yaml` (60 questions) and assert typed metrics R@3 >= 0.95 and R@5 >= 0.98 with the real graph (role: `head_data`); latency test (pack p95 < 100 ms, generous); `asyncio.wait_for` path: point a driver at a paused/closed port and assert a clean `GraphUnavailable` within the timeout (use a closed port, not `docker pause`).
- [ ] **Step 5:** Catalog tests: one query returns 22 metrics and 5 roles; `Catalog` equals the gateway's later expectations (required/sensitive dims, `allowed_scopes`); a Python-vs-SQL-vs-graph `can()` agreement test (carry-forward): for every persona x every source/table, `prism.security.access.can`, the SQL `prism_sec.can` (via the existing db fixtures) and graph visibility (`CAN_READ`/`allowed_scopes`) agree.
- [ ] **Step 6:** Commit `feat: role-filtered hybrid retrieval, context pack and catalog`.

### Task 6: `app` database: idempotent migrations, audit and query log

**Files:**
- Create: `backend/prism/db/app_migrate.py`, `backend/prism/gateway/audit.py`, `backend/tests/gateway/test_audit.py`
- Modify: `backend/prism/db/migrate.py` and `backend/prism/sim/cli.py` only as needed so `--reset` / `recreate_databases` never drops `app`.

**Interfaces:**
- Produces: `migrate_app(settings)` (creates `app.audit`, `app.query_log` with `CREATE TABLE IF NOT EXISTS` and versioned migration table); `AuditWriter(pool)` with `await write(event: dict)` that never raises into the caller (logs and increments a dropped counter) and `await query_history(sub, limit)`; event fields: `ts, sub, persona, tool, source, metric_id, status, rows, bytes, truncated, ms, question_hash, result_handle, error_code`.

- [ ] **Step 1:** Write tests first: `migrate_app` twice is a no-op; `--reset` leaves `app` rows intact (insert a row, run the reset path on the small profile, assert the row survives); an audit write with Postgres down returns without raising and increments `dropped`; audit rows contain NO query text beyond a SHA-256 of the question and the metric id/dimension names (no row values, no tokens).
- [ ] **Step 2:** Implement; add explicit audit handler config so audit lines reach the log (Plan 2 backlog: `prism.mcp.audit` only appeared via SDK root handler). `question_hash` only; the raw question is stored in `query_log` only when the caller's role allows it (all demo personas do; make it a settings flag `store_questions`, default True for the demo).
- [ ] **Step 3:** Commit `feat: app db migrations, audit and query log`.

### Task 7: Gateway core: policy, downstream calls, result store, combine

**Files:**
- Create: `backend/prism/gateway/policy.py`, `results.py`, `combine.py`, `downstream.py` and `backend/tests/gateway/test_policy.py`, `test_results.py`, `test_combine.py`, `test_downstream.py`

**Interfaces:**
- Consumes: `Catalog` (Task 5), `mint_source_token`, `prism.mcp.client.call_tool`, `AuditWriter`.
- Produces:
  - `Policy(catalog).check_metric(claims, metric_id, dimensions, filters) -> MetricPlan(source, tool, arguments)` or raises `UserFacingError` with stable `code` (`unknown_metric`, `not_permitted`, `sensitive_dimension`, `missing_required_dimension`, `grain_too_fine`, `metrics_only`). Enforces: role scope on the metric's `allowed_scopes`; `metrics_only` callers cannot use `query_source`; sensitive dimensions rejected for metrics-only callers (group-by and filter); required dimensions present (`open_break_amount` requires `ccy`); grain / minimum-group-size policy for metrics-only callers: a metric whose dimensions include `portfolio_id` or other identifier-grain dimensions may not be grouped at identifier grain together with a date dimension unless the result has at least `min_group_size` (default 5) underlying rows per group (implement as a dimension-level `grain: fine` flag in the metric YAML plus gateway check; add the flag for `nav_break_bps_max` by `portfolio_id`+`nav_date`).
  - `ResultStore(max_bytes=32MB, ttl_s=900, max_handles_per_sub=50)`: `put(sub, columns, rows, meta) -> handle` (`r_` + 12 hex), `get(sub, handle)` only for the same `sub` (cross-user handle access returns "unknown handle", never "forbidden"), `summary(handle)` (columns, row count, 5 sample rows, units, truncated flag) used to keep LLM context small, eviction LRU.
  - `combine(store, sub, sql, handles: dict[str, str]) -> handle`: DuckDB in-memory connection per call, tables registered from handles only, `SET enable_external_access=false`, `SET lock_configuration=true`, only a single `SELECT`/`WITH` statement (validate with sqlglot DuckDB dialect), row cap 10,000, 5 s wall-clock timeout via `conn.interrupt()` from a timer, rejects mixed-currency summation hint: if any input handle has a `ccy`-unit column the output must keep it in the GROUP BY (check via sqlglot: every aggregate over an amount column must have `ccy` in GROUP BY, else `UserFacingError("currency_mixing")`).
  - `Downstream(settings, audit, cap=4)`: `async run_metric(claims, plan) -> SourceMetricResult` and `async query(claims, source, request)`; per-(`sub`,`source`) `asyncio.Semaphore(cap)` (source cap is 3; set gateway cap 3 and queue, plus retry "too many concurrent requests"/"busy" 3 times with jittered backoff); unwraps `ExceptionGroup`/`MCPError` auth failures into `UserFacingError("source_auth")`, timeouts into `source_timeout`, tool errors (`is_error`) into `SourceError` with the source's own message; never includes tokens in errors; result `truncated` preserved.

- [ ] **Step 1:** Write the failing tests for each unit, using fakes (no live servers): policy matrix over all 5 personas x 22 metrics from the Catalog (expected table derived from `tests/db/test_rbac.py::expected()`, never edited to fit); the metrics-only and sensitive-dimension cases; `grain_too_fine`; result store isolation between two `sub`s, TTL expiry (injected clock), byte cap eviction; combine: allowed join of two handles, `ATTACH`, `COPY`, `read_csv`, `INSTALL`, multiple statements, `PRAGMA` and file access all rejected (run each against a real DuckDB), timeout via a cross-join bomb returns `combine_timeout` within ~6 s, currency-mixing guard; downstream: semaphore never exceeds the cap under 12 parallel calls (fake client with a counter), retry on "too many concurrent requests", ExceptionGroup unwrapping, token never appears in any raised message.
- [ ] **Step 2:** Implement until green. Keep each module < 250 lines.
- [ ] **Step 3:** Commit `feat: gateway policy, result store, combine and downstream client`.

### Task 8: Semantic Gateway MCP server (:8200)

**Files:**
- Create: `backend/prism/gateway/server.py`, `backend/prism/gateway/cli.py`, `backend/tests/gateway/test_server.py`
- Modify: `backend/prism/mcp/servers.py` only for shared constants if needed; `backend/Procfile`; `scripts/start_backend.sh`; `README.md`; `Makefile`

**Interfaces:**
- Consumes: everything above; the generic MCP app factory in `backend/prism/mcp/base.py` (same `PrismTokenVerifier`, stateless HTTP, JSON responses, `TransportSecuritySettings` host allow-list; audience `gateway-mcp`).
- Produces tools (all return compact JSON; errors are `Error executing tool <name>: <message>` with `is_error`):
  - `search_context(question: str, max_items: int = 8) -> pack` (role-filtered, embeds the question with the local model, calls `context_pack`).
  - `run_metric(metric_id: str, dimensions: list[str] = [], filters: dict = {}, limit: int | None = None) -> {handle, summary}` (NO source argument; gateway resolves the source from the catalog).
  - `query_source(source: str, request: dict) -> {handle, summary}` (refused for metrics-only; per-source `query` tool shape `{"request": {...}}`; friendly message when `{"sql": ...}` is passed bare).
  - `get_rows(handle: str, offset: int = 0, limit: int = 50) -> rows` (small pages; default limit 50, max 200).
  - `combine(sql: str, handles: dict[str, str]) -> {handle, summary}`.
  - `record_answer(question: str, plan: str, handles: list[str], verified: bool)` (query-history capture, writes `app.query_log` and stages a candidate Question node for the distiller in Task 9; gated to `sub` only).
- The gateway catalog is held in memory and refreshed on a timer (30 s) and by `POST /admin/reload-catalog`? No HTTP admin surface in the MVP: refresh by comparing `max(loaded_version)` every 30 s; atomic swap of the `Catalog` object.

- [ ] **Step 1:** Write failing tests with an in-process gateway app (`httpx` ASGI transport is not enough for MCP; follow `tests/mcp/test_servers.py`'s pattern with a real uvicorn-in-thread on `http://127.0.0.1:<port>`): tool listing has exactly these six tools; unauthenticated call -> 401; token with wrong audience -> 401; `cash_ops_emea` `run_metric('open_breaks')` returns a handle and a summary with <= 5 sample rows (use a fake Downstream); the same persona asking `run_metric('price_conflicts')` -> `not_permitted` with no other source names in the message; `bi_analyst` `query_source` -> `metrics_only`; cross-`sub` `get_rows` -> "unknown handle"; `search_context` returns an empty pack for a persona with no scopes; every call writes one audit row (fake writer) with no row values; graph unavailable -> tool error `context_unavailable` within 5 s and an audit row; dev-default secret refusal when `Settings.jwt_secret` is the dev default and `PRISM_ENV != test`.
- [ ] **Step 2:** Implement with `create_app(settings, ...)`/factory `prism.gateway.server:create_app_from_env` (uvicorn `--factory`), startup: `Embedder.load()+warmup()` (fail fast with a clear message if the model cache is missing: "run `make models`"), graph connectivity check, `load_catalog`, `migrate_app`.
- [ ] **Step 3:** Procfile gets `gateway: uvicorn prism.gateway.server:create_app_from_env --factory --host 127.0.0.1 --port 8200`; `scripts/start_backend.sh`: add 8200 to the port-busy check, start Neo4j via compose (same idempotent pattern as Postgres), wait for bolt readiness (poll `RETURN 1` up to 60 s), run `uv run python -m prism.graph.cli load` only when `check` says the graph is empty or its `loaded_version` is older than the code's `GRAPH_SCHEMA_VERSION`, run the model download if the cache is empty, then exec honcho. The script must never run a reset/destructive op on existing data (no `--reset` paths).
- [ ] **Step 4:** `python -m prism.gateway.cli list|call <tool> --as PERSONA --args JSON` (loopback only, never prints tokens; same shape as `prism.mcp.cli`).
- [ ] **Step 5:** Commit `feat: semantic gateway MCP server`.

### Task 9: Query history distillation and end-to-end verification

**Files:**
- Create: `backend/prism/graph/history.py`, `backend/tests/graph/test_history.py`, `backend/tests/gateway/test_e2e_live.py` (marker `live`: requires the running stack)
- Modify: `backend/prism/graph/cli.py` (`distill` command), `README.md`

**Interfaces:**
- Produces: `distill(driver, embedder, pg_pool, ns="prism") -> DistillReport`: reads `app.query_log` rows with `verified = true` and a result that succeeded, upserts `(:Question)-[:ANSWERED_BY]->(:Execution)-[:USED]->(:Metric)` with `allowed_scopes` = the intersection of the scopes of the objects used (a past question is only ever shown to roles that could run it), embeds the question text, deduplicates by normalised text hash, never stores result rows or user identity (no `sub`, only a `count` of times asked).

- [ ] **Step 1:** Tests: distilling the same log twice is idempotent; a question that used a CashRecon metric is invisible to `invest_ops_growth` in retrieval; no `sub`/email/row values exist on any Question/Execution node (assert on all properties); failed or unverified executions are never distilled.
- [ ] **Step 2:** Live e2e (`pytest -m live`, documented in README; skipped unless all of ports 8200-8205 and Neo4j answer): for each demo story the expected number is reproduced through the gateway only: Vendor A x Corp bond conflicts (60 vs 90; +20% WoW), SRC001 late feeds, PF001/002/005 exceptions per day, LE00016 64 of 79 aged USD breaks; run on the full profile, because the small profile does not carry the stories (Plan 1 carry-forward #5). Then a fan-out test: 6 concurrent `run_metric` calls as one user succeed; a `bi_analyst` session can get metrics but every attempt at `query_source`, sensitive dimensions or fine grain is refused; `combine` of two handles works and mixed-currency sums are refused.
- [ ] **Step 3:** Update `README.md` (architecture diagram of the gateway, `make db`, `make models`, start script, the CLI examples, the six tools with one-line examples); update `docs/superpowers/plans/plan-2-carry-forward.md` with a short "Resolved in Plan 3" list; write `docs/superpowers/plans/plan-3-carry-forward.md` (what Plan 4 (agents) consumes: tool signatures, pack shape, handle semantics, error codes, per-(sub,source) concurrency, known limits).
- [ ] **Step 4:** Commit `feat: query history distillation, e2e checks and docs`.

---

## Self-review

- Spec coverage: §4 semantic layer (Tasks 3-5), §4.4 ontology / glossary / history (Tasks 3, 9), §5 security (Tasks 5, 7, 8: role gate in graph, policy, audience tokens, metrics-only, audit), §6 token efficiency (pack budget, handle + summary, `get_rows` paging, combine), the gateway-only rule (Task 8). Out of scope here: agents, UI, LLM calls (Plans 4-6).
- Carry-forward coverage: stable error codes (Task 7), gateway semaphore per (sub, source) and retry (Task 7), ExceptionGroup unwrapping (Task 7), grain / minimum-group-size (Task 7), `app` DB idempotent and excluded from resets (Task 6), audit handler (Task 6), dev-default secrets refusal for the gateway (Task 8), Python/SQL/graph `can()` agreement (Task 5), graph-loader principal (closed by ruling in Task 4), `query` tool friendly message (Task 8), bind Neo4j to 127.0.0.1 (Task 1), full-profile story assertions (Task 9). Left for later: BackendBase extraction, SQL-guard hardening backlog, per-audience signing keys.
- Risks: DuckDB `combine` is an additional SQL surface (Task 7 tests enumerate the denied statements; the review must red-team it); the role gate exists in four places after this plan (Python, SQL, graph Cypher, gateway policy) and the agreement test is the guard; quality numbers come from a glossary and eval set written by the same author, so Plan 6 must extend the eval set with real user questions.
- Execution notes: Tasks 4, 5, 7, 8 are security-sensitive (independent red-team review after each); Task 7 `combine` and Task 8 auth get the most scrutiny; run the whole suite after every task.
