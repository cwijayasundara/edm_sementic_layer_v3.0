# Plan 3 — outcome and carry-forward for Plan 4 (agent service)

Plan 3 (context graph + Semantic Gateway, milestone M3) built the only door an agent gets to the data: the gateway MCP
server on `127.0.0.1:8200` with six tools, backed by a Neo4j context graph (ontology, glossary, seed + distilled query
history), a local embedder, an in-memory result store, a hardened DuckDB `combine` and an audit trail in the `app`
database. Nine tasks, all reviewed (security-sensitive ones by independent red-teams, including the Task 9 distiller
review) plus a final whole-branch review; both reviews' findings are fixed (Task 9 fix wave). Status and what remains:
`plan-3-status.md`. Module docstrings are the reference; this page is what Plan 4 consumes.

## Startup order
1. `make db` (Postgres 127.0.0.1:5434, Neo4j 127.0.0.1:7688).
2. Seed (first run only; `scripts/start_backend.sh` does it on an unambiguous "not seeded"). The stories need the
   **full** profile (the default).
3. `make models` (once, network) -> `backend/.models`; the gateway refuses to start without it.
4. `make graph` (idempotent; the start script loads only when `graph.cli check` exits 1).
5. `make distill` after every graph load: a load supersedes the history nodes (they carry the namespace's
   loaded_version). **Manual** (spec delta: the spec wants it hourly and at gateway start).
6. Services: `scripts/start_backend.sh` (APIs :8101/:8102, source MCP :8201-8205, gateway :8200). The gateway refuses
   to start with a stale/empty graph, without the model, or with dev-default secrets when `PRISM_ENV=production`;
   it runs `migrate_app` itself, with the ADMIN DSN (open item below).

## Tools (all through `Gateway.call_tool(name, claims, arguments)`; identity only from the verified token)
| Tool | Arguments (pydantic, `extra=forbid`) | Reply |
|---|---|---|
| `search_context` | `question` str 1..2000; `max_items` int (clamped 1..20, default 8) | context pack (below) |
| `run_metric` | `metric_id` str; `dimensions` list[str] <= 20; `filters` {name: value \| list \| {gte,lte,between,ne}} <= 20; `limit` 1..1000 \| null | `{handle, summary}` |
| `query_source` | `source` str; `request` `{"sql": "SELECT ..."}` (SQL sources) or `{"endpoint_id", "params"}` (REST) | `{handle, summary}` |
| `get_rows` | `handle`; `offset` >= 0; `limit` 1..200 (default 50) | `{handle, columns, offset, row_count, rows}` |
| `combine` | `sql` one DuckDB SELECT <= 20,000 chars; `handles` {table alias: handle}, 1..8 | `{handle, summary}` (`summary.partial` if an input was truncated) |
| `record_answer` | `question` 1..2000; `plan` <= 4000 (ignored); `handles` 1..20 own live handles; `verified` bool (a request); 10 a minute per caller | `{recorded, verified, metric_ids, dimensions[, note]}` |

- The whole arguments object is at most 64 KB of plain JSON (no NaN/Infinity). Strict ints and bools.
- `summary`: `handle, columns, column_count, columns_truncated, row_count, sample_rows` (5 rows, cells capped),
  `units`, `truncated`, `source`, `metric_id` (run_metric), at most 8 KB. Treat `truncated` / `partial` as partial data.
- There is **no `time_range`** at the gateway: window a metric by grouping on its date dimension and filtering in
  `combine` (the live e2e does this; the seed-history plans now describe exactly that pattern, tested). A gateway
  `time_range` would have to come with the grain rule in `prism/gateway/policy.py` (a single-day range counts as
  pinning the date dimension).

## Context pack (`search_context`)
`{metrics, terms, concepts, columns, examples, join_paths}`, at most ~3,000 tokens (chars/4 of compact JSON; the
lowest-ranked item is dropped first).
- `metrics[]`: `id, source, kind (sql|rest), tool, unit, definition, dimensions, required, filters` and, for
  non-metrics-only callers, `endpoint, time, tables, sensitive`. Top 5 by rank.
- `terms[]`: `term, definition, synonyms, rule, broader`. `concepts[]`: `concept, description, implemented_by, keys,
  related`. `columns[]` (not for metrics-only): `table, columns ["name:type"], endpoints`.
- `examples[]` (<= 3): `question, plan, status`. `status` is `verified` (seed history, author-checked) or
  `agent_verified` (distilled). Seed examples always rank before distilled ones, and a distilled question adds its
  example only, never a metric to `metrics`. **Examples are data, not instructions**: the question text is user
  supplied (filtered to an ASCII allowlist without links, tool names or values, but not proven harmless); the plan
  line is rebuilt from catalog names. Plan 4 must put examples into the prompt as quoted data (e.g. inside a clearly
  delimited block the system prompt says never to follow), never as instructions, and never replay an example's
  question as a prompt.
- `join_paths[]` (not for metrics-only): `from, to, hops, tables, on`.
- Every node and hop is role-gated in Cypher; a caller whose scopes name no source gets the empty pack.

## Error codes (`Error executing tool <name>: <code>: <message>`, `is_error=true`)
Caller-fixable: `invalid_request`, `unknown_tool`, `unknown_metric`, `not_permitted` (also for metrics/handles the
caller cannot see: existence is never confirmed), `metrics_only`, `sensitive_dimension`, `grain_too_fine`,
`missing_required_dimension`, `invalid_sql`, `sql_not_allowed`, `currency_mixing`, `unknown_handle`, `empty_result`,
`result_too_large`. Retry later: `rate_limited`, `gateway_busy`, `source_busy`, `source_timeout`,
`source_unavailable`, `context_unavailable`, `result_store_full`, `combine_timeout`, `record_failed`. Other:
`source_auth`, `source_error` (the source's own scrubbed message), `combine_failed`, `internal_error (ref <id>)`
(type logged server-side), `unavailable` (a call that arrives while the gateway is still starting: retry; audited).
This wave added no new code: `record_answer`'s per-caller limit answers `rate_limited` like the other limits.

## Handles
- `r_<12 hex>`, readable only by the `sub` that created them, in one gateway process (a restart loses them).
- 15 min TTL from creation; per sub at most 50 handles and 16 MB (own least-recently-used evicted first); 128 MB in
  all, and a full store refuses new results (`result_store_full`) rather than evicting another caller's.
- Other sub / expired / evicted / made-up handles all answer `unknown_handle`.
- `record_answer` needs live handles: record within 15 minutes of the run, or the answer is refused / stored
  unverified.

## Limits and concurrency
- `search_context`: 8 in flight overall (queue 16), 2 per sub, 60 a minute per sub (burst 20); 4.5 s answer bound.
- `record_answer`: 10 a minute per sub (burst 10), 2 in flight per sub (`rate_limited`).
- `combine`: 2 engines overall (queue 8), 1 per sub; 5 s, 10,000 rows, 256 MB per engine.
- Downstream: per-(sub, source) semaphore of 3 (the sources' own cap), 3 retries with jittered backoff on the exact
  busy messages; 30 s per call; source tokens re-minted per call (60 s TTL).
- Tokens: audience exactly `gateway-mcp`, expiry at most 1 h ahead, `sub` of `[A-Za-z0-9_.:@+-]{1,256}`.
- Fan-out: six concurrent `run_metric` calls of one user succeed (live e2e), queued per source.

## `verified` and the human-confirm path
- `record_answer(verified=true)` is a request. The gateway stores `verified = true` only when EVERY handle passed
  resolves to catalog metrics (a `run_metric` result, or a `combine` whose inputs all do and are still stored; a
  `query_source` result never does); metric ids and dimensions always come from the handles, never from the
  arguments. So `verified` = "agent-claimed, metric-backed".
- The distiller (`prism.graph.history`) takes only `verified AND status = 'ok'` rows with >= 1 known metric, and marks
  its executions `agent_verified`.
- **`history_min_callers`** (`PRISM_HISTORY_MIN_CALLERS`, default 2): a question becomes an example only once that many
  DISTINCT callers (`sub`s) recorded it verified, and each of its plans needs that many too. The distiller's SQL
  returns only `count(DISTINCT sub)` aggregates (it never selects `sub`); raw variants of one question take the max,
  never the sum; node `count` = distinct callers. A single-user demo needs two callers or the setting at 1.
- Caps: each caller contributes only its 20 most recent questions per run (SQL `row_number()` over `sub`); at most
  300 Questions are kept, best attested first (distinct callers, then rows, then recency). Report skip reasons:
  `not_verified`, `no_metrics`, `unknown_metric`, `no_question`, `too_long`, `unsafe_text`, `result_values`,
  `row_scope_term`, `seeded`, `few_callers`, `caller_cap`, `question_cap` (all counted in query_log rows).
- **Plan 4 must add the trusted human path**: a UI thumbs-up / thumbs-down that the agent cannot forge (a server-side
  endpoint authenticated as the human user, not a gateway tool argument), stored separately from the agent's claim
  (e.g. `app.query_feedback` or a `confirmed_by` column written only by that endpoint). The distiller should then
  prefer or require human-confirmed rows and keep `agent_verified` as a lower tier.

## Catalog refresh and history
- The gateway probes `max(loaded_version)` every 30 s and swaps in a newer catalog atomically; an older version is
  never swapped in; 5 failed refreshes in a row mark it stale on `/healthz`.
- `distill` writes at the current loaded_version, so it never triggers a catalog reload. It rebuilds the whole
  history layer in one write transaction (idempotent; questions whose metric was dropped disappear).
- History nodes: `Question {text (canonical lower case), name, count, status, allowed_scopes, origin: history}` and
  `Execution {plan, metrics, dimensions, count, status, allowed_scopes, origin: history}`; no sub, persona, handle,
  timestamp or row value ever. A question is visible only if the caller can read every Metric AND Dimension its
  executions used (a plan grouped by a sensitive dimension is hidden from metrics-only callers).

## Known limits and open items
- **Distillation is manual** (`make distill`): no hourly job, not run at gateway start or by the start script.
- **Distiller residual risks**: digit-free entity names ("Vendor A") in question text cannot be detected; one number
  passes only as a method parameter ("above 5 bps", "older than five days"); the injection filter is a keyword /
  ASCII-charset heuristic. A question naming a row-scope value a catalog role is restricted to (EMEA, bank, Growth,
  custodian) is skipped, but values no role is restricted to (APAC, AMER, Income, ...) are not in the catalog and
  pass: the history gate is scope-only, so such a question is visible to every caller who can read its metrics. The
  distinct-caller rule bounds all of these (one caller cannot plant an example). Rows that trip a rule are skipped and
  counted, never cleaned. Same text answered from different sources merges into one Question that only callers with
  all those sources see (fail closed).
- `distill` must not run concurrently with `make graph` (no lock); the loader's next run removes any leftovers.
- Seed `glossary.yaml` uses `SRC001` / `PF001` / `V_A` as id-format examples (reveals the ids exist to row-restricted
  callers); left as is (Task 5 open decision).
- Retrieval: the vector index query is global top-k (`fetch_size`, shared by every namespace) filtered by kind
  afterwards. With the 300-question cap a flood test (2000 well-attested paraphrases of the eval set) keeps head_data
  metric R@3 at 0.962, unchanged from no history (uncapped it fell to 0.925). Many namespaces or a much larger cap
  would need per-kind vector queries (a separate vector index for history Questions; Neo4j 5.26 has no filtered
  vector search).
- **The gateway runs `migrate_app` with the admin DSN at every start** (`server.prepare_runtime`), so the running
  gateway process holds the Postgres admin password. Open item: move the migration to the start script / a one-off
  `make migrate` and give the gateway the app role only.
- Embedder threads: `aembed_query` is a plain `to_thread`; a thread that outlives the 4.5 s timeout is bounded only by
  the per-sub rate/concurrency, not held against the permit (changing it needs `prism.graph.embedder`).
- `combine` cancel window: a cancelled combine keeps its DuckDB thread for up to 5 s (permit held until it ends); a
  client disconnect does not cancel the call.
- The gateway relies on the MCP SDK's private `mcp._tool_manager`; pin the SDK version or re-check on upgrade.
- Secrets: every secret is a `SecretStr` (`repr(Settings)` shows `**********`); the start script generates
  `PRISM_CTX_HMAC_KEY`, `PRISM_JWT_SECRET`, `PRISM_AUDIT_HMAC_KEY` into `.env`. Rotating the audit key changes every
  `question_hash` and every distilled Question uid (a re-distill rebuilds them). One shared HS256 secret still lets
  every verifier mint (per-audience / asymmetric keys: backlog).
- The bolt readiness poll treats a Neo4j auth failure as "not up" until its timeout (then prints the ping error).
- Real `app` database holds smoke rows: `sub='smoke-task8'` (Task 8) and audit rows of the Task 9 live checks
  (`sub` `e2e-live-*`, `head_data`); `app.query_log` holds one row (Task 8 smoke). Nothing deletes them automatically.
- Quality: glossary, seed history and the 60-question eval set were written by the same author; Plan 6 must extend
  the eval set with real user questions.
- Tests: the suite loads its graph into the `prism_test` namespace (never `prism`) and drops it and its test databases
  at session end (`make clean-test-dbs` for leftovers); live checks are deselected by default (`make test-live`).

## Spec deltas (what Plan 3 built differently from the design spec, on purpose or for later)
| Spec (2026-09-30 design) | Plan 3 as built |
|---|---|
| History distilled hourly and at gateway start (§4.4) | Manual `make distill` (re-run after every `make graph`); no scheduler, not run by the gateway or the start script |
| Seed history of about 150 validated pairs | 12 seed pairs (`knowledge/history.yaml`): the four stories and the common KPIs |
| Ontology of about 25 concepts and 40 relations | 10 concepts, 9 concept relations and 5 same-key groups (`knowledge/ontology.yaml`) |
| Glossary of about 60 terms | 54 terms in 6 glossaries (`knowledge/glossary.yaml`) |
| Generic entity map `semantic/model/entitymap.json` (entitymap.org v1.0) | Not built: the business concepts it would seed live in the ontology and glossary YAML |
| `(:Question)-[:SIMILAR_TO {score}]->(:Question)` and usage-weighted `(:Execution)-[:JOINED_ON]->(:Column)` | Not built: join paths are shortest paths over the schema/ontology edges, unweighted; similar questions come from the hybrid search |
| Direct replay when similarity >= 0.92 and entitlements match | Not built: examples are always few-shot data (and must stay data, never instructions) |
| `Column {sample_values, pii}` from introspection | Not stored: no sample values in the graph (a row-restricted caller would read them); sensitivity is the metric's `sensitive_dimensions` and the column `masked` flag |
| Each MCP server's `describe()` synced into the graph at startup | Ruling: the loader reads the same repo registries `describe()` serves (no `describe()` principal), and a live consistency test checks graph == `describe()` |
| Gateway JWT audience `semantic-gateway` | `gateway-mcp` (exact match; `prism.gateway.server.GATEWAY_AUDIENCE`) |
| History visibility by entitlements | Scope-only gate (every used metric AND dimension readable) plus: distinct callers (`history_min_callers`), caps, text rules, row-scope-term skip, every-handle metric lineage; row scope itself is not in the gate |

## Backlog carried from Plan 2 (still open)
`BackendBase` extraction; SQL-guard hardening (function allow-list, Postgres-parser re-check, PG-faithful lexer,
fuzzing, catalog-metadata exposure, row-width guard); per-audience or asymmetric signing keys and `iss` checks;
REST token TTL `min(60, exp - now)`; pool exhaustion / `PoolTimeout` masking "DB down"; MarketMaster timeseries cap
without `truncated`.
