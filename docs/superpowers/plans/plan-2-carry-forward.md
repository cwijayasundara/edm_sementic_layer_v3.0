# Plan 2 — outcome and carry-forward for Plan 3

Plan 2 (source MCP servers) is complete on branch `plan-2-source-mcp`: 10 tasks, each reviewed (security-sensitive ones by independent red-teams), then two whole-branch reviews, one fix wave and a scoped re-review. 1026 tests pass (`cd backend && uv run pytest -q -W error`). The live stack was verified end to end by an independent reviewer on the full seeded data.

## Resolved in Plan 3
- Gateway-side semaphore per (sub, source), queueing instead of failing, with jittered retry on the sources' exact
  busy messages (`prism.gateway.downstream`); six concurrent `run_metric` calls of one user succeed (live e2e).
- `ExceptionGroup` / `MCPError` from the SDK unwrapped and mapped to stable codes (`source_auth`, `source_timeout`,
  `source_unavailable`, `source_error`); every gateway refusal carries a stable `code`.
- Metrics-only enforced at the gateway (no `query_source`, no sensitive dimension) plus the grain rule (identifier x
  date never pinned together; minimum-group-size refused outright).
- Python / SQL / graph `can()` agreement test (Task 5); graph-loader principal not needed (registries read directly).
- `app` database: idempotent declarative migrations, excluded from resets; `app.audit` + `app.query_log` with an
  explicit audit log handler (Plan 1 #2 and the Plan 2 audit-handler item).
- Dev-default secrets refused when `PRISM_ENV=production` (all seven secrets, as `SecretStr`), 32-character minimum
  for signing / HMAC keys, gateway tokens limited to 1 h and an exact audience (Plan 1 #6, for the gateway).
- `query` argument shape: friendly `QUERY_SHAPE` message at the gateway and README examples.
- Neo4j bound to 127.0.0.1 (Plan 1 #8); full-profile story assertions through the gateway (`pytest -m live`,
  Plan 1 #5).
- Still open from this page: `BackendBase` extraction, the SQL-guard hardening backlog, per-audience signing keys,
  REST token TTL, pool exhaustion items, MarketMaster timeseries `truncated` (carried in `plan-3-carry-forward.md`).

## What exists
- One MCP server per platform (RefMaster 8201, MarketMaster 8202, CashRecon 8203, AssetRecon 8204, FeedHub 8205), all with the same three tools: `describe`, `run_metric` (governed metrics, preferred) and `query` (guarded free-form access; refused for metrics-only principals).
- Audience-bound tokens: `<source>-mcp` for the servers, `<source>-api` for the mock REST APIs (re-minted per call from the caller's forwarded claims; the incoming token is never passed through).
- SQL sources: read-only `bi_reader` transaction with a signed row-level-security context, an AST SQL guard (policy re-enforced on the re-parsed regenerated SQL; size and nesting caps; three review rounds including two red-teams), a governed-metric compiler over 19 YAML metrics, a named-cursor byte budget, a cumulative deadline and a per-principal in-flight cap.
- REST sources: endpoint registry (params validated before any request, path values allow-listed), 3 endpoint-backed metrics, streamed response size cap, overall deadline, per-principal cap.
- `python -m prism.mcp.cli list|call` smoke tool (loopback URLs only), Procfile / `scripts/start_backend.sh` wiring, README section.

## Contract notes for the Plan 3 gateway / graph loader
- `MetricResult.truncated` exists; the summary text says "(truncated)". Treat truncated results as partial.
- `describe()` entries now carry `sensitive_dimensions` and `required_dimensions`. `open_break_amount` REQUIRES the `ccy` dimension (amounts are never summed across currencies).
- Metrics-only principals (`claims["metrics_only"]`, fail-closed when missing): no `query`; cannot group by OR filter on a metric's sensitive dimensions (today `matched_by` on `manual_matches` and `auto_match_rate`). Grain / minimum-group-size policy is the gateway's job (e.g. `nav_break_bps_max` by portfolio_id + nav_date returns one row per portfolio per day).
- Backends validate request shapes first: `dimensions` must be a list of strings, `filters` an object with string keys (direct callers passing `None` are rejected; the MCP tool path is unaffected).
- Errors are `Error executing tool <name>: <message>` with `is_error=true`; auth failures through `prism.mcp.client` surface as an `ExceptionGroup` wrapping `MCPError` with no status code — the gateway must unwrap and map them.
- Tokens for the servers: `prism.mcp.results.mint_source_token(claims, source, settings, ttl_s=60)` (claim whitelist in one place).
- Per-principal cap is 3 in-flight calls per `sub` per source process (describe counts). A user's parallel sub-agent fan-out will hit it: queue with a gateway-side semaphore per (sub, source), make the cap/pool configurable (cap ~4-6, pool >= 2x cap), retry "too many concurrent requests" / "busy" with jitter.
- **Closed by Plan 3 Task 4 (not needed):** the graph loader reads the repo registries (metric YAML, REST YAML, DDL) directly instead of `describe()`, so no `svc:graph-loader` principal exists; a test pins graph metrics == `describe` for `head_data`. Original note: `describe` is role-filtered: the graph loader needs its own principal (`sub` like `svc:graph-loader`, all-source scopes, `rows: {}`, `metrics_only: true`, short TTL); verify tables without a row dimension are readable with `rows: {}`. `describe` is missing what spec §4.4 wants: typed/versioned models, REST response fields and endpoint method/path, column descriptions and PII/masked flags, sample values, FK/join keys, dimension types and value domains, filter operator support (REST filters take a single value), metric grain and tables, direction (higher/lower is better), default order/max limit. Use RefMaster's `data_dictionary` endpoint for glossary input.
- Metric ids are unique across all 22 (a test pins it) because the gateway's `run_metric` will not take a source argument; overlapping metrics have sharper descriptions now (`late_feeds` is a superset of `missing_or_failed_deliveries`, `open_*` definitions differ: status <> 'closed' vs status = 'open').

## Decisions / owner action needed
- **Documentation screenshots (resolved):** the reference screenshots that showed real client and vendor names were removed from the repository, from all git history and from disk (owner decision). `.gitignore` keeps them from being re-added.
- Dev passwords (`PRISM_PG_*`) come from the tracked `.env.example` template (loopback only); JWT and context keys are generated per machine. Plan 1 carry-forward #6 (refuse dev-default secrets outside tests; minimum `jwt_secret` length; cap the `prism-token` TTL) is still open and now covers five more token-accepting services.

## Backlog (from reviews)
- Security: per-audience or asymmetric signing keys so servers can verify but not mint; maximum token lifetime and `iss` check in the verifier; REST token TTL `min(60, exp - now)`; a Python-vs-SQL-vs-graph `can()` agreement test once the graph adds a third copy.
- SQL guard: function allow-list instead of a deny-list; enforce policy with Postgres's own parser (pglast/libpg_query) on the regenerated SQL; PG-faithful lexer for E-strings/backslashes; nightly rotating-seed fuzz; decide catalog-metadata exposure (PUBLIC-readable `pg_catalog` / `information_schema`); server-side row-width guard or `fetchmany(1)` (a chunk of 10 wide rows is still fetched before the size check).
- Resource bounds: cumulative deadline excludes the pool wait; many principals can still exhaust the 8-connection pool; `PoolTimeout` also masks "DB down" as "busy"; `aclose()` does not abort in-flight REST streams.
- Observability: audit lines only reach logs because the MCP SDK installs a root handler — add an explicit handler / `--log-config` for `prism.mcp.audit`; audit SDK-level rejections (401/421/validation) at the gateway; add a `query` fingerprint and row count to audit lines; log tracebacks under the internal-error ref at DEBUG; noisy INFO logs (httpx URLs with params, duplicated tool errors).
- Code: extract a shared `BackendBase` (in-flight limiter, entitlement helpers, closed flag, `MetricInfo` model) from `sql_backend.py` / `rest_backend.py`; stable error codes on `SourceError`; client reuse with a per-request auth hook (each `call_tool` currently costs three round trips); `query` tool argument shape is `{"request": {...}}` — a bare `{"sql": ...}` produces a raw pydantic error (add a README `query` example / friendlier message).
- Data/API: MarketMaster timeseries silently caps at 500 points with no `limit` in the body so `truncated` can never be true there; the shipped `matched_by` operator ids are the only person identifiers in the catalog today.
