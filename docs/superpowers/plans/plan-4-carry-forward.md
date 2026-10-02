# Plan 4 carry-forward (for M5 UI and M6)

## SSE contract: `POST /chat`
Request `{"question": "<1..2000 chars>"}`, header `Authorization: Bearer <persona JWT, audience gateway-mcp>`.
Response `text/event-stream`; each frame is `event: <type>\ndata: <json>\n\n` and the JSON repeats `type`.
Order: `plan*`, `widget*`, `summary`, `telemetry`; or `... error`, `telemetry`. `telemetry` is always last. Before the
stream starts: `401` (bad or missing token), `422` (body), `503` (no `ANTHROPIC_API_KEY`).

```
event: plan
data: {"type": "plan", "tool": "run_metric", "label": "metric open_breaks"}

event: widget
data: {"type": "widget", "widget": {"id": "w1", "type": "bar", "title": "Open breaks by region", "handle": "r_aaaaaaaaaaaa", "encoding": {"x": "region", "y": "value", "series": null, "value": null, "unit": null}}, "handle_info": {"columns": ["region", "value"], "row_count": 4, "source": "cashrecon", "metric_id": "open_breaks"}}

event: summary
data: {"type": "summary", "text": "EMEA has the most open breaks."}

event: telemetry
data: {"type": "telemetry", "run_id": "9f0c...", "path": "metric", "models": ["claude-sonnet-5-5", "claude-haiku-4-5-20251001"], "input_tokens": 5120, "output_tokens": 640, "cache_read_input_tokens": 3900, "llm_turns": 5, "tool_calls": 4, "tool_latency_ms": 812.4, "cost_usd": 0.0312}

event: error
data: {"type": "error", "code": "run_limit", "message": "That question needed more steps than allowed. Try a narrower question."}
```
- `plan.tool` is `run_metric | query_source | combine`; `label` is `metric <id>`, `query <source>` or `combine`.
- `path` in telemetry: `metric | direct | delegated`. `cost_usd` is an estimate.
- Error codes: `invalid_question`, `model_unavailable`, `model_truncated`, `run_limit`, `gateway_unavailable`,
  `internal_error`. Messages are fixed text (no provider or gateway detail). A `summary` alone (no widgets) is a normal
  answer to a question that produced no handle, including a refusal ("not permitted") or a model refusal.
- Widgets never carry rows: fetch them with `GET /results/{handle}`. When the visualizer fails the harness emits one
  `table` widget over the last handle (a fallback), so there is always a way to see the data.
- A client disconnect cancels the run; the supervisor task is stopped before the gateway session closes and the run is
  still written to `app.agent_runs` (`error_code: client_disconnected`).

## `GET /kpis`
`{"tiles": [{"label": "Open breaks", "metric_id": "open_breaks", "unit": "", "status": "ok", "value": 12}]}`.
`status` is `ok | unavailable`; an unavailable tile has no `value` and no message. Tiles come from
`backend/prism/agent/kpis.yaml` (per role, max 4; optional `unit`, `dimensions`, `filters`, `scale`). Percent tiles are
already scaled to 0..100 (`unit: "%"`); the amount tile is the USD total (`unit: "USD"`). `502` when the gateway is
unreachable. **Cache invariant:** 60 s per `(sorted roles, scope digest)`; the digest hashes the token's `scopes`,
`rows` and `metrics_only` claims, i.e. everything the gateway enforces besides roles. Never key a cache on less. A
total outage is not cached.

## `GET /results/{handle}?offset=0&limit=50`
`handle` must match `r_[0-9a-f]{12}`, `limit` 1..200. Returns the gateway's `get_rows` page
`{"handle", "columns", "offset", "row_count", "rows"}` as the caller (their own token). `404` for an unknown handle
or one the caller may not read (the two are indistinguishable), `502` when the gateway is down.

## `POST /dev/token`
`{"persona_id": "head_data"}` returns `{"token": "<JWT>"}` (1 h). Answers only when `PRISM_AGENT_DEV_TOKEN_ENABLED=true`
AND `PRISM_ENV != production` AND the client address is loopback; otherwise `404`. Local demo only: it mints with the
real JWT secret. CORS allows exactly one origin, `http://localhost:3000`, methods GET/POST, headers
Authorization/Content-Type. TrustedHost: the `Host` header must be `localhost`, `127.0.0.1` or `[::1]` (plus
`testserver` outside production, for the test client); anything else is `400`. This is the DNS-rebinding guard: a
page on another origin that resolves its own name to 127.0.0.1 cannot reach the agent. Putting the agent behind a
reverse proxy or another host name requires extending `ALLOWED_HOSTS` in `prism/agent/api.py` deliberately.

## DashboardSpec
`prism.agent.spec.SPEC_JSON_SCHEMA` (a pydantic JSON schema of `DashboardSpec`): 1..8 widgets
(`kpi|bar|stacked_bar|line|heatmap|table|pie|scatter`), each `{id, type, title, handle, encoding{x,y,series,value,unit}}`
plus a `narrative` (the spec keeps it; the streamed `summary` is the supervisor's text, falling back to the narrative).
Validated against the handles the run produced (unknown handle, unknown column, missing required encoding field). The
schema has `$defs`/`$ref`; it is sent to the model as is (inlining was deliberately not done).

## The `verified` human-confirmation gap
The agent calls `record_answer(..., verified=True)` for every delivered, non-fallback dashboard that rests on
governed-metric handles (it sends only those handles). The gateway decides whether to accept the claim and distils
only metric-backed history, but nothing asks a human. M6 should add a thumbs-up/confirm step and send `verified`
only after it. Closed in Plan 6: see plan-6 carry-forward.

## Spec deltas (what the design said and what was built)
- Escalation: the spec escalates to the stronger model on multi-source questions or low confidence; we escalate once,
  only when the supervisor hits a run limit (`RunLimitExceeded`: turns, tool calls, wall clock).
- Visualization check "chart type suits the column types" is not possible: gateway summaries carry column names only,
  no types. Only existence of handles/columns and required encodings are checked.
- No semantic answer cache; that is M6.
- Summary text is the supervisor's final text (it saw the sample rows), not the spec narrative.

## First real-API-call risks (never exercised: every test is scripted)
- Adaptive thinking, `max_tokens` (now 16000, thinking tokens count toward it) and `effort` are not set explicitly;
  tune after a first real run. `max_tokens` with no tool call surfaces as `model_truncated`.
- A forced `tool_choice` is rejected by some 5.5 models. Forced calls are only the visualize subagent, which runs on
  `agent_subagent_model` (Haiku 4.5). Keep it on Haiku or add a guard that drops the force.
- A prompt below a model's minimum cacheable prefix never caches (the Haiku subagent prompts are short); the supervisor
  prompt plus tools is the cached part. Check `cache_read_input_tokens` in telemetry.
- The cost table is an estimate, not verified against the price list. Model ids are settings
  (`PRISM_AGENT_SUPERVISOR_MODEL`, `..._ESCALATION_MODEL`, `..._SUBAGENT_MODEL`).
- Thinking blocks are replayed unchanged (opaque `RawBlock`); a history with a thinking block must never be edited.
- Provider errors are logged (logger `prism.agent`) with model id, status, error type and a 300-char message; they
  never reach the client.

## Known limits
- The result-handle store lives in the gateway process: a gateway restart invalidates every handle (`/results` then
  404s, dashboards already rendered cannot page).
- Gateway per-caller limits (2 `search_context`, 1 `combine`, running plus queued) are respected by queuing in the
  agent; a different agent instance for the same caller would not share that queue.
- The live suite writes `agent-live-*` rows into the real app database.
- One shared `KpiService` per process; the cache is in memory.
