# M8 — Reasoning trace (decision trace) of an answer

Date: 2026-10-02. Parent spec: `2026-09-30-agentic-data-intelligence-design.md`. Builds on
`2026-10-02-context-graph-design.md` (M7: `lineage(handle)`, the context graph dialog).
References: Neo4j Agent Memory reasoning model (ReasoningTrace / ReasoningStep / ToolCall / TOUCHED),
https://neo4j.com/labs/agent-memory/ and https://neo4j.com/blog/agentic-ai/context-graph-decision-traces/.

## 1. Goal and scope
Record, per agent run, a decision trace — the steps the agent took, the tools it called with what arguments and
results, the short notes it wrote along the way, the final answer — in Neo4j, linked to the context-graph nodes the
run actually used. Show it to the person who asked as a "Reasoning" timeline next to the context graph, with the
touched nodes highlighted in the graph.

**Decisions (from brainstorming):**
- Readers: **the asker only** (owner-bound; 7-day retention).
- Store: **Neo4j**, trace nodes beside (never inside) the catalog.
- Writer: **the gateway** (new tools `record_trace`, `get_trace`); the agent holds no graph credentials.

**In scope:** gateway tools and Cypher; agent step collection, trace write, `GET /runs/{run_id}/trace`, confirm
marks the trace; UI "How I got this" link, Graph / Reasoning tabs, timeline, highlight ring.

**Out of scope:** precedent search / agent reuse of traces (`get_similar_traces`); reviewer access to other users'
traces; extended thinking; traces for saved-dashboard re-runs (no model call).

**Success:**
- After an answer, "How I got this" shows its steps within 1 s on the demo stack.
- No caller can read, overwrite or link into another caller's trace; trace nodes never appear in `search_context`,
  `lineage` or the catalog, and survive `make graph`.
- A trace never links to a node the reader cannot see through `search_context` today.

## 2. Graph model (`prism/graph/traces.py`)
Trace nodes carry NO `:Ctx` label (so the loader's supersede, `gate()`, retrieval, lineage and the catalog never
match them) and an `ns` property (test isolation).

```
(:Trace {run_id, sub, ns, question, answer, path, status, confirmed, created_at, expires_at})
  -[:HAS_STEP]->(:TraceStep {seq, parent, kind, label, note, considered, ms, status, error_code, touched})
       -[:CALLED]->(:ToolCall {tool, args_json, handle, rows, truncated})
  (:TraceStep)-[:TOUCHED]->(catalog node: Metric | Source | Dimension | Table | Endpoint | Column | Field)
(:Trace)-[:ANSWERED_WITH]->(:Metric)
```
- `kind` ∈ context, metric, query, combine, delegate, visualize, answer, refusal, error.
- `parent`: seq of the enclosing `delegate` step, or null.
- `touched`: the step's touched local_uids as a list property (survives a reload that deletes a catalog node).
- `considered`: metric ids a `search_context` step's pack offered (plain strings, never links).
- Constraint: `Trace.run_id` unique per `ns` (`CREATE CONSTRAINT trace_run IF NOT EXISTS FOR (t:Trace) REQUIRE
  (t.ns, t.run_id) IS UNIQUE`), created by the gateway at startup (idempotent) and by `make graph`.

## 3. Gateway (`prism/gateway`)
### 3.1 `record_trace(run_id, question, answer, path, status, steps[])`
- `run_id` matches the agent's run-id pattern; `sub` from the verified token; another caller's existing `run_id`
  → `invalid_request` (the same message as a malformed request); the caller's own existing `run_id` → replaced.
- Each step: `{seq, parent?, kind, label, note?, considered?, ms?, status?, error_code?, tool?, args?, handle?}`.
  Caps: ≤ 40 steps; `label` ≤ 200, `note` ≤ 500, `args` JSON ≤ 2000, `question`/`answer` ≤ 2000, `considered` ≤ 20
  ids; whole arguments ≤ 64 KB (existing). Over-long text is truncated, not refused; more than 40 steps is refused.
- Links: for a step with a `handle`, the gateway resolves the handle with the caller's store (`_lineage_plan`): an
  unknown / foreign / expired handle yields no links (the step is still stored). `TOUCHED` targets = the metric(s),
  the used dimensions, and the free-form sources of that handle, each matched as a `:Ctx` node in the namespace AND
  passing `gate()` for the caller. `ANSWERED_WITH` = the metrics of every step handle. `rows` / `truncated` come
  from the handle's stored result, not from the agent.
- Retention: before writing, delete the caller's traces with `expires_at < now`; `expires_at = now +
  PRISM_TRACE_RETENTION_DAYS` (default 7).
- Rate limit: the `record_answer` limiter. Audit: one row (`run_id`, step count); no text.

### 3.2 `get_trace(run_id)`
- Owner-only: `Trace {run_id, ns, sub: caller}` with `expires_at >= now`; any miss → `unknown_trace`.
- Returns `{run_id, question, answer, path, status, confirmed, created_at, steps: [{seq, parent, kind, label, note,
  considered, ms, status, error_code, tool, args, handle, rows, truncated, touched: [{id, kind, label}]}]}`.
  `touched` is re-gated: only nodes that exist and pass `gate()` for the reader now.
- Rate limit: the `search_context` limiter; graph failures → `context_unavailable`.

### 3.3 `confirm_answer` marks the trace
`confirm_answer(record_id)` keeps its contract; the agent additionally calls a gateway tool
`mark_trace_confirmed(run_id)` (owner-only, idempotent, `unknown_trace` on miss) after a successful confirm.

### 3.4 Never offered to the model
`record_trace`, `get_trace`, `mark_trace_confirmed` are absent from `GATEWAY_TOOL_NAMES`, `SUPERVISOR_TOOLS`,
`SUBAGENT_TOOLS`.

## 4. Agent (`prism/agent`)
- `RunState.steps: list[dict]`; `ToolBox` appends a step per gateway tool call, `delegate` and `visualize`
  (tool, args, handle, ms, status, error_code; `considered` for search_context from the pack's metric ids);
  subagent steps carry `parent` = the delegate step's seq.
- `MessagesRunner(observe=...)`: optional hook called with the assistant text that precedes a batch of tool uses;
  that text (≤ 500 chars) becomes the `note` of those steps.
- At the end of a run the service appends an `answer` / `refusal` / `error` step and calls `record_trace` BEFORE
  emitting telemetry; failure logs `trace_write_failed` (type only) and never changes the answer.
- `GET /runs/{run_id}/trace` → gateway `get_trace`; `unknown_trace` → 404, `rate_limited` → 429, else 502.
- `POST /answers/{record_id}/confirm` takes an optional `run_id` and, after a successful confirm, calls
  `mark_trace_confirmed` (failure ignored).

## 5. UI (`frontend`)
- Chat turn: a "How I got this" link once telemetry arrived; opens the dialog on the Reasoning tab.
- Canvas items gain `runId` (set when their turn's telemetry arrives; absent for saved-dashboard items).
- Dialog (from M7) gets tabs **Graph** | **Reasoning**.
- Reasoning tab: a timeline, one row per step (kind icon, plain label, `rows` and duration, muted note, collapsible
  args code block, error/refusal badge, chips for touched nodes; nested delegate steps indented); final row is the
  answer with a "Confirmed" badge. Clicking a chip switches to Graph with that node selected.
- Graph tab: nodes touched by the loaded trace get a highlight ring.
- States: loading; not found/expired ("This reasoning trace has expired or is not available."); unavailable +
  Retry ("The reasoning trace is unavailable."); no run ("This widget was re-run from a saved dashboard; it has no
  reasoning trace.").

## 6. Testing
- Gateway (Neo4j test namespace): owner round-trip; foreign `get_trace` and foreign `record_trace` overwrite refused
  identically; links only for the caller's own live handles (crafted foreign / expired handle → no links);
  metrics-only reader gets no physical-schema chips; re-gating on read after a scope change; retention delete and
  expired read refused; caps; trace nodes invisible to `search_context`, `lineage`, `load_catalog`, and surviving a
  graph load; one audit row per call; tools absent from all LLM tool lists.
- Agent: observe hook → notes; ToolBox steps incl. nested delegate; record before telemetry; write failure does not
  affect the answer; endpoint error mapping; confirm marks the trace.
- Frontend: timeline mapper, tab states, chip → graph selection, highlight ring, canvas `runId`; Playwright smoke.
- Live check against the running stack (the user restarts it, or authorises the restart).

## 7. Risks
- **Personal text in Neo4j** (questions, notes, SQL): owner-only reads, 7-day retention, no audit copy.
- **Catalog contamination:** no `:Ctx` label, unique constraint per `ns`, tests over every catalog read path.
- **Write latency on the answer path:** one bounded write (timeout = the gateway call timeout) before telemetry;
  failure is non-fatal.
