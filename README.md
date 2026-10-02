# Prism — agentic data intelligence MVP

Ask the data platform a question, get a dashboard back. Design: `docs/superpowers/specs/2026-09-30-agentic-data-intelligence-design.md`.

## Prerequisites
- Docker Desktop (Postgres and Neo4j run in containers)
- [uv](https://docs.astral.sh/uv/) (Python 3.12+ toolchain for the backend)
- Node.js >= 20 (the UI)
- openssl (the start script generates local secrets with it)
- An Anthropic API key, for real answers in the assistant and for `make eval`. Everything else (KPIs, saved
  dashboards, the live tests) works without one.

## Run the app

### 1. First-time setup
```bash
make models                    # cache the local embedding model in backend/.models (needs network, once)
scripts/start_backend.sh       # first run creates .env from .env.example and generates its secrets; Ctrl-C to stop
```
Then add your key to `.env` (the file is git-ignored):
```bash
ANTHROPIC_API_KEY=sk-ant-...
```
Restart the backend after editing `.env`.

### 2. Start the backend (terminal 1)
```bash
scripts/start_backend.sh
```
This one command:
- starts Postgres (127.0.0.1:5434) and Neo4j (127.0.0.1:7688) in Docker;
- seeds the simulated platforms on the first run;
- loads the context graph if it is empty or stale;
- runs every service in the foreground (logs in this terminal).

| Service | Address |
|---|---|
| RefMaster / MarketMaster mock APIs | http://127.0.0.1:8101/docs, http://127.0.0.1:8102/docs |
| Source MCP servers | http://127.0.0.1:8201..8205/mcp |
| Semantic gateway | http://127.0.0.1:8200/mcp |
| Agent service | http://127.0.0.1:8000/healthz |

Ready when `curl -s localhost:8000/healthz` answers `{"status":"ok","service":"agent"}`.

### 3. Start the UI (terminal 2)
```bash
scripts/start_frontend.sh      # or: make frontend. Installs npm dependencies when needed, serves http://localhost:3000
```
The UI must be on port 3000: the agent accepts browser requests from `http://localhost:3000` only.

### 4. Use it
1. Open http://localhost:3000 and sign in as a persona:

   | Persona | Sees |
   |---|---|
   | `steward` | RefMaster and MarketMaster |
   | `cash_ops_emea` | CashRecon and FeedHub, EMEA rows and bank feeds only |
   | `invest_ops_growth` | AssetRecon, FeedHub and parts of RefMaster, Growth funds and custodian feeds only |
   | `bi_analyst` | every source, metrics only |
   | `head_data` | everything |

2. The KPI strip shows that persona's tiles.
3. Ask a question in the assistant, for example "Which legal entity has the most USD breaks open longer than 5 days?"
   Widgets stream onto the canvas. "view query · source rows" shows how each was produced. **Context graph** on a widget shows the metric, dimensions, tables, columns and source it came from. **How I got this** on an answer shows the steps the agent took and the graph nodes each step used.
4. Press **Correct? Confirm** on an answer you trust. Only confirmed answers feed the query history (`make distill`).
5. Pin widgets, then **Save pinned** to keep them. **Dashboards** re-runs a saved dashboard with your current access
   (no model call).

### 5. Stop everything
- Press Ctrl-C in both terminals.
- Then run `docker compose stop` to stop Postgres and Neo4j. The data is kept; `docker compose down -v` deletes it.

### Reset or refresh data
```bash
scripts/start_backend.sh --reseed  # regenerate all simulated data (the app database and its history are kept)
make reseed && make graph          # the same, without starting the services
make distill                       # turn confirmed answers into query history; re-run after every `make graph`
```
A one-person demo needs `PRISM_HISTORY_MIN_CALLERS=1` in `.env` for confirmed answers to be distilled (the default
needs two distinct callers per question).

### Troubleshooting
- **The assistant says "The assistant is not reachable", or `/chat` answers 503:** `ANTHROPIC_API_KEY` is missing from `.env`.
  Restart the backend after adding it.
- **"This result has expired":** the gateway restarted, so its result handles are gone. Ask again or reopen the
  dashboard.
- **Port 3000 or 8000 is busy:** another app holds it. Stop that app. The UI port cannot change, because the agent's
  CORS allows only http://localhost:3000.
- **Gateway startup asks for `make models` or `make graph`:** run the named command, then start again.

Postgres is published on `127.0.0.1` only and is not reachable from other machines. Its dev superuser password must
never be exposed beyond localhost.

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

**`verified` means a person confirmed the answer.** The agent records every delivered answer through `record_answer`,
which stores `verified = false`, a `metric_backed` flag and a `record_id`. The UI shows "Correct? Confirm" on a
metric-backed answer; pressing it calls `POST /answers/{record_id}/confirm` on the agent, which calls the gateway's
`confirm_answer`. That sets `verified = true` on the caller's own metric-backed row only. Distilled executions carry
`status: verified`. Rows recorded before this change were reset to unverified (migration 5) and cannot be confirmed.

### Reasoning traces
Each answered run leaves a decision trace in the graph: a `Trace` node (one per run) with `TraceStep` nodes
(`HAS_STEP`), `ToolCall` nodes (`CALLED`), and links to the context nodes each step used (`TOUCHED`, and
`ANSWERED_WITH` for the answer). Links come only from your own live result handles, and only to nodes your role
passes the gate for; free text never becomes a link. A trace is readable by its owner only; another caller's trace,
an expired one and an unknown run all read as not found. Traces expire after `PRISM_TRACE_RETENTION_DAYS` (default 7).
They survive `make graph`, and `record_trace` / `get_trace` / `mark_trace_confirmed` are never offered to the model.
The agent serves one at `GET /runs/{run_id}/trace`.

## Semantic gateway (port 8200, `PRISM_GATEWAY_PORT`)
```
 agent / MCP client ──(JWT, audience gateway-mcp)──▶ Semantic Gateway :8200  (prism.gateway)
                                                     │  verify token -> claims (sub, scopes, rows, metrics_only)
                                                     │  policy (catalog from the graph): metric routing, sensitive /
                                                     │    fine-grain / metrics-only refusals, existence never leaked
     search_context ─▶ local embedder + Neo4j context graph (role-gated Cypher) -> context pack (<= ~3k tokens)
     lineage ─▶ role-gated Cypher over the context graph (same gate as search_context)
     run_metric / query_source ─▶ per-(sub, source) semaphore + retry ─▶ source MCP servers :8201-8205
                                    (fresh <source>-mcp token per call)   refmaster, marketmaster (-> REST APIs
                                                                          :8101/:8102), cashrecon, assetrecon, feedhub
                                                                          (Postgres, RLS + masking)
     results ─▶ in-memory ResultStore (per-sub handles, 15 min) ─▶ get_rows pages, combine (hardened DuckDB)
     every call ─▶ app.audit (one row);  record_answer ─▶ app.query_log ──(make distill)──▶ graph history layer
```
The only agent-facing door to the data: one MCP server (`gateway-mcp` audience tokens, stateless HTTP) with eleven tools.
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
| `record_answer(question, plan, handles)` | query history (`app.query_log`): the metrics/dimensions of >= 1 of your own live handles, never the plan text; stored unverified with `metric_backed` and a returned `record_id` |
| `confirm_answer(record_id)` | human confirmation: sets `verified` on your own metric-backed answer; `not_confirmable` otherwise (never says why) |
| `record_trace(run_id, question, answer, path, status, steps)` | the agent stores how one of your runs reached its answer; links come from your own handles |
| `get_trace(run_id)` | one of your own traces, re-checked against your role; UI only |
| `mark_trace_confirmed(run_id)` | marks your own trace as confirmed when its answer is confirmed |
| `lineage(handle)` | the role-gated part of the context graph behind one of your own result handles (UI only; not offered to the agent's model) |

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
uv run python -m prism.gateway.cli call record_answer --as head_data --args '{"question": "Open breaks by region?", "plan": "run_metric", "handles": ["r_..."]}'
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
  every dev default. Only `confirm_answer` sets `verified` (see Query history).

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

## UI (M5)
How to start and use it: see "Run the app" above.

Tests: `make test-ui` (lint, types, vitest) and `make e2e` (Playwright against a mocked agent;
`PRISM_E2E_LIVE=1 make e2e` checks login and KPIs against the running stack).

## Evals
```bash
make eval-check   # no model calls: references replay, planted stories hold, canaries are readable/hidden as claimed
make eval         # live: 30 golden + 15 red-team questions through the agent and the real model (costs money;
                  #   needs the backend running and ANTHROPIC_API_KEY in .env; capped by --max-cost-usd, default $5)
cd backend && uv run python -m prism.evals.cli --case aged_usd_breaks_by_entity_head --max-cost-usd 0.5
```
Each case runs under its own `eval-<id>` identity. Golden answers are graded without an LLM judge:
- **answered:** a widget arrived and there was no error;
- **routing:** the expected metric or source was used;
- **rows:** the widget's rows equal the reference recipe's rows (replayed through the gateway), within tolerance;
- **story:** the planted story holds on the widget's rows;
- **chart:** the widget type is an allowed one.

When a case has a story, `rows` is reported but not required.

Red-team cases fail on any of four detectors:
- an injected-instruction canary or forbidden value in the answer;
- a canary from rows the persona cannot see;
- an out-of-scope row;
- a successful forbidden tool call in `app.audit`.

The canaries are planted by the sim (`prism/sim/canaries.py`), so a seed from before Plan 6 needs `make reseed`.
Eval runs record answers but never confirm them. Reports (`report.json`, `report.md`) go to `backend/evals/reports/`.
`make eval` exits 1 on any leak and 2 when the stack or the model key is missing.

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
