# M7 — Context graph view of an answer

Date: 2026-10-02. Parent spec: `2026-09-30-agentic-data-intelligence-design.md` (§4 context graph, gateway).
Follows: `2026-10-02-m6-evals-verified-design.md`. Later step (separate spec): the per-run reasoning trace, shown as
a second tab of the dialog defined here.

## 1. Goal and scope
Let a user see, for any widget on the canvas, the part of the context graph its data came from: the governed
metric, the dimensions it was grouped by, the tables, endpoints, fields and columns behind them, the source, the
business terms that define the metric and confirmed past questions that used it. Rendered as an interactive
force-layout graph (reference look: a Neo4j-Bloom-style node-link view).

**In scope:**
- Gateway: a new read-only tool `lineage(handle)` (the eighth tool), role-gated by the existing `gate()`.
- Agent: `GET /lineage/{handle}`, a pass-through like `GET /results/{handle}`.
- UI: a "Context graph" button on every widget card and a large dialog with the graph, a node detail panel and a
  text list view.

**Out of scope:**
- The reasoning trace (next step).
- Lineage for `query_source` results beyond their source (no SQL parsing).
- Lineage keyed by metric id or recipe (lineage of an expired handle). Decision: handle-keyed only, see §2.1.
- Exposing `lineage` to the LLM agent.
- Editing the graph or navigating beyond the answer's subgraph.

**Success:**
- From an answer's widget, one click shows its lineage graph within 1 s on the demo data.
- The graph never shows a node the caller's role cannot see through `search_context`; a metrics-only caller sees no
  Table, Column, Field or Endpoint and no sensitive dimension.
- Another caller's handle, an expired handle and a forbidden handle are indistinguishable (`unknown_handle` / 404).

## 2. Gateway (`prism/gateway`, `prism/graph`)

### 2.1 Tool contract
`lineage(handle: str) -> {nodes, edges, truncated, governed}`

- **Keyed by the caller's own live handle** (decision A). The owner check is the result store's: `store.get(sub,
  handle)`; any miss is `unknown_handle`. No metric id is accepted from the caller, so the tool cannot probe the
  catalog. Consequence, accepted: after the 15-minute handle lifetime the graph is unavailable ("expired"), exactly
  like the card's rows; a reopened dashboard re-runs and mints fresh handles.
- Arguments model in `ARG_MODELS` (`handle` matches the existing handle pattern); `READ_ONLY` annotation; one
  `app.audit` row per call (catalog names and counts only, as for every tool).
- Rate limit: shares the per-caller `search_context` budget (2 in flight, 60 a minute): both are graph reads.
- Errors: `unknown_handle`, `context_unavailable` (graph down or timeout), `rate_limited`, `gateway_busy`.

### 2.2 What the handle resolves to
A new helper next to `_structured_plan`, `_lineage_plan(sub, handle)`, walks the same way (the handle, then the
`inputs` of combine outputs that are still stored, at most 64 handles) but keeps the pairing per handle:

- `run_metric` result: `(metric_id, dimensions used)`; only ids in the catalog, only dimensions of that metric.
- `query_source` result: its `source` only (no governed lineage).
- `combine` result: a synthetic root plus the plans of its live inputs; an expired input is skipped silently.

`governed` in the response is true when at least one metric was resolved.

### 2.3 Subgraph (one role-gated Cypher read, `prism/graph/lineage.py`)
For each `(metric, dims)`; every matched node passes `gate()` (namespace, scopes, never `Role`, no physical schema or
sensitive dimension for metrics-only callers, Question/Execution only with all USED objects visible):

| Node kind | Pattern |
|---|---|
| Metric | `(m:Metric {id})` |
| Dimension | `(m)-[:HAS_DIMENSION]->(d:Dimension)` with `d.name IN dims` |
| Column, Table | `(d)-[:ON_COLUMN]->(c:Column)<-[:HAS_COLUMN]-(t:Table)`; `(m)-[:COMPUTED_FROM]->(t:Table)` |
| Endpoint, Field | `(m)-[:COMPUTED_FROM]->(e:Endpoint)`, `(e)-[:BACKED_BY]->(t)`, `(e)-[:RETURNS]->(f:Field)-[:MAPS_TO]->(c)` for used columns `c` only |
| Source | `(s:Source)-[:HAS_TABLE|HAS_ENDPOINT]->(t|e)`; for a metrics-only caller, `(s:Source {name: m.source})` |
| BusinessTerm | `(b:BusinessTerm)-[:DEFINES]->(m)` |
| Question | up to 3 `(q:Question)-[:ANSWERED_BY]->(:Execution)-[:USED]->(m)`, seed questions first, then by count |

A `query_source` result contributes only `(s:Source {name})`, if visible. A combine contributes a root node
`{kind: "Result", label: "Combined result"}` with an edge `COMBINES` to each input's metric (or source).

### 2.4 Response
```json
{"nodes": [{"id": "metric:price_conflicts", "kind": "Metric", "label": "price_conflicts",
            "source": "marketmaster", "detail": "Vendor price quotes deviating from the golden price."}],
 "edges": [{"from": "metric:price_conflicts", "to": "dimension:...", "type": "HAS_DIMENSION"}],
 "truncated": false, "governed": true}
```
- `id` is the node's `local_uid` (never the internal `uid`); `kind` is one of Metric, Dimension, Column, Table,
  Endpoint, Field, Source, BusinessTerm, Question, Result.
- `detail` is a fixed per-kind property (metric/term definition, column/field type, table qualified name, question
  text), capped at 300 characters. No row values, no embeddings, no `allowed_scopes`.
- At most 150 nodes, kept in priority order Metric, Source, Dimension, Table, Endpoint, BusinessTerm, Column, Field,
  Question; edges to dropped nodes are dropped; `truncated` says so. Response well under the 64 KB tool-output budget.

### 2.5 Agent service (`prism/agent/api.py`)
`GET /lineage/{handle}` (authenticated like `/results`): handle format check (bad format: 404); calls the gateway's
`lineage`; `unknown_handle` / `not_permitted`: 404; `rate_limited`: 429; any other gateway error: 502 with a fixed
message. `lineage` is NOT added to `GATEWAY_TOOL_NAMES` or any LLM tool list.

## 3. UI (`frontend`)

### 3.1 Entry point
`WidgetCard` footer: a "Context graph" button (network icon) beside "View query · source rows". Disabled while the
card has no live result (loading, not permitted, unavailable, expired).

### 3.2 Dialog (`components/ContextGraphDialog.tsx`)
- Large dialog (about 90% of the viewport) on the existing `ui/dialog`; title "Context graph" plus the widget title.
- States: loading (skeleton), ok, expired (404: the existing `EXPIRED_TEXT`), error (502/network: "The context
  graph is unavailable." with Retry), empty ("No context is available for this result."), 401 through the session's
  sign-out path.
- Notes: "Free-form query: no governed lineage" when `governed` is false; "Graph shortened to 150 nodes" when
  `truncated`.
- Body: graph canvas (left), node detail panel (right, opens on node click: kind, label, source, detail, neighbours
  list), and a collapsible "List view" listing nodes grouped by kind (screen readers; e2e assertions).

### 3.3 Graph (`components/ContextGraph.tsx`, `lib/graph.ts`)
- ECharts `graph` series, `layout: "force"`, `roam: true`, `draggable: true`; a legend by kind (click hides a kind);
  "Reset layout" re-seeds the force layout.
- `lib/graph.ts` is a pure function `toGraphOption(lineage) -> EChartsOption` (unit-tested, like `lib/charts.ts`):
  - Size: Metric 56, Result 48, Source 44, Table/Endpoint 36, Dimension/BusinessTerm/Question 28, Column/Field 20.
  - Colour: Metric and Result navy; Source, Table, Endpoint the source's `hex`; Dimension, Column and Field a tint
    of their source colour; BusinessTerm green `#2f8f83`; Question orange `#d08a1c`.
  - Labels on nodes (truncated to 18 characters, full text in the tooltip); edge type shown on hover only.

## 4. Testing
- **Gateway** (pytest; graph tests in the `prism_test` namespace, comparing `local_uid`):
  owner check (another `sub`'s handle: `unknown_handle`); metrics-only caller gets no Table/Column/Field/Endpoint and no
  sensitive dimension; a role without a source's scope gets none of its nodes; a combine merges its inputs' lineage
  and skips an expired input; a `query_source` handle yields only its Source and `governed: false`; the 150-node cap
  and priority; one audit row per call; `lineage` is registered with `READ_ONLY` and absent from the agent's LLM tools.
- **Agent:** `/lineage/{handle}` maps 404, 429 and 502; bad handle format is 404; unauthenticated is 401.
- **Frontend:** `lib/graph.ts` (sizes, colours, edges, labels); dialog states (loading, ok, expired, error + retry,
  empty, notes); the button is disabled without a live result; Playwright smoke: open the graph, assert the list view.
- **Live check:** one steward question against the running stack; open the graph; screenshot for review.

## 5. Risks
- **Graph size on wide metrics:** capped at 150 nodes with priority; demo metrics stay far below.
- **Readability of force layouts:** fixed sizes per kind and hover-only edge labels; the list view is the fallback.
- **Gate drift:** the Cypher reuses `gate()` itself (no copy), so the role test that keeps Python `can()`, SQL RLS and
  the graph in agreement covers it.
