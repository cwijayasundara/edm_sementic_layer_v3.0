# Prism — agentic data intelligence MVP

Ask the data platform a question, get a dashboard back. Design: `docs/superpowers/specs/2026-09-30-agentic-data-intelligence-design.md`.

## Prerequisites
Docker Desktop, [uv](https://docs.astral.sh/uv/), openssl. (Node >= 20 once the frontend arrives in a later milestone.)

## Run the backend
```bash
make db                             # Postgres (127.0.0.1:5434) + Neo4j (127.0.0.1:7688) only, in Docker
make models                         # once: cache the local embedding model in backend/.models (needs network)
scripts/start_backend.sh            # first run creates .env, starts Postgres (5434) + Neo4j (7688), seeds data, caches the
                                    # embedding model, loads the context graph if empty/stale, starts every service
scripts/start_backend.sh --reseed   # regenerate all simulated data
```
Postgres is published on `127.0.0.1` only (host port 5434) and is not reachable from other machines; its dev superuser password must never be exposed beyond localhost.

## Try the APIs
```bash
cd backend
TOKEN=$(uv run python -m prism.security.cli steward refmaster-api)
curl -s -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8101/api/v1/exceptions/summary?group_by=domain"
TOKEN=$(uv run python -m prism.security.cli steward marketmaster-api)
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8102/api/v1/prices/conflicts/summary?from=2026-09-24&to=2026-09-30"
```
Personas: `steward`, `cash_ops_emea`, `invest_ops_growth`, `bi_analyst`, `head_data`.

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

## Context graph
```bash
make models      # once: cache the local embedding model in backend/.models
make graph       # build the Neo4j context graph from the metric/REST registries, DDL and knowledge YAML (idempotent)
make distill     # distil verified query history (app.query_log) into the graph; re-run after every `make graph`
cd backend && uv run python -m prism.graph.cli counts   # per-label / per-relationship counts
cd backend && uv run python -m prism.graph.cli check    # 0 current, 1 empty or stale (load it), 2 could not tell
cd backend && HF_HUB_OFFLINE=1 uv run python -m prism.graph.cli distill   # JSON report; exit 2 = could not run
```

### Query history
`record_answer` writes `app.query_log`; `distill` turns its eligible rows into
`(:Question)-[:ANSWERED_BY]->(:Execution)-[:USED]->(:Metric|:Dimension)` so `search_context` can return them as
`examples`. Eligible = `verified = true`, `status = 'ok'`, at least one metric id and every metric id still in the
graph. One Question per question text (case and whitespace ignored, keyed by an HMAC under `PRISM_AUDIT_HMAC_KEY`)
whoever asked it; one Execution per distinct metrics/dimensions plan, whose plan line is rebuilt from catalog names
(never the caller's text). A question, and each of its plans, needs at least `PRISM_HISTORY_MIN_CALLERS` (default 2)
DISTINCT callers (`count` on the nodes = distinct callers, counted in SQL; the distiller never reads `sub` itself), so
one caller cannot plant examples for others; **a single-user demo needs two callers or `PRISM_HISTORY_MIN_CALLERS=1`**.
`record_answer` is rate-limited per caller (10 a minute), each caller contributes at most its 20 most recent
questions per run and at most 300 questions are distilled (most distinct callers first, then rows, then recency). Distilled questions add examples only, never metrics, and
curated seed examples always rank before distilled ones. A past question is shown only to callers who can read every metric and
dimension it used; because that gate is scope-only, a question naming a row-scope value a role is restricted to
(EMEA, bank, Growth, custodian, read from the catalog roles) is skipped (`row_scope_term`), and the gateway stores
`verified` only when EVERY handle of the answer has metric lineage (a `query_source` result never does). Residual risk:
values no role is restricted to (APAC, Income) and digit-free entity names are not detected. The distiller reads only `question`, `plan`, `metric_ids`, `verified`, `status` (never `sub`,
persona or handles; only aggregate counts) as the app role in a READ ONLY transaction, and SKIPS (counts, never
cleans into the graph) a row whose text is over 200 characters, uses anything but ASCII letters, digits, blanks and
`? , . - '` (no `:` `/` `@` `_`; any non-ASCII letter, homoglyph or zero-width character), names a tool, an SQL verb or
an instruction, carries a domain or spells out a link, or carries values (more than one digit, number words, a number that is not a
method parameter, decimals, ids such as `SRC001`, letter+digit tokens). Digit-free
entity names ("Vendor A") cannot be told from vocabulary; the scope gate bounds that residual risk. A graph load
supersedes history nodes, so run `make distill` after `make graph`.

The distiller has had an independent security review; its findings (poisoning by one caller, floods, homoglyph /
link / instruction text, values in words, row-scope leaks) are fixed as described above.

**`verified` today means "agent-claimed and metric-backed", not human-confirmed:** the gateway stores it true only
when the agent asked AND a governed metric resolves from the caller's own live handles. Distilled executions therefore
carry `status: agent_verified` (seed history carries `verified`). The human confirmation (UI thumbs-up) is Plan 4.

## Semantic gateway (port 8200, `PRISM_GATEWAY_PORT`)
```
 agent / MCP client ──(JWT, audience gateway-mcp)──▶ Semantic Gateway :8200  (prism.gateway)
                                                     │  verify token -> claims (sub, scopes, rows, metrics_only)
                                                     │  policy (catalog from the graph): metric routing, sensitive /
                                                     │    fine-grain / metrics-only refusals, existence never leaked
     search_context ─▶ local embedder + Neo4j context graph (role-gated Cypher) -> context pack (<= ~3k tokens)
     run_metric / query_source ─▶ per-(sub, source) semaphore + retry ─▶ source MCP servers :8201-8205
                                    (fresh <source>-mcp token per call)   refmaster, marketmaster (-> REST APIs
                                                                          :8101/:8102), cashrecon, assetrecon, feedhub
                                                                          (Postgres, RLS + masking)
     results ─▶ in-memory ResultStore (per-sub handles, 15 min) ─▶ get_rows pages, combine (hardened DuckDB)
     every call ─▶ app.audit (one row);  record_answer ─▶ app.query_log ──(make distill)──▶ graph history layer
```
The only agent-facing door to the data: one MCP server (`gateway-mcp` audience tokens, stateless HTTP) with six tools.
Identity and entitlements come from the verified token, never from tool arguments; every call writes one row to
`app.audit` (catalog names, counts and codes only: no row values, tokens or question text; the question is kept as
an HMAC).

| Tool | What it does |
|---|---|
| `search_context(question, max_items=8)` | role-filtered context pack (metrics, terms, concepts, columns, examples, join paths) |
| `run_metric(metric_id, dimensions=[], filters={}, limit=None)` | governed metric; the catalog picks the source; returns `{handle, summary}` |
| `query_source(source, request)` | guarded free-form read, `request={"sql": "SELECT ..."}` or `{"endpoint_id": ..., "params": {...}}`; not for metrics-only roles |
| `get_rows(handle, offset=0, limit=50)` | one page (max 200 rows) of your own result |
| `combine(sql, handles)` | one DuckDB SELECT over your own handles (`{"t": "<handle>"}`), in memory |
| `record_answer(question, plan, handles, verified)` | query history (`app.query_log`): the metrics/dimensions of >= 1 of your own live handles, never the plan text; `verified` is a request, stored true only when a governed metric backs the handles (the human-confirmed path is Plan 4) |

Errors read `Error executing tool <name>: <code>: <message>` (codes such as `not_permitted`, `metrics_only`,
`invalid_request`, `unknown_handle`, `context_unavailable`, `source_unavailable`, `rate_limited`, `gateway_busy`).
Per caller: 2 `search_context` calls in flight and 60 a minute; 10 `record_answer` calls a minute; 1 `combine` at a time (its permit is held until the
DuckDB thread really ends, even when the call is cancelled). Tokens must carry exactly the `gateway-mcp` audience,
expire within an hour and have a `sub` of `[A-Za-z0-9_.:@+-]`. `/healthz` is public and shows only `status`
(`ok` / `degraded` / `starting`), whether the catalog is `current` or `stale` (5 failed refreshes in a row) and
whether the `audit` is `ok` or `degraded` (an audit or query-log row was dropped in the last 5 minutes; no counts). Results live 15 minutes in an
in-memory store: 128 MB in all, 16 MB per caller (`PRISM_GATEWAY_STORE_MB`, `PRISM_GATEWAY_STORE_PER_SUB_MB`), so eight
callers can hold a full quota at once; past that a new result is refused (`result_store_full`) rather than evicting
someone else's. The gateway refuses to start without the embedding model (`make models`), with an empty or stale
context graph (`make graph`), or with dev-default secrets when `PRISM_ENV=production`; it re-reads the catalog when the
graph's version moves forward (every 30 s; an older version is never swapped in). The CLI targets
`PRISM_GATEWAY_URL` (default `http://127.0.0.1:8200`): set it too when you change `PRISM_GATEWAY_PORT`.
One-line examples (from `backend/`; `--as` mints a short-lived demo token for a persona, loopback only):
```bash
uv run python -m prism.gateway.cli list --as head_data
uv run python -m prism.gateway.cli call search_context --as cash_ops_emea --args '{"question": "open breaks by entity"}'
uv run python -m prism.gateway.cli call run_metric --as cash_ops_emea --args '{"metric_id": "open_breaks", "dimensions": ["region"]}'
uv run python -m prism.gateway.cli call query_source --as head_data --args '{"source": "cashrecon", "request": {"sql": "SELECT region, count(*) FROM breaks GROUP BY region"}}'
uv run python -m prism.gateway.cli call get_rows --as cash_ops_emea --args '{"handle": "r_...", "offset": 0, "limit": 50}'
uv run python -m prism.gateway.cli call combine --as head_data --args '{"sql": "SELECT ccy, sum(value) AS total FROM a GROUP BY ccy", "handles": {"a": "r_..."}}'
uv run python -m prism.gateway.cli call record_answer --as head_data --args '{"question": "Open breaks by region?", "plan": "run_metric", "handles": ["r_..."], "verified": true}'
```
Handles belong to the `sub` that created them (the CLI's `sub` is the persona id) and live 15 minutes in the running
gateway process; another caller's handle answers `unknown_handle`.

Security notes:
- The gateway is the only door: agents never get source tokens, SQL credentials or graph access. Source tokens are
  minted per call from the verified claims (claim whitelist), never forwarded.
- Role checks run in four places that a test keeps in agreement: Python `can()`, the SQL RLS functions, the graph's
  Cypher gate and the gateway policy. A metric or handle the caller may not see answers exactly like one that does
  not exist.
- Metrics-only callers (`bi_analyst`): no `query_source`, no sensitive dimension, no identifier x date grain.
- `combine` runs one guarded SELECT in a locked-down in-memory DuckDB (no files, network or extensions; 5 s, 10,000
  rows); amounts are never summed across currencies (`currency_mixing`).
- Audit and query log hold catalog names, counts and codes; the question text only in `app.query_log` (when
  `PRISM_STORE_QUESTIONS` is on), as an HMAC elsewhere. Every secret is a `SecretStr`; `PRISM_ENV=production` refuses
  every dev default. `record_answer`'s `verified` is a request (see Query history).

## Agent service (M4, port 8000, `PRISM_AGENT_PORT`)
FastAPI service in front of the gateway: it answers a question by driving the gateway tools as the caller (the caller's
own token is forwarded; the agent holds no data credentials) and streams the result as Server-Sent Events. Started with
everything else by `scripts/start_backend.sh`, or alone with `make agent` (needs the gateway up). Without
`ANTHROPIC_API_KEY` it still starts: `/kpis`, `/results` and `/healthz` work and `/chat` answers `503`.
```bash
T=$(curl -s -XPOST localhost:8000/dev/token -H 'content-type: application/json' -d '{"persona_id":"head_data"}' | jq -r .token)
curl -s localhost:8000/kpis -H "Authorization: Bearer $T"                    # four dashboard tiles for the persona
curl -s "localhost:8000/results/r_0123456789ab?offset=0&limit=50" -H "Authorization: Bearer $T"   # page a result handle
curl -sN -XPOST localhost:8000/chat -H "Authorization: Bearer $T" -H 'content-type: application/json' \
  -d '{"question":"Which legal entity has the most aged USD breaks?"}'       # SSE: plan* widget* summary telemetry (needs the key)
```
- `GET /healthz` (public) · `POST /dev/token` · `POST /chat` (SSE) · `GET /kpis` · `GET /results/{handle}`; all but the
  first two need `Authorization: Bearer <persona JWT>` (audience `gateway-mcp`).
- Dev token security: `/dev/token` mints a persona token with the real JWT secret, so it is off by default. It answers
  only when `PRISM_AGENT_DEV_TOKEN_ENABLED=true` AND `PRISM_ENV` is not `production` AND the client is loopback; otherwise
  `404`. `scripts/start_backend.sh` turns it on by default for the local launcher (it binds 127.0.0.1 only) unless the environment or `.env` sets `PRISM_AGENT_DEV_TOKEN_ENABLED=false`; it also refuses an agent port that is 0, above 65535 or equal to the gateway port. `make agent` does not enable it.
- Tests: the default suite covers the agent with a scripted model and a fake gateway. `make test-live` (stack up) also
  drives the planted stories (aged USD breaks, SRC001 late feeds) through `/chat` with the scripted model, checks the
  persona matrix and handle isolation, and asserts every KPI tile in `backend/prism/agent/kpis.yaml` answers for its
  persona. Live runs write smoke rows into the real app database (`app.audit_log`, `app.agent_runs`, `app.query_log`).

## Tests
```bash
make test        # needs Docker (starts Postgres); live checks are deselected (pyproject addopts -m "not live")
make test-fast   # pure-Python tests only
make test-live   # live end-to-end checks through the running gateway (pytest -m live)
make clean-test-dbs  # drop leftover test databases (testapp_*, testappnew_*, testmig_*, testhist_*, testhistcli_*)
```
The suite loads its context graph into the `prism_test` namespace (never the configured `prism` one, whose distilled
history a test load would otherwise supersede) and drops it, and the test databases it creates, at session end.
`pytest -m live` (`backend/tests/gateway/test_e2e_live.py`) reproduces the four planted stories through the gateway
only (Vendor A x Corp bond 60 of 90 price conflicts this week, total +20% week over week; SRC001 late feeds;
PF001/PF002/PF005 position exceptions per day; LE00016 64 of 79 aged USD breaks), plus a six-call fan-out, the
`bi_analyst` refusals and `combine`. It skips unless the gateway, the five source MCP servers, Neo4j and Postgres
answer AND the databases hold the full profile (`seed_info.profile`); it never re-seeds. The probe and every call
use `PRISM_GATEWAY_URL`, which must be a loopback URL (the tokens are minted with the real JWT secret).
