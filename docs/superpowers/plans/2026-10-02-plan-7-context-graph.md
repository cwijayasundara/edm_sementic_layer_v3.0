# Context Graph View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From any widget on the canvas, one click shows the role-gated part of the Neo4j context graph its data came from, as an interactive force-layout graph.

**Architecture:** A new read-only gateway tool `lineage(handle)` resolves the caller's own result handle to its metrics/dimensions (or free-form sources), runs one role-gated Cypher read built on `retrieval.gate()`, and returns `{nodes, edges, truncated, governed}`. The agent exposes it as `GET /lineage/{handle}` (not to the LLM). The UI adds a "Context graph" button on each widget card that opens a dialog rendering the graph with ECharts.

**Tech Stack:** Python 3.13, Neo4j 5.26 (Cypher, `CALL (x) {}` subqueries), FastMCP gateway, FastAPI agent, pytest; Next.js 15, React 19, ECharts 5 (`graph` series), zod, vitest, Playwright.

**Spec:** `docs/superpowers/specs/2026-10-02-context-graph-design.md`

## Global Constraints
- Lineage is keyed by the caller's own live handle only; no metric id is accepted from the caller.
- Every graph node passes `retrieval.gate()` (reuse the function, never copy its predicate).
- Node ids are `local_uid`, never `uid`; no row values, embeddings or `allowed_scopes` in responses.
- `detail` is capped at 300 characters; at most 150 nodes; priority Metric, Source, Dimension, Table, Endpoint, BusinessTerm, Column, Field, Question (a combine's Result node first of all).
- At most 3 Question nodes per metric, seed questions first.
- Another caller's, an expired and a forbidden handle are indistinguishable: gateway `unknown_handle`, agent 404 `{"detail": "not found"}`.
- `lineage` is NOT in `prism.agent.prompts.GATEWAY_TOOL_NAMES` or any LLM tool list.
- `lineage` shares the `search_context` limiter (2 in flight, 60 a minute per caller) and its timeout (`CONTEXT_TIMEOUT_S`).
- UI copy, verbatim: "Context graph", "The context graph is unavailable.", "No context is available for this result.", "Free-form query: no governed lineage", "Graph shortened to 150 nodes", "List view", "Reset layout"; expired uses the existing `EXPIRED_TEXT`.
- Commits as `cwijayasundara@gmail.com` (`git -c user.email=cwijayasundara@gmail.com commit ...`), ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Spec addition: every metric gets a synthetic `Source -[PROVIDES]-> Metric` edge (keeps a metrics-only graph connected), and a Question links with a synthetic `ASKED_ABOUT` edge straight to the metric (the Execution node is not shown).

## Review Focus
1. **A handle whose combine inputs have expired** → the graph still opens with whatever is still live (no error); pinned in Task 3 (`test_lineage_combine_skips_expired_inputs`).
2. **A metric whose dimension is sensitive, asked by a metrics-only role** → that Dimension never appears; pinned in Task 2 (`test_lineage_metrics_only_hides_schema_and_sensitive_dimensions`).
3. **Labels with no `name`/`table` (odd graph data)** → fall back to `local_uid`, never crash; pinned in Task 1 (`test_label_falls_back_to_local_uid`).
4. **Very long labels / question text** → node label truncated to 18 chars, full text in tooltip and panel; pinned in Task 5 (`shortLabel` test).
5. **Opening the dialog, closing it, reopening on another card quickly** → the earlier request's response never overwrites the later one; pinned in Task 6 (`ignores a stale response after the handle changes`).

---

### Task 1: Pure lineage assembly (`prism/graph/lineage.py`, no Neo4j)

**Files:**
- Create: `backend/prism/graph/lineage.py`
- Test: `backend/tests/graph/test_lineage.py`

**Interfaces:**
- Produces: `KINDS: tuple[str, ...]`, `MAX_NODES = 150`, `DETAIL_MAX = 300`, `MAX_QUESTIONS = 3`, `RESULT_ID = "result:combined"`,
  `build_lineage(rows: list[dict], *, combined_inputs: list[str] | None = None, max_nodes: int = MAX_NODES) -> dict`
  returning `{"nodes": [{"id", "kind", "label", "source"?, "detail"?}], "edges": [{"from", "to", "type"}], "truncated": bool}`.
  A row is `{"a": node_map | None, "t": str | None, "b": node_map | None}`; a node map has `local_uid`, `kind` and any of
  `name, qualified_name, table, endpoint_id, source, definition, description, type, text`.

- [ ] **Step 1: Write the failing tests**

```python
"""build_lineage: pure assembly of lineage rows into the UI graph (no Neo4j)."""
from prism.graph.lineage import DETAIL_MAX, RESULT_ID, build_lineage


def n(kind, uid, **kw):
    return {"kind": kind, "local_uid": uid, **kw}


M = n("Metric", "metric:open_breaks", name="open_breaks", source="cashrecon", definition="Open cash breaks")
D = n("Dimension", "dim:open_breaks.region", name="region", source=None)
C = n("Column", "column:cashrecon.breaks.region", name="region", table="breaks", source="cashrecon", type="text")
T = n("Table", "table:cashrecon.breaks", name="breaks", qualified_name="cashrecon.breaks", source="cashrecon")
S = n("Source", "source:cashrecon", name="cashrecon")


def test_nodes_and_edges_are_deduplicated_and_labelled():
    rows = [{"a": M, "t": None, "b": None}, {"a": M, "t": "HAS_DIMENSION", "b": D},
            {"a": D, "t": "ON_COLUMN", "b": C}, {"a": T, "t": "HAS_COLUMN", "b": C}, {"a": T, "t": "HAS_COLUMN", "b": C},
            {"a": S, "t": "HAS_TABLE", "b": T}, {"a": S, "t": "PROVIDES", "b": M}]
    g = build_lineage(rows)
    by_id = {x["id"]: x for x in g["nodes"]}
    assert set(by_id) == {"metric:open_breaks", "dim:open_breaks.region", "column:cashrecon.breaks.region",
                          "table:cashrecon.breaks", "source:cashrecon"}
    assert by_id["column:cashrecon.breaks.region"]["label"] == "breaks.region"
    assert by_id["table:cashrecon.breaks"]["label"] == "cashrecon.breaks"
    assert by_id["metric:open_breaks"] == {"id": "metric:open_breaks", "kind": "Metric", "label": "open_breaks",
                                           "source": "cashrecon", "detail": "Open cash breaks"}
    assert len(g["edges"]) == 5 and g["truncated"] is False
    assert {"from": "table:cashrecon.breaks", "to": "column:cashrecon.breaks.region", "type": "HAS_COLUMN"} in g["edges"]


def test_label_falls_back_to_local_uid():
    g = build_lineage([{"a": n("Column", "column:x"), "t": None, "b": None}])
    assert g["nodes"] == [{"id": "column:x", "kind": "Column", "label": "column:x"}]


def test_unknown_kinds_are_dropped_with_their_edges():
    role = n("Role", "role:steward", name="steward")
    g = build_lineage([{"a": role, "t": "CAN_READ", "b": S}])
    assert [x["id"] for x in g["nodes"]] == ["source:cashrecon"] and g["edges"] == []


def test_detail_is_capped():
    long = n("BusinessTerm", "term:break", name="break", definition="x" * 1000)
    (node,) = build_lineage([{"a": long, "t": None, "b": None}])["nodes"]
    assert len(node["detail"]) == DETAIL_MAX


def test_cap_keeps_priority_kinds_and_drops_dangling_edges():
    cols = [n("Column", f"column:c{i:03}", name=f"c{i}") for i in range(200)]
    rows = [{"a": M, "t": None, "b": None}, {"a": S, "t": "PROVIDES", "b": M}] + \
           [{"a": D, "t": "ON_COLUMN", "b": c} for c in cols]
    g = build_lineage(rows, max_nodes=10)
    kinds = [x["kind"] for x in g["nodes"]]
    assert g["truncated"] is True and len(kinds) == 10
    assert kinds[:3] == ["Metric", "Source", "Dimension"]
    ids = {x["id"] for x in g["nodes"]}
    assert all(e["from"] in ids and e["to"] in ids for e in g["edges"])


def test_combined_root_links_to_present_inputs_only():
    g = build_lineage([{"a": M, "t": None, "b": None}],
                      combined_inputs=["metric:open_breaks", "source:feedhub"])
    assert g["nodes"][0] == {"id": RESULT_ID, "kind": "Result", "label": "Combined result"}
    assert g["edges"] == [{"from": RESULT_ID, "to": "metric:open_breaks", "type": "COMBINES"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest -q tests/graph/test_lineage.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'prism.graph.lineage'`

- [ ] **Step 3: Write the implementation**

```python
"""Lineage of one result for the UI's context-graph view: the role-gated part of the context graph behind the
metrics (and free-form sources) of a result handle. `build_lineage` assembles query rows into the response; the
Cypher (lineage / alineage) applies retrieval.gate() to every node it returns. Nodes are identified by local_uid
only; no row values, embeddings or scopes leave this module."""
from __future__ import annotations

KINDS = ("Metric", "Source", "Dimension", "Table", "Endpoint", "BusinessTerm", "Column", "Field", "Question")
PRIORITY = {"Result": -1, **{k: i for i, k in enumerate(KINDS)}}
MAX_NODES = 150
DETAIL_MAX = 300
MAX_QUESTIONS = 3
RESULT_ID = "result:combined"


def _label(n: dict) -> str:
    kind, name = n["kind"], n.get("name")
    if kind == "Column" and n.get("table") and name:
        return f"{n['table']}.{name}"
    if kind == "Field" and n.get("endpoint_id") and name:
        return f"{n['endpoint_id']}.{name}"
    if kind == "Table" and n.get("qualified_name"):
        return n["qualified_name"]
    if kind == "Question" and n.get("text"):
        return n["text"]
    return name or n["local_uid"]


def _detail(n: dict) -> str | None:
    raw = {"Metric": n.get("definition") or n.get("description"), "BusinessTerm": n.get("definition"),
           "Column": n.get("type"), "Field": n.get("type"), "Endpoint": n.get("description"),
           "Table": n.get("qualified_name"), "Question": n.get("text")}.get(n["kind"])
    return str(raw)[:DETAIL_MAX] if raw else None


def _node(n: dict) -> dict:
    out = {"id": n["local_uid"], "kind": n["kind"], "label": _label(n)}
    if n.get("source"):
        out["source"] = n["source"]
    if (detail := _detail(n)) is not None:
        out["detail"] = detail
    return out


def _known(n: dict | None) -> bool:
    return bool(n) and n.get("kind") in KINDS and bool(n.get("local_uid"))


def build_lineage(rows: list[dict], *, combined_inputs: list[str] | None = None,
                  max_nodes: int = MAX_NODES) -> dict:
    """Rows {a, t, b} (t and b None for a lone node) -> {nodes, edges, truncated}. Nodes of unknown kinds are
    dropped with their edges; past `max_nodes` the lowest-priority kinds go first, and edges to dropped nodes too.
    `combined_inputs` (a combine result) adds a Result root linked by COMBINES to each input node present."""
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        a, t, b = r.get("a"), r.get("t"), r.get("b")
        for x in (a, b):
            if _known(x):
                nodes.setdefault(x["local_uid"], _node(x))
        if t and _known(a) and _known(b):
            edges[(a["local_uid"], t, b["local_uid"])] = {"from": a["local_uid"], "to": b["local_uid"], "type": t}
    if combined_inputs is not None:
        nodes[RESULT_ID] = {"id": RESULT_ID, "kind": "Result", "label": "Combined result"}
        for target in combined_inputs:
            if target in nodes:
                edges[(RESULT_ID, "COMBINES", target)] = {"from": RESULT_ID, "to": target, "type": "COMBINES"}
    ordered = sorted(nodes.values(), key=lambda x: (PRIORITY[x["kind"]], x["id"]))
    kept = ordered[:max_nodes]
    ids = {x["id"] for x in kept}
    kept_edges = sorted((e for e in edges.values() if e["from"] in ids and e["to"] in ids),
                        key=lambda e: (e["from"], e["type"], e["to"]))
    return {"nodes": kept, "edges": kept_edges, "truncated": len(ordered) > max_nodes}


__all__ = ["DETAIL_MAX", "KINDS", "MAX_NODES", "MAX_QUESTIONS", "RESULT_ID", "build_lineage"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/graph/test_lineage.py && uv run ruff check prism/graph/lineage.py tests/graph/test_lineage.py`
Expected: 6 passed; `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/graph/lineage.py backend/tests/graph/test_lineage.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(graph): assemble result lineage into a capped, labelled node-link graph

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Role-gated lineage Cypher (`lineage` / `alineage`)

**Files:**
- Modify: `backend/prism/graph/lineage.py` (append)
- Test: `backend/tests/graph/test_lineage.py` (append; `@pytest.mark.neo4j`, uses the session `context_graph` in `prism_test`)

**Interfaces:**
- Consumes: `retrieval.gate(x)`, `retrieval.gate_params(scopes, metrics_only, ns)`, `retrieval.run_read`, `retrieval.arun_read`, `retrieval.DEFAULT_TIMEOUT_S`; Task 1's `build_lineage`, `KINDS`, `MAX_QUESTIONS`.
- Produces:
  `lineage(driver, plans: Mapping[str, Iterable[str]], sources: Iterable[str], claims: dict, *, combined_inputs: list[str] | None = None, ns: str | None = None, timeout_s: float = DEFAULT_TIMEOUT_S) -> dict`
  and `async alineage(adriver, plans, sources, claims, *, combined_inputs=None, ns=None, timeout_s=DEFAULT_TIMEOUT_S) -> dict`
  (same return as `build_lineage`). `plans` maps metric id -> dimension names used. `claims` needs `scopes` and `metrics_only`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/graph/test_lineage.py`:

```python
import pytest

from prism.graph.lineage import lineage
from prism.graph.model import build_graph, visible
from prism.security.personas import PERSONAS, claims_for
from tests.graph_ns import TEST_GRAPH_NS


@pytest.fixture(scope="module")
def graph(graph_embedder):
    return build_graph(graph_embedder)


@pytest.fixture(scope="module")
def run(neo4j_driver, context_graph):
    def _run(who, plans, sources=(), **kw):
        claims = claims_for(who) if isinstance(who, str) else who
        return lineage(neo4j_driver, plans, sources, claims, ns=TEST_GRAPH_NS, **kw)
    return _run


@pytest.mark.neo4j
def test_lineage_of_a_metric_reaches_source_table_and_dimension_columns(run):
    g = run("steward", {"price_conflicts": ["vendor_id"]})
    kinds = {x["kind"] for x in g["nodes"]}
    assert {"Metric", "Source", "Dimension", "Table"} <= kinds
    ids = {x["id"] for x in g["nodes"]}
    assert "metric:price_conflicts" in ids and "source:marketmaster" in ids
    assert {"from": "source:marketmaster", "to": "metric:price_conflicts", "type": "PROVIDES"} in g["edges"]
    dims = [x for x in g["nodes"] if x["kind"] == "Dimension"]
    assert [d["label"] for d in dims] == ["vendor_id"]          # only the dimensions the result used
    assert all(not x["id"].startswith("prism_test:") for x in g["nodes"])   # local_uid, never uid


@pytest.mark.neo4j
@pytest.mark.parametrize("persona", sorted(PERSONAS))
def test_every_lineage_node_is_visible_to_the_caller(run, graph, persona):
    """The Python twin of gate() agrees: nothing in a lineage graph is hidden from that caller elsewhere."""
    claims = claims_for(persona)
    for metric in ("price_conflicts", "open_breaks", "manual_matches", "open_position_exceptions"):
        dims = sorted(graph.nodes[f"metric:{metric}"]["props"].get("dimensions") or [])   # Graph.nodes: uid -> {labels, props}
        g = run(claims, {metric: dims})
        leaked = [x["id"] for x in g["nodes"] if x["id"] not in graph.nodes or not visible(graph, claims, x["id"])]
        assert leaked == [], (persona, metric, leaked)


@pytest.mark.neo4j
def test_lineage_metrics_only_hides_schema_and_sensitive_dimensions(run):
    g = run("bi_analyst", {"manual_matches": ["matched_by", "region"]})
    kinds = {x["kind"] for x in g["nodes"]}
    assert not kinds & {"Table", "Column", "Field", "Endpoint"}
    assert "matched_by" not in {x["label"] for x in g["nodes"]}
    assert "metric:manual_matches" in {x["id"] for x in g["nodes"]}
    head = run("head_data", {"manual_matches": ["matched_by"]})
    assert "matched_by" in {x["label"] for x in head["nodes"]}   # the control: it exists


@pytest.mark.neo4j
def test_lineage_of_a_foreign_metric_is_empty(run):
    assert run("cash_ops_emea", {"price_conflicts": ["vendor_id"]})["nodes"] == []


@pytest.mark.neo4j
def test_free_form_source_yields_only_its_visible_source(run):
    g = run("head_data", {}, ["cashrecon"])
    assert g["nodes"] == [{"id": "source:cashrecon", "kind": "Source", "label": "cashrecon"}]
    assert run("steward", {}, ["cashrecon"])["nodes"] == []


@pytest.mark.neo4j
def test_questions_are_capped_and_link_to_their_metric(run):
    g = run("steward", {"price_conflicts": []})
    qs = [x for x in g["nodes"] if x["kind"] == "Question"]
    assert 1 <= len(qs) <= 3
    assert all({"from": q["id"], "to": "metric:price_conflicts", "type": "ASKED_ABOUT"} in g["edges"] for q in qs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest -q tests/graph/test_lineage.py -m neo4j`
Expected: FAIL with `ImportError: cannot import name 'lineage'`

- [ ] **Step 3: Write the implementation**

Append to `backend/prism/graph/lineage.py` (and add the import at the top: `from collections.abc import Iterable, Mapping` and
`from prism.graph.retrieval import DEFAULT_TIMEOUT_S, arun_read, gate, gate_params, run_read`):

```python
def _proj(x: str) -> str:
    return (f"{x} {{.local_uid, .name, .qualified_name, .table, .endpoint_id, .source, .definition, .description,"
            f" .type, .text, kind: [l IN labels({x}) WHERE l IN $kinds][0]}}")


_USED_DIM = "d.name IN p.dims AND " + gate("d")
LINEAGE_CYPHER = f"""
UNWIND $plans AS p
MATCH (m:Metric {{ns: $ns, id: p.metric}}) WHERE {gate('m')}
CALL (m, p) {{
  RETURN m AS a, null AS t, null AS b
  UNION
  MATCH (m)-[r:HAS_DIMENSION]->(d:Dimension) WHERE {_USED_DIM}
  RETURN m AS a, type(r) AS t, d AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[r:ON_COLUMN]->(c:Column) WHERE {_USED_DIM} AND {gate('c')}
  RETURN d AS a, type(r) AS t, c AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[:ON_COLUMN]->(c:Column)<-[r:HAS_COLUMN]-(tb:Table)
  WHERE {_USED_DIM} AND {gate('c')} AND {gate('tb')}
  RETURN tb AS a, type(r) AS t, c AS b
  UNION
  MATCH (m)-[r:COMPUTED_FROM]->(x) WHERE (x:Table OR x:Endpoint) AND {gate('x')}
  RETURN m AS a, type(r) AS t, x AS b
  UNION
  MATCH (m)-[:COMPUTED_FROM]->(e:Endpoint)-[r:BACKED_BY]->(tb:Table) WHERE {gate('e')} AND {gate('tb')}
  RETURN e AS a, type(r) AS t, tb AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[:ON_COLUMN]->(c:Column)<-[:MAPS_TO]-(f:Field)<-[r:RETURNS]-(e:Endpoint)
  WHERE (m)-[:COMPUTED_FROM]->(e) AND {_USED_DIM} AND {gate('c')} AND {gate('f')} AND {gate('e')}
  RETURN e AS a, type(r) AS t, f AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[:ON_COLUMN]->(c:Column)<-[r:MAPS_TO]-(f:Field)<-[:RETURNS]-(e:Endpoint)
  WHERE (m)-[:COMPUTED_FROM]->(e) AND {_USED_DIM} AND {gate('c')} AND {gate('f')} AND {gate('e')}
  RETURN f AS a, type(r) AS t, c AS b
  UNION
  MATCH (m)-[:COMPUTED_FROM]->(x)<-[r:HAS_TABLE|HAS_ENDPOINT]-(s:Source) WHERE {gate('x')} AND {gate('s')}
  RETURN s AS a, type(r) AS t, x AS b
  UNION
  MATCH (m)-[:HAS_DIMENSION]->(d:Dimension)-[:ON_COLUMN]->(c:Column)<-[:HAS_COLUMN]-(tb:Table)<-[r:HAS_TABLE]-(s:Source)
  WHERE {_USED_DIM} AND {gate('c')} AND {gate('tb')} AND {gate('s')}
  RETURN s AS a, type(r) AS t, tb AS b
  UNION
  MATCH (s:Source {{ns: $ns, name: m.source}}) WHERE {gate('s')}
  RETURN s AS a, 'PROVIDES' AS t, m AS b
  UNION
  MATCH (bt:BusinessTerm)-[r:DEFINES]->(m) WHERE {gate('bt')}
  RETURN bt AS a, type(r) AS t, m AS b
  UNION
  CALL (m) {{
    MATCH (q:Question)-[:ANSWERED_BY]->(e:Execution)-[:USED]->(m) WHERE {gate('q')} AND {gate('e')}
    WITH q, max(coalesce(e.count, 0)) AS n
    ORDER BY q.origin IS NOT NULL, n DESC, q.local_uid
    LIMIT $max_questions
    RETURN q
  }}
  RETURN q AS a, 'ASKED_ABOUT' AS t, m AS b
}}
RETURN {_proj('a')} AS a, t, CASE WHEN b IS NULL THEN null ELSE {_proj('b')} END AS b
"""

SOURCES_CYPHER = f"""
UNWIND $sources AS sn
MATCH (s:Source {{ns: $ns, name: sn}}) WHERE {gate('s')}
RETURN {_proj('s')} AS a, null AS t, null AS b
"""


def _params(plans: Mapping[str, Iterable[str]], sources: Iterable[str], claims: dict, ns: str | None) -> dict:
    scopes = [str(s) for s in claims.get("scopes") or []]
    return {"plans": [{"metric": m, "dims": sorted(set(d))} for m, d in sorted(plans.items())],
            "sources": sorted(set(sources)), "kinds": list(KINDS), "max_questions": MAX_QUESTIONS,
            **gate_params(scopes, bool(claims.get("metrics_only")), ns)}


def lineage(driver, plans: Mapping[str, Iterable[str]], sources: Iterable[str], claims: dict, *,
            combined_inputs: list[str] | None = None, ns: str | None = None,
            timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Role-gated lineage graph of the given metrics (with the dimensions a result used) and free-form sources."""
    params = _params(plans, sources, claims, ns)
    rows = run_read(driver, LINEAGE_CYPHER, params, timeout_s) if params["plans"] else []
    if params["sources"]:
        rows += run_read(driver, SOURCES_CYPHER, params, timeout_s)
    return build_lineage(rows, combined_inputs=combined_inputs)


async def alineage(adriver, plans: Mapping[str, Iterable[str]], sources: Iterable[str], claims: dict, *,
                   combined_inputs: list[str] | None = None, ns: str | None = None,
                   timeout_s: float = DEFAULT_TIMEOUT_S) -> dict:
    """Async twin of lineage (neo4j.AsyncDriver)."""
    params = _params(plans, sources, claims, ns)
    rows = await arun_read(adriver, LINEAGE_CYPHER, params, timeout_s) if params["plans"] else []
    if params["sources"]:
        rows += await arun_read(adriver, SOURCES_CYPHER, params, timeout_s)
    return build_lineage(rows, combined_inputs=combined_inputs)
```

Update `__all__` to add `"LINEAGE_CYPHER", "SOURCES_CYPHER", "alineage", "lineage"`.

Note: `gate('q')` already requires every USED object of a Question to be visible; `gate('e')` repeats it for the Execution.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/graph/test_lineage.py && uv run ruff check prism/graph tests/graph/test_lineage.py`
Expected: all passed (Neo4j up via `make db`); `All checks passed!`. If a statement is rejected, `GraphError` hides the
server text: rerun the Cypher in `cypher-shell` / Neo4j Browser with the printed params to see the syntax error.

- [ ] **Step 5: Commit**

```bash
git add backend/prism/graph/lineage.py backend/tests/graph/test_lineage.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(graph): role-gated lineage Cypher for a result's metrics and sources

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Gateway tool `lineage(handle)`

**Files:**
- Modify: `backend/prism/gateway/service.py` (module docstring "seven tools" -> "eight tools"; `TOOLS`; `LineageArgs`; `ARG_MODELS`; `LineageFn`; `Gateway.__init__`; `_lineage_plan`; `_lineage`; `__all__`)
- Modify: `backend/prism/gateway/server.py` (tool schema function, `_TOOL_FUNCS`, runtime wiring, docstring "seven" -> "eight")
- Test: `backend/tests/gateway/test_server.py`

**Interfaces:**
- Consumes: Task 2's `alineage(adriver, plans, sources, claims, *, combined_inputs, ns, timeout_s)`.
- Produces: gateway tool `lineage` with arguments `{"handle": str}` returning
  `{"nodes": [...], "edges": [...], "truncated": bool, "governed": bool}`;
  `LineageFn = Callable[[dict[str, set[str]], list[str], dict, list[str] | None], Awaitable[dict]]` (plans, sources, claims, combined_inputs);
  `Gateway(..., lineage: LineageFn | None = None)`.

- [ ] **Step 1: Write the failing tests**

In `test_server.py`, update `test_tool_list_is_exactly_the_gateway_tools`: add `"lineage"` to both the set and the
`TOOLS` tuple (append it last) and add `assert set(tools["lineage"].input_schema["properties"]) == {"handle"}`. Then add:

```python
from prism.agent.prompts import GATEWAY_TOOL_NAMES

GRAPH = {"nodes": [{"id": "metric:open_breaks", "kind": "Metric", "label": "open_breaks"}], "edges": [],
         "truncated": False}


class FakeLineage:
    def __init__(self, result=GRAPH):
        self.calls: list[tuple] = []
        self.result = result

    async def __call__(self, plans, sources, claims, combined):
        self.calls.append(({k: set(v) for k, v in plans.items()}, list(sources), dict(claims), combined))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_lineage_resolves_the_handles_metric_and_dimensions(settings, fake_catalog):
    fake, audit = FakeLineage(), FakeAudit()
    gw = make_gateway(settings, fake_catalog, audit=audit, lineage=fake)
    async with gateway_client(settings, gw, "cash_ops_emea") as c:
        h = body(await c.call_tool("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"]}))["handle"]
        out = body(await c.call_tool("lineage", {"handle": h}))
    assert out == {**GRAPH, "governed": True}
    (plans, sources, claims, combined), = fake.calls
    assert plans == {"open_breaks": {"region"}} and sources == [] and combined is None
    assert claims["sub"] == "cash_ops_emea" and claims["metrics_only"] is False
    assert [(r["tool"], r["status"]) for r in audit.rows][-1] == ("lineage", "ok")


async def test_lineage_refuses_another_callers_handle_like_an_expired_one(settings, fake_catalog):
    fake = FakeLineage()
    gw = make_gateway(settings, fake_catalog, lineage=fake)
    _, app = create_app(settings, gateway=gw)
    async with serving(app) as base:
        async with mcp_client(f"{base}/mcp", token(settings, "head_data")) as head:
            h = body(await head.call_tool("run_metric", {"metric_id": "open_breaks"}))["handle"]
        async with mcp_client(f"{base}/mcp", token(settings, "cash_ops_emea")) as other:
            r = await other.call_tool("lineage", {"handle": h})
            gone = await other.call_tool("lineage", {"handle": "r_000000000000"})
    assert r.is_error and "unknown_handle" in text(r)
    assert gone.is_error and text(gone) == text(r)          # another caller's handle reads exactly like a made-up one
    assert fake.calls == []


async def test_lineage_of_a_free_form_result_names_only_its_source(settings, fake_catalog):
    fake = FakeLineage({"nodes": [], "edges": [], "truncated": False})
    gw = make_gateway(settings, fake_catalog, lineage=fake)
    async with gateway_client(settings, gw, "head_data") as c:
        q = body(await c.call_tool("query_source", {"source": "cashrecon", "request": {"sql": "SELECT 1"}}))["handle"]
        out = body(await c.call_tool("lineage", {"handle": q}))
    assert out["governed"] is False
    assert fake.calls[0][:2] == ({}, ["cashrecon"]) and fake.calls[0][3] is None


async def test_lineage_of_a_combine_merges_inputs_and_names_the_root_targets(settings, fake_catalog):
    fake = FakeLineage()
    gw = make_gateway(settings, fake_catalog, lineage=fake)
    async with gateway_client(settings, gw, "cash_ops_emea") as c:
        h1 = body(await c.call_tool("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"]}))["handle"]
        h2 = body(await c.call_tool("run_metric", {"metric_id": "aged_open_breaks", "dimensions": ["ccy"]}))["handle"]
        h3 = body(await c.call_tool("combine", {"sql": "SELECT count(*) AS n FROM a, b", "handles": {"a": h1, "b": h2}}))["handle"]
        body(await c.call_tool("lineage", {"handle": h3}))
    plans, sources, _, combined = fake.calls[0]
    assert plans == {"open_breaks": {"region"}, "aged_open_breaks": {"ccy"}} and sources == []
    assert combined == ["metric:aged_open_breaks", "metric:open_breaks"]


async def test_lineage_combine_skips_expired_inputs(settings, fake_catalog):
    fake, store = FakeLineage(), ResultStore()
    gw = make_gateway(settings, fake_catalog, lineage=fake, store=store)
    async with gateway_client(settings, gw, "cash_ops_emea") as c:
        h1 = body(await c.call_tool("run_metric", {"metric_id": "open_breaks", "dimensions": ["region"]}))["handle"]
        h3 = body(await c.call_tool("combine", {"sql": "SELECT count(*) AS n FROM a", "handles": {"a": h1}}))["handle"]
        store._drop(h1)     # the input expired; the combine output is still live
        out = body(await c.call_tool("lineage", {"handle": h3}))
    assert out["governed"] is False and fake.calls[0][0] == {} and fake.calls[0][3] == []


async def test_lineage_passes_metrics_only_and_maps_graph_outages(settings, fake_catalog):
    fake = FakeLineage(GraphUnavailable("down"))
    gw = make_gateway(settings, fake_catalog, lineage=fake)
    async with gateway_client(settings, gw, "bi_analyst") as c:
        h = body(await c.call_tool("run_metric", {"metric_id": "open_breaks"}))["handle"]
        r = await c.call_tool("lineage", {"handle": h})
    assert r.is_error and "context_unavailable" in text(r) and "down" not in text(r)
    assert fake.calls[0][2]["metrics_only"] is True


def test_lineage_is_never_offered_to_the_llm():
    assert "lineage" not in GATEWAY_TOOL_NAMES
```

`store._drop(handle)` is the store's private eviction (`backend/prism/gateway/results.py:204`, used by its TTL path).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest -q tests/gateway/test_server.py -k "lineage or tool_list"`
Expected: FAIL (`unknown tool` / unexpected keyword `lineage` / tool list mismatch)

- [ ] **Step 3: Implement in `service.py`**

```python
TOOLS = ("search_context", "run_metric", "query_source", "get_rows", "combine", "record_answer", "confirm_answer",
         "lineage")
```

```python
class LineageArgs(_Args):
    handle: Annotated[str, Field(min_length=1, max_length=64, description="One of your own result handles")]
```

Add `"lineage": LineageArgs` to `ARG_MODELS`. Next to `ContextFn`:

```python
# plans (metric id -> dimensions used), free-form sources, claims (metrics_only fail-closed), combine root targets
LineageFn = Callable[[dict[str, set[str]], list[str], dict, list[str] | None], Awaitable[dict]]
```

`Gateway.__init__`: add the keyword parameter `lineage: LineageFn | None = None` and `self.lineage = lineage`.

Tool and helper (place `_lineage` after `_confirm_answer`, `_lineage_plan` after `_structured_plan`):

```python
    async def _lineage(self, claims: dict, args: LineageArgs, rec: CallRecord) -> dict:
        sub = claims["sub"]
        plans, sources, combined = self._lineage_plan(sub, args.handle)   # unknown_handle before any graph read
        rec.fields["result_handle"] = args.handle
        if self.lineage is None:
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly")
        safe = {**claims, "metrics_only": is_metrics_only(claims)}
        release = await self.context_limiter.acquire(sub)   # graph reads share the search_context budget
        try:
            async with asyncio.timeout(self.context_timeout_s):
                graph = await self.lineage(plans, sources, safe, combined)
        except (TimeoutError, GraphUnavailable, GraphError):
            raise GatewayError("context_unavailable", "the context graph is unavailable; try again shortly") from None
        finally:
            release()
        rec.fields["rows"] = len(graph["nodes"])
        return {**graph, "governed": bool(plans)}

    def _lineage_plan(self, sub: str, handle: str) -> tuple[dict[str, set[str]], list[str], list[str] | None]:
        """Per-metric dimensions and the sources of free-form results behind `handle` (combine outputs: through
        their inputs that are still stored; an expired input is skipped), plus, for a combine, the local uids its
        root links to. Raises unknown_handle for another caller's, an expired or a made-up handle."""
        top = self.store.get(sub, handle)
        catalog = self.policy.catalog
        plans: dict[str, set[str]] = {}
        sources: set[str] = set()
        seen: set[str] = set()
        todo = [handle]
        while todo and len(seen) < 64:
            h = todo.pop()
            if h in seen:
                continue
            seen.add(h)
            try:
                entry = self.store.get(sub, h)
            except GatewayError:
                continue
            plan, inputs, source = entry.meta.get("plan"), entry.meta.get("inputs"), entry.meta.get("source")
            if isinstance(plan, dict):
                for mid in plan.get("metric_ids") or ():
                    metric = catalog.metrics.get(mid)
                    if metric is not None:
                        plans.setdefault(mid, set()).update(
                            d for d in plan.get("dimensions") or () if d in metric.dimensions)
            elif isinstance(inputs, (list, tuple)):
                todo.extend(i for i in inputs if isinstance(i, str))
            elif isinstance(source, str):
                sources.add(source)
        combined = None
        if isinstance(top.meta.get("inputs"), (list, tuple)):
            combined = [f"metric:{m}" for m in sorted(plans)] + [f"source:{s}" for s in sorted(sources)]
        return plans, sorted(sources), combined
```

- [ ] **Step 4: Implement in `server.py`**

Schema function (after `confirm_answer`):

```python
async def lineage(handle: str) -> CallToolResult:
    """The part of the context graph behind one of your own result handles (metric, dimensions, tables, columns,
    source, terms, past questions), filtered to what your role may see. For the UI; never needed to answer."""
    raise NotImplementedError
```

Add `lineage` to the `_TOOL_FUNCS` tuple. Import `alineage` (`from prism.graph.lineage import alineage`). In `start()`:

```python
        async def lineage_fn(plans, sources, claims, combined):
            return await alineage(adriver, plans, sources, claims, combined_inputs=combined, ns=settings.graph_ns,
                                  timeout_s=timeout)
```

and pass `lineage=lineage_fn` to `Gateway(...)`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/gateway && uv run ruff check prism/gateway tests/gateway`
Expected: all passed (the gateway CLI test counts `len(TOOLS)`, so it follows automatically); `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add backend/prism/gateway/service.py backend/prism/gateway/server.py backend/tests/gateway/test_server.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(gateway): lineage tool: role-gated context graph of the caller's own result handle

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Agent endpoint `GET /lineage/{handle}`

**Files:**
- Modify: `backend/prism/agent/api.py` (after `results`)
- Test: `backend/tests/agent/test_api.py`

**Interfaces:**
- Consumes: gateway tool `lineage` (Task 3) through `GatewayPort.call("lineage", {"handle": h})`.
- Produces: `GET /lineage/{handle}` -> 200 `{nodes, edges, truncated, governed}`; 404 `{"detail": "not found"}`; 429 `{"detail": "too many requests"}`; 502 `{"detail": "data service unavailable"}`; 401 unauthenticated.

- [ ] **Step 1: Write the failing test**

```python
LINEAGE = {"nodes": [{"id": "metric:open_breaks", "kind": "Metric", "label": "open_breaks"}], "edges": [],
           "truncated": False, "governed": True}


def test_lineage_passthrough_and_error_mapping():
    h = {"Authorization": f"Bearer {token()}"}
    gw = FakeGateway({"lineage": LINEAGE})
    c = app_with(gw)
    assert c.get("/lineage/r_aaaaaaaaaaaa", headers=h).json() == LINEAGE
    assert gw.calls == [("lineage", {"handle": "r_aaaaaaaaaaaa"})]
    assert c.get("/lineage/not-a-handle", headers=h).status_code == 404
    assert c.get("/lineage/r_aaaaaaaaaaaa").status_code == 401
    for code, status, detail in (("unknown_handle", 404, "not found"), ("not_permitted", 404, "not found"),
                                 ("rate_limited", 429, "too many requests"),
                                 ("context_unavailable", 502, "data service unavailable")):
        r = app_with(FakeGateway({"lineage": GatewayError(code, "secret detail")})).get(
            "/lineage/r_bbbbbbbbbbbb", headers=h)
        assert (r.status_code, r.json()) == (status, {"detail": detail})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest -q tests/agent/test_api.py::test_lineage_passthrough_and_error_mapping`
Expected: FAIL (404 for the valid handle: route missing)

- [ ] **Step 3: Implement**

```python
    @app.get("/lineage/{handle}")
    async def lineage(handle: str, user: UserContext = Depends(current_user)) -> dict:
        if not HANDLE.match(handle):
            raise HTTPException(404, "not found")
        try:
            async with factory(user) as gateway:
                return await gateway.call("lineage", {"handle": handle})
        except GatewayError as exc:
            if exc.code in ("unknown_handle", "not_permitted"):
                raise HTTPException(404, "not found") from None
            if exc.code == "rate_limited":
                raise HTTPException(429, "too many requests") from None
            raise HTTPException(502, "data service unavailable") from None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest -q tests/agent && uv run ruff check prism/agent tests/agent`
Expected: all passed except the known `test_agent_settings_defaults` when `.env` overrides the model (pre-existing; it
passes with an unset `PRISM_AGENT_SUPERVISOR_MODEL`); `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add backend/prism/agent/api.py backend/tests/agent/test_api.py
git -c user.email=cwijayasundara@gmail.com commit -m "feat(agent): GET /lineage/{handle} pass-through with masked errors

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Frontend schema, API client and graph option mapper

**Files:**
- Modify: `frontend/lib/schemas.ts`, `frontend/lib/api.ts`, `frontend/lib/charts.ts` (export `tint`)
- Create: `frontend/lib/graph.ts`
- Test: `frontend/tests/graph.test.ts`

**Interfaces:**
- Consumes: `/lineage/{handle}` JSON (Task 4); `sourceById(id)?.hex` from `lib/sources.ts`; `tint(hex, amount)` from `lib/charts.ts`.
- Produces: `LINEAGE_KINDS`, `type LineageKind`, zod `Lineage` / `type Lineage` / `type LineageNode`;
  `api.lineage(token: string, handle: string): Promise<Lineage>`;
  `KIND_LABEL: Record<LineageKind, string>`, `NODE_SIZE: Record<LineageKind, number>`, `nodeColor(n: LineageNode): string`,
  `shortLabel(s: string, max?: number): string`, `toGraphOption(g: Lineage): EChartsOption` (node `name` = node id).

- [ ] **Step 1: Write the failing tests** (`frontend/tests/graph.test.ts`)

```ts
/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, expect, it } from "vitest";
import { NODE_SIZE, nodeColor, shortLabel, toGraphOption } from "@/lib/graph";
import { Lineage } from "@/lib/schemas";

const g = Lineage.parse({
  nodes: [
    { id: "metric:open_breaks", kind: "Metric", label: "open_breaks", source: "cashrecon", detail: "Open breaks" },
    { id: "source:cashrecon", kind: "Source", label: "cashrecon" },
    { id: "column:cashrecon.breaks.region", kind: "Column", label: "breaks.region", source: "cashrecon" },
    { id: "question:q1", kind: "Question", label: "Which legal entity has the most USD breaks open longer than 5 days?" },
  ],
  edges: [{ from: "source:cashrecon", to: "metric:open_breaks", type: "PROVIDES" }],
  truncated: false, governed: true,
});

describe("toGraphOption", () => {
  it("one force graph, nodes named by id, sized and categorised by kind", () => {
    const s = (toGraphOption(g) as any).series[0];
    expect(s.type).toBe("graph");
    expect(s.layout).toBe("force");
    expect(s.data.map((d: any) => d.name)).toEqual(g.nodes.map((n) => n.id));
    expect(s.data[0].symbolSize).toBe(NODE_SIZE.Metric);
    expect(s.categories.map((c: any) => c.name)).toEqual(["Metric", "Source", "Column", "Past question"]);
    expect(s.links).toEqual([{ source: "source:cashrecon", target: "metric:open_breaks", type: "PROVIDES" }]);
  });

  it("labels show a short form and tooltips the full label or the edge type", () => {
    const o = toGraphOption(g) as any;
    const q = o.series[0].data[3];
    expect(o.series[0].label.formatter({ data: q })).toBe(shortLabel(q.fullLabel));
    expect(o.tooltip.formatter({ dataType: "node", data: q })).toContain("Past question: Which legal entity");
    expect(o.tooltip.formatter({ dataType: "edge", data: { type: "PROVIDES" } })).toBe("PROVIDES");
  });
});

describe("node styling", () => {
  it("metric navy, source in its colour, column a lighter tint, question orange", () => {
    expect(nodeColor(g.nodes[0])).toBe("#14213d");
    expect(nodeColor(g.nodes[1])).toBe("#2f8f83");
    expect(nodeColor(g.nodes[2])).not.toBe("#2f8f83");
    expect(nodeColor(g.nodes[3])).toBe("#d08a1c");
  });

  it("shortLabel truncates with an ellipsis", () => {
    expect(shortLabel("open_breaks")).toBe("open_breaks");
    expect(shortLabel("x".repeat(40))).toHaveLength(18);
    expect(shortLabel("x".repeat(40)).endsWith("…")).toBe(true);
  });
});

describe("Lineage schema", () => {
  it("rejects an unknown node kind", () => {
    expect(() => Lineage.parse({ ...g, nodes: [{ id: "role:x", kind: "Role", label: "x" }] })).toThrow();
  });
});
```

Note: `nodeColor` of the Source compares against `sourceById("cashrecon")` — the Source node carries no `source`
field, so map a Source node by its label (`sourceById(n.source ?? (n.kind === "Source" ? n.label : null))`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run tests/graph.test.ts < /dev/null`
Expected: FAIL (cannot resolve `@/lib/graph`)

- [ ] **Step 3: Implement**

`lib/schemas.ts` (append):

```ts
export const LINEAGE_KINDS = ["Result", "Metric", "Source", "Dimension", "Table", "Endpoint", "BusinessTerm", "Column",
  "Field", "Question"] as const;
export type LineageKind = (typeof LINEAGE_KINDS)[number];
export const LineageNode = z.object({ id: z.string(), kind: z.enum(LINEAGE_KINDS), label: z.string(),
  source: z.string().optional(), detail: z.string().optional() });
export type LineageNode = z.infer<typeof LineageNode>;
export const LineageEdge = z.object({ from: z.string(), to: z.string(), type: z.string() });
export const Lineage = z.object({ nodes: z.array(LineageNode), edges: z.array(LineageEdge), truncated: z.boolean(),
  governed: z.boolean() });
export type Lineage = z.infer<typeof Lineage>;
```

`lib/api.ts`: import `Lineage` from schemas and add to `api`:

```ts
  async lineage(token: string, handle: string): Promise<Lineage> {
    return Lineage.parse(await json(await request(`/lineage/${encodeURIComponent(handle)}`, { token })));
  },
```

`lib/charts.ts`: change `function tint(` to `export function tint(`.

`lib/graph.ts`:

```ts
/* eslint-disable @typescript-eslint/no-explicit-any */
import type { EChartsOption } from "echarts";
import { tint } from "@/lib/charts";
import { LINEAGE_KINDS, type Lineage, type LineageKind, type LineageNode } from "@/lib/schemas";
import { sourceById } from "@/lib/sources";

export const KIND_LABEL: Record<LineageKind, string> = {
  Result: "Combined result", Metric: "Metric", Source: "Source", Dimension: "Dimension", Table: "Table",
  Endpoint: "Endpoint", BusinessTerm: "Business term", Column: "Column", Field: "Field", Question: "Past question",
};
export const NODE_SIZE: Record<LineageKind, number> = {
  Metric: 56, Result: 48, Source: 44, Table: 36, Endpoint: 36, Dimension: 28, BusinessTerm: 28, Question: 28,
  Column: 20, Field: 20,
};
const NAVY = "#14213d", TERM = "#2f8f83", QUESTION = "#d08a1c", NEUTRAL = "#5a6478";

export function nodeColor(n: LineageNode): string {
  if (n.kind === "Metric" || n.kind === "Result") return NAVY;
  if (n.kind === "BusinessTerm") return TERM;
  if (n.kind === "Question") return QUESTION;
  const base = sourceById(n.source ?? (n.kind === "Source" ? n.label : null))?.hex ?? NEUTRAL;
  return n.kind === "Source" || n.kind === "Table" || n.kind === "Endpoint" ? base : tint(base, 0.45);
}

export function shortLabel(s: string, max = 18): string {
  return s.length <= max ? s : `${s.slice(0, max - 1)}…`;
}

/** Lineage -> one ECharts force graph. Node `name` is the node id (ECharts links resolve by name). */
export function toGraphOption(g: Lineage): EChartsOption {
  const kinds = LINEAGE_KINDS.filter((k) => g.nodes.some((n) => n.kind === k));
  return {
    tooltip: { formatter: (p: any) => (p.dataType === "edge" ? p.data.type : `${KIND_LABEL[p.data.kind as LineageKind]}: ${p.data.fullLabel}`) },
    legend: { data: kinds.map((k) => KIND_LABEL[k]), bottom: 0, type: "scroll", icon: "circle" },
    series: [{
      type: "graph", layout: "force", roam: true, draggable: true,
      force: { repulsion: 220, edgeLength: [50, 120], gravity: 0.08 },
      categories: kinds.map((k) => ({ name: KIND_LABEL[k] })),
      label: { show: true, position: "bottom", fontSize: 10, color: "#3a4357", formatter: (p: any) => shortLabel(p.data.fullLabel) },
      edgeSymbol: ["none", "arrow"], edgeSymbolSize: 6,
      lineStyle: { color: "#c3cad6", width: 1, curveness: 0.08 },
      emphasis: { focus: "adjacency", lineStyle: { width: 2 } },
      data: g.nodes.map((n) => ({ name: n.id, fullLabel: n.label, kind: n.kind, category: kinds.indexOf(n.kind),
        symbolSize: NODE_SIZE[n.kind], itemStyle: { color: nodeColor(n) } })),
      links: g.edges.map((e) => ({ source: e.from, target: e.to, type: e.type })),
    }],
  } as EChartsOption;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run < /dev/null && ./node_modules/.bin/tsc --noEmit < /dev/null && npx eslint app components lib < /dev/null`
Expected: all tests pass; no type or lint errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/lib/schemas.ts frontend/lib/api.ts frontend/lib/charts.ts frontend/lib/graph.ts frontend/tests/graph.test.ts
git -c user.email=cwijayasundara@gmail.com commit -m "feat(ui): lineage schema, API client and force-graph option mapper

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Context graph dialog and the widget button

**Files:**
- Create: `frontend/components/ContextGraph.tsx`, `frontend/components/ContextGraphDialog.tsx`, `frontend/lib/copy.ts`
- Modify: `frontend/components/WidgetCard.tsx` (footer button + dialog)
- Test: `frontend/tests/contextgraph.test.tsx`, `frontend/tests/widgetcard.test.tsx`

**Interfaces:**
- Consumes: `api.lineage` and `toGraphOption`, `KIND_LABEL` (Task 5); `useSession().call`; `EXPIRED_TEXT` (moved to `lib/copy.ts` in this task).
- Produces: `ContextGraph({ lineage, onSelect, height })`; `ContextGraphDialog({ open, onOpenChange, handle, title })`;
  exported copy constants `GRAPH_UNAVAILABLE`, `GRAPH_EMPTY`, `NOT_GOVERNED`, `SHORTENED`.

- [ ] **Step 1: Write the failing tests** (`frontend/tests/contextgraph.test.tsx`)

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/dynamic", () => ({ default: () => (p: { onSelect: (id: string) => void }) =>
  <button type="button" data-testid="graph" onClick={() => p.onSelect("metric:open_breaks")}>graph</button> }));
const lineage = vi.hoisted(() => vi.fn());
vi.mock("@/components/SessionProvider", () => ({ useSession: () => ({ call: (fn: (t: string) => unknown) => fn("T") }) }));
vi.mock("@/lib/api", async (orig) => ({ ...(await orig<typeof import("@/lib/api")>()), api: { lineage } }));

import { ContextGraphDialog, GRAPH_EMPTY, GRAPH_UNAVAILABLE, NOT_GOVERNED, SHORTENED } from "@/components/ContextGraphDialog";
import { EXPIRED_TEXT } from "@/components/WidgetCard";
import { ApiError } from "@/lib/api";

const G = { nodes: [{ id: "metric:open_breaks", kind: "Metric", label: "open_breaks", detail: "Open cash breaks" },
  { id: "source:cashrecon", kind: "Source", label: "cashrecon" }],
  edges: [{ from: "source:cashrecon", to: "metric:open_breaks", type: "PROVIDES" }], truncated: false, governed: true };
const open = (handle = "r_aaaaaaaaaaaa") =>
  render(<ContextGraphDialog open onOpenChange={vi.fn()} handle={handle} title="Open breaks by region" />);

beforeEach(() => lineage.mockReset());

describe("ContextGraphDialog", () => {
  it("loads the lineage of the handle and lists nodes by kind", async () => {
    lineage.mockResolvedValueOnce(G);
    open();
    expect(await screen.findByTestId("graph")).toBeInTheDocument();
    expect(lineage).toHaveBeenCalledWith("T", "r_aaaaaaaaaaaa");
    await userEvent.click(screen.getByText("List view"));
    expect(screen.getByText("cashrecon")).toBeInTheDocument();
  });

  it("shows a node's detail and neighbours when it is selected", async () => {
    lineage.mockResolvedValueOnce(G);
    open();
    await userEvent.click(await screen.findByTestId("graph"));
    expect(screen.getByText("Open cash breaks")).toBeInTheDocument();
    expect(screen.getByText(/PROVIDES/)).toBeInTheDocument();
  });

  it("notes free-form and shortened graphs", async () => {
    lineage.mockResolvedValueOnce({ ...G, governed: false, truncated: true });
    open();
    expect(await screen.findByText(NOT_GOVERNED)).toBeInTheDocument();
    expect(screen.getByText(SHORTENED)).toBeInTheDocument();
  });

  it("expired on 404, unavailable with retry otherwise, empty when there are no nodes", async () => {
    lineage.mockRejectedValueOnce(new ApiError(404));
    const { unmount } = open();
    expect(await screen.findByText(EXPIRED_TEXT)).toBeInTheDocument();
    unmount();
    lineage.mockRejectedValueOnce(new ApiError(502)).mockResolvedValueOnce({ ...G, nodes: [], edges: [] });
    open();
    expect(await screen.findByText(GRAPH_UNAVAILABLE)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(GRAPH_EMPTY)).toBeInTheDocument();
  });

  it("ignores a stale response after the handle changes", async () => {
    let resolveFirst: (v: unknown) => void = () => {};
    lineage.mockImplementationOnce(() => new Promise((r) => { resolveFirst = r; }))
      .mockResolvedValueOnce({ ...G, nodes: [{ id: "metric:b", kind: "Metric", label: "second" }], edges: [] });
    const { rerender } = open("r_aaaaaaaaaaaa");
    rerender(<ContextGraphDialog open onOpenChange={vi.fn()} handle="r_bbbbbbbbbbbb" title="B" />);
    await userEvent.click(await screen.findByText("List view"));
    resolveFirst(G);
    expect(await screen.findByText("second")).toBeInTheDocument();
    expect(screen.queryByText("cashrecon")).toBeNull();
  });
});
```

In `tests/widgetcard.test.tsx` add (the existing `next/dynamic` mock stubs both the chart and the graph):

```tsx
  it("enables Context graph only once the result has loaded", async () => {
    results.mockResolvedValueOnce({ handle: "r_aaaaaaaaaaaa", columns: ["region", "value"], offset: 0, row_count: 1,
      rows: [["EMEA", 3]] });
    render(<WidgetCard item={base} {...props} />);
    const btn = screen.getByRole("button", { name: "Context graph" });
    expect(btn).toBeDisabled();
    await screen.findByTestId("chart");
    expect(btn).toBeEnabled();
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run tests/contextgraph.test.tsx tests/widgetcard.test.tsx < /dev/null`
Expected: FAIL (cannot resolve `@/components/ContextGraphDialog`; no "Context graph" button)

- [ ] **Step 3: Implement `components/ContextGraph.tsx`**

```tsx
"use client";
import * as echarts from "echarts";
import ReactECharts from "echarts-for-react";
import { useMemo } from "react";
import { PRISM_THEME, registerPrismTheme } from "@/lib/echartsTheme";
import { toGraphOption } from "@/lib/graph";
import type { Lineage } from "@/lib/schemas";

registerPrismTheme(echarts, getComputedStyle(document.body).fontFamily);

export function ContextGraph({ lineage, onSelect, height }:
  { lineage: Lineage; onSelect: (id: string) => void; height: number }) {
  const option = useMemo(() => toGraphOption(lineage), [lineage]);
  const onEvents = useMemo(() => ({
    click: (p: { dataType?: string; data?: { name?: string } }) => {
      if (p.dataType === "node" && p.data?.name) onSelect(p.data.name);
    },
  }), [onSelect]);
  return <ReactECharts echarts={echarts} option={option} theme={PRISM_THEME} notMerge onEvents={onEvents}
    style={{ height, width: "100%" }} />;
}
```

- [ ] **Step 4: Implement `components/ContextGraphDialog.tsx`**

```tsx
"use client";
import { Network, RotateCcw } from "lucide-react";
import dynamic from "next/dynamic";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { useSession } from "@/components/SessionProvider";
import { ApiError, Unauthorized, api } from "@/lib/api";
import { EXPIRED_TEXT } from "@/lib/copy";
import { KIND_LABEL } from "@/lib/graph";
import { LINEAGE_KINDS, type Lineage } from "@/lib/schemas";

const ContextGraph = dynamic(() => import("@/components/ContextGraph").then((m) => m.ContextGraph),
  { ssr: false, loading: () => <Skeleton className="h-[560px] w-full" /> });

export const GRAPH_UNAVAILABLE = "The context graph is unavailable.";
export const GRAPH_EMPTY = "No context is available for this result.";
export const NOT_GOVERNED = "Free-form query: no governed lineage";
export const SHORTENED = "Graph shortened to 150 nodes";

type Load = { kind: "loading" } | { kind: "ok"; lineage: Lineage } | { kind: "expired" } | { kind: "error" };

export function ContextGraphDialog({ open, onOpenChange, handle, title }:
  { open: boolean; onOpenChange: (open: boolean) => void; handle: string; title: string }) {
  const { call } = useSession();
  const callRef = useRef(call);
  callRef.current = call;
  const [load, setLoad] = useState<Load>({ kind: "loading" });
  const [nonce, setNonce] = useState(0);
  const [layout, setLayout] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoad({ kind: "loading" });
    setSelected(null);
    callRef.current((t) => api.lineage(t, handle))
      .then((lineage) => { if (!cancelled) setLoad({ kind: "ok", lineage }); })
      .catch((e) => {
        if (cancelled || e instanceof Unauthorized) return;
        setLoad(e instanceof ApiError && e.status === 404 ? { kind: "expired" } : { kind: "error" });
      });
    return () => { cancelled = true; };
  }, [open, handle, nonce]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[min(92vw,1400px)]!">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2"><Network className="size-4" aria-hidden />Context graph</DialogTitle>
          <p className="text-sm text-muted-foreground">{title}</p>
        </DialogHeader>
        <Body load={load} layout={layout} selected={selected} onSelect={setSelected}
          onRetry={() => setNonce((n) => n + 1)} onReset={() => { setLayout((n) => n + 1); setSelected(null); }} />
      </DialogContent>
    </Dialog>
  );
}

function Notice({ children }: { children: React.ReactNode }) {
  return <div className="grid min-h-[320px] place-items-center text-center text-sm text-muted-foreground"><div>{children}</div></div>;
}

function Body({ load, layout, selected, onSelect, onRetry, onReset }: {
  load: Load; layout: number; selected: string | null; onSelect: (id: string) => void; onRetry: () => void; onReset: () => void;
}) {
  if (load.kind === "loading") return <Skeleton className="h-[560px] w-full" />;
  if (load.kind === "expired") return <Notice>{EXPIRED_TEXT}</Notice>;
  if (load.kind === "error") {
    return <Notice><p>{GRAPH_UNAVAILABLE}</p>
      <Button size="sm" variant="outline" className="mt-3" onClick={onRetry}>Retry</Button></Notice>;
  }
  const g = load.lineage;
  if (g.nodes.length === 0) return <Notice>{GRAPH_EMPTY}</Notice>;
  const node = g.nodes.find((n) => n.id === selected);
  const labelOf = (id: string) => g.nodes.find((n) => n.id === id)?.label ?? id;
  const links = node ? g.edges.filter((e) => e.from === node.id || e.to === node.id) : [];
  return (
    <div className="space-y-3">
      {(!g.governed || g.truncated) && (
        <div className="flex flex-wrap gap-2 text-xs">
          {!g.governed && <span className="rounded bg-[var(--prism-paper)] px-2 py-1">{NOT_GOVERNED}</span>}
          {g.truncated && <span className="rounded bg-[var(--prism-paper)] px-2 py-1">{SHORTENED}</span>}
        </div>)}
      <div className="grid gap-3 lg:grid-cols-[1fr_18rem]">
        <div className="relative rounded-lg border bg-[#fbfcfd]">
          <Button size="sm" variant="outline" className="absolute top-2 left-2 z-10" onClick={onReset}>
            <RotateCcw aria-hidden />Reset layout</Button>
          <ContextGraph key={layout} lineage={g} onSelect={onSelect} height={560} />
        </div>
        <aside className="rounded-lg border p-3 text-sm" aria-label="Node details">
          {node ? (
            <div className="space-y-2">
              <p className="text-xs font-medium text-muted-foreground">{KIND_LABEL[node.kind]}{node.source ? ` · ${node.source}` : ""}</p>
              <p className="font-semibold break-words text-[var(--prism-ink)]">{node.label}</p>
              {node.detail && <p className="break-words text-muted-foreground">{node.detail}</p>}
              {links.length > 0 && <ul className="space-y-1 border-t pt-2 text-xs">
                {links.map((e) => <li key={`${e.from}|${e.type}|${e.to}`} className="break-words">
                  {e.from === node.id ? `${e.type} → ${labelOf(e.to)}` : `${labelOf(e.from)} → ${e.type}`}</li>)}
              </ul>}
            </div>
          ) : <p className="text-muted-foreground">Select a node to see what it is and how it connects.</p>}
        </aside>
      </div>
      <details className="rounded-lg border px-3 py-2 text-sm">
        <summary className="cursor-pointer font-medium">List view</summary>
        <div className="mt-2 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {LINEAGE_KINDS.filter((k) => g.nodes.some((n) => n.kind === k)).map((k) => (
            <section key={k}>
              <h4 className="text-xs font-medium text-muted-foreground">{KIND_LABEL[k]}</h4>
              <ul className="mt-1 space-y-0.5">
                {g.nodes.filter((n) => n.kind === k).map((n) => <li key={n.id} className="break-words">{n.label}</li>)}
              </ul>
            </section>))}
        </div>
      </details>
    </div>
  );
}
```

Create `frontend/lib/copy.ts` with the expired text moved out of `WidgetCard.tsx` (the dialog and the card both need
it, and the card imports the dialog, so it cannot live in either):

```ts
export const EXPIRED_TEXT = "This result has expired. Ask again or reopen the dashboard.";
```

In `WidgetCard.tsx` replace `export const EXPIRED_TEXT = "...";` with
`import { EXPIRED_TEXT } from "@/lib/copy";` plus `export { EXPIRED_TEXT };` (existing tests import it from the card).

- [ ] **Step 5: Wire the button into `WidgetCard.tsx`**

Add `Network` to the lucide import, `import { ContextGraphDialog } from "@/components/ContextGraphDialog";`,
state `const [graphOpen, setGraphOpen] = useState(false);`. In the footer, before "View query · source rows", insert
(and remove `ml-auto` from the "View query" button):

```tsx
          <button type="button" disabled={load.kind !== "ok"} onClick={() => setGraphOpen(true)}
            className="ml-auto inline-flex items-center gap-1 font-medium text-[var(--prism-navy)] underline-offset-4 hover:underline disabled:pointer-events-none disabled:opacity-40">
            <Network className="size-3.5" aria-hidden />Context graph</button>
```

After the expand `Dialog`:

```tsx
      {live && <ContextGraphDialog open={graphOpen} onOpenChange={setGraphOpen} handle={item.widget.handle}
        title={item.widget.title} />}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd frontend && npx vitest run < /dev/null && ./node_modules/.bin/tsc --noEmit < /dev/null && npx eslint app components lib < /dev/null`
Expected: all tests pass; no type or lint errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/components/ContextGraph.tsx frontend/components/ContextGraphDialog.tsx frontend/components/WidgetCard.tsx frontend/lib/copy.ts frontend/tests/contextgraph.test.tsx frontend/tests/widgetcard.test.tsx
git -c user.email=cwijayasundara@gmail.com commit -m "feat(ui): context graph dialog with node details and list view; widget button

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Smoke test, docs, live check and push

**Files:**
- Modify: `frontend/e2e/smoke.spec.ts`, `frontend/e2e/fixtures.ts`
- Modify: `README.md` (gateway section: tool table, "seven tools" -> "eight tools", data-flow diagram line; UI "Use it" step 3)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Extend the mocked smoke test**

In `e2e/fixtures.ts` add:

```ts
export const LINEAGE = { nodes: [
  { id: "metric:open_breaks", kind: "Metric", label: "open_breaks", source: "cashrecon", detail: "Open cash breaks" },
  { id: "source:cashrecon", kind: "Source", label: "cashrecon" },
  { id: "table:cashrecon.breaks", kind: "Table", label: "cashrecon.breaks", source: "cashrecon" },
], edges: [{ from: "source:cashrecon", to: "metric:open_breaks", type: "PROVIDES" },
  { from: "metric:open_breaks", to: "table:cashrecon.breaks", type: "COMPUTED_FROM" }], truncated: false, governed: true };
```

In the mocked route handler of `smoke.spec.ts` add `if (url.pathname.startsWith("/lineage/")) return json(LINEAGE);`
(import `LINEAGE`), and after the widget has rendered:

```ts
    await page.getByRole("button", { name: "Context graph" }).click();
    await expect(page.getByRole("dialog").getByText("Context graph")).toBeVisible();
    await page.getByText("List view").click();
    await expect(page.getByRole("dialog").getByText("cashrecon.breaks")).toBeVisible();
    await page.keyboard.press("Escape");
```

- [ ] **Step 2: Run the smoke test**

Run: `cd frontend && npx playwright test e2e/smoke.spec.ts < /dev/null`
Expected: `1 passed, 1 skipped` (needs the dev server on :3000; the config reuses a running one)

- [ ] **Step 3: Update README**

In "Semantic gateway": change "seven tools" to "eight tools"; add the row
`| lineage(handle) | the role-gated part of the context graph behind one of your own result handles (UI only; not offered to the agent's model) |`;
add `lineage ─▶ role-gated Cypher over the context graph (same gate as search_context)` to the diagram. In "Use it"
step 3 append: "**Context graph** on a widget shows the metric, dimensions, tables, columns and source it came from."

- [ ] **Step 4: Full checks**

Run: `cd backend && uv run pytest -q && uv run ruff check . ; cd ../frontend && npx vitest run < /dev/null && ./node_modules/.bin/tsc --noEmit < /dev/null`
Expected: all green apart from the known `.env`-dependent `test_agent_settings_defaults`.

- [ ] **Step 5: Live check** (needs the running stack restarted: `scripts/start_backend.sh`, `scripts/start_frontend.sh`)

Sign in as `steward`, ask "Which price vendor drives the most corporate bond price conflicts?", press **Context graph**
on the widget, confirm Metric `price_conflicts`, Source `marketmaster`, Dimension `vendor_id`/`asset_class`, Table
`marketmaster.price_suspects` and the endpoint appear; then sign in as `bi_analyst`, ask "How many price conflicts are
there by asset class?" and confirm no Table, Column, Field or Endpoint nodes. Take a screenshot of each for the user.

- [ ] **Step 6: Commit and push**

```bash
git add frontend/e2e/smoke.spec.ts frontend/e2e/fixtures.ts README.md
git -c user.email=cwijayasundara@gmail.com commit -m "test(ui): smoke covers the context graph; docs: lineage tool

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
git push origin main
```
